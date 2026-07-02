# src/personal_agent/memory/fusion.py
"""Cross-path dedup and Reciprocal Rank Fusion for multi-path recall.

Pure functions — no substrate, no config, no I/O. See ADR-0104 (Multi-Path Retrieval
with Rank Fusion) and docs/specs/MULTI_PATH_RETRIEVAL_DESIGN_SPEC.md sections 3 and 4
for the design this implements.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

DEFAULT_RRF_K = 60

ItemKind = Literal["entity", "turn"]


@dataclass(frozen=True)
class RankedResult:
    """One arm's ranked hit for a single item.

    Args:
        item_id: Canonical identity — Turn.turn_id for turns, Entity elementId for
            entities. Never a free-text name.
        rank: 1-based rank position within the arm's ranked list.
        kind: Whether item_id is an entity elementId ("entity", the default for the
            dense and multi-query arms) or a Turn.turn_id ("turn", surfaced by the
            lexical arm). Carried so the multi-path core (FRE-724) can resolve the
            heterogeneous fused set to doc text and expand it to entities.

    Raises:
        ValueError: If rank < 1.
    """

    item_id: str
    rank: int
    kind: ItemKind = "entity"

    def __post_init__(self) -> None:
        """Validate the 1-based rank invariant.

        Raises:
            ValueError: If rank < 1.
        """
        if self.rank < 1:
            raise ValueError(f"rank must be >= 1, got {self.rank}")


@dataclass(frozen=True)
class FusedResult:
    """One item's fused position across arms.

    Args:
        item_id: Canonical identity, deduped by construction — appears once.
        score: Summed RRF score across every arm that surfaced this item.
        arm_count: Number of distinct arms that surfaced this item (the agreement count).
        kind: The item's kind ("entity"/"turn"), propagated from the arms that
            surfaced it. All occurrences of one item_id share a single kind.
    """

    item_id: str
    score: float
    arm_count: int
    kind: ItemKind = "entity"


@dataclass(frozen=True)
class MultiPathRecallResult:
    """The multi-path core's fused + reranked output plus its telemetry (FRE-724).

    Args:
        items: Fused candidates, ordered post-rerank (best-first). Deduped by
            construction; each carries its kind for downstream resolution.
        arms_executed: Names of every arm the core invoked (not just non-empty
            ones) — the AC-1 "≥2 independent arms ran" evidence.
        arms_failed: Names of arms that raised and fell open to no candidates.
        per_arm_counts: Candidate count per executed arm, including zero-count arms.
        fused_set_size: Size of the capped set handed to the reranker (AC-6a:
            never exceeds the configured reranker input cap).
        path: Which recall path the core served ("broad"/"entity"/"proactive").
    """

    items: Sequence[FusedResult]
    arms_executed: Sequence[str]
    arms_failed: Sequence[str]
    per_arm_counts: Mapping[str, int]
    fused_set_size: int
    path: str


def dedup_arm_ranking(results: Sequence[RankedResult]) -> list[RankedResult]:
    """Collapse repeated items within one arm's ranked list to their best rank.

    Output order is first-occurrence order of each distinct item_id in the input
    (stable, deterministic) — not re-sorted by the (possibly improved) kept rank.

    Args:
        results: One arm's ranked hits, possibly containing the same item_id more
            than once.

    Returns:
        One RankedResult per distinct item_id, at its lowest (best) observed rank.
    """
    best_rank: dict[str, int] = {}
    kinds: dict[str, ItemKind] = {}
    for result in results:
        kinds.setdefault(result.item_id, result.kind)
        current = best_rank.get(result.item_id)
        if current is None or result.rank < current:
            best_rank[result.item_id] = result.rank
    return [
        RankedResult(item_id=item_id, rank=rank, kind=kinds[item_id])
        for item_id, rank in best_rank.items()
    ]


def reciprocal_rank_fusion(
    arm_rankings: Sequence[Sequence[RankedResult]],
    k: int = DEFAULT_RRF_K,
) -> list[FusedResult]:
    """Fuse several arms' ranked lists by Reciprocal Rank Fusion.

    Each arm is first deduped to its best rank per item (dedup_arm_ranking), then RRF
    sums 1 / (k + rank) across arms, keyed by item_id. Fusion never compares raw arm
    scores — arm score scales are not comparable (FRE-695).

    Args:
        arm_rankings: One ranked result list per retrieval arm.
        k: RRF constant. Defaults to 60 (Cormack et al. 2009). Must be >= 0.

    Returns:
        Fused ranking, one FusedResult per distinct item_id. Sorted by descending
        score; ties broken by ascending item_id for deterministic output.

    Raises:
        ValueError: If k < 0.
    """
    if k < 0:
        raise ValueError(f"k must be >= 0, got {k}")

    scores: dict[str, float] = {}
    arm_counts: dict[str, int] = {}
    kinds: dict[str, ItemKind] = {}
    for arm in arm_rankings:
        for result in dedup_arm_ranking(arm):
            scores[result.item_id] = scores.get(result.item_id, 0.0) + 1.0 / (k + result.rank)
            arm_counts[result.item_id] = arm_counts.get(result.item_id, 0) + 1
            kinds.setdefault(result.item_id, result.kind)

    fused = [
        FusedResult(
            item_id=item_id, score=score, arm_count=arm_counts[item_id], kind=kinds[item_id]
        )
        for item_id, score in scores.items()
    ]
    fused.sort(key=lambda r: (-r.score, r.item_id))
    return fused
