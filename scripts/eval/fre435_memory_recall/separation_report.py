"""FRE-694 — pure separation metrics for the embedder-ceiling benchmark.

The FRE-694 question is whether a higher-quality embedder opens a *clean floor* on
the FRE-670 probe: a cosine cutoff that separates true matches (positives) from
no-record (negatives). Recall@5 saturates and hides this; separation is the metric.

This module is the pure geometry core — Matryoshka client-side reduction, the
overlap counts that define a clean floor, robust percentiles, and the per-arm
aggregation. Free of any ``personal_agent`` / substrate / embedder import so it is
fully unit-testable; the harness (``separation_benchmark.py``) feeds it cosines.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from scripts.eval.fre435_memory_recall.calibration import FloorPoint


def best_separation_at_observed(
    positives: Sequence[float], negatives: Sequence[float]
) -> FloorPoint:
    """Best Youden's J swept at the *observed* scores (not a fixed grid).

    For a cross-encoder reranker the score scale is arbitrary and often compressed, so
    a fixed 0.0–0.95/0.05 grid (``calibration.sweep_floor``) can straddle the real
    separating threshold and understate separation (codex review, FRE-695). Sweeping
    candidate floors at every observed score finds the true max-J operating point on
    that arm's own distribution. The floor is arm-specific — never transferable.

    Args:
        positives: True-match scores (should survive the floor).
        negatives: Distractor scores (should be dropped).

    Returns:
        The :class:`~calibration.FloorPoint` at the maximum Youden's J. A floor just
        above the highest observed score (recall 0, fpr 0, J 0) is always a candidate,
        so the result is never worse than J = 0.

    Raises:
        ValueError: If either sequence is empty.
    """
    if not positives or not negatives:
        raise ValueError("separation needs non-empty positive and negative samples")
    candidates = sorted({*positives, *negatives})
    candidates.append(candidates[-1] + 1e-9)  # a floor above everything: J = 0 baseline
    n_pos, n_neg = len(positives), len(negatives)
    best = FloorPoint(floor=candidates[-1], recall=0.0, false_positive_rate=0.0)
    for floor in candidates:
        recall = sum(1 for p in positives if p >= floor) / n_pos
        fpr = sum(1 for n in negatives if n >= floor) / n_neg
        point = FloorPoint(floor=floor, recall=recall, false_positive_rate=fpr)
        if point.youden_j > best.youden_j:
            best = point
    return best


def truncate_renormalize(vec: Sequence[float], dim: int) -> list[float]:
    """Reduce an MRL embedding to ``dim`` components and renormalize to unit length.

    Valid for Matryoshka-trained embedders (Qwen3-Embedding): the first ``dim``
    components are themselves a coherent lower-dimensional embedding; renormalizing
    restores unit length so cosine == dot product.

    Args:
        vec: The native-dimension embedding.
        dim: Target dimensionality (``<= len(vec)``).

    Returns:
        The unit-length ``dim``-component embedding.

    Raises:
        ValueError: If ``vec`` is shorter than ``dim`` (a length/dimension mismatch —
            never silently truncate the wrong way) or its first ``dim`` components are
            all zero (a degenerate / failed embedding — fail loud, never score a zero).
    """
    if len(vec) < dim:
        raise ValueError(f"vector length {len(vec)} < requested dim {dim}")
    head = list(vec[:dim])
    norm = sum(x * x for x in head) ** 0.5
    if norm == 0.0:
        raise ValueError("cannot renormalize a zero vector (degenerate embedding)")
    return [x / norm for x in head]


def percentile(values: Sequence[float], p: float) -> float:
    """Linear-interpolation percentile (the numpy 'linear' convention).

    Args:
        values: Samples (need not be sorted; non-empty).
        p: Percentile in ``[0, 100]``.

    Returns:
        The interpolated percentile.

    Raises:
        ValueError: If ``values`` is empty.
    """
    if not values:
        raise ValueError("percentile of an empty sequence")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (p / 100.0) * (len(ordered) - 1)
    lower = int(rank)
    frac = rank - lower
    if lower + 1 >= len(ordered):
        return ordered[-1]
    return ordered[lower] + frac * (ordered[lower + 1] - ordered[lower])


def overlap_counts(positives: Sequence[float], negatives: Sequence[float]) -> tuple[int, int]:
    """Count the overlap between the positive and negative cosine clouds.

    Args:
        positives: True-match cosines.
        negatives: Hardest non-match cosines.

    Returns:
        ``(neg_above_min_pos, pos_below_max_neg)`` — negatives at/above the lowest
        positive, and positives at/below the highest negative. Both zero ⇔ a clean
        floor exists (``max(negatives) < min(positives)``). Empty inputs ⇒ ``(0, 0)``.
    """
    if not positives or not negatives:
        return (0, 0)
    min_pos = min(positives)
    max_neg = max(negatives)
    neg_above = sum(1 for n in negatives if n >= min_pos)
    pos_below = sum(1 for pos in positives if pos <= max_neg)
    return (neg_above, pos_below)


@dataclass(frozen=True)
class SeparationStats:
    """Separation summary for one (arm, dimension).

    Attributes:
        n_positives: Number of positive (true-match) cosine samples.
        n_negatives: Number of negative (hardest non-match) cosine samples.
        pos_min/pos_median/pos_max/pos_p5: Positive cosine distribution.
        neg_min/neg_median/neg_max/neg_p95: Negative cosine distribution.
        neg_above_min_pos: Negatives at/above the lowest positive.
        pos_below_max_neg: Positives at/below the highest negative.
        clean_floor: ``max(neg) < min(pos)`` — a cutoff cleanly separates the clouds.
        robust_clean: ``p5(pos) > p95(neg)`` — separation robust to a few outliers.
    """

    n_positives: int
    n_negatives: int
    pos_min: float
    pos_median: float
    pos_max: float
    pos_p5: float
    neg_min: float
    neg_median: float
    neg_max: float
    neg_p95: float
    neg_above_min_pos: int
    pos_below_max_neg: int
    clean_floor: bool
    robust_clean: bool


def summarize_separation(positives: Sequence[float], negatives: Sequence[float]) -> SeparationStats:
    """Aggregate positive/negative cosines into a :class:`SeparationStats`.

    Args:
        positives: Per-expected-entity true-match cosines (a compound case
            contributes one sample per expected entity, so a weak supporting fact
            cannot hide behind a strong primary — codex review).
        negatives: Per-query strongest non-match cosine (positives and controls).

    Returns:
        The separation summary.

    Raises:
        ValueError: If either sequence is empty.
    """
    if not positives or not negatives:
        raise ValueError("separation needs non-empty positive and negative samples")
    neg_above, pos_below = overlap_counts(positives, negatives)
    pos_p5 = percentile(positives, 5)
    neg_p95 = percentile(negatives, 95)
    return SeparationStats(
        n_positives=len(positives),
        n_negatives=len(negatives),
        pos_min=min(positives),
        pos_median=percentile(positives, 50),
        pos_max=max(positives),
        pos_p5=pos_p5,
        neg_min=min(negatives),
        neg_median=percentile(negatives, 50),
        neg_max=max(negatives),
        neg_p95=neg_p95,
        neg_above_min_pos=neg_above,
        pos_below_max_neg=pos_below,
        clean_floor=(neg_above == 0 and pos_below == 0),
        robust_clean=(pos_p5 > neg_p95),
    )
