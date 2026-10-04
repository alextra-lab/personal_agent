"""Proactive memory scoring and budget controls (ADR-0039, FRE-174–175)."""

from __future__ import annotations

import json
import math
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Literal

import structlog

from personal_agent.captains_log.turn_evidence import DropReason, mark_truncated
from personal_agent.config import settings
from personal_agent.config.calibration import (
    PROACTIVE_RERANK_RELEVANCE_BOUND_FILE,
    calibrated_reranker_model,
)
from personal_agent.memory.models import RelevanceValue
from personal_agent.memory.proactive_types import (
    ProactiveMemoryCandidate,
    ProactiveMemoryDiscard,
    ProactiveMemorySuggestions,
    ProactiveScoreComponents,
)
from personal_agent.memory.relevance_gate import relevance_verdict
from personal_agent.memory.reranker import measured_scores, rerank

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


def _below_relevance_bound(
    embedding_term: float,
    overlap: float,
    topic: float,
    *,
    measured: bool,
) -> bool:
    """Whether the relevance gate rejects this candidate (ADR-0148 D4, FRE-1477).

    The obligation D4 states is that no item is admitted on a non-relevance signal alone.
    Recency is the signal that was buying admission: FRE-1287 removed the subscore floors
    and left the weights, and at the *measured* top-ranked non-match -- the very item
    admission is decided on when the answer is not in the corpus -- recency still carries
    a candidate over the 0.30 bar with nothing else behind it.

    So the predicate sits **ahead of** :func:`_combine_scores` rather than inside it. A
    weight adjustment would leave recency in the sum and able to compensate; a gate before
    the sum cannot be compensated for at all. It binds only where there is no relevance
    evidence of any kind -- zero entity overlap and zero topic hits -- so a candidate with
    any other evidence is untouched.

    ``measured`` carries the score's provenance, and it is not a formality.
    ``_augment_proactive_with_lexical`` (``service.py:1124``, FRE-724) appends lexical-arm
    entity hits whose ``vector_score`` is ``recall_similarity_floor`` -- a configuration
    constant, not a measurement -- and that path runs in production. Comparing a constant
    against a calibrated bound would decide admission on a number that never measured
    anything, which is the pathology this gate exists to stop. An unmeasured score is
    therefore no relevance evidence, exactly as a zero overlap is.

    Args:
        embedding_term: The normalized embedding subscore, from
            :func:`_normalize_vector_score`. This is the path's own scorer output, and the
            value the bound is calibrated against.
        overlap: The entity-overlap subscore.
        topic: The topic-coherence subscore.
        measured: Whether ``embedding_term`` derives from a real embedding comparison.

    Returns:
        True when the candidate must not be admitted.
    """
    cfg = settings
    bound = cfg.proactive_memory_relevance_bound
    # A missing calibration leaves the gate inert and never defaults to zero (ADR-0148
    # D4). Production is in exactly that state: the calibration measured the serving arm
    # and reported that no bound satisfies both of D4's constraints, so it committed the
    # incompatibility instead of a number -- see
    # config/calibration/proactive_relevance_bound.json and the research note beside it.
    if bound is None or not cfg.proactive_memory_relevance_gate_enabled:
        return False
    if overlap > 0.0 or topic > 0.0:
        return False
    return not measured or embedding_term < bound


CandidateIdentity = tuple[str, str]
"""Kind-qualified candidate identity, as :func:`_candidate_identity` builds it."""


def rerank_gate_armed() -> bool:
    """Whether the proactive reranker bound is in force (ADR-0148 D4, FRE-1545).

    Returns:
        True when a calibrated bound is configured and the path's relevance gate is on.
        False means no score is consulted, so :func:`score_proactive_relevance` makes no
        reranker call at all and an unarmed deployment pays nothing for it.
    """
    return (
        settings.proactive_memory_rerank_relevance_bound is not None
        and settings.proactive_memory_relevance_gate_enabled
    )


def _rerank_verdict(
    identity: CandidateIdentity,
    overlap: float,
    topic: float,
    relevance: Mapping[CandidateIdentity, RelevanceValue] | None,
) -> DropReason | None:
    """Whether the reranker bound rejects this candidate (ADR-0148 D4, FRE-1545).

    The same placement and the same binding condition as :func:`_below_relevance_bound`:
    ahead of :func:`_combine_scores`, so recency cannot compensate, and only for a
    candidate with no relevance evidence of any other kind -- zero entity overlap and zero
    topic hits. That condition is ADR-0148 D4's proactive clause. What changes is the
    relevance value: the reranker's own score for this candidate, because FRE-1477 measured
    that the embedder cannot separate relevant from irrelevant at D4's rate.

    The predicate is the one broad recall and entity match apply
    (:func:`~personal_agent.memory.relevance_gate.relevance_verdict`). A candidate with no
    score, or a score from a model other than the calibrated one, is UNAVAILABLE rather
    than rejected: the path cannot tell, which is a different fact from "irrelevant", and
    it must not admit on order alone either (ADR-0148 D4's no-score rule).

    Args:
        identity: The candidate's kind-qualified identity.
        overlap: The entity-overlap subscore.
        topic: The topic-coherence subscore.
        relevance: Reranker values keyed by identity, from
            :func:`score_proactive_relevance`. None means nothing was scored.

    Returns:
        The drop reason, or None when the candidate passes this gate.
    """
    if overlap > 0.0 or topic > 0.0:
        return None
    value = relevance.get(identity) if relevance is not None else None
    return relevance_verdict(
        value.score if value is not None else None,
        value.model if value is not None else None,
        bound=settings.proactive_memory_rerank_relevance_bound,
        gate_enabled=settings.proactive_memory_relevance_gate_enabled,
        calibrated_model=calibrated_reranker_model(PROACTIVE_RERANK_RELEVANCE_BOUND_FILE),
    )


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


_SplitItem = tuple[_CandidateKind, dict[str, Any], dict[str, Any]]


def _deduped_candidates(raw_rows: Sequence[dict[str, Any]]) -> tuple[list[_SplitItem], int]:
    """Split every raw row into its candidates, then collapse shared identities (FRE-1061).

    One loop, shared by :func:`build_proactive_suggestions` and
    :func:`score_proactive_relevance`, so the identity a score is keyed by is the identity
    the gate looks it up by (FRE-1545, codex plan-review). Collapses per kind: episodes on
    ``conversation_id``, entities on ``name``. The old row-level turn-id dedupe silently
    erased a *distinct entity* whose best turn collided with a higher-ranked entity's
    (29→13 on the melon turn); at candidate level only the genuinely shared episode
    collapses.

    Args:
        raw_rows: Rows from ``MemoryService.suggest_proactive_raw()``.

    Returns:
        ``(items, split_count)`` -- the deduplicated ``(kind, payload, row)`` triples in
        first-seen order, and how many candidates the split produced before the collapse.
    """
    split_items: list[_SplitItem] = [
        (kind, payload, row) for row in raw_rows for kind, payload in _split_row_payloads(row)
    ]
    seen: set[CandidateIdentity] = set()
    items: list[_SplitItem] = []
    for kind, payload, row in split_items:
        key = _candidate_identity(kind, payload)
        if key in seen:
            continue
        seen.add(key)
        items.append((kind, payload, row))
    return items, len(split_items)


def _rerank_document(kind: _CandidateKind, row: dict[str, Any]) -> str:
    """The document the reranker scores for one candidate (FRE-1545).

    Byte-identical to ``MemoryService._resolve_item_texts``, the document builder broad
    recall reranks with and FRE-1479 calibrated: ``coalesce(name, '') + ' ' +
    coalesce(description, '')`` for an entity, ``coalesce(summary, user_message, '')`` for
    a turn. Built from the **raw row**, not the episode payload: the payload replaces a
    falsey summary with a truncated user message, which ``coalesce`` does not do.

    Args:
        kind: The candidate's kind.
        row: The raw row the candidate was split from.

    Returns:
        The document text, possibly empty.
    """
    if kind == "entity":
        return f"{row.get('name') or ''} {row.get('description') or ''}"
    summary = row.get("summary")
    if summary is not None:
        return str(summary)
    user_message = row.get("user_message")
    return "" if user_message is None else str(user_message)


async def score_proactive_relevance(
    raw_rows: Sequence[dict[str, Any]],
    query_text: str,
    *,
    trace_id: str,
    session_id: str | None = None,
) -> dict[CandidateIdentity, RelevanceValue]:
    """Score every proactive candidate with the serving reranker (ADR-0148 D4, FRE-1545).

    Computed upstream of :func:`build_proactive_suggestions`, which stays synchronous and
    pure: the async adapter already holds the query and the raw rows, and one reranker
    call over the deduplicated candidates is the whole I/O. Uses the one reranker client
    (:func:`~personal_agent.memory.reranker.rerank`) and the provenance rule broad recall
    uses (:func:`~personal_agent.memory.reranker.measured_scores`).

    Skipped, at no cost, when the gate is not armed. Two kinds of candidate are not sent:
    an entity with an empty description (FRE-1114 drops it before the gate anyway) and an
    empty document (nothing to score). Neither is scored, so if the gate binds one it is
    UNAVAILABLE, never admitted on order.

    ``rerank()`` degrades rather than raises, but the call is guarded the way
    ``_rerank_fused_items`` guards it: a raise yields no scores, which the gate reports as
    UNAVAILABLE.

    Args:
        raw_rows: Rows from ``MemoryService.suggest_proactive_raw()``.
        query_text: The user message the candidates were retrieved for.
        trace_id: Request trace id (ADR-0074).
        session_id: Session id, threaded with ``trace_id``.

    Returns:
        Each measured candidate's reranker score and the model that produced it, keyed by
        kind-qualified identity. A candidate absent from the mapping was not measured.
    """
    if not rerank_gate_armed() or not query_text.strip():
        return {}
    identities: list[CandidateIdentity] = []
    documents: list[str] = []
    items, _ = _deduped_candidates(raw_rows)
    for kind, payload, row in items:
        if kind == "entity" and not (payload.get("description") or "").strip():
            continue
        document = _rerank_document(kind, row)
        if not document.strip():
            continue
        identities.append(_candidate_identity(kind, payload))
        documents.append(document)
    if not documents:
        return {}

    started = time.perf_counter()
    try:
        results = await rerank(
            query=query_text,
            documents=documents,
            # Every candidate needs a score: rerank()'s own default is reranker_top_k (10),
            # which would leave the rest unscored and therefore UNAVAILABLE.
            top_k=len(documents),
            trace_id=trace_id,
            session_id=session_id,
        )
    except Exception as exc:
        log.warning(
            "proactive_rerank_failed",
            trace_id=trace_id,
            session_id=session_id,
            error=str(exc),
            candidate_count=len(documents),
        )
        return {}
    rerank_ms = (time.perf_counter() - started) * 1000.0

    values = {identities[i]: value for i, value in measured_scores(results, len(documents)).items()}
    log.info(
        "proactive_memory_reranked",
        trace_id=trace_id,
        session_id=session_id,
        candidate_count=len(documents),
        scored_count=len(values),
        models=sorted({v.model for v in values.values()}),
        rerank_ms=round(rerank_ms, 1),
    )
    return values


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
    relevance: Mapping[CandidateIdentity, RelevanceValue] | None = None,
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
        relevance: Reranker values keyed by candidate identity, from
            :func:`score_proactive_relevance` (FRE-1545). None means nothing was scored:
            when the reranker gate is armed, a candidate it binds is then UNAVAILABLE,
            so a caller that forgets the mapping fails safe rather than open.

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
    # collapse shared identities per kind (see _deduped_candidates).
    #
    # A dedupe collapse is deliberately NOT recorded as a discard (owner call,
    # 2026-07-30, on a confirmed code-review finding — the rationale survives the
    # FRE-1061 restatement from rows to candidates). The collapsed candidate shares its
    # kind-qualified identity with the one that was kept, so recording it as a drop
    # would put one identity in the record twice, once admitted and once dropped,
    # asserting that a memory was lost when that very memory reached the model, and the
    # FRE-1021 census would over-report recall loss. The delta stays visible as the
    # split_candidate_count/deduped_candidate_count pair on the event below.
    items, split_count = _deduped_candidates(raw_rows)
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
                    # Combined here rather than above so the gate below can precede the
                    # combination on every path that reaches it. This candidate is already
                    # rejected by an unrelated filter, so the score only labels its record,
                    # which keeps the existing FRE-1114 record shape byte-for-byte.
                    relevance_score=_combine_scores(vector_score, overlap, recency, topic),
                    drop_reason=DropReason.RECALL_EMPTY_DESCRIPTION,
                )
            )
            continue

        # ADR-0148 D4 (FRE-1545): the reranker bound, ahead of the combination, so recency
        # cannot compensate. relevance_score stays None for the same reason as below.
        rerank_verdict = _rerank_verdict(
            _candidate_identity(kind, payload), overlap, topic, relevance
        )
        if rerank_verdict is not None:
            discarded.append(
                ProactiveMemoryDiscard(
                    kind=kind,
                    payload=payload,
                    relevance_score=None,
                    drop_reason=rerank_verdict,
                )
            )
            continue

        # ADR-0148 D4 (FRE-1477): the relevance gate, ahead of the combination. See
        # _below_relevance_bound for why placement is the whole point. relevance_score
        # stays None -- no final score was computed, and ProactiveMemoryDiscard reserves
        # None for exactly that rather than fabricating a 0.0.
        if _below_relevance_bound(
            vector_score, overlap, topic, measured=bool(row.get("vector_score_measured", True))
        ):
            discarded.append(
                ProactiveMemoryDiscard(
                    kind=kind,
                    payload=payload,
                    relevance_score=None,
                    drop_reason=DropReason.RECALL_RELEVANCE_BOUND,
                )
            )
            continue

        final = _combine_scores(vector_score, overlap, recency, topic)
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
