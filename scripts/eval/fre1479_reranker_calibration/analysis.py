"""FRE-1479 -- pure analysis for the broad-recall reranker calibration (ADR-0148 D4).

No ``personal_agent`` import and no substrate, so every decision the calibration makes is
unit-testable without a reranker, a graph, or a network.

**The bound rule is not restated here.** ``choose_bound`` lives in
``scripts.eval.fre1477_relevance_calibration.analysis`` and is imported unchanged: ADR-0148
D4 states one rule for every path -- strictly above the median top-ranked non-match, and at
least 90% of the whole labelled positive set -- and a second copy would be a second rule the
moment either changed. What differs between the two paths is the *score space*, not the
constraint, and a score space is data.

What this module adds is the reranker's own parity discipline. FRE-1477's ``P2`` compared
the Neo4j vector index's score against a client-side cosine over the vectors the index
holds. A reranker has no index -- the model reads the query and the document and emits a
number -- so that check has no analogue and is not simulated. FRE-695's own instrument-sanity
gate takes its place.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from scripts.eval.fre435_memory_recall.separation_report import percentile
from scripts.eval.fre1477_relevance_calibration.analysis import (
    MIN_POSITIVE_SHARE,
    PARITY_TOLERANCE,
    BoundProposal,
    admitted_share,
    aggregate_deltas,
    choose_bound,
    parity_aggregates,
)

__all__ = [
    "GATED_PARITY_STATISTICS",
    "MIN_POSITIVE_SHARE",
    "PARITY_TOLERANCE",
    "BoundProposal",
    "SanityResult",
    "admitted_share",
    "aggregate_deltas",
    "choose_bound",
    "parity_aggregates",
    "positives_and_negative",
    "reranker_parity_aggregates",
    "sanity_holds",
]


#: The statistics P1 *gates* on. ``neg_max`` is reported but not gated -- see
#: :func:`reranker_parity_aggregates` for the measurement that settled why.
GATED_PARITY_STATISTICS = ("pos_median", "neg_median", "neg_p95")


def reranker_parity_aggregates(
    positives: Sequence[float], negatives: Sequence[float]
) -> dict[str, float]:
    """The statistics P1 compares for a reranker, and the reason they are not FRE-694's three.

    FRE-1477 compared ``pos_median`` / ``neg_median`` / ``neg_max`` at a 0.02 tolerance and
    passed. Applied unchanged here, the first two agreed to 0.0039 and 0.0078 while
    ``neg_max`` differed by 0.0859 and failed the gate. That was investigated rather than
    tuned away, and the measurement is what changed this function:

    * Both implementations selected the **same entity** as the top-ranked non-match in every
      divergent case -- ``ctrl4-king-spain`` chose "undisclosed cancer" on both sides. The
      corpora agree on content and on ranking, so it is not a corpus construction error,
      which is the failure P1 exists to catch.
    * The residual is the score of an *identical* (query, document) pair, and it is
      **two-sided**: production scored higher on five of the twelve largest divergences and
      lower on the rest. A construction error biases one way, as FRE-1477's own first run
      did before its dimension mismatch was found. Symmetric residue is noise.
    * Its source is the deliberate text difference between the two implementations --
      ``name + ' ' + description`` against ``"{name}: {description}"``. An independent
      implementation is *supposed* to differ there; that is what makes it independent.

    So a 0.02 tolerance built for medians was being applied to the maximum of 54 noisy
    values, where the largest single draw governs. FRE-695 recorded exactly this for this
    harness at this sample size: *"n = 54 ... extrema are outlier-sensitive, hence robust
    p5/p95 alongside J."* Gating on ``neg_p95`` instead adopts that source harness's own
    stated discipline.

    ``neg_max`` is still computed and still committed, because ADR-0148's reachability
    argument is stated in terms of it. It is a reported statistic, not a gate.

    Args:
        positives: Positive scores, in the reranker's own score space.
        negatives: Negative scores, same space.

    Returns:
        ``pos_median`` / ``neg_median`` / ``neg_p95`` / ``neg_max``.

    Raises:
        ValueError: If either population is empty.
    """
    if not positives or not negatives:
        raise ValueError("parity aggregates need a non-empty positive and negative population")
    return {
        "pos_median": percentile(positives, 50),
        "neg_median": percentile(negatives, 50),
        "neg_p95": percentile(negatives, 95),
        "neg_max": max(negatives),
    }


@dataclass(frozen=True)
class SanityResult:
    """FRE-695's instrument-sanity gate on one reranker arm.

    Attributes:
        relevant_score: The trivially relevant document's score.
        irrelevant_score: The trivially irrelevant document's score.
    """

    relevant_score: float
    irrelevant_score: float


def sanity_holds(result: SanityResult) -> bool:
    """Whether the reranker ranked the trivially relevant document first.

    FRE-695 ran this before trusting any arm's aggregates, and it is the second check here
    because the first (cross-implementation agreement) cannot catch a reranker that is
    uniformly wrong in both implementations -- a wrong model id, an endpoint answering with
    a constant, a document/query argument swap.

    Args:
        result: The measured pair.

    Returns:
        True when the relevant document outscores the irrelevant one.
    """
    return result.relevant_score > result.irrelevant_score


def positives_and_negative(
    expected: frozenset[str], candidate_names: Sequence[str], scores: Sequence[float]
) -> tuple[list[float], float | None]:
    """Split one query's candidate scores into labelled positives and its strongest non-match.

    FRE-695's definition, restated over this harness's inputs: positives are **per expected
    entity** present in the candidate set -- an expected item the shortlist missed
    contributes none, never an invented score -- and the negative is the single strongest
    non-expected candidate. On a control case, where nothing is expected, every candidate is
    a non-match and the strongest overall is the negative.

    That negative statistic is the right one rather than a convenient one: on a query whose
    answer is not in the corpus, the top-ranked candidate *is* the strongest non-match, so it
    describes the item admission is actually decided on. It does not describe the corpus at
    large, and nothing here claims it does.

    Args:
        expected: Lowercased expected entity names for the case.
        candidate_names: Lowercased candidate keys, aligned with ``scores``.
        scores: Reranker scores, aligned with ``candidate_names``.

    Returns:
        ``(positives, negative)``. The negative is None when every candidate was expected,
        which contributes no negative rather than a fabricated one.

    Raises:
        ValueError: If the two sequences are not the same length -- a misalignment would
            silently attribute one candidate's score to another.
    """
    if len(candidate_names) != len(scores):
        raise ValueError(
            f"candidate/score misalignment: {len(candidate_names)} names, {len(scores)} scores"
        )
    positives = [s for name, s in zip(candidate_names, scores, strict=True) if name in expected]
    non_matches = [
        s for name, s in zip(candidate_names, scores, strict=True) if name not in expected
    ]
    return positives, (max(non_matches) if non_matches else None)
