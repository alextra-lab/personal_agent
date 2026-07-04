"""FRE-770 — inter-annotator agreement (IAA) for the ADR-0109 V2 gold relabel.

Pure statistics over blind classification labels gathered from N model raters
per gold entity: Fleiss' kappa (chance-corrected, multi-rater) and raw pairwise
agreement. No I/O — the driver (``relabel_v2_types.py``) gathers labels and
calls these; unit tests exercise hand-computed values.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Literal

#: Fleiss' kappa is undefined when the expected chance-agreement term is 1.0
#: (every rater on every item picked from a single observed category) — the
#: denominator (1 - P_bar_e) is zero. Treated as a status, never coerced to
#: 0.0 or 1.0.
_ZERO_VARIANCE_EPSILON = 1e-9


@dataclass(frozen=True)
class KappaResult:
    """Fleiss' kappa for one category set over one set of rated items.

    Attributes:
        kappa: Chance-corrected agreement, or ``None`` when undefined.
        status: ``"ok"`` or ``"undefined_zero_variance"``.
        n_items: Number of rated items the statistic was computed over.
        n_positive: For a one-vs-rest binarization, the count of rater
            assignments to the target category (0 for a plain multi-category
            call over all types).
        raw_agreement: The mean pairwise exact-match agreement fraction.
    """

    kappa: float | None
    status: Literal["ok", "undefined_zero_variance"]
    n_items: int
    n_positive: int
    raw_agreement: float


def _validate(rater_labels: Sequence[Sequence[str]]) -> int:
    """Validate uniform rater count and return it.

    Args:
        rater_labels: One inner sequence of labels per item; every item must
            carry the same number of raters (>=2).

    Returns:
        The uniform number of raters per item.

    Raises:
        ValueError: If empty, or rater counts are non-uniform or <2.
    """
    if not rater_labels:
        raise ValueError("rater_labels is empty")
    n_raters = len(rater_labels[0])
    if n_raters < 2:
        raise ValueError("need at least 2 raters per item")
    if any(len(item) != n_raters for item in rater_labels):
        raise ValueError("every item must carry the same number of rater labels")
    return n_raters


def pairwise_agreement(rater_labels: Sequence[Sequence[str]]) -> float:
    """Exact-match agreement rate across every rater pair, over every item.

    Args:
        rater_labels: One inner sequence of labels per item (uniform rater count).

    Returns:
        Agreeing pairs / total pairs, across all items.
    """
    _validate(rater_labels)
    agree = 0
    total = 0
    for item in rater_labels:
        for a, b in combinations(item, 2):
            total += 1
            agree += a == b
    return agree / total


def pairwise_agreement_by_pair(
    rater_labels: Sequence[Sequence[str]], rater_names: Sequence[str]
) -> Mapping[tuple[str, str], float]:
    """Exact-match agreement rate for each distinct rater-index pair.

    Args:
        rater_labels: One inner sequence of labels per item; rater order must be
            consistent across all items (index 0 is always the same rater).
        rater_names: One name per rater index, same order as the inner sequences.

    Returns:
        A mapping from ``(rater_a, rater_b)`` (in ``rater_names`` order) to its
        agreement fraction over all items.

    Raises:
        ValueError: If ``rater_names`` length does not match raters per item.
    """
    n_raters = _validate(rater_labels)
    if len(rater_names) != n_raters:
        raise ValueError("rater_names length must match raters per item")
    result: dict[tuple[str, str], float] = {}
    for i, j in combinations(range(n_raters), 2):
        agree = sum(item[i] == item[j] for item in rater_labels)
        result[(rater_names[i], rater_names[j])] = agree / len(rater_labels)
    return result


def fleiss_kappa(rater_labels: Sequence[Sequence[str]], categories: Sequence[str]) -> KappaResult:
    """Fleiss' kappa over an arbitrary category set.

    Args:
        rater_labels: One inner sequence of labels per item (uniform rater count).
        categories: The full declared category set (unused categories are fine —
            they contribute zero to the chance-agreement term).

    Returns:
        The :class:`KappaResult`; ``status="undefined_zero_variance"`` (kappa
        ``None``) when every rater on every item agrees on a single observed
        category, making the chance-agreement term 1.0 (a 0/0 form).

    Raises:
        ValueError: On malformed input (see :func:`_validate`), or a label
            outside ``categories``.
    """
    n_raters = _validate(rater_labels)
    n_items = len(rater_labels)
    cat_set = set(categories)

    per_item_counts: list[Counter[str]] = []
    for item in rater_labels:
        counts: Counter[str] = Counter(item)
        unknown = set(counts) - cat_set
        if unknown:
            raise ValueError(f"label(s) {unknown} not in declared categories")
        per_item_counts.append(counts)

    p_i = [
        (sum(c * c for c in counts.values()) - n_raters) / (n_raters * (n_raters - 1))
        for counts in per_item_counts
    ]
    p_bar = sum(p_i) / n_items

    totals: Counter[str] = Counter()
    for counts in per_item_counts:
        totals.update(counts)
    total_assignments = n_items * n_raters
    p_j = {cat: totals.get(cat, 0) / total_assignments for cat in categories}
    p_bar_e = sum(p * p for p in p_j.values())

    raw_agreement = pairwise_agreement(rater_labels)

    if abs(1.0 - p_bar_e) < _ZERO_VARIANCE_EPSILON:
        return KappaResult(
            kappa=None,
            status="undefined_zero_variance",
            n_items=n_items,
            n_positive=0,
            raw_agreement=raw_agreement,
        )

    kappa = (p_bar - p_bar_e) / (1.0 - p_bar_e)
    return KappaResult(
        kappa=kappa,
        status="ok",
        n_items=n_items,
        n_positive=0,
        raw_agreement=raw_agreement,
    )


def fleiss_kappa_one_vs_rest(rater_labels: Sequence[Sequence[str]], target: str) -> KappaResult:
    """Fleiss' kappa for a single category vs. everything else.

    Binarizes every label to ``"pos"`` (== target) / ``"neg"`` (otherwise) and
    delegates to :func:`fleiss_kappa`. ``n_positive`` is filled in with the
    actual count of ``target`` assignments (unlike the general multi-category
    call, where it is always 0) so a sparse type's prevalence is visible even
    when the kappa itself is undefined.

    Args:
        rater_labels: One inner sequence of labels per item.
        target: The category to isolate.

    Returns:
        The one-vs-rest :class:`KappaResult` for ``target``.
    """
    binarized = [["pos" if label == target else "neg" for label in item] for item in rater_labels]
    result = fleiss_kappa(binarized, categories=("pos", "neg"))
    n_positive = sum(label == target for item in rater_labels for label in item)
    return KappaResult(
        kappa=result.kappa,
        status=result.status,
        n_items=result.n_items,
        n_positive=n_positive,
        raw_agreement=result.raw_agreement,
    )


@dataclass(frozen=True)
class IAAReport:
    """The full inter-annotator-agreement report for one relabel run.

    Attributes:
        overall: Multi-category Fleiss' kappa across the full declared V2 type set.
        per_type: One-vs-rest :class:`KappaResult` per V2 type.
        by_rater_pair: Raw agreement fraction for each distinct rater pair.
        disagreements: Item ids (case/entity keys) where raters split.
    """

    overall: KappaResult
    per_type: Mapping[str, KappaResult]
    by_rater_pair: Mapping[tuple[str, str], float]
    disagreements: Sequence[str]


def build_iaa_report(
    rater_labels: Sequence[Sequence[str]],
    item_ids: Sequence[str],
    rater_names: Sequence[str],
    categories: Sequence[str],
) -> IAAReport:
    """Assemble the full :class:`IAAReport` from one relabel run's raw labels.

    Args:
        rater_labels: One inner sequence of labels per item (uniform rater count).
        item_ids: One id per item, same order as ``rater_labels`` — identifies
            disagreements in the report.
        rater_names: One name per rater index, same order as the inner sequences.
        categories: The full declared V2 type set.

    Returns:
        The assembled :class:`IAAReport`.

    Raises:
        ValueError: If ``item_ids`` length does not match ``rater_labels``.
    """
    if len(item_ids) != len(rater_labels):
        raise ValueError("item_ids must have one entry per item")

    overall = fleiss_kappa(rater_labels, categories)
    per_type = {cat: fleiss_kappa_one_vs_rest(rater_labels, cat) for cat in categories}
    by_pair = pairwise_agreement_by_pair(rater_labels, rater_names)
    disagreements = [
        item_id for item_id, item in zip(item_ids, rater_labels, strict=True) if len(set(item)) > 1
    ]
    return IAAReport(
        overall=overall, per_type=per_type, by_rater_pair=by_pair, disagreements=disagreements
    )
