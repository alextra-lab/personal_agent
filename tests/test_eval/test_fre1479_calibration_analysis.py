"""FRE-1479 — the reranker calibration's decisions, without a reranker or a network.

The driver (``calibrate.py``) supplies measured score populations; these tests pin what the
analysis makes of them. The bound rule itself is FRE-1477's ``choose_bound``, imported
unchanged rather than copied, so the two paths cannot drift apart on what ADR-0148 D4 says —
one test asserts exactly that identity, because a later copy is the failure this arrangement
prevents.
"""

from __future__ import annotations

import pytest
from scripts.eval.fre1477_relevance_calibration import analysis as proactive_analysis
from scripts.eval.fre1479_reranker_calibration.analysis import (
    MIN_POSITIVE_SHARE,
    PARITY_TOLERANCE,
    SanityResult,
    admitted_share,
    aggregate_deltas,
    choose_bound,
    parity_aggregates,
    positives_and_negative,
    sanity_holds,
)


class TestTheBoundRuleIsShared:
    """ADR-0148 D4 states one rule for every path; a second copy would be a second rule."""

    def test_choose_bound_is_fre_1477s_function_not_a_copy(self) -> None:
        assert choose_bound is proactive_analysis.choose_bound

    def test_the_positive_share_is_the_adr_figure(self) -> None:
        assert MIN_POSITIVE_SHARE == 0.90

    def test_the_tolerance_is_fre_694s_own(self) -> None:
        assert PARITY_TOLERANCE == 0.02


class TestChoosingTheBound:
    """The two constraints, and the reserved case where no value satisfies both."""

    def test_a_separating_population_yields_a_bound_above_the_negative_median(self) -> None:
        positives = [0.90, 0.85, 0.80, 0.75, 0.70, 0.65, 0.60, 0.55, 0.50, 0.45]
        negatives = [0.40, 0.35, 0.30, 0.25, 0.20]
        proposal = choose_bound(positives, negatives)
        assert not proposal.incompatible
        assert proposal.bound is not None
        assert proposal.bound > proposal.negative_median
        assert proposal.positive_admitted_share == 1.0

    def test_an_overlapping_population_reports_the_incompatibility(self) -> None:
        """D4's reserved case: report and stop, never pick a number."""
        positives = [0.9, 0.8, 0.2, 0.1]
        negatives = [0.5, 0.5, 0.5]
        proposal = choose_bound(positives, negatives)
        assert proposal.incompatible
        assert proposal.bound is None
        assert proposal.positive_admitted_share is None
        assert proposal.reason

    def test_the_bound_rejects_the_median_top_ranked_non_match(self) -> None:
        positives = [0.99] * 20
        negatives = [0.10, 0.20, 0.30]
        proposal = choose_bound(positives, negatives)
        assert proposal.bound is not None
        assert admitted_share(negatives, proposal.bound) < 1.0
        assert proposal.negative_median == 0.20

    def test_an_empty_population_raises_rather_than_returning_a_bound(self) -> None:
        """A broken measurement is not a calibration result and must never reach a bound."""
        with pytest.raises(ValueError):
            choose_bound([], [0.3])
        with pytest.raises(ValueError):
            choose_bound([0.9], [])


class TestAdmission:
    """Below the bound is rejected — the ``>=`` convention of memory/service.py:5020."""

    def test_a_score_exactly_at_the_bound_is_admitted(self) -> None:
        assert admitted_share([0.5], 0.5) == 1.0

    def test_a_score_below_the_bound_is_not(self) -> None:
        assert admitted_share([0.49], 0.5) == 0.0

    def test_an_empty_population_admits_nothing_rather_than_everything(self) -> None:
        assert admitted_share([], 0.5) == 0.0


class TestTheMetric:
    """FRE-695's positive/negative definition, over compound (turn) labels."""

    def test_positives_are_per_expected_entity(self) -> None:
        positives, negative = positives_and_negative(
            frozenset({"kafka", "knossos"}), ["kafka", "knossos", "ratatouille"], [0.9, 0.8, 0.1]
        )
        assert positives == [0.9, 0.8]
        assert negative == 0.1

    def test_the_negative_is_the_strongest_non_match(self) -> None:
        _, negative = positives_and_negative(
            frozenset({"kafka"}), ["kafka", "knossos", "venice"], [0.9, 0.4, 0.7]
        )
        assert negative == 0.7

    def test_a_control_case_contributes_only_a_negative(self) -> None:
        positives, negative = positives_and_negative(frozenset(), ["a", "b"], [0.3, 0.6])
        assert positives == []
        assert negative == 0.6

    def test_an_all_expected_candidate_set_contributes_no_negative(self) -> None:
        """None rather than 0.0 — a fabricated negative would drag the median down."""
        positives, negative = positives_and_negative(frozenset({"a"}), ["a"], [0.9])
        assert positives == [0.9]
        assert negative is None

    def test_a_misaligned_pair_raises(self) -> None:
        """Silently zipping to the shorter list would attribute one score to another item."""
        with pytest.raises(ValueError, match="misalignment"):
            positives_and_negative(frozenset({"a"}), ["a", "b"], [0.9])


class TestParity:
    """The instrument is validated before any number is reported."""

    def test_aggregates_are_the_three_fre_694_statistics(self) -> None:
        aggregates = parity_aggregates([0.8, 0.6, 0.4], [0.5, 0.3, 0.1])
        assert set(aggregates) == {"pos_median", "neg_median", "neg_max"}
        assert aggregates["neg_max"] == 0.5

    def test_key_mismatch_raises_rather_than_intersecting(self) -> None:
        """A silently intersected comparison would report parity over whichever keys matched."""
        with pytest.raises(ValueError, match="key mismatch"):
            aggregate_deltas({"pos_median": 0.5}, {"neg_median": 0.5})

    def test_sanity_holds_when_the_relevant_document_outranks(self) -> None:
        assert sanity_holds(SanityResult(relevant_score=0.78, irrelevant_score=0.20))

    def test_sanity_fails_on_a_tie(self) -> None:
        """A constant-scoring endpoint must not pass the gate that exists to catch it."""
        assert not sanity_holds(SanityResult(relevant_score=0.5, irrelevant_score=0.5))

    def test_sanity_is_scale_agnostic(self) -> None:
        """Reranker scales are arbitrary (FRE-695); the gate is rank order, not a threshold."""
        assert sanity_holds(SanityResult(relevant_score=0.02, irrelevant_score=0.001))
