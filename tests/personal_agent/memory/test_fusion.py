# tests/personal_agent/memory/test_fusion.py
"""Tests for RRF fusion and cross-path dedup (ADR-0104 AC-2, FRE-722).

Pure-function unit tests — no substrate. See docs/specs/MULTI_PATH_RETRIEVAL_DESIGN_SPEC.md
sections 3 and 4 for the design this implements.
"""

from __future__ import annotations

import pytest

from personal_agent.memory.fusion import (
    DEFAULT_RRF_K,
    FusedResult,
    MultiPathRecallResult,
    RankedResult,
    dedup_arm_ranking,
    reciprocal_rank_fusion,
)


class TestRankedResult:
    """Tests for the RankedResult input contract."""

    def test_rank_below_one_raises(self) -> None:
        """Rank < 1 is not a valid 1-based rank."""
        with pytest.raises(ValueError, match="rank must be >= 1"):
            RankedResult(item_id="turn:1", rank=0)

    def test_negative_rank_raises(self) -> None:
        """Negative rank is not a valid 1-based rank."""
        with pytest.raises(ValueError, match="rank must be >= 1"):
            RankedResult(item_id="turn:1", rank=-1)


class TestDedupArmRanking:
    """Tests for within-arm dedup (cross-path dedup, spec section 4)."""

    def test_no_repeats_passes_through_unchanged(self) -> None:
        results = [
            RankedResult(item_id="turn:1", rank=1),
            RankedResult(item_id="turn:2", rank=2),
        ]
        assert dedup_arm_ranking(results) == results

    def test_repeated_item_keeps_best_lowest_rank(self) -> None:
        results = [
            RankedResult(item_id="turn:1", rank=5),
            RankedResult(item_id="turn:1", rank=2),
        ]
        deduped = dedup_arm_ranking(results)
        assert deduped == [RankedResult(item_id="turn:1", rank=2)]

    def test_empty_input_returns_empty(self) -> None:
        assert dedup_arm_ranking([]) == []

    def test_output_order_is_first_occurrence(self) -> None:
        results = [
            RankedResult(item_id="turn:2", rank=3),
            RankedResult(item_id="turn:1", rank=5),
            RankedResult(item_id="turn:2", rank=1),
        ]
        deduped = dedup_arm_ranking(results)
        assert [r.item_id for r in deduped] == ["turn:2", "turn:1"]
        assert deduped[0].rank == 1


class TestReciprocalRankFusion:
    """Tests for RRF fusion (ADR-0104 AC-2)."""

    def test_empty_arm_rankings_returns_empty(self) -> None:
        assert reciprocal_rank_fusion([]) == []

    def test_negative_k_raises(self) -> None:
        arm_rankings = [[RankedResult(item_id="turn:1", rank=1)]]
        with pytest.raises(ValueError, match="k must be >= 0"):
            reciprocal_rank_fusion(arm_rankings, k=-1)

    def test_k_default_is_60(self) -> None:
        """A single arm, rank-1 item scores exactly 1/(60+1)."""
        arm_rankings = [[RankedResult(item_id="turn:1", rank=1)]]
        fused = reciprocal_rank_fusion(arm_rankings)
        assert DEFAULT_RRF_K == 60
        assert fused == [FusedResult(item_id="turn:1", score=1 / 61, arm_count=1)]

    def test_custom_k_changes_score(self) -> None:
        """k=0 on a rank-1 item gives score 1.0 (1 / (0 + 1))."""
        arm_rankings = [[RankedResult(item_id="turn:1", rank=1)]]
        fused = reciprocal_rank_fusion(arm_rankings, k=0)
        assert fused == [FusedResult(item_id="turn:1", score=1.0, arm_count=1)]

    def test_agreement_property_two_arms_outranks_one_arm_at_same_rank(self) -> None:
        """An item ranked r by two arms outranks an item ranked r by one arm."""
        arm1 = [
            RankedResult(item_id="agreed", rank=3),
            RankedResult(item_id="solo", rank=3),
        ]
        arm2 = [RankedResult(item_id="agreed", rank=3)]
        fused = reciprocal_rank_fusion([arm1, arm2])

        by_id = {r.item_id: r for r in fused}
        assert by_id["agreed"].score > by_id["solo"].score
        assert fused[0].item_id == "agreed"

    def test_broad_support_beats_single_top_rank(self) -> None:
        """Broad multi-arm support outranks one arm's rank-1 hit surfaced nowhere else."""
        top_in_one_arm = [RankedResult(item_id="lone_top", rank=1)]
        arm_a = [RankedResult(item_id="broad", rank=10)]
        arm_b = [RankedResult(item_id="broad", rank=8)]
        arm_c = [RankedResult(item_id="broad", rank=12)]

        fused = reciprocal_rank_fusion([top_in_one_arm, arm_a, arm_b, arm_c])

        by_id = {r.item_id: r for r in fused}
        assert by_id["broad"].score > by_id["lone_top"].score
        assert fused[0].item_id == "broad"

    def test_dedup_by_construction_one_entry_per_item_across_arms(self) -> None:
        """An item present in two arms yields exactly one FusedResult, summed."""
        arm1 = [RankedResult(item_id="shared", rank=2)]
        arm2 = [RankedResult(item_id="shared", rank=4)]

        fused = reciprocal_rank_fusion([arm1, arm2])

        assert len(fused) == 1
        expected_score = 1 / (DEFAULT_RRF_K + 2) + 1 / (DEFAULT_RRF_K + 4)
        assert fused[0] == FusedResult(item_id="shared", score=expected_score, arm_count=2)

    def test_distinct_ids_never_merge(self) -> None:
        """Two different item_ids remain two separate fused entries, never merged."""
        arm1 = [
            RankedResult(item_id="entity:vision-elementid-1", rank=1),
            RankedResult(item_id="entity:perception-elementid-2", rank=2),
        ]
        fused = reciprocal_rank_fusion([arm1])

        assert len(fused) == 2
        item_ids = {r.item_id for r in fused}
        assert item_ids == {
            "entity:vision-elementid-1",
            "entity:perception-elementid-2",
        }

    def test_within_arm_repeat_deduped_before_fusion_not_summed_twice(self) -> None:
        """A repeated item in one arm contributes once (its best rank), not twice."""
        arm_with_repeat = [
            RankedResult(item_id="turn:1", rank=5),
            RankedResult(item_id="turn:1", rank=2),
        ]

        fused = reciprocal_rank_fusion([arm_with_repeat])

        assert fused == [FusedResult(item_id="turn:1", score=1 / (DEFAULT_RRF_K + 2), arm_count=1)]

    def test_equal_score_tie_break_is_deterministic_by_item_id(self) -> None:
        """Items with identical fused scores sort by ascending item_id."""
        arm1 = [
            RankedResult(item_id="b_item", rank=1),
            RankedResult(item_id="a_item", rank=1),
        ]
        fused = reciprocal_rank_fusion([arm1])

        assert fused[0].score == fused[1].score
        assert [r.item_id for r in fused] == ["a_item", "b_item"]


class TestKindPropagation:
    """Kind (turn vs entity) must survive dedup and fusion (FRE-724).

    The multi-path core reranks and resolves a heterogeneous fused set: dense /
    multi-query arms surface Entity elementIds, the lexical arm surfaces both
    Turn.turn_id (kind='turn') and Entity elementId (kind='entity'). The
    downstream resolver needs the kind to fetch doc text and expand to entities.
    """

    def test_ranked_result_defaults_to_entity_kind(self) -> None:
        """Existing arms construct RankedResult without kind; default is 'entity'."""
        assert RankedResult(item_id="e1", rank=1).kind == "entity"

    def test_dedup_preserves_kind(self) -> None:
        """Within-arm dedup keeps the item's kind, not just its best rank."""
        results = [
            RankedResult(item_id="t1", rank=5, kind="turn"),
            RankedResult(item_id="t1", rank=2, kind="turn"),
        ]
        deduped = dedup_arm_ranking(results)
        assert deduped == [RankedResult(item_id="t1", rank=2, kind="turn")]

    def test_fusion_carries_turn_kind_onto_fused_result(self) -> None:
        """A turn-kind ranked hit fuses into a turn-kind FusedResult."""
        arm = [RankedResult(item_id="turn-abc", rank=1, kind="turn")]
        fused = reciprocal_rank_fusion([arm])
        assert fused[0].kind == "turn"

    def test_fusion_carries_entity_kind_by_default(self) -> None:
        """Entity-kind (the default) is preserved through fusion."""
        arm = [RankedResult(item_id="elem-1", rank=1)]
        fused = reciprocal_rank_fusion([arm])
        assert fused[0].kind == "entity"

    def test_same_id_across_arms_keeps_single_kind(self) -> None:
        """An item surfaced by two arms keeps one consistent kind after fusion."""
        arm1 = [RankedResult(item_id="elem-9", rank=1, kind="entity")]
        arm2 = [RankedResult(item_id="elem-9", rank=3, kind="entity")]
        fused = reciprocal_rank_fusion([arm1, arm2])
        assert len(fused) == 1
        assert fused[0].kind == "entity"
        assert fused[0].arm_count == 2


class TestMultiPathRecallResult:
    """The core's return envelope (FRE-724)."""

    def test_carries_arms_and_telemetry_fields(self) -> None:
        """The result exposes ordered items plus AC-1/AC-6 telemetry."""
        result = MultiPathRecallResult(
            items=(FusedResult(item_id="e1", score=0.5, arm_count=2, kind="entity"),),
            arms_executed=("dense", "lexical"),
            arms_failed=(),
            per_arm_counts={"dense": 1, "lexical": 0},
            fused_set_size=1,
            path="broad",
        )
        assert [i.item_id for i in result.items] == ["e1"]
        assert set(result.arms_executed) == {"dense", "lexical"}
        assert result.per_arm_counts["lexical"] == 0
        assert result.fused_set_size == 1
        assert result.path == "broad"
