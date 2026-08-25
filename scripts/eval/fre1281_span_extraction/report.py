"""Score aggregation and bar evaluation (FRE-1281, ADR-0138 AC-7).

**The reporter enforces the held-out discipline, rather than the author promising it.**
:func:`render_markdown` refuses to emit per-document diffs or document text for the
held-out partition. ADR-0138's governance note is the reason — "An implementation cannot
special-case probes it has not seen" — and a promise not to look is not a mechanism.

Corpus figures are ratios of sums, not means of per-document ratios, so a one-span
document does not weigh as much as a ten-span one.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict
from scripts.eval.fre1281_span_extraction.bars import Bar, extractor_bars, unmet
from scripts.eval.fre1281_span_extraction.corpus import (
    EXEMPT_CLASSES,
    NON_EXEMPT_CLASSES,
    Partition,
    SpanClass,
)
from scripts.eval.fre1281_span_extraction.metrics import (
    DocumentScore,
    f1,
    ratio,
    sum_by_class,
)


class ScoreReport(BaseModel):
    """Corpus-level metrics, keyed to match the bars in :mod:`bars`.

    Attributes:
        partition: Which partition was scored, or ``None`` for the whole corpus.
        documents: How many documents contributed.
        metrics: Metric key to observed value; ``None`` where the denominator was empty.
        degraded_documents: Documents where the post-pass had to fail closed.
    """

    model_config = ConfigDict(frozen=True)

    partition: Partition | None
    documents: int
    metrics: dict[str, float | None]
    degraded_documents: int = 0

    def bar_results(self) -> tuple[tuple[Bar, float | None], ...]:
        """Pair every extractor bar with what this run measured.

        Returns:
            One entry per bar in :func:`~bars.extractor_bars`, in report order.
        """
        return tuple((bar, self.metrics.get(bar.key)) for bar in extractor_bars())

    def unmet_bars(self) -> tuple[Bar, ...]:
        """Bars this run failed or could not evaluate.

        Returns:
            The failing bars. An unmeasured bar counts as failing: ADR-0138 AC-7 fails
            when "any class is unreported", so silence is not an abstention.
        """
        return unmet(self.bar_results())

    @property
    def passed(self) -> bool:
        """Whether every extractor bar was met."""
        return not self.unmet_bars()


def aggregate(
    scores: Sequence[DocumentScore],
    *,
    partition: Partition | None = None,
    degraded_documents: int = 0,
) -> ScoreReport:
    """Roll per-document tallies into corpus metrics.

    Args:
        scores: Per-document scores.
        partition: Which partition these came from, for the report header.
        degraded_documents: Count of documents whose post-pass failed closed.

    Returns:
        A report whose ``metrics`` keys line up with the preregistered bars.
    """
    gold_non_exempt = sum_by_class(s.gold_non_exempt for s in scores)
    recalled = sum_by_class(s.recalled for s in scores)
    gold_exempt = sum_by_class(s.gold_exempt for s in scores)
    swept = sum_by_class(s.swept for s in scores)

    metrics: dict[str, float | None] = {
        "recall.overall": ratio(sum(recalled.values()), sum(gold_non_exempt.values())),
        "precision.overall": ratio(
            sum(s.precise for s in scores), sum(s.predicted_non_exempt for s in scores)
        ),
    }

    boundary_matched = sum(s.boundary_matched for s in scores)
    metrics["decomposition.boundary_f1"] = f1(
        ratio(boundary_matched, sum(s.predicted_claims for s in scores)),
        ratio(boundary_matched, sum(s.gold_claims for s in scores)),
    )

    for span_class in sorted(NON_EXEMPT_CLASSES, key=lambda c: c.value):
        metrics[f"recall.class.{span_class.value}"] = ratio(
            recalled.get(span_class, 0), gold_non_exempt.get(span_class, 0)
        )
    for span_class in sorted(EXEMPT_CLASSES, key=lambda c: c.value):
        metrics[f"fp_rate.class.{span_class.value}"] = ratio(
            swept.get(span_class, 0), gold_exempt.get(span_class, 0)
        )

    return ScoreReport(
        partition=partition,
        documents=len(scores),
        metrics=metrics,
        degraded_documents=degraded_documents,
    )


def _fmt(value: float | None) -> str:
    """Render a metric, making an unmeasured one visibly different from a zero."""
    return "unmeasured" if value is None else f"{value:.3f}"


def render_markdown(
    report: ScoreReport,
    *,
    scores: Sequence[DocumentScore] | None = None,
    run_id: str = "",
    extractor: str = "",
) -> str:
    """Render a report, withholding per-document detail for the held-out partition.

    Args:
        report: The aggregated metrics.
        scores: Per-document tallies. Rendered **only** for the dev partition — see the
            module docstring.
        run_id: Identifier stamped into the header.
        extractor: What produced the spans, stamped into the header.

    Returns:
        Markdown.
    """
    partition = "full corpus" if report.partition is None else report.partition.value
    lines = [
        f"# FRE-1281 span extraction — {run_id or 'unnamed run'}",
        "",
        f"- partition: **{partition}**",
        f"- documents: {report.documents}",
        f"- extractor: {extractor or 'unspecified'}",
        f"- degraded documents (post-pass failed closed): {report.degraded_documents}",
        "",
        "## Bars",
        "",
        "| bar | observed | required | met |",
        "| --- | --- | --- | --- |",
    ]
    for bar, observed in report.bar_results():
        held = bar.holds(observed)
        mark = {True: "yes", False: "NO", None: "unmeasured"}[held]
        comparator = ">=" if bar.direction.value == "at_least" else "<="
        lines.append(f"| `{bar.key}` | {_fmt(observed)} | {comparator} {bar.value} | {mark} |")

    failing = report.unmet_bars()
    lines += ["", f"**Verdict: {'PASS' if report.passed else 'FAIL'}**"]
    if failing:
        lines.append("")
        lines.append("Unmet bars, each with the failure it was preregistered to prevent:")
        lines.append("")
        for bar in failing:
            lines.append(f"- `{bar.key}` — {bar.justification}")

    if report.partition is Partition.HELDOUT:
        lines += [
            "",
            "> Per-document detail is withheld for the held-out partition by construction",
            "> (report.py). Aggregate figures only.",
        ]
    elif scores:
        lines += [
            "",
            "## Per-document",
            "",
            "| doc | gold claims | predicted | matched |",
            "| --- | --- | --- | --- |",
        ]
        for score in scores:
            lines.append(
                f"| {score.doc_id} | {score.gold_claims} | {score.predicted_claims} | "
                f"{score.boundary_matched} |"
            )
    return "\n".join(lines) + "\n"


def class_coverage_gaps(report: ScoreReport) -> tuple[str, ...]:
    """Metric keys that went unmeasured.

    ADR-0138 AC-7 fails if "any class is unreported", so this is surfaced rather than
    left for a reader to notice a blank cell.

    Args:
        report: The aggregated metrics.

    Returns:
        Keys whose value is ``None``.
    """
    return tuple(key for key, value in sorted(report.metrics.items()) if value is None)


__all__ = [
    "ScoreReport",
    "SpanClass",
    "aggregate",
    "class_coverage_gaps",
    "render_markdown",
]
