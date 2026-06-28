"""Unit tests for the FRE-655 floor-calibration sweep (pure, no substrate)."""

from scripts.eval.fre435_memory_recall.calibration import (
    FloorPoint,
    propose_floor,
    sweep_floor,
)


class TestSweepFloor:
    """A global threshold sweep over positive vs negative cosine distributions."""

    def test_sweep_covers_range_and_computes_rates(self) -> None:
        """Each floor point reports recall (positives kept) and FPR (negatives kept)."""
        positives = [0.80, 0.70, 0.90]
        negatives = [0.20, 0.10, 0.30]
        points = sweep_floor(positives, negatives, start=0.0, stop=1.0, step=0.1)
        # Floors from 0.0 to 1.0 inclusive of start.
        assert points[0].floor == 0.0
        # At floor 0.0 everything is kept: recall 1.0, fpr 1.0.
        assert points[0].recall == 1.0
        assert points[0].false_positive_rate == 1.0
        # At floor 0.5 all positives kept, all negatives dropped.
        mid = next(p for p in points if abs(p.floor - 0.5) < 1e-9)
        assert mid.recall == 1.0
        assert mid.false_positive_rate == 0.0

    def test_high_floor_drops_positives(self) -> None:
        """A floor above some positives reduces recall."""
        points = sweep_floor([0.4, 0.6], [0.1], start=0.0, stop=1.0, step=0.1)
        p_at_half = next(p for p in points if abs(p.floor - 0.5) < 1e-9)
        assert p_at_half.recall == 0.5  # only 0.6 survives

    def test_empty_inputs_safe(self) -> None:
        """Empty distributions yield defined (zero) rates, no crash."""
        points = sweep_floor([], [], start=0.0, stop=0.2, step=0.1)
        assert all(p.recall == 0.0 and p.false_positive_rate == 0.0 for p in points)


class TestProposeFloor:
    """The proposed floor maximises the recall/noise separation (Youden's J)."""

    def test_picks_separating_floor(self) -> None:
        """Cleanly separable distributions → a floor between them, recall 1.0 / fpr 0.0."""
        positives = [0.75, 0.82, 0.69]
        negatives = [0.15, 0.22, 0.31]
        proposal = propose_floor(positives, negatives, start=0.0, stop=1.0, step=0.05)
        assert 0.31 < proposal.floor <= 0.69
        assert proposal.recall == 1.0
        assert proposal.false_positive_rate == 0.0

    def test_overlap_trades_off(self) -> None:
        """Overlapping distributions → J maximised where the margin is best."""
        positives = [0.5, 0.6, 0.7]
        negatives = [0.4, 0.55, 0.65]
        proposal = propose_floor(positives, negatives, start=0.0, stop=1.0, step=0.05)
        assert isinstance(proposal, FloorPoint)
        # Youden's J = recall - fpr is maximised and non-negative for this split.
        assert proposal.recall - proposal.false_positive_rate >= 0.0
