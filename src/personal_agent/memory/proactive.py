"""Proactive memory scoring and budget controls (ADR-0039, FRE-174–175)."""

from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import structlog

from personal_agent.captains_log.turn_evidence import DropReason, mark_truncated
from personal_agent.config import settings
from personal_agent.memory.proactive_types import (
    ProactiveMemoryCandidate,
    ProactiveMemoryDiscard,
    ProactiveMemorySuggestions,
    ProactiveScoreComponents,
)

log = structlog.get_logger(__name__)


def estimate_tokens_from_text(text: str) -> int:
    """Match context assembly heuristic: word count × 1.3."""
    return int(len(text.split()) * 1.3)


def _estimate_payload_tokens(payload: dict[str, Any]) -> int:
    return estimate_tokens_from_text(json.dumps(payload, sort_keys=True, default=str))


def _overlap_subscore(session_entities: set[str], candidate_entities: list[str]) -> float:
    """Saturate at 3+ overlapping entity names."""
    if not session_entities or not candidate_entities:
        return 0.0
    cset = {e.strip() for e in candidate_entities if e}
    inter = len(session_entities & cset)
    if inter >= 3:
        return 1.0
    return inter / 3.0


def _recency_subscore(timestamp_iso: str | None, half_life_days: float) -> float:
    """Exponential decay with half-life in days (1.0 at t=0)."""
    if not timestamp_iso or half_life_days <= 0:
        return 0.5
    try:
        raw = timestamp_iso.replace("Z", "+00:00")
        ts = datetime.fromisoformat(raw)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
        return float(math.exp(-math.log(2) * age_days / half_life_days))
    except (ValueError, TypeError, OSError):
        return 0.5


def _topic_subscore(
    session_topic_hint: str | None,
    entity_name: str,
    key_entities: list[str],
) -> float:
    """MVP topic proxy: keyword overlap with entity names (ADR-0039 stub)."""
    if not session_topic_hint or not session_topic_hint.strip():
        return 0.5
    tokens = {w for w in session_topic_hint.lower().split() if len(w) > 2}
    if not tokens:
        return 0.5
    names = {entity_name.lower(), *[e.lower() for e in key_entities if e]}
    hits = 0
    for name in names:
        for t in tokens:
            if t in name or name in t:
                hits += 1
                break
    if hits == 0:
        return 0.3
    return min(1.0, hits / 2.0)


def _normalize_vector_score(score: float) -> float:
    """Neo4j vector index scores are typically cosine-like; clamp to [0,1]."""
    return max(0.0, min(1.0, float(score)))


def _combine_scores(
    emb: float,
    overlap: float,
    recency: float,
    topic: float,
) -> float:
    cfg = settings
    total = (
        cfg.proactive_memory_w_embedding * emb
        + cfg.proactive_memory_w_entity * overlap
        + cfg.proactive_memory_w_recency * recency
        + cfg.proactive_memory_w_topic * topic
    )
    return max(0.0, min(1.0, total))


def _dedupe_raw_by_turn_id(
    raw_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep first row per turn_id (vector order is best-first).

    Args:
        raw_rows: Rows as retrieved, best-first.

    Returns:
        Tuple of (kept, dropped). The dropped rows are returned rather than discarded
        silently so the evidence record can name them (FRE-1060) — this is the earliest
        gate on the path and the only one that fires before a score exists.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for row in raw_rows:
        tid = row.get("turn_id")
        if tid:
            s = str(tid)
            if s in seen:
                dropped.append(row)
                continue
            seen.add(s)
        out.append(row)
    return out, dropped


def _build_payload_for_row(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return (kind, payload) for a raw graph row."""
    name = row.get("name") or "unknown"
    entity_type = row.get("entity_type")
    description = row.get("description")
    turn_id = row.get("turn_id")
    user_message = row.get("user_message")
    summary = row.get("summary")
    key_entities = row.get("key_entities") or []

    if turn_id and (user_message is not None or summary):
        return (
            "episode",
            {
                "type": "episode",
                # FRE-1004: carry the episode's durable identity so the turn evidence
                # record can name which episode was admitted. Without it every proactive
                # episode is anonymous and two in one turn are indistinguishable.
                "conversation_id": turn_id,
                "user_message": user_message,
                "summary": summary or mark_truncated(user_message or "", 400),
                "key_entities": key_entities,
            },
        )
    return (
        "entity",
        {
            "type": "entity",
            "name": name,
            "entity_type": entity_type,
            "description": description,
            "mention_count": row.get("mention_count", 0),
        },
    )


def _discard_row(
    row: dict[str, Any], score: float | None, reason: DropReason
) -> ProactiveMemoryDiscard:
    """Record a raw graph row a pre-selection gate removed (FRE-1060).

    For the two gates that fire before a candidate is built — the turn-id dedupe and the
    relevance threshold — so the payload is derived here purely to make the drop nameable.

    Args:
        row: The raw graph row.
        score: The final score, or None where the gate fired before one was computed.
        reason: The gate that removed it.

    Returns:
        The discard record.
    """
    kind, payload = _build_payload_for_row(row)
    return ProactiveMemoryDiscard(
        kind=kind,  # type: ignore[arg-type]
        payload=payload,
        relevance_score=score,
        drop_reason=reason,
    )


def _discard_candidate(
    candidate: ProactiveMemoryCandidate, reason: DropReason
) -> ProactiveMemoryDiscard:
    """Record a scored candidate a selection gate removed (FRE-1060).

    For the six gates that fire once the candidate exists, so its kind, payload and score
    are carried through unchanged rather than re-derived.

    Args:
        candidate: The scored candidate.
        reason: The gate that removed it.

    Returns:
        The discard record.
    """
    return ProactiveMemoryDiscard(
        kind=candidate.kind,
        payload=candidate.payload,
        relevance_score=candidate.relevance_score,
        drop_reason=reason,
    )


def build_proactive_suggestions(
    raw_rows: list[dict[str, Any]],
    session_entity_names: set[str],
    session_topic_hint: str | None,
    trace_id: str,
    query_embedding_ms: float | None,
) -> ProactiveMemorySuggestions:
    """Score raw Neo4j rows, apply threshold, candidate cap, budget, diminishing returns.

    Args:
        raw_rows: Rows from MemoryService.suggest_proactive_raw().
        session_entity_names: Entities linked to the current session (for overlap).
        session_topic_hint: Optional short topic proxy (e.g. recent user text).
        trace_id: Correlation id for logs.
        query_embedding_ms: Optional timing for observability.

    Returns:
        ProactiveMemorySuggestions with trimmed, ranked candidates **and** every
        candidate a gate discarded, each naming the gate (FRE-1060). Emitted plus
        discarded accounts for every row in ``raw_rows``: nothing is silently lost.
    """
    cfg = settings
    retrieved_count = len(raw_rows)
    raw_rows, duplicate_rows = _dedupe_raw_by_turn_id(raw_rows)
    deduped_count = len(raw_rows)
    discarded: list[ProactiveMemoryDiscard] = [
        _discard_row(row, None, DropReason.RECALL_DUPLICATE) for row in duplicate_rows
    ]
    scored: list[ProactiveMemoryCandidate] = []

    for row in raw_rows:
        vector_score = _normalize_vector_score(float(row.get("vector_score", 0.0)))
        name = str(row.get("name") or "")
        key_entities = list(row.get("key_entities") or [])
        if name and name not in key_entities:
            key_entities = [name, *key_entities]

        overlap = _overlap_subscore(session_entity_names, key_entities)
        recency = _recency_subscore(
            row.get("timestamp_iso") or row.get("timestamp"),
            cfg.proactive_memory_recency_half_life_days,
        )
        topic = _topic_subscore(session_topic_hint, name, key_entities)
        final = _combine_scores(vector_score, overlap, recency, topic)

        if final < cfg.proactive_memory_min_score:
            # Recorded, not skipped (FRE-1060). The payload is built here purely so the
            # drop is nameable; the row is not otherwise used.
            discarded.append(_discard_row(row, final, DropReason.RECALL_SCORE_THRESHOLD))
            continue

        kind, payload = _build_payload_for_row(row)
        components = ProactiveScoreComponents(
            embedding=vector_score,
            entity_overlap=overlap,
            recency=recency,
            topic_coherence=topic,
        )
        scored.append(
            ProactiveMemoryCandidate(
                kind=kind,  # type: ignore[arg-type]
                payload=payload,
                relevance_score=final,
                score_components=components,
            )
        )

    scored.sort(key=lambda c: c.relevance_score, reverse=True)
    after_threshold = len(scored)
    capped = scored[: cfg.proactive_memory_max_candidates]
    discarded.extend(
        _discard_candidate(c, DropReason.RECALL_CANDIDATE_CAP)
        for c in scored[cfg.proactive_memory_max_candidates :]
    )

    selected: list[ProactiveMemoryCandidate] = []
    token_budget = 0
    prev_score: float | None = None
    oversized: list[ProactiveMemoryCandidate] = []
    stop_index: int | None = None
    stop_reason: DropReason | None = None

    # Selection is unchanged — the branch order, the comparisons and the break/continue
    # semantics are all as before. The only addition is recording *which* branch ended it,
    # because the deciding gate was previously indistinguishable from the others and this
    # ticket forbids widening a gate before it can be measured.
    for index, cand in enumerate(capped):
        if len(selected) >= cfg.proactive_memory_max_injected_items:
            stop_index, stop_reason = index, DropReason.RECALL_ITEM_CAP
            break
        if cand.relevance_score < cfg.proactive_memory_diminishing_score_floor:
            stop_index, stop_reason = index, DropReason.RECALL_SCORE_FLOOR
            break
        if prev_score is not None:
            if prev_score - cand.relevance_score > cfg.proactive_memory_diminishing_score_gap:
                stop_index, stop_reason = index, DropReason.RECALL_SCORE_GAP
                break
        est = _estimate_payload_tokens(cand.payload)
        if est > cfg.proactive_memory_max_tokens:
            oversized.append(cand)
            continue
        if token_budget + est > cfg.proactive_memory_max_tokens:
            stop_index, stop_reason = index, DropReason.RECALL_TOKEN_BUDGET
            break
        selected.append(cand)
        token_budget += est
        prev_score = cand.relevance_score

    # An oversized candidate is stepped over, so its index is always below any later
    # ``stop_index`` and it can never also appear in the terminated tail below.
    discarded.extend(_discard_candidate(c, DropReason.RECALL_ITEM_OVERSIZED) for c in oversized)
    if stop_reason is not None and stop_index is not None:
        discarded.extend(_discard_candidate(c, stop_reason) for c in capped[stop_index:])

    # FRE-1060: fires on *any* discard. The old guard was `len(selected) <
    # after_threshold`, which is blind to the two gates upstream of scoring — a turn whose
    # only losses were duplicates or sub-threshold rows emitted no event at all, leaving an
    # observability hole in exactly the mechanism this ticket exists to close. Every case
    # that logged before still logs: the old condition implies this one.
    if discarded:
        log.info(
            "proactive_memory_budget_trimmed",
            trace_id=trace_id,
            before_count=after_threshold,
            after_count=len(selected),
            token_estimate=token_budget,
            threshold=cfg.proactive_memory_max_tokens,
            # This event named a budget when any of six gates could have decided, and on
            # the melon turn the ranked cap and the token budget were both consistent with
            # its numbers. These fields end that ambiguity: `stop_reason` is the single
            # terminal gate that ended selection (at most one can fire), and the three
            # counts separate retrieval, dedupe and threshold losses that `before_count`
            # alone conflated.
            stop_reason=stop_reason.value if stop_reason is not None else None,
            discarded_by_gate=dict(Counter(d.drop_reason.value for d in discarded)),
            retrieved_row_count=retrieved_count,
            deduped_row_count=deduped_count,
            scored_count=after_threshold,
        )

    return ProactiveMemorySuggestions(
        candidates=selected,
        discarded=discarded,
        query_embedding_ms=query_embedding_ms,
    )
