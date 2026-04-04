"""Proactive memory scoring and budget controls (ADR-0039, FRE-174–175)."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

import structlog

from personal_agent.config import settings
from personal_agent.memory.proactive_types import (
    ProactiveMemoryCandidate,
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


def _dedupe_raw_by_turn_id(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep first row per turn_id (vector order is best-first)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in raw_rows:
        tid = row.get("turn_id")
        if tid:
            s = str(tid)
            if s in seen:
                continue
            seen.add(s)
        out.append(row)
    return out


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
                "user_message": user_message,
                "summary": summary or (user_message or "")[:200],
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
        ProactiveMemorySuggestions with trimmed, ranked candidates.
    """
    cfg = settings
    raw_rows = _dedupe_raw_by_turn_id(raw_rows)
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

    selected: list[ProactiveMemoryCandidate] = []
    token_budget = 0
    prev_score: float | None = None

    for cand in capped:
        if len(selected) >= cfg.proactive_memory_max_injected_items:
            break
        if cand.relevance_score < cfg.proactive_memory_diminishing_score_floor:
            break
        if prev_score is not None:
            if prev_score - cand.relevance_score > cfg.proactive_memory_diminishing_score_gap:
                break
        est = _estimate_payload_tokens(cand.payload)
        if est > cfg.proactive_memory_max_tokens:
            continue
        if token_budget + est > cfg.proactive_memory_max_tokens:
            break
        selected.append(cand)
        token_budget += est
        prev_score = cand.relevance_score

    if len(selected) < after_threshold:
        log.info(
            "proactive_memory_budget_trimmed",
            trace_id=trace_id,
            before_count=after_threshold,
            after_count=len(selected),
            token_estimate=token_budget,
            threshold=cfg.proactive_memory_max_tokens,
        )

    return ProactiveMemorySuggestions(
        candidates=selected,
        query_embedding_ms=query_embedding_ms,
    )
