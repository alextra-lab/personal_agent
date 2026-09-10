"""FRE-1477 -- pure analysis for the proactive relevance-bound calibration (ADR-0148 D4).

No ``personal_agent`` import and no substrate, so every decision the calibration makes is
unit-testable without an embedder, a graph, or a network. The driver
(``calibrate.py``) supplies the measured score populations; this module decides what they
mean.

Two jobs:

* **Choose the bound**, under ADR-0148 D4's two constraints, or report that no value
  satisfies both. It never picks a number in the incompatible case -- that is a finding
  for the owner, and the response is a reranker-side bound or a different arm.
* **Validate the instrument**, reproducing FRE-694's parity discipline on the serving arm.

All scores are in Neo4j score space, ``(cosine + 1) / 2``, which is what
``db.index.vector.queryNodes`` returns and what the proactive row carries as
``vector_score``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from scripts.eval.fre435_memory_recall.separation_report import percentile

#: FRE-694's own instrument tolerance, reused unchanged so the gate is the same gate.
PARITY_TOLERANCE = 0.02

#: ADR-0148 AC-5: the bound must admit at least this share of the whole labelled positive
#: set. The denominator is every labelled positive, named here so it cannot be re-cut.
MIN_POSITIVE_SHARE = 0.90

#: Granularity of the bound above the median top-ranked non-match. The bound must be
#: strictly above that median (ADR-0148 D4), and one step is the smallest such value this
#: harness will propose. Three decimals is finer than the fourth-decimal noise the probe
#: set's 54 cases can resolve, so the step is not the binding constraint on the answer.
BOUND_STEP = 0.001


@dataclass(frozen=True)
class BoundProposal:
    """The calibration's verdict on a measured pair of score populations.

    Attributes:
        bound: The chosen bound in Neo4j score space, or None when incompatible.
        positive_admitted_share: Share of the whole labelled positive set the bound
            admits, or None when incompatible.
        negative_rejected_share: Share of the negatives the bound rejects, or None when
            incompatible. Reported because the bound's whole purpose is to reject, and a
            bound that clears both constraints while rejecting few negatives is a weak
            result the owner should see rather than infer.
        negative_median: Median of the negatives -- the value the bound must reject.
        incompatible: True when no value satisfies both constraints.
        reason: Why, when incompatible. None otherwise.
    """

    bound: float | None
    positive_admitted_share: float | None
    negative_rejected_share: float | None
    negative_median: float
    incompatible: bool
    reason: str | None


def admitted_share(scores: Sequence[float], bound: float) -> float:
    """Share of *scores* the bound admits.

    Admission is ``score >= bound``. ADR-0148 D4 states the predicate as "below the bound
    is rejected", matching the dense arm's existing ``>=`` comparison
    (``memory/service.py:5020``) rather than fighting that convention.

    Args:
        scores: The score population.
        bound: The bound to apply.

    Returns:
        The admitted share in ``[0, 1]``, or 0.0 for an empty population.
    """
    if not scores:
        return 0.0
    return sum(1 for s in scores if s >= bound) / len(scores)


def choose_bound(
    positives: Sequence[float],
    negatives: Sequence[float],
    *,
    min_positive_share: float = MIN_POSITIVE_SHARE,
    step: float = BOUND_STEP,
) -> BoundProposal:
    """Choose the relevance bound, or report that no value satisfies both constraints.

    ADR-0148 D4 sets two constraints. The bound must sit strictly above the median
    top-ranked non-match, so that median is rejected. It must admit at least
    ``min_positive_share`` of the whole labelled positive set.

    Only one candidate needs testing. Admitted share falls monotonically as the bound
    rises, so the *lowest* bound that rejects the median admits the most positives. If
    that candidate fails the positive constraint, every higher bound fails it too, and no
    value satisfies both. Choosing the lowest satisfying bound also matches the existing
    ``propose_floor`` tie-break, which favours recall in a recall-first system.

    Args:
        positives: Every labelled positive, per expected entity, in Neo4j score space.
        negatives: Each query's strongest non-match, in Neo4j score space.
        min_positive_share: The share of ``positives`` the bound must admit.
        step: How far above the median the lowest candidate bound sits.

    Returns:
        The proposal, which reports an incompatibility rather than a number when no
        bound satisfies both constraints.

    Raises:
        ValueError: If either population is empty. An empty population is a broken
            measurement, not a calibration result, and must never reach a bound.
    """
    if not positives:
        raise ValueError("cannot calibrate a bound with no labelled positives")
    if not negatives:
        raise ValueError("cannot calibrate a bound with no negatives")

    negative_median = percentile(negatives, 50)
    candidate = round(negative_median + step, 6)
    share = admitted_share(positives, candidate)
    if share < min_positive_share:
        return BoundProposal(
            bound=None,
            positive_admitted_share=None,
            negative_rejected_share=None,
            negative_median=negative_median,
            incompatible=True,
            reason=(
                f"the lowest bound that rejects the median top-ranked non-match "
                f"({candidate:.4f}, median {negative_median:.4f}) admits only "
                f"{share:.1%} of the {len(positives)} labelled positives, below the "
                f"required {min_positive_share:.0%}. Admitted share falls as the bound "
                f"rises, so no higher bound satisfies both constraints either. The "
                f"serving arm does not separate the two populations at the required "
                f"rate (ADR-0148 D4)."
            ),
        )
    return BoundProposal(
        bound=candidate,
        positive_admitted_share=share,
        negative_rejected_share=1.0 - admitted_share(negatives, candidate),
        negative_median=negative_median,
        incompatible=False,
        reason=None,
    )


def parity_aggregates(positives: Sequence[float], negatives: Sequence[float]) -> dict[str, float]:
    """The three statistics FRE-694's parity gate compares.

    Uses the same ``percentile`` helper FRE-694 used, so the two sides of the comparison
    aggregate identically and any difference is a real one.

    Args:
        positives: Positive scores, in Neo4j score space.
        negatives: Negative scores, in Neo4j score space.

    Returns:
        ``pos_median`` / ``neg_median`` / ``neg_max``.

    Raises:
        ValueError: If either population is empty.
    """
    if not positives or not negatives:
        raise ValueError("parity aggregates need a non-empty positive and negative population")
    return {
        "pos_median": percentile(positives, 50),
        "neg_median": percentile(negatives, 50),
        "neg_max": max(negatives),
    }


def aggregate_deltas(left: dict[str, float], right: dict[str, float]) -> dict[str, float]:
    """Per-statistic absolute difference between two aggregate sets (parity check P1).

    Args:
        left: One implementation's aggregates.
        right: The other implementation's aggregates.

    Returns:
        Absolute differences, keyed as the inputs are.

    Raises:
        ValueError: If the two sets do not name the same statistics. A silently
            intersected comparison would report parity over whichever keys happened to
            match.
    """
    if set(left) != set(right):
        raise ValueError(f"aggregate key mismatch: {sorted(left)} vs {sorted(right)}")
    return {key: abs(left[key] - right[key]) for key in left}


def pair_deltas(pairs: Sequence[tuple[float, float]]) -> list[float]:
    """Per-candidate absolute difference between paired scores (parity check P2).

    Recomputed from the recorded pairs rather than read back as stored deltas, so a
    re-run of the assertion is a real re-run.

    Args:
        pairs: ``(index_score, offline_score)`` for each scored candidate.

    Returns:
        The absolute differences, in the order given.

    Raises:
        ValueError: If ``pairs`` is empty -- an empty parity check passes vacuously and
            would report a validated instrument on no evidence.
    """
    if not pairs:
        raise ValueError("parity check P2 needs at least one recorded score pair")
    return [abs(index - offline) for index, offline in pairs]
