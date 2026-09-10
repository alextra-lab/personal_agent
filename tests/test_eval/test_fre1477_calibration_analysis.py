"""FRE-1477 -- the calibration's decisions, tested without an embedder or a graph.

The driver runs against a live endpoint and the test substrate. Every *decision* it makes
lives in ``analysis.py`` and is exercised here, including the case ADR-0148 D4 reserves:
when no bound satisfies both constraints, the calibration must report that and stop
rather than pick a number.
"""

from __future__ import annotations

import pytest
from scripts.eval.fre1477_relevance_calibration.analysis import (
    MIN_POSITIVE_SHARE,
    admitted_share,
    aggregate_deltas,
    choose_bound,
    pair_deltas,
    parity_aggregates,
)


class TestAdmittedShare:
    """Admission is ``score >= bound`` -- below the bound is rejected (ADR-0148 D4)."""

    def test_a_score_exactly_at_the_bound_is_admitted(self) -> None:
        assert admitted_share([0.70], 0.70) == 1.0

    def test_a_score_just_below_the_bound_is_rejected(self) -> None:
        assert admitted_share([0.6999], 0.70) == 0.0

    def test_share_is_the_fraction_admitted(self) -> None:
        assert admitted_share([0.5, 0.7, 0.8, 0.9], 0.7) == 0.75

    def test_an_empty_population_admits_nothing(self) -> None:
        assert admitted_share([], 0.7) == 0.0


class TestChooseBoundWhenTheArmSeparates:
    """A separable pair of populations yields a bound that clears both constraints."""

    def test_the_bound_is_strictly_above_the_median_non_match(self) -> None:
        positives = [0.80, 0.82, 0.84, 0.86, 0.88, 0.90, 0.92, 0.94, 0.96, 0.98]
        negatives = [0.50, 0.55, 0.60, 0.65, 0.70]
        proposal = choose_bound(positives, negatives)
        assert proposal.incompatible is False
        assert proposal.bound is not None
        assert proposal.bound > proposal.negative_median

    def test_the_median_non_match_is_rejected_by_the_chosen_bound(self) -> None:
        positives = [0.80 + 0.02 * i for i in range(10)]
        negatives = [0.50, 0.55, 0.60, 0.65, 0.70]
        proposal = choose_bound(positives, negatives)
        assert proposal.bound is not None
        assert admitted_share([proposal.negative_median], proposal.bound) == 0.0

    def test_the_whole_positive_set_share_is_reported_and_clears_the_bar(self) -> None:
        positives = [0.80 + 0.02 * i for i in range(10)]
        negatives = [0.50, 0.55, 0.60, 0.65, 0.70]
        proposal = choose_bound(positives, negatives)
        assert proposal.positive_admitted_share == 1.0
        assert proposal.positive_admitted_share >= MIN_POSITIVE_SHARE

    def test_the_negative_rejection_share_is_reported(self) -> None:
        """A bound clearing both constraints while rejecting few negatives is a weak
        result the owner must see, not infer.
        """
        positives = [0.80 + 0.02 * i for i in range(10)]
        negatives = [0.50, 0.55, 0.60, 0.65, 0.70]
        proposal = choose_bound(positives, negatives)
        assert proposal.negative_rejected_share == pytest.approx(0.6)

    def test_exactly_the_minimum_share_is_accepted(self) -> None:
        """The constraint is 'at least', so the boundary case must not be rejected."""
        positives = [0.90] * 9 + [0.50]
        negatives = [0.60]
        proposal = choose_bound(positives, negatives)
        assert proposal.incompatible is False
        assert proposal.positive_admitted_share == pytest.approx(0.9)


class TestChooseBoundWhenTheArmDoesNotSeparate:
    """The seeded negative: ADR-0148 D4's reserved case must report, never pick.

    Without this the harness could satisfy every other test by always returning a number,
    and the one behaviour the ADR insists on -- stopping -- would be untested.
    """

    def test_an_overlapping_pair_reports_incompatible(self) -> None:
        positives = [0.60, 0.62, 0.64, 0.66, 0.68, 0.70, 0.72, 0.74, 0.76, 0.78]
        negatives = [0.60, 0.65, 0.70, 0.75, 0.80]
        proposal = choose_bound(positives, negatives)
        assert proposal.incompatible is True

    def test_an_incompatible_result_carries_no_bound_at_all(self) -> None:
        positives = [0.60 + 0.02 * i for i in range(10)]
        negatives = [0.60, 0.65, 0.70, 0.75, 0.80]
        proposal = choose_bound(positives, negatives)
        assert proposal.bound is None
        assert proposal.positive_admitted_share is None
        assert proposal.negative_rejected_share is None

    def test_the_reason_names_the_measured_shortfall(self) -> None:
        positives = [0.60 + 0.02 * i for i in range(10)]
        negatives = [0.60, 0.65, 0.70, 0.75, 0.80]
        proposal = choose_bound(positives, negatives)
        assert proposal.reason is not None
        assert "10 labelled positives" in proposal.reason
        assert "90%" in proposal.reason

    def test_the_median_is_still_reported_when_incompatible(self) -> None:
        """The owner's next decision needs the measurement, not just the verdict."""
        positives = [0.60 + 0.02 * i for i in range(10)]
        negatives = [0.60, 0.65, 0.70, 0.75, 0.80]
        proposal = choose_bound(positives, negatives)
        assert proposal.negative_median == pytest.approx(0.70)


class TestChooseBoundRefusesABrokenMeasurement:
    """An empty population is a broken run, never a calibration result."""

    def test_no_positives_raises(self) -> None:
        with pytest.raises(ValueError, match="no labelled positives"):
            choose_bound([], [0.7])

    def test_no_negatives_raises(self) -> None:
        with pytest.raises(ValueError, match="no negatives"):
            choose_bound([0.9], [])


class TestParityAggregates:
    """FRE-694's three statistics, on FRE-694's own percentile convention."""

    def test_reports_the_three_statistics(self) -> None:
        aggregates = parity_aggregates([0.70, 0.80, 0.90], [0.50, 0.60, 0.75])
        assert aggregates == {"pos_median": 0.80, "neg_median": 0.60, "neg_max": 0.75}

    def test_an_empty_population_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            parity_aggregates([], [0.5])


class TestAggregateDeltas:
    """P1 -- the cross-implementation comparison."""

    def test_reports_the_absolute_difference_per_statistic(self) -> None:
        deltas = aggregate_deltas({"a": 0.700, "b": 0.500}, {"a": 0.712, "b": 0.495})
        assert deltas["a"] == pytest.approx(0.012)
        assert deltas["b"] == pytest.approx(0.005)

    def test_a_key_mismatch_raises_rather_than_intersecting(self) -> None:
        """Silently comparing the shared keys would report parity over whichever
        statistics happened to match.
        """
        with pytest.raises(ValueError, match="aggregate key mismatch"):
            aggregate_deltas({"a": 0.1, "b": 0.2}, {"a": 0.1})


class TestPairDeltas:
    """P2 -- index fidelity, recomputed from the recorded pairs."""

    def test_recomputes_each_pairs_difference(self) -> None:
        assert pair_deltas([(0.70, 0.71), (0.60, 0.58)]) == pytest.approx([0.01, 0.02])

    def test_no_pairs_raises_rather_than_passing_vacuously(self) -> None:
        with pytest.raises(ValueError, match="at least one recorded score pair"):
            pair_deltas([])
