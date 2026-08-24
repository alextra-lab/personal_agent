"""Pure scoring for span extraction (FRE-1281, ADR-0138 AC-7). No I/O, no LLM.

Every definition here is fixed in the preregistration commit rather than settled once
numbers are in view. A plan review made the reason concrete: recall, precision and the
false-positive rate all have several defensible denominators, and choosing among them
after seeing results is post-hoc tuning wearing a methodology's clothes.

**Matching.** A predicted span matches a gold span at ``IoU >= 0.5`` over character
ranges. Matching is one-to-one and greedy by descending IoU, so one sprawling prediction
cannot claim credit for three gold spans.

**Recall** (overall, and per non-exempt class) — gold ``CLAIM_NON_EXEMPT`` spans matched
by a predicted ``CLAIM_NON_EXEMPT`` span, over all gold non-exempt spans in scope. This
is the number the contract's strength is bounded by.

**Precision** — predicted non-exempt spans that match some gold non-exempt span, over all
predicted non-exempt spans. False positives block legitimate generation.

**False-positive rate** (per exempt class) — gold exempt spans of that class at least
half covered by predicted non-exempt text, over all gold exempt spans of that class.
Coverage rather than IoU, deliberately: half a code body swept into the contract is swept
in whether or not a single prediction lines up with its boundaries, and an IoU rule would
let three small predictions carve up a long code block and score zero.

**Decomposition boundary F1** — segmentation quality, label-blind: predicted claim spans
against gold claim spans at the same IoU threshold. This is AC-1's evidence, measured over
the whole corpus rather than on one memorable sentence, because a single named example can
be special-cased and a corpus-wide boundary score cannot.

A vacuous denominator returns ``None``, never ``1.0``. An empty class that scored itself
perfect would be indistinguishable from a class that was actually handled.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict
from scripts.eval.fre1281_span_extraction.corpus import (
    EXEMPT_CLASSES,
    NON_EXEMPT_CLASSES,
    GoldDocument,
    GoldSpan,
    SpanClass,
)
from scripts.eval.fre1281_span_extraction.corpus import SpanLabel as GoldLabel

from personal_agent.grounding.spans import Span, SpanLabel

MATCH_IOU_THRESHOLD = 0.5
"""Character-range IoU at which a prediction is credited with a gold span."""

SWEEP_COVERAGE_THRESHOLD = 0.5
"""Fraction of a gold exempt span that must be covered to count as swept in."""


def _iou(a_start: int, a_end: int, b_start: int, b_end: int) -> float:
    """Intersection over union of two half-open character ranges."""
    intersection = max(0, min(a_end, b_end) - max(a_start, b_start))
    if intersection == 0:
        return 0.0
    union = max(a_end, b_end) - min(a_start, b_start)
    return intersection / union


def match_spans(gold: Sequence[GoldSpan], predicted: Sequence[Span]) -> dict[int, int]:
    """Greedily pair gold spans with predicted spans, one to one.

    Args:
        gold: Gold spans, anchored.
        predicted: Predicted spans.

    Returns:
        Map from index in ``gold`` to index in ``predicted``. Pairs below
        :data:`MATCH_IOU_THRESHOLD` are never formed.
    """
    candidates: list[tuple[float, int, int]] = []
    for gold_index, gold_span in enumerate(gold):
        for pred_index, pred_span in enumerate(predicted):
            score = _iou(gold_span.start, gold_span.end, pred_span.start, pred_span.end)
            if score >= MATCH_IOU_THRESHOLD:
                candidates.append((score, gold_index, pred_index))

    # Descending IoU, then by index so the pairing is deterministic under ties — an
    # unstable matcher would make the same run score differently on a rerun.
    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))

    matched: dict[int, int] = {}
    used_predictions: set[int] = set()
    for _, gold_index, pred_index in candidates:
        if gold_index in matched or pred_index in used_predictions:
            continue
        matched[gold_index] = pred_index
        used_predictions.add(pred_index)
    return matched


def _covered_fraction(span: GoldSpan, covering: Iterable[Span]) -> float:
    """Fraction of a gold span's characters covered by any of ``covering``.

    Args:
        span: The gold span.
        covering: Predicted spans, which may overlap each other.

    Returns:
        Covered characters over span length, in ``[0, 1]``.
    """
    length = span.end - span.start
    if length <= 0:
        return 0.0
    covered = set()
    for other in covering:
        start = max(span.start, other.start)
        end = min(span.end, other.end)
        if end > start:
            covered.update(range(start, end))
    return len(covered) / length


class DocumentScore(BaseModel):
    """Per-document tallies, summed into corpus totals by :mod:`report`.

    Kept as counts rather than ratios so the corpus figure is a ratio of sums, not a mean
    of per-document ratios — the latter would weight a one-span document as heavily as a
    ten-span one.

    Attributes:
        doc_id: Which document.
        gold_non_exempt: Gold non-exempt spans, by class.
        recalled: Of those, the ones a predicted non-exempt span matched.
        predicted_non_exempt: Count of predicted non-exempt spans.
        precise: Of those, the ones matching a gold non-exempt span.
        gold_exempt: Gold exempt spans, by class.
        swept: Of those, the ones at least half covered by predicted non-exempt text.
        gold_claims: Gold claim spans (exempt or not).
        predicted_claims: Predicted claim spans.
        boundary_matched: Claim spans paired label-blind at the IoU threshold.
    """

    model_config = ConfigDict(frozen=True)

    doc_id: str
    gold_non_exempt: dict[SpanClass, int]
    recalled: dict[SpanClass, int]
    predicted_non_exempt: int
    precise: int
    gold_exempt: dict[SpanClass, int]
    swept: dict[SpanClass, int]
    gold_claims: int
    predicted_claims: int
    boundary_matched: int


def score_document(document: GoldDocument, predicted: Sequence[Span]) -> DocumentScore:
    """Score one document's predictions against its gold labelling.

    Args:
        document: The labelled document.
        predicted: The extractor's spans for ``document.text``.

    Returns:
        Tallies for this document.
    """
    gold_claims = list(document.claim_spans)
    gold_non_exempt = [s for s in gold_claims if s.label is GoldLabel.CLAIM_NON_EXEMPT]
    gold_exempt = [s for s in gold_claims if s.label is GoldLabel.CLAIM_EXEMPT]

    pred_non_exempt = [s for s in predicted if s.label is SpanLabel.CLAIM_NON_EXEMPT]
    pred_claims = [s for s in predicted if s.is_claim]

    recall_pairs = match_spans(gold_non_exempt, pred_non_exempt)
    recalled: dict[SpanClass, int] = {}
    non_exempt_totals: dict[SpanClass, int] = {}
    for index, gold_span in enumerate(gold_non_exempt):
        assert gold_span.span_class is not None  # guaranteed by the loader
        non_exempt_totals[gold_span.span_class] = non_exempt_totals.get(gold_span.span_class, 0) + 1
        if index in recall_pairs:
            recalled[gold_span.span_class] = recalled.get(gold_span.span_class, 0) + 1

    # Precision reuses the same one-to-one pairing: a prediction is precise exactly when
    # it is some gold non-exempt span's match. Scoring it independently would let one
    # prediction be "correct" for several gold spans at once.
    precise = len(recall_pairs)

    swept: dict[SpanClass, int] = {}
    exempt_totals: dict[SpanClass, int] = {}
    for gold_span in gold_exempt:
        assert gold_span.span_class is not None
        exempt_totals[gold_span.span_class] = exempt_totals.get(gold_span.span_class, 0) + 1
        if _covered_fraction(gold_span, pred_non_exempt) >= SWEEP_COVERAGE_THRESHOLD:
            swept[gold_span.span_class] = swept.get(gold_span.span_class, 0) + 1

    boundary_pairs = match_spans(gold_claims, pred_claims)

    return DocumentScore(
        doc_id=document.doc_id,
        gold_non_exempt=non_exempt_totals,
        recalled=recalled,
        predicted_non_exempt=len(pred_non_exempt),
        precise=precise,
        gold_exempt=exempt_totals,
        swept=swept,
        gold_claims=len(gold_claims),
        predicted_claims=len(pred_claims),
        boundary_matched=len(boundary_pairs),
    )


def ratio(numerator: int, denominator: int) -> float | None:
    """Divide, returning ``None`` rather than a misleading value on an empty denominator.

    Args:
        numerator: Count of successes.
        denominator: Count of opportunities.

    Returns:
        The ratio, or ``None`` when there was nothing to measure.
    """
    if denominator == 0:
        return None
    return numerator / denominator


def f1(precision: float | None, recall: float | None) -> float | None:
    """Harmonic mean, propagating ``None`` and handling the zero-sum case.

    Args:
        precision: Precision, or ``None``.
        recall: Recall, or ``None``.

    Returns:
        F1, or ``None`` if either input is unmeasured.
    """
    if precision is None or recall is None:
        return None
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def sum_by_class(
    scores: Iterable[dict[SpanClass, int]],
) -> dict[SpanClass, int]:
    """Sum per-class tallies across documents.

    Args:
        scores: Per-document class tallies.

    Returns:
        One entry per class seen.
    """
    total: dict[SpanClass, int] = {}
    for entry in scores:
        for span_class, count in entry.items():
            total[span_class] = total.get(span_class, 0) + count
    return total


__all__ = [
    "EXEMPT_CLASSES",
    "MATCH_IOU_THRESHOLD",
    "NON_EXEMPT_CLASSES",
    "SWEEP_COVERAGE_THRESHOLD",
    "DocumentScore",
    "f1",
    "match_spans",
    "ratio",
    "score_document",
    "sum_by_class",
]
