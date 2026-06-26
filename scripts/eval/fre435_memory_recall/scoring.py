"""FRE-488 — pure scoring seam (no substrate, no LLM, no I/O).

Turns a raw retrieval outcome into a scored :class:`CaseResult`. Kept free of any
``personal_agent`` import so it is fully unit-testable; ``harness.py`` (the thin
I/O driver) calls :func:`flatten_recall` on a ``MemoryRecallResult`` and feeds the
result here.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from scripts.eval.fre435_memory_recall.attribution import (
    AttributionInput,
    attribute,
)
from scripts.eval.fre435_memory_recall.metrics import (
    WriteOutcome,
    false_negative,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    retrieval_miss,
)
from scripts.eval.fre435_memory_recall.probes import ENTITY_NS, EPISODE_NS, ProbeCase
from scripts.eval.fre435_memory_recall.report import CaseResult


def _entity_key(entity: Mapping[str, Any]) -> str | None:
    """Pick the raw entity key (name) from an entity dict."""
    for field_name in ("name", "entity_name", "id"):
        value = entity.get(field_name)
        if value:
            return str(value)
    return None


def _episode_key(episode: Mapping[str, Any]) -> str | None:
    """Pick the raw episode key (turn id) from an episode dict."""
    for field_name in ("turn_id", "id", "episode_id"):
        value = episode.get(field_name)
        if value:
            return str(value)
    return None


def flatten_recall(
    episodes: Sequence[Mapping[str, Any]],
    entities: Sequence[Mapping[str, Any]],
    relevance_scores: Mapping[str, float],
) -> tuple[str, ...]:
    """Flatten a recall result into ordered, namespaced, de-duplicated ids.

    Entities and episodes live in distinct id namespaces (``entity:`` /
    ``episode:``) so they never collide (codex review). Order is by relevance
    score descending where a score is available, otherwise insertion order
    (entities then episodes); the first occurrence of a duplicate id wins.

    Args:
        episodes: Episode dicts (keyed by ``turn_id``).
        entities: Entity dicts (keyed by ``name``).
        relevance_scores: Per-result scores keyed by raw id (entity name / turn id).

    Returns:
        Ordered tuple of namespaced ids.
    """
    # Sort key = (relevance score, -insertion order) so higher score ranks first
    # and, on ties, earlier insertion (entities before episodes) is preserved.
    pairs: list[tuple[str, tuple[float, int]]] = []
    for index, entity in enumerate(entities):
        raw = _entity_key(entity)
        if raw is None:
            continue
        score = relevance_scores.get(raw, -math.inf)
        pairs.append((f"{ENTITY_NS}{raw.strip().lower()}", (score, -index)))
    for index, episode in enumerate(episodes):
        raw = _episode_key(episode)
        if raw is None:
            continue
        score = relevance_scores.get(raw, -math.inf)
        pairs.append((f"{EPISODE_NS}{raw}", (score, -(index + len(entities)))))

    pairs.sort(key=lambda p: p[1], reverse=True)
    ordered: list[str] = []
    seen: set[str] = set()
    for rid, _ in pairs:
        if rid not in seen:
            seen.add(rid)
            ordered.append(rid)
    return tuple(ordered)


def score_case(
    case: ProbeCase,
    retrieved: Sequence[str],
    denied: bool,
    write_outcome: WriteOutcome,
    prod_k: int,
    k_sweep: Sequence[int],
) -> CaseResult:
    """Score one case into a :class:`CaseResult`.

    Args:
        case: The probe case (carries the expected-recall labels).
        retrieved: Ordered namespaced ids from the actual recall call.
        denied: Whether the system denied having prior context.
        write_outcome: The write-path outcome for the case.
        prod_k: The production cut-off the headline metrics key on.
        k_sweep: The ``k`` values to sweep.

    Returns:
        The scored case result with an attributed hypothesis.
    """
    relevant = set(case.relevant_ids)
    recall_by_k = {k: recall_at_k(retrieved, relevant, k) for k in k_sweep}
    precision_by_k = {k: precision_at_k(retrieved, relevant, k) for k in k_sweep}
    rr = reciprocal_rank(retrieved, relevant)
    ndcg = ndcg_at_k(retrieved, relevant, prod_k)
    fn = false_negative(retrieved, relevant, denied)
    miss = retrieval_miss(retrieved, relevant, prod_k)

    recall_prod = recall_by_k.get(prod_k)
    max_k = max(k_sweep)
    recall_max = recall_by_k.get(max_k)
    write_gap = write_outcome.entities_expected > 0 and write_outcome.entities_landed == 0
    desc_fail = (
        write_outcome.description_integrity is not None
        and write_outcome.description_integrity < 0.5
    )
    failed = bool(fn) or bool(miss) or write_gap or desc_fail

    hypothesis = attribute(
        AttributionInput(
            failed=failed,
            expected_writes=write_outcome.entities_expected,
            entities_landed=write_outcome.entities_landed,
            description_integrity=write_outcome.description_integrity,
            false_negative=fn,
            recall_at_prod_k=recall_prod,
            recall_at_max_k=recall_max,
        )
    )

    return CaseResult(
        case_id=case.case_id,
        tags=case.tags,
        relevant_count=len(relevant),
        retrieved_ids=tuple(retrieved),
        denied=denied,
        recall_by_k=recall_by_k,
        precision_by_k=precision_by_k,
        reciprocal_rank=rr,
        ndcg_at_prod_k=ndcg,
        false_negative=fn,
        retrieval_miss=miss,
        write_outcome=write_outcome,
        failed=failed,
        hypothesis=hypothesis,
    )
