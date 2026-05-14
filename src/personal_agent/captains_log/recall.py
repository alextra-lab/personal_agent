"""Reflection recall for context assembly (ADR-0067 / FRE-348).

Surfaces a small, recency- and relevance-bounded slice of past Captain's Log
reflections back into the agent's context so cross-session use cases that
fall through entity-extraction (UC-1 resumable refactor state, UC-3 abstract
idea recovery, UC-4 evolving hypothesis) have a viable retrieval path.

Selection policy is documented in `ADR-0067-reflection-surfacing-in-context-assembly.md`.
This module is the read side; ADR-0030 covers the write side (dedup + promotion).

Failure mode: every error path returns ``[]`` and logs a warning. Context
assembly never blocks on reflection recall.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import structlog

from personal_agent.captains_log.manager import REFLECTIONS_INDEX_PREFIX
from personal_agent.config.settings import get_settings

if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch

logger = structlog.get_logger(__name__)


def _capitalized_entity_hints(text: str) -> list[str]:
    """Cheap entity-hint extractor — capitalised words longer than 3 chars.

    Mirrors ``request_gateway/context.py:_capitalized_entity_hints`` to keep
    this module free of cross-package imports.
    """
    if not text:
        return []
    words = text.split()
    return [w.strip('",.:;!?') for w in words if len(w) > 3 and w[0].isupper()][:10]


def _build_query(
    *,
    entity_hints: list[str],
    recency_days: int,
    min_seen_count: int,
) -> dict[str, Any]:
    """Build the Elasticsearch query body.

    The query selects reflections within the recency window, with a non-trivial
    seen_count, that contain at least one of the entity hints in either the
    rationale or the proposed-change/failure-path text. Results are ordered by
    seen_count then recency.
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=recency_days)

    text_should: list[dict[str, Any]] = []
    for hint in entity_hints:
        text_should.append({"match_phrase": {"rationale": hint}})
        text_should.append({"match_phrase": {"proposed_change.what": hint}})
        text_should.append({"match_phrase": {"failure_path.fix_what": hint}})

    must: list[dict[str, Any]] = [
        {"range": {"timestamp": {"gte": start.isoformat(), "lte": now.isoformat()}}},
        {
            "bool": {
                "should": [
                    {"range": {"proposed_change.seen_count": {"gte": min_seen_count}}},
                    # Failure-path-only reflections may not have a proposed_change at all.
                    # Surface those when they include a fix suggestion regardless of seen_count.
                    {"exists": {"field": "failure_path.fix_what"}},
                ],
                "minimum_should_match": 1,
            }
        },
    ]
    if text_should:
        must.append({"bool": {"should": text_should, "minimum_should_match": 1}})

    must_not: list[dict[str, Any]] = [
        # Skip pure-rationale entries with no actionable content
        {
            "bool": {
                "must": [
                    {"bool": {"must_not": [{"exists": {"field": "proposed_change.what"}}]}},
                    {"bool": {"must_not": [{"exists": {"field": "failure_path.fix_what"}}]}},
                ]
            }
        },
        # Skip entries whose linked Linear issue is already done
        {"term": {"status": "approved"}},
    ]

    return {
        "query": {"bool": {"must": must, "must_not": must_not}},
        "sort": [
            {"proposed_change.seen_count": {"order": "desc", "missing": "_last"}},
            {"timestamp": {"order": "desc"}},
        ],
    }


def _format_reflection_line(doc: dict[str, Any]) -> str | None:
    """Render one reflection as a single bullet line for the section.

    Returns None if the document has no actionable content (defensive).
    """
    timestamp_raw = doc.get("timestamp", "")
    date_str = str(timestamp_raw)[:10] if timestamp_raw else "unknown"

    pc = doc.get("proposed_change") or {}
    fp = doc.get("failure_path") or {}

    seen_count = pc.get("seen_count", 1) if pc else 1
    category = pc.get("category") if pc else None
    scope = pc.get("scope") if pc else None
    tag = f"{category}/{scope}" if category and scope else (category or scope or "")

    rationale = (doc.get("rationale") or "").strip().replace("\n", " ")[:120]
    proposed = (pc.get("what") or "").strip().replace("\n", " ")[:80] if pc else ""
    fix = (fp.get("fix_what") or "").strip().replace("\n", " ")[:80] if fp else ""
    linear_id = doc.get("linear_issue_id")

    if not (rationale or proposed or fix):
        return None

    head = f"- {date_str}"
    if seen_count and seen_count > 1:
        head += f" (seen {seen_count}x"
        if tag:
            head += f", {tag}"
        head += ")"
    elif tag:
        head += f" ({tag})"
    head += ": "

    parts: list[str] = []
    if rationale:
        parts.append(rationale.rstrip(".") + ".")
    if proposed:
        parts.append(f"Proposed: {proposed.rstrip('.')}.")
    if fix:
        parts.append(f"Fix: {fix.rstrip('.')}.")
    if linear_id:
        parts.append(f"→ tracked as {linear_id}.")

    return head + " ".join(parts)


def format_reflections_section(reflections: list[dict[str, Any]]) -> str | None:
    """Render the system-message body for a set of selected reflections.

    Args:
        reflections: ES documents (already filtered + ordered + truncated).

    Returns:
        The full section text including the header, or None if the input is
        empty or all entries fail to format.
    """
    lines = [line for line in (_format_reflection_line(d) for d in reflections) if line]
    if not lines:
        return None
    header = (
        "## Recent reflections from your prior work\n\n"
        "These are signals from your earlier sessions, not directives. The current "
        "turn may warrant a different approach — use these only as context.\n\n"
    )
    return header + "\n".join(lines)


async def query_relevant_reflections(
    user_message: str,
    *,
    es_client: AsyncElasticsearch | None = None,
    trace_id: str = "",
    session_id: str = "",
) -> list[dict[str, Any]]:
    """Return up to ``max_results`` reflections relevant to the current turn.

    Args:
        user_message: The current user message; entity hints are extracted from it.
        es_client: Optional preconfigured Elasticsearch client. When None, a new
            client is created from settings (and closed on this call).
        trace_id: Trace identifier for log correlation.
        session_id: Session identifier for log correlation.

    Returns:
        List of ES source documents (possibly empty). Never raises.
    """
    settings = get_settings()
    if not getattr(settings, "reflection_recall_enabled", True):
        return []

    entity_hints = _capitalized_entity_hints(user_message)
    if not entity_hints:
        # No relevance signal — skip the surface entirely. (Surfacing without
        # a relevance filter would inject the same recurring reflections on
        # every turn, defeating the anti-thrash design.)
        return []

    recency_days: int = int(getattr(settings, "reflection_recall_recency_days", 14))
    max_results: int = int(getattr(settings, "reflection_recall_max_results", 3))
    min_seen_count: int = int(getattr(settings, "reflection_recall_min_seen_count", 2))

    started_at = time.perf_counter()
    owns_client = False

    try:
        if es_client is None:
            try:
                from elasticsearch import AsyncElasticsearch as ESClient
            except ModuleNotFoundError:
                logger.warning(
                    "reflection_recall_skipped_no_es_module",
                    trace_id=trace_id,
                    session_id=session_id,
                )
                return []
            es_client = ESClient([settings.elasticsearch_url], request_timeout=5)
            owns_client = True

        body = _build_query(
            entity_hints=entity_hints,
            recency_days=recency_days,
            min_seen_count=min_seen_count,
        )

        # Use both reflections and captures-of-config-proposals indices via the wildcard.
        index_pattern = f"{REFLECTIONS_INDEX_PREFIX}-*"
        response = await es_client.search(
            index=index_pattern,
            **body,
            size=max_results,
            ignore_unavailable=True,
            allow_no_indices=True,
        )
        hits = response.get("hits", {}).get("hits", []) or []
        docs: list[dict[str, Any]] = [h.get("_source", {}) for h in hits if h.get("_source")]
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0

        logger.info(
            "reflection_recall_completed",
            trace_id=trace_id,
            session_id=session_id,
            entity_hint_count=len(entity_hints),
            candidates_considered=int(
                response.get("hits", {}).get("total", {}).get("value", len(hits)) or len(hits)
            ),
            selected_count=len(docs),
            selected_entry_ids=[d.get("entry_id", "") for d in docs],
            elapsed_ms=elapsed_ms,
            recency_days=recency_days,
            min_seen_count=min_seen_count,
        )
        return docs
    except Exception as e:  # noqa: BLE001 — never block context assembly on recall errors
        logger.warning(
            "reflection_recall_failed",
            trace_id=trace_id,
            session_id=session_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        return []
    finally:
        if owns_client and es_client is not None:
            try:
                await es_client.close()
            except Exception:  # noqa: BLE001
                pass
