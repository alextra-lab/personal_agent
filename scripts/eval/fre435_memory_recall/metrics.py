"""FRE-488 — D1 metric core for the memory-recall harness (ADR-0087 §D1).

Pure functions only — no substrate, no LLM, no I/O. They operate on
*namespace-agnostic* id sequences/sets; the harness is responsible for
flattening a ``MemoryRecallResult`` into ordered ids and for namespacing labels
(``entity:`` / ``episode:``) so the two id spaces never collide.

Conventions (codex review, FRE-488):
* ``recall_at_k`` / ``reciprocal_rank`` / ``ndcg_at_k`` return ``None`` when there
  is nothing relevant to find (``|relevant| == 0``) so they are *excluded* from
  aggregates — averaging a vacuous ``1.0`` would silently inflate the score.
* ``false_negative`` (the ADR headline) and ``retrieval_miss`` are distinct: the
  former is "denied / returned nothing despite prior context", the latter is
  "returned the *wrong* context".
* ``ndcg_at_k`` normalises against ``min(k, |relevant|)`` ideal hits.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class RecallPrecision:
    """A ``(recall, precision)`` pair at a single ``k``.

    Attributes:
        recall: recall@k (``None`` when ``|relevant| == 0``).
        precision: precision@k.
    """

    recall: float | None
    precision: float


@dataclass(frozen=True)
class WriteOutcome:
    """Per-case write-path outcome (write-completeness inputs).

    Attributes:
        extraction_fired: Whether entity extraction produced any candidate.
        entities_landed: Count of non-empty semantic facts that landed.
        entities_expected: Count of entities the case expected to land.
        description_integrity: Proxy integrity score in ``[0, 1]`` (``None`` when
            not scored — the LLM-judge is later work).
        joinable: Whether facts join back to a real session (``None`` when the
            joinability probe was not run).
    """

    extraction_fired: bool
    entities_landed: int
    entities_expected: int
    description_integrity: float | None = None
    joinable: bool | None = None


def _check_k(k: int) -> None:
    """Raise if ``k`` is not a positive cut-off."""
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")


def _hits_in_top_k(retrieved: Sequence[str], relevant: set[str], k: int) -> int:
    """Count distinct relevant ids present in the top-``k`` retrieved ids."""
    return len(set(retrieved[:k]) & relevant)


def recall_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float | None:
    """recall@k = relevant hits in top-k / total relevant.

    Args:
        retrieved: Ordered retrieved ids (best first).
        relevant: The relevant id set.
        k: Cut-off rank (>= 1).

    Returns:
        recall@k, or ``None`` when ``relevant`` is empty (excluded from aggregates).
    """
    _check_k(k)
    if not relevant:
        return None
    return _hits_in_top_k(retrieved, relevant, k) / len(relevant)


def precision_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    """precision@k = relevant hits in top-k / k.

    Args:
        retrieved: Ordered retrieved ids (best first).
        relevant: The relevant id set.
        k: Cut-off rank (>= 1).

    Returns:
        precision@k (0.0 when nothing relevant is retrieved).
    """
    _check_k(k)
    return _hits_in_top_k(retrieved, relevant, k) / k


def reciprocal_rank(retrieved: Sequence[str], relevant: set[str]) -> float | None:
    """Reciprocal rank of the first relevant id (MRR contribution).

    Args:
        retrieved: Ordered retrieved ids (best first).
        relevant: The relevant id set.

    Returns:
        ``1 / rank`` of the first relevant id, ``0.0`` if none retrieved, or
        ``None`` when ``relevant`` is empty.
    """
    if not relevant:
        return None
    for rank, rid in enumerate(retrieved, start=1):
        if rid in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float | None:
    """Normalised DCG at ``k`` with binary gains.

    IDCG normalises against ``min(k, |relevant|)`` ideal hits, so a perfect
    ranking that cannot fit all relevant items into the top-``k`` still scores
    ``1.0`` (codex review, FRE-488).

    Args:
        retrieved: Ordered retrieved ids (best first).
        relevant: The relevant id set.
        k: Cut-off rank (>= 1).

    Returns:
        nDCG@k, or ``None`` when ``relevant`` is empty.
    """
    _check_k(k)
    if not relevant:
        return None
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, rid in enumerate(retrieved[:k], start=1)
        if rid in relevant
    )
    ideal_hits = min(k, len(relevant))
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else None


def false_negative(retrieved: Sequence[str], relevant: set[str], denied: bool) -> bool | None:
    """The ADR-0087 headline failure: prior context exists but is not surfaced.

    A false negative is when the relevant context demonstrably exists yet the
    system returns nothing or explicitly denies ("no prior discussions"). This
    is *narrower* than :func:`retrieval_miss` — returning the wrong context is a
    miss but not a false negative.

    Args:
        retrieved: Ordered retrieved ids (best first).
        relevant: The relevant id set.
        denied: Whether the system explicitly denied having prior context.

    Returns:
        ``True``/``False``, or ``None`` when there was nothing relevant to recall.
    """
    if not relevant:
        return None
    return len(retrieved) == 0 or denied


def retrieval_miss(retrieved: Sequence[str], relevant: set[str], k: int) -> bool | None:
    """Whether no relevant id appears in the top-``k`` retrieved ids.

    Superset signal to :func:`false_negative`: it also catches the case where the
    system returned *something* (not denied) but not the relevant context.

    Args:
        retrieved: Ordered retrieved ids (best first).
        relevant: The relevant id set.
        k: Cut-off rank (>= 1).

    Returns:
        ``True`` if ``recall@k == 0``, else ``False``; ``None`` when ``relevant``
        is empty.
    """
    _check_k(k)
    if not relevant:
        return None
    return _hits_in_top_k(retrieved, relevant, k) == 0


def k_sweep(
    retrieved: Sequence[str], relevant: set[str], ks: Sequence[int]
) -> dict[int, RecallPrecision]:
    """recall/precision across a sweep of ``k`` values.

    Separates "not in the index at all" (low recall even at large ``k``) from
    "ranked too low" (recall rises as ``k`` grows).

    Args:
        retrieved: Ordered retrieved ids (best first).
        relevant: The relevant id set.
        ks: The cut-offs to evaluate.

    Returns:
        Mapping of ``k`` to its :class:`RecallPrecision`.
    """
    return {
        k: RecallPrecision(
            recall=recall_at_k(retrieved, relevant, k),
            precision=precision_at_k(retrieved, relevant, k),
        )
        for k in ks
    }


def extraction_fire_rate(outcomes: Sequence[WriteOutcome]) -> float | None:
    """Fraction of write outcomes where extraction fired.

    Args:
        outcomes: Per-case write outcomes.

    Returns:
        Fire rate, or ``None`` when there are no outcomes.
    """
    if not outcomes:
        return None
    return sum(1 for o in outcomes if o.extraction_fired) / len(outcomes)


def landing_rate(outcomes: Sequence[WriteOutcome]) -> float | None:
    """Landed facts / expected facts, aggregated over all expected entities.

    Args:
        outcomes: Per-case write outcomes.

    Returns:
        Landing rate, or ``None`` when no entities were expected.
    """
    expected = sum(o.entities_expected for o in outcomes)
    if expected == 0:
        return None
    landed = sum(min(o.entities_landed, o.entities_expected) for o in outcomes)
    return landed / expected


def mean_optional(values: Sequence[float | None]) -> float | None:
    """Mean of the non-``None`` values, or ``None`` if there are none.

    Args:
        values: Values that may include ``None`` (excluded entries).

    Returns:
        Mean over present values, or ``None``.
    """
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present) / len(present)
