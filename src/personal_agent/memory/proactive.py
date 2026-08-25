"""Proactive memory scoring and budget controls (ADR-0039, FRE-174–175)."""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, Literal

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
    """Exponential decay with half-life in days (1.0 at t=0).

    A missing or unparseable timestamp carries no recency evidence and scores 0.0, not
    a neutral guess (FRE-1287, ADR-0138). The prior 0.5 fallback let an unknown-age
    memory buy half credit for a signal it never supplied — combined with the other
    subscores' floors, that was enough for a same-day, otherwise-irrelevant memory to
    clear the admission bar on recency alone.
    """
    if not timestamp_iso or half_life_days <= 0:
        return 0.0
    try:
        raw = timestamp_iso.replace("Z", "+00:00")
        ts = datetime.fromisoformat(raw)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
        return float(math.exp(-math.log(2) * age_days / half_life_days))
    except (ValueError, TypeError, OSError):
        return 0.0


def _topic_subscore(
    session_topic_hint: str | None,
    entity_name: str,
    key_entities: list[str],
) -> float:
    """MVP topic proxy: keyword overlap with entity names (ADR-0039 stub).

    No hint, no tokens, or zero keyword hits all score 0.0, not a neutral or partial
    guess (FRE-1287, ADR-0138). "No evidence this candidate is on-topic" is not
    evidence that it is — the prior 0.5/0.3 fallbacks handed a floor to exactly the
    subscore meant to measure relevance, and it was the one most often outvoted.
    """
    if not session_topic_hint or not session_topic_hint.strip():
        return 0.0
    tokens = {w for w in session_topic_hint.lower().split() if len(w) > 2}
    if not tokens:
        return 0.0
    names = {e.lower() for e in ([entity_name] if entity_name else []) + key_entities if e}
    hits = 0
    for name in names:
        for t in tokens:
            if t in name or name in t:
                hits += 1
                break
    if hits == 0:
        return 0.0
    return min(1.0, hits / 2.0)


def _normalize_vector_score(score: float) -> float:
    """Rescale Neo4j's ``(1 + cos) / 2`` embedding score so orthogonal maps to 0.0.

    Neo4j's vector index normalizes cosine similarity into [0,1] via ``(1 + cos) / 2``,
    so a candidate with *no* directional relation to the query (cos=0) still scores
    0.5, not 0.0 — a floor on the one subscore meant to carry the actual relevance
    signal (FRE-1287, ADR-0138). Undoing that normalization recovers cosine in
    [-1,1] and clamps non-positive similarity (orthogonal or opposed) to 0.0: no
    positive embedding evidence, no embedding credit.
    """
    clamped = max(0.0, min(1.0, float(score)))
    return max(0.0, 2.0 * clamped - 1.0)


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


_CandidateKind = Literal["entity", "episode"]


def _split_row_payloads(row: dict[str, Any]) -> list[tuple[_CandidateKind, dict[str, Any]]]:
    """Return every (kind, payload) candidate a raw graph row carries (FRE-1061).

    A raw row from :meth:`~personal_agent.memory.service.MemoryService.suggest_proactive_raw`
    is an *(entity, best cross-session turn)* **pair** — the Cypher is entity-anchored and
    attaches the turn as context. The predecessor (``_build_payload_for_row``) forced a
    binary choice and always chose the episode when a turn with text existed, which made
    an entity unreachable as an entity once it had been discussed in any other session —
    measured live 2026-07-30, that was 7,442 of 7,446 production entities
    (``telemetry/entity_recall_findings_explore_2026-07-30.md``). Emitting both keeps the
    distilled semantic memory (name / type / description) alongside the episodic excerpt.

    Order is load-bearing: the entity payload comes **first**, so after the stable
    score sort the entity precedes its equal-scored sibling episode. This is a tie-break,
    not an admission guarantee — later gates (the empty-description filter, caps,
    budget) still apply per candidate. The empty-description filter (FRE-1114) is the
    earliest of these — it runs before scoring even joins the ranked list, so it can
    never itself win a slot a populated candidate would otherwise take. The renderer
    keeps its own description filter too, as a backstop for content that reaches it by
    some other route.

    Args:
        row: One raw graph row, carrying entity fields and/or best-turn fields.

    Returns:
        One or two ``(kind, payload)`` tuples: an entity payload when the row names an
        entity, an episode payload when it carries a turn with text. A row with neither
        falls back to the legacy ``name="unknown"`` entity payload so it stays visible in
        the candidate accounting rather than vanishing.
    """
    name = row.get("name")
    turn_id = row.get("turn_id")
    user_message = row.get("user_message")
    summary = row.get("summary")

    payloads: list[tuple[_CandidateKind, dict[str, Any]]] = []
    if name:
        payloads.append(
            (
                "entity",
                {
                    "type": "entity",
                    "name": name,
                    "entity_type": row.get("entity_type"),
                    "description": row.get("description"),
                    # None when the row carries no real count — the renderer omits an
                    # absent count, where a defaulted 0 would print "(mentioned 0x)"
                    # on every entity line (FRE-1061 review finding).
                    "mention_count": row.get("mention_count"),
                },
            )
        )
    if turn_id and (user_message is not None or summary):
        payloads.append(
            (
                "episode",
                {
                    "type": "episode",
                    # FRE-1004: carry the episode's durable identity so the turn evidence
                    # record can name which episode was admitted. Without it every proactive
                    # episode is anonymous and two in one turn are indistinguishable.
                    "conversation_id": turn_id,
                    "user_message": user_message,
                    "summary": summary or mark_truncated(user_message or "", 400),
                    "key_entities": row.get("key_entities") or [],
                },
            )
        )
    if not payloads:
        payloads.append(
            (
                "entity",
                {
                    "type": "entity",
                    "name": "unknown",
                    "entity_type": row.get("entity_type"),
                    "description": row.get("description"),
                    "mention_count": row.get("mention_count"),
                },
            )
        )
    return payloads


def _candidate_identity(kind: str, payload: dict[str, Any]) -> tuple[str, str]:
    """Kind-qualified identity for dedupe (FRE-1061).

    Entity identity is its ``name``; episode identity is its ``conversation_id`` —
    matching :func:`personal_agent.captains_log.turn_evidence.memory_item_identity`.
    The kind qualifier keeps an entity and an episode with the same identity string
    apart *here*; downstream evidence identity remains unqualified, so an entity
    literally named like a turn id would still collide there (documented residual
    risk, codex plan-review 2026-07-30).
    """
    if kind == "entity":
        return (kind, str(payload.get("name") or ""))
    return (kind, str(payload.get("conversation_id") or ""))


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


_MENTIONED_ENTITY_PIN_LIMIT = 2
"""Bound on FRE-1062 mentioned-entity pins per turn.

Two, not "all resolved mentions": the resolver can return several names per message
(``MESSAGE_ENTITY_HINT_LIMIT``), and pinning them all would let one crowded message
evict every ranked candidate. Two covers the dominant one-or-two-subject message shape
observed live (melon + ice cream) while leaving most of the injected set to rank."""


def build_proactive_suggestions(
    raw_rows: list[dict[str, Any]],
    session_entity_names: set[str],
    session_topic_hint: str | None,
    trace_id: str,
    query_embedding_ms: float | None,
    mentioned_entity_names: Sequence[str] | None = None,
) -> ProactiveMemorySuggestions:
    """Score raw Neo4j rows; apply the empty-description filter, threshold, caps, budget.

    Args:
        raw_rows: Rows from MemoryService.suggest_proactive_raw().
        session_entity_names: Entities linked to the current session (for overlap).
        session_topic_hint: Optional short topic proxy (e.g. recent user text).
        trace_id: Correlation id for logs.
        query_embedding_ms: Optional timing for observability.
        mentioned_entity_names: Graph-resolved entity names the message literally
            mentions (FRE-1041 resolver output, graph casing). Feeds the FRE-1062
            mentioned-entity pin — distinct from ``session_entity_names``, which only
            nudges the overlap subscore. None or empty pins nothing.

    Returns:
        ProactiveMemorySuggestions with trimmed, ranked candidates **and** every
        candidate a gate discarded, each naming the gate (FRE-1060). Emitted plus
        discarded accounts for every **deduplicated candidate** (FRE-1061: one raw row
        splits into up to two candidates): nothing that could have reached the model is
        silently lost.
    """
    cfg = settings
    retrieved_count = len(raw_rows)
    # FRE-1061: split every (entity, best-turn) pair row into its candidates, then
    # collapse shared identities per kind — episodes on conversation_id, entities on
    # name. The old row-level turn-id dedupe silently erased a *distinct entity* whose
    # best turn collided with a higher-ranked entity's (29→13 on the melon turn); at
    # candidate level only the genuinely shared episode collapses.
    split_items: list[tuple[_CandidateKind, dict[str, Any], dict[str, Any]]] = [
        (kind, payload, row) for row in raw_rows for kind, payload in _split_row_payloads(row)
    ]
    split_count = len(split_items)
    # A dedupe collapse is deliberately NOT recorded as a discard (owner call,
    # 2026-07-30, on a confirmed code-review finding — the rationale survives the
    # FRE-1061 restatement from rows to candidates). The collapsed candidate shares its
    # kind-qualified identity with the one that was kept, so recording it as a drop
    # would put one identity in the record twice, once admitted and once dropped,
    # asserting that a memory was lost when that very memory reached the model, and the
    # FRE-1021 census would over-report recall loss. The delta stays visible as the
    # split_candidate_count/deduped_candidate_count pair on the event below.
    seen: set[tuple[str, str]] = set()
    items: list[tuple[_CandidateKind, dict[str, Any], dict[str, Any]]] = []
    for kind, payload, row in split_items:
        key = _candidate_identity(kind, payload)
        if key in seen:
            continue
        seen.add(key)
        items.append((kind, payload, row))
    deduped_count = len(items)
    discarded: list[ProactiveMemoryDiscard] = []
    scored: list[ProactiveMemoryCandidate] = []

    for kind, payload, row in items:
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
        # The pair shares its row's subscores by construction, so the sibling candidates
        # carry one score and the stable sort below keeps the entity (emitted first)
        # ahead of its episode.

        if kind == "entity" and not (payload.get("description") or "").strip():
            # FRE-1114: an empty-description entity carries no usable content. Dropped
            # here -- before ranking, the candidate-cap window, pins, the item cap or
            # the token budget -- so it can never win a slot only to be filtered at the
            # renderer with nothing left to backfill it. Episodes are never checked:
            # they always carry non-empty text by construction (_split_row_payloads).
            discarded.append(
                ProactiveMemoryDiscard(
                    kind=kind,
                    payload=payload,
                    relevance_score=final,
                    drop_reason=DropReason.RECALL_EMPTY_DESCRIPTION,
                )
            )
            continue

        if final < cfg.proactive_memory_min_score:
            # Recorded, not skipped (FRE-1060).
            discarded.append(
                ProactiveMemoryDiscard(
                    kind=kind,
                    payload=payload,
                    relevance_score=final,
                    drop_reason=DropReason.RECALL_SCORE_THRESHOLD,
                )
            )
            continue

        components = ProactiveScoreComponents(
            embedding=vector_score,
            entity_overlap=overlap,
            recency=recency,
            topic_coherence=topic,
        )
        scored.append(
            ProactiveMemoryCandidate(
                kind=kind,
                payload=payload,
                relevance_score=final,
                score_components=components,
            )
        )

    scored.sort(key=lambda c: c.relevance_score, reverse=True)
    after_threshold = len(scored)

    # FRE-1062: two stated selection rules ahead of the rank walk, as a *permutation* of
    # the same candidate list — same gates, same DropReasons, same conservation.
    #
    # 1. Episode floor, FIRST: the best-ranked episode that clears the existing
    #    diminishing_score_floor (the system's own quality bar — no new constant) is
    #    admitted before anything can consume its slot or budget. The first live
    #    post-FRE-1061 turn showed the answer's substance riding the single admitted
    #    episode, which survived by rank luck; ordering the floor ahead of the pins is
    #    what makes the guarantee real (two pins can otherwise exhaust the token budget
    #    or a small item cap before the episode is visited — codex plan-review).
    # 2. Mentioned-entity pins, NEXT: up to _MENTIONED_ENTITY_PIN_LIMIT entity
    #    candidates the message literally names (FRE-1041 resolver, graph casing),
    #    score order. Still subject to min_score (threshold ran above), the token
    #    budget, the oversize skip AND max_injected_items — the item cap is the hard
    #    output bound and nothing exceeds it. What a pin bypasses is rank: the
    #    max_candidates window ordering and the diminishing floor/gap heuristics.
    #    The literal mention is the justification.
    #
    # Walk order is admission priority, not presentation: the renderer partitions
    # items by kind, so head-first changes which candidates survive, never how they
    # read to the model.
    mentioned = set(mentioned_entity_names or ())
    floor_cand = next(
        (
            c
            for c in scored
            if c.kind == "episode"
            and c.relevance_score >= cfg.proactive_memory_diminishing_score_floor
        ),
        None,
    )
    pins: list[ProactiveMemoryCandidate] = []
    if mentioned:
        for cand in scored:
            if len(pins) >= _MENTIONED_ENTITY_PIN_LIMIT:
                break
            if cand.kind == "entity" and cand.payload.get("name") in mentioned:
                pins.append(cand)
    head: list[ProactiveMemoryCandidate] = ([floor_cand] if floor_cand else []) + pins
    head_ids = {_candidate_identity(c.kind, c.payload) for c in head}
    rest = [c for c in scored if _candidate_identity(c.kind, c.payload) not in head_ids]
    # ONE capacity-safe window: head is truncated with everything else by the single
    # bound, so a legal max_candidates=1 cannot go negative and the total holds.
    ordered = head + rest
    walk = ordered[: cfg.proactive_memory_max_candidates]
    discarded.extend(
        _discard_candidate(c, DropReason.RECALL_CANDIDATE_CAP)
        for c in ordered[cfg.proactive_memory_max_candidates :]
    )
    head_len = min(len(head), len(walk))

    selected: list[ProactiveMemoryCandidate] = []
    token_budget = 0
    prev_score: float | None = None
    oversized: list[ProactiveMemoryCandidate] = []
    stop_index: int | None = None
    stop_reason: DropReason | None = None

    # The gate loop is the FRE-1060 loop over the reordered walk. The diminishing
    # floor/gap checks apply only in the rest region (head items are pre-qualified:
    # the floor episode by the floor itself, pins by the stated bypass); prev_score
    # still updates on EVERY admission so the gap gate keeps its pre-FRE-1062 meaning
    # of "drop versus the previously admitted item" — with an empty head this loop is
    # byte-for-byte the previous behaviour.
    for index, cand in enumerate(walk):
        if len(selected) >= cfg.proactive_memory_max_injected_items:
            stop_index, stop_reason = index, DropReason.RECALL_ITEM_CAP
            break
        if index >= head_len:
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
        # Attribution over the explicit walk, never the pre-reorder list — the tail of
        # the reordered walk is what the terminal gate actually cut (codex plan-review).
        discarded.extend(_discard_candidate(c, stop_reason) for c in walk[stop_index:])

    selected_object_ids = {id(c) for c in selected}
    pinned_admitted = sum(1 for c in pins if id(c) in selected_object_ids)
    floor_admitted = floor_cand is not None and id(floor_cand) in selected_object_ids

    # The guard is deliberately UNCHANGED — it fires only when selection itself trimmed.
    # An earlier revision widened it to `if discarded:` on the reasoning that the old
    # condition is blind to the gates upstream of scoring. That reasoning was wrong, and a
    # confirmed code-review finding caught it: this event is named `budget_trimmed` and
    # existing consumers (the EVAL-proactive-memory README, any panel counting it) read it
    # as the trim signal. Firing it on a turn where nothing was trimmed — `before_count ==
    # after_count`, `stop_reason` null — would put a step-change in that series at the
    # deploy boundary with no configuration change behind it. Not losing events is not the
    # same as not corrupting them. The per-candidate record, not this event, is the
    # complete surface for the pre-selection gates.
    if len(selected) < after_threshold:
        log.info(
            "proactive_memory_budget_trimmed",
            trace_id=trace_id,
            before_count=after_threshold,
            after_count=len(selected),
            token_estimate=token_budget,
            threshold=cfg.proactive_memory_max_tokens,
            # This event named a budget when any of six gates could have decided, and on
            # the melon turn the ranked cap and the token budget were both consistent with
            # its numbers. `stop_reason` ends that ambiguity — it is the single terminal
            # gate that ended selection, and at most one can fire.
            # FRE-1061: `deduped_row_count` is gone, not renamed — its unit changed
            # (rows → candidates) and a field that silently changes unit corrupts every
            # series built on it. `retrieved_row_count` keeps its meaning; the two new
            # counts bracket the split-then-dedupe step so retrieval, pair-splitting and
            # collapse losses stay separately readable.
            stop_reason=stop_reason.value if stop_reason is not None else None,
            discarded_by_gate=dict(Counter(d.drop_reason.value for d in discarded)),
            retrieved_row_count=retrieved_count,
            split_candidate_count=split_count,
            deduped_candidate_count=deduped_count,
            # FRE-1062: how the two stated rules fired on this turn — pins admitted and
            # whether the reserved episode reached the model. Additive fields only.
            pinned_mention_count=pinned_admitted,
            episode_floor_applied=floor_admitted,
        )

    return ProactiveMemorySuggestions(
        candidates=selected,
        discarded=discarded,
        query_embedding_ms=query_embedding_ms,
    )
