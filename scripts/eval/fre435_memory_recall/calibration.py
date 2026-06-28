"""FRE-655 — recall_similarity_floor calibration (pure, no substrate).

Given the cosine-score distributions of probe *positives* (the expected entity
for each case) and *negatives* (the top distractor that should be excluded),
sweep candidate floor values and pick the threshold that best separates them.

The chosen floor is **global** (one ``recall_similarity_floor`` for all queries,
per ``settings``), so the proposal must hold across the whole probe set, not
per case. The separation metric is Youden's J (recall − false-positive-rate),
which is the standard single-threshold operating point for a binary screen.

Kept free of any ``personal_agent`` / substrate import so it is fully unit
testable; ``ab_relevance_bounded.py`` feeds it the captured score lists.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class FloorPoint:
    """One point on the floor sweep.

    Attributes:
        floor: The candidate cosine threshold.
        recall: Fraction of positives kept (score >= floor).
        false_positive_rate: Fraction of negatives kept (score >= floor).
    """

    floor: float
    recall: float
    false_positive_rate: float

    @property
    def youden_j(self) -> float:
        """Recall minus false-positive-rate (the separation margin)."""
        return self.recall - self.false_positive_rate


def _kept_fraction(scores: Sequence[float], floor: float) -> float:
    """Fraction of ``scores`` at or above ``floor`` (0.0 for an empty list)."""
    if not scores:
        return 0.0
    return sum(1 for s in scores if s >= floor) / len(scores)


def sweep_floor(
    positive_scores: Sequence[float],
    negative_scores: Sequence[float],
    *,
    start: float = 0.0,
    stop: float = 0.95,
    step: float = 0.05,
) -> list[FloorPoint]:
    """Sweep candidate floors and report recall + false-positive-rate at each.

    Args:
        positive_scores: Cosine scores of the expected entities (should survive).
        negative_scores: Cosine scores of the top distractors (should be dropped).
        start: First floor value (inclusive).
        stop: Last floor value (inclusive within floating tolerance).
        step: Sweep increment.

    Returns:
        Floor points ordered by ascending floor.
    """
    points: list[FloorPoint] = []
    steps = int(round((stop - start) / step)) + 1
    for i in range(steps):
        floor = round(start + i * step, 6)
        points.append(
            FloorPoint(
                floor=floor,
                recall=_kept_fraction(positive_scores, floor),
                false_positive_rate=_kept_fraction(negative_scores, floor),
            )
        )
    return points


def propose_floor(
    positive_scores: Sequence[float],
    negative_scores: Sequence[float],
    *,
    start: float = 0.0,
    stop: float = 0.95,
    step: float = 0.05,
) -> FloorPoint:
    """Pick the floor at the recall/noise Pareto knee (max Youden's J).

    Ties on J are broken toward the *lower* floor, which favours recall — a
    conservative default for a recall-first system (the owner can raise it).

    Args:
        positive_scores: Cosine scores of the expected entities.
        negative_scores: Cosine scores of the top distractors.
        start: First floor value (inclusive).
        stop: Last floor value (inclusive within floating tolerance).
        step: Sweep increment.

    Returns:
        The chosen :class:`FloorPoint`.
    """
    points = sweep_floor(positive_scores, negative_scores, start=start, stop=stop, step=step)
    # Max J; on a tie keep the first (lowest floor) seen.
    best = points[0]
    for point in points[1:]:
        if point.youden_j > best.youden_j:
            best = point
    return best
