"""FRE-670 — pure aggregation for the three-arm semantic recall comparison.

Turns per-case recall outcomes into the owner-requested report shape: recall@k
split by register (``natural`` vs ``imagery``), the register delta, and the control
abstention rate at a cosine floor. Kept free of any ``personal_agent`` or substrate
import so it is fully unit-testable and shared by the BM25 arm
(``keyword_baseline.py``) and the vector arm (``ab_relevance_bounded.py`` calibrate).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def _mean(values: Sequence[float]) -> float:
    """Arithmetic mean (0.0 over an empty sequence)."""
    return sum(values) / len(values) if values else 0.0


def aggregate_by_register(
    per_case: Sequence[tuple[str, Mapping[int, float]]], k_values: Sequence[int]
) -> dict[str, dict[str, dict[int, float]]]:
    """Aggregate per-case recall into overall + per-register means.

    Args:
        per_case: ``(register, {k: recall})`` for each scored positive.
        k_values: The recall cut-offs to aggregate.

    Returns:
        ``{"overall": {k: mean}, "by_register": {register: {k: mean}}}``.
    """
    buckets: dict[str, dict[int, list[float]]] = {}
    overall: dict[int, list[float]] = {k: [] for k in k_values}
    for register, recall_by_k in per_case:
        bucket = buckets.setdefault(register, {k: [] for k in k_values})
        for k in k_values:
            bucket[k].append(recall_by_k[k])
            overall[k].append(recall_by_k[k])
    return {
        "overall": {k: _mean(v) for k, v in overall.items()},
        "by_register": {
            register: {k: _mean(v) for k, v in bucket.items()}
            for register, bucket in buckets.items()
        },
    }


def control_abstention(top_cosines: Sequence[float], floor: float) -> tuple[int, int]:
    """Count controls whose top vector score falls below the floor (correct abstention).

    Args:
        top_cosines: The strongest vector score returned for each control query.
        floor: The relevance floor below which recall is suppressed (ADR-0100).

    Returns:
        ``(abstained, total)``.
    """
    abstained = sum(1 for cosine in top_cosines if cosine < floor)
    return abstained, len(top_cosines)


def register_delta(by_register: Mapping[str, Mapping[int, float]], k: int) -> float | None:
    """Natural-minus-imagery recall at ``k`` (``None`` if either register is absent).

    A large positive delta means recall degrades on oblique phrasing — the core
    FRE-670 measurement.
    """
    natural = by_register.get("natural", {}).get(k)
    imagery = by_register.get("imagery", {}).get(k)
    if natural is None or imagery is None:
        return None
    return natural - imagery
