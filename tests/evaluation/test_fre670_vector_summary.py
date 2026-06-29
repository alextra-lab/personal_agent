"""FRE-670 — pure aggregation for the vector arm's per-register recall + abstention.

These helpers turn per-case vector-recall outcomes (computed by the co-resident
``calibrate`` pass in ``ab_relevance_bounded.py``) into the owner-requested report:
recall@1/@5 split by register, the natural-vs-imagery register delta, and the
control abstention rate at a cosine floor. Pure — no substrate, no embedder.
"""

from __future__ import annotations

from scripts.eval.fre435_memory_recall.semantic_report import (
    aggregate_by_register,
    control_abstention,
    register_delta,
)


def test_aggregate_by_register() -> None:
    """Means are computed per register and overall."""
    per_case = [
        ("imagery", {1: 0.0, 5: 1.0}),
        ("imagery", {1: 1.0, 5: 1.0}),
        ("natural", {1: 1.0, 5: 1.0}),
    ]
    agg = aggregate_by_register(per_case, (1, 5))
    assert agg["by_register"]["imagery"][1] == 0.5
    assert agg["by_register"]["imagery"][5] == 1.0
    assert agg["by_register"]["natural"][1] == 1.0
    assert agg["overall"][1] == (0.0 + 1.0 + 1.0) / 3


def test_control_abstention() -> None:
    """A control abstains when its top cosine is below the floor."""
    abstained, total = control_abstention([0.1, 0.4, 0.9, 0.55], floor=0.5)
    assert (abstained, total) == (2, 4)  # 0.1 and 0.4 are below 0.5
    assert control_abstention([], floor=0.5) == (0, 0)


def test_register_delta() -> None:
    """Register delta is natural minus imagery at k (None when a register is absent)."""
    by_register = {"natural": {1: 0.9, 5: 1.0}, "imagery": {1: 0.5, 5: 0.7}}
    assert register_delta(by_register, 5) == 1.0 - 0.7
    assert register_delta({"natural": {5: 1.0}}, 5) is None  # imagery missing
