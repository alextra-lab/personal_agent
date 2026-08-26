"""Scoring the judge, against bars fixed before the run (FRE-1286 AC-3, AC-6).

**The bars are declared here, as numbers, and they are declared *first*.** A bar chosen
after seeing the score is not a bar; it is a description. ADR-0138 puts the same discipline
on D3(c)'s false-rejection rate ("fixing this unit is a decision, not a tuning parameter,
because AC-8's false-rejection measurement can only be taken once the matching rule is
settled"), and the judge is owed no less.

**Per-class detection rates, never one accuracy number.** AC-3 requires contradiction and
quantifier reversal to be detected, and a single accuracy figure lets a judge that is
systematically blind to one class hide behind the other three. AC-3's "*fails if* either
class passes at above its stated rate" is a per-class statement, so the metric is too.

**The false-rejection rate is the arm that stops the trivial judge.** Answering
``not_supported`` to everything scores 1.0 on all three negative classes; only the
``supported`` class fails it. In production it is also the expensive error: under D4 a
false rejection costs a refusal the user did not deserve.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from scripts.eval.fre1286_entailment.corpus import CaseClass, EntailmentCase

BARS: dict[str, float] = {
    "accuracy": 0.85,
    "detection_contradicted": 0.90,
    "detection_quantifier_reversal": 0.80,
    "detection_not_supported": 0.85,
    "false_rejection_rate": 0.10,
    "undecided_rate": 0.05,
}
"""Preregistered bars, fixed 2026-08-26 before the first scored run (FRE-1286).

Rates are floors except ``false_rejection_rate`` and ``undecided_rate``, which are
ceilings. Contradiction carries the highest floor because it is the residue ADR-0138 names
most concretely — a source stating the negation while containing every token of the claim —
and because a judge that misses it leaves D3(c) exactly where it already was. Quantifier
reversal sits lower on purpose: it is the harder linguistic call, and a bar set where it
cannot be met is a bar that gets quietly re-tuned.
"""

_CEILINGS = frozenset({"false_rejection_rate", "undecided_rate"})


@dataclass(frozen=True)
class ClassScore:
    """One class's detection rate.

    Attributes:
        total: Cases in the class.
        correct: Cases whose verdict matched the label.
        verdicts: What the judge answered, counted — so a systematic error is visible as a
            shape rather than only as a lower number.
    """

    total: int
    correct: int
    verdicts: dict[str, int] = field(default_factory=dict)

    @property
    def rate(self) -> float:
        """Fraction of the class the judge got right."""
        return self.correct / self.total if self.total else 0.0


@dataclass(frozen=True)
class Report:
    """What one scored run found.

    Attributes:
        by_class: Per-class scores.
        accuracy: Fraction of all cases whose verdict matched the label.
        false_rejection_rate: Fraction of ``supported`` cases the judge did not support.
        undecided_rate: Fraction of all cases the judge could not answer — a judge failure,
            kept out of the miss numbers so an outage never reads as a detection.
        total: Cases scored.
    """

    by_class: dict[CaseClass, ClassScore]
    accuracy: float
    false_rejection_rate: float
    undecided_rate: float
    total: int

    def bar_results(self) -> dict[str, tuple[float, float, bool]]:
        """Compare every measured figure against its preregistered bar.

        Returns:
            ``{name: (measured, bar, met)}``. Ceilings and floors are both here, compared
            in their own direction.
        """
        measured = {
            "accuracy": self.accuracy,
            "detection_contradicted": self._rate(CaseClass.CONTRADICTED),
            "detection_quantifier_reversal": self._rate(CaseClass.QUANTIFIER_REVERSAL),
            "detection_not_supported": self._rate(CaseClass.NOT_SUPPORTED),
            "false_rejection_rate": self.false_rejection_rate,
            "undecided_rate": self.undecided_rate,
        }
        return {
            name: (
                value,
                BARS[name],
                value <= BARS[name] if name in _CEILINGS else value >= BARS[name],
            )
            for name, value in measured.items()
        }

    def _rate(self, case_class: CaseClass) -> float:
        score = self.by_class.get(case_class)
        return score.rate if score is not None else 0.0


def score(cases: tuple[EntailmentCase, ...], verdicts: dict[str, str]) -> Report:
    """Score one run of the judge over the corpus.

    Args:
        cases: The corpus, or one partition of it.
        verdicts: ``{case id: verdict}``. A case with no entry counts as ``undecided`` —
            a call the harness could not complete is the judge failing to answer, and
            dropping it silently would flatter the accuracy of whatever did answer.

    Returns:
        The report.
    """
    by_class: dict[CaseClass, ClassScore] = {}
    correct_total = 0
    undecided_total = 0

    for case_class in CaseClass:
        members = [case for case in cases if case.case_class is case_class]
        if not members:
            continue
        counts: Counter[str] = Counter()
        correct = 0
        for case in members:
            verdict = verdicts.get(case.id, "undecided")
            counts[verdict] += 1
            if verdict == case.expected:
                correct += 1
        by_class[case_class] = ClassScore(
            total=len(members), correct=correct, verdicts=dict(counts)
        )
        correct_total += correct

    for case in cases:
        if verdicts.get(case.id, "undecided") == "undecided":
            undecided_total += 1

    supported = [case for case in cases if case.case_class is CaseClass.SUPPORTED]
    false_rejections = sum(
        1 for case in supported if verdicts.get(case.id, "undecided") != "supported"
    )

    total = len(cases)
    return Report(
        by_class=by_class,
        accuracy=correct_total / total if total else 0.0,
        false_rejection_rate=false_rejections / len(supported) if supported else 0.0,
        undecided_rate=undecided_total / total if total else 0.0,
        total=total,
    )


def render(report: Report) -> str:
    """Render a report as the lines that go in a handoff.

    Args:
        report: What :func:`score` produced.

    Returns:
        A plain-text block, bars included, so the numbers and what they had to beat are
        never separated.
    """
    lines = [f"cases scored: {report.total}", ""]
    for case_class, class_score in report.by_class.items():
        shape = ", ".join(f"{k}={v}" for k, v in sorted(class_score.verdicts.items()))
        lines.append(
            f"{case_class.value:<22} {class_score.correct}/{class_score.total} "
            f"= {class_score.rate:.3f}  ({shape})"
        )
    lines.append("")
    for name, (measured, bar, met) in report.bar_results().items():
        direction = "<=" if name in _CEILINGS else ">="
        lines.append(f"{'PASS' if met else 'FAIL'}  {name:<32} {measured:.3f} {direction} {bar}")
    return "\n".join(lines)


__all__ = ["BARS", "ClassScore", "Report", "render", "score"]
