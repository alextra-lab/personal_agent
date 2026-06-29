"""FRE-694 — pure unit tests for the embedder-separation metric (no embedder/substrate).

Covers the Matryoshka client-side reduction (first-N + L2 renormalize), the
"clean floor" overlap metric, robust percentiles, and the per-arm aggregation —
the geometry behind the separation verdict. Embedder-free: synthetic vectors only.
"""

from __future__ import annotations

import math

import pytest
from scripts.eval.fre435_memory_recall.separation_benchmark import _score
from scripts.eval.fre435_memory_recall.separation_report import (
    overlap_counts,
    percentile,
    summarize_separation,
    truncate_renormalize,
)


def test_score_is_neo4j_normalized_cosine() -> None:
    """_score maps cosine [-1,1] -> [0,1] as Neo4j's vector index does ((cos+1)/2).

    This is the transform the 0.6B parity gate validated; locking it prevents a
    silent revert to raw cosine (which broke parity by a uniform ~0.24 shift).
    """
    assert _score([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)  # identical → cos 1 → 1.0
    assert _score([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.5)  # orthogonal → cos 0 → 0.5
    assert _score([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(0.0)  # opposite → cos -1 → 0.0


def test_truncate_renormalize_first_n_and_unit_norm() -> None:
    """Takes the first N components and renormalizes to unit length."""
    out = truncate_renormalize([3.0, 4.0, 9.9, 9.9], 2)
    assert out == pytest.approx([0.6, 0.8])
    assert math.isclose(sum(x * x for x in out) ** 0.5, 1.0)


def test_truncate_renormalize_full_length_is_identity_direction() -> None:
    """Requesting the native length renormalizes without dropping components."""
    out = truncate_renormalize([0.0, 3.0, 4.0], 3)
    assert out == pytest.approx([0.0, 0.6, 0.8])


def test_truncate_renormalize_rejects_zero_and_short(  # noqa: D103
) -> None:
    with pytest.raises(ValueError, match="zero"):
        truncate_renormalize([0.0, 0.0], 2)
    with pytest.raises(ValueError, match="length"):
        truncate_renormalize([1.0, 2.0], 3)  # too short for the requested dim


def test_overlap_counts() -> None:
    """Counts negatives at/above the lowest positive and positives at/below the highest negative."""
    # Overlapping clouds.
    assert overlap_counts([0.80, 0.90], [0.50, 0.85]) == (1, 1)
    # Clean separation: max(neg) < min(pos).
    assert overlap_counts([0.80, 0.90], [0.50, 0.60]) == (0, 0)
    # Empty guards.
    assert overlap_counts([], [0.5]) == (0, 0)
    assert overlap_counts([0.5], []) == (0, 0)


def test_percentile_linear_interpolation() -> None:
    """Linear-interpolation percentile matches the numpy 'linear' convention."""
    assert percentile([10.0, 20.0, 30.0], 0) == 10.0
    assert percentile([10.0, 20.0, 30.0], 100) == 30.0
    assert percentile([10.0, 20.0, 30.0], 50) == 20.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == pytest.approx(2.5)


def test_summarize_separation_clean_and_overlapping() -> None:
    """Aggregation reports the clean-floor verdict and a robust (p5/p95) verdict."""
    clean = summarize_separation(positives=[0.80, 0.85, 0.90], negatives=[0.40, 0.55, 0.60])
    assert clean.n_positives == 3 and clean.n_negatives == 3
    assert clean.neg_above_min_pos == 0 and clean.pos_below_max_neg == 0
    assert clean.clean_floor is True
    assert clean.pos_median == pytest.approx(0.85)

    overlapping = summarize_separation(positives=[0.70, 0.78, 0.82], negatives=[0.60, 0.79, 0.81])
    assert overlapping.clean_floor is False
    assert overlapping.neg_above_min_pos >= 1
    # Robust verdict uses p5(pos) vs p95(neg), less outlier-sensitive than min/max.
    assert overlapping.robust_clean == (overlapping.pos_p5 > overlapping.neg_p95)
