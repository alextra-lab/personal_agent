"""Preregistered bars for span extraction (FRE-1281, ADR-0138 AC-7).

**These values are fixed before any extractor exists.** The commit introducing this file
precedes the first line of ``personal_agent.grounding`` and every scoring run; ``git
log`` is the artifact history the ticket's AC-5 asks for. A bar set after inspecting the
outcome measures nothing.

**Preregistration alone is not enough, which is the part that is easy to miss.** 0%
per-class recall, recorded in advance, satisfies the timing rule perfectly and means
nothing. ADR-0138 therefore binds a floor principle: every bar must be justified against
the failure it prevents, and *demonstrated to reject a deliberately broken baseline* — "a
bar that a known-broken implementation would pass is not a bar."

So each bar here carries :attr:`Bar.rejects_baselines`, and
``tests/evaluation/test_fre1281_bar_floor.py`` scores those baselines against the real
corpus and asserts each one actually fails the bars naming it. The claim is executable
rather than argued, which matters because a plan review found two of the five baselines
did not, in fact, bite the bars they were credited with.

The bars are **not** retuned after seeing results. A miss is reported as measured; the
ticket says the contract's strength *is* extraction recall, so a low number is a finding
about the extractor, not an invitation to move the line.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict
from scripts.eval.fre1281_span_extraction.corpus import (
    EXEMPT_CLASSES,
    NON_EXEMPT_CLASSES,
    SpanClass,
)

BARS_VERSION = 1
"""Bumped only by a deliberate, dated decision — never to accommodate a result."""


class BaselineName(StrEnum):
    """The deliberately broken extractors each bar must reject.

    ``ORACLE`` is the positive control rather than a baseline: it replays gold, and it
    must pass *every* bar. Without it, a set of bars could be strict by being
    unsatisfiable, which is a different way of measuring nothing.
    """

    NULL = "null"
    EXEMPT_ALL = "exempt_all"
    ACCEPT_ALL = "accept_all"
    ENTITY_TRIGGERED = "entity_triggered"
    FENCE_TRUSTING = "fence_trusting"
    ORACLE = "oracle"


class Direction(StrEnum):
    """Whether a metric must reach a floor or stay under a ceiling."""

    AT_LEAST = "at_least"
    AT_MOST = "at_most"


class Bar(BaseModel):
    """One preregistered threshold.

    Attributes:
        key: Metric identifier, matching the key ``report.py`` emits.
        value: The threshold.
        direction: Floor or ceiling.
        justification: The failure this bar prevents. Required — an unjustified bar is
            the vacuous kind ADR-0138's floor principle exists to catch.
        rejects_baselines: Broken baselines this bar must fail. Asserted by
            ``test_fre1281_bar_floor.py``, not merely asserted here.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    value: float
    direction: Direction
    justification: str
    rejects_baselines: tuple[BaselineName, ...]

    def holds(self, observed: float | None) -> bool | None:
        """Evaluate the bar against a measurement.

        Args:
            observed: The measured value, or ``None`` for a vacuous denominator.

        Returns:
            ``True``/``False``, or ``None`` when there was nothing to measure — never
            silently ``True``, which would let an empty class report as a pass.
        """
        if observed is None:
            return None
        if self.direction is Direction.AT_LEAST:
            return observed >= self.value
        return observed <= self.value


OVERALL_RECALL = Bar(
    key="recall.overall",
    value=0.90,
    direction=Direction.AT_LEAST,
    justification=(
        "A claim the extractor misses is a claim the contract never sees (ADR-0138 D1). "
        "The contract can be no stronger than this number, so it is the headline."
    ),
    rejects_baselines=(BaselineName.NULL, BaselineName.EXEMPT_ALL),
)

OVERALL_PRECISION = Bar(
    key="precision.overall",
    value=0.80,
    direction=Direction.AT_LEAST,
    justification=(
        "False positives block legitimate generation, which is D1's usability bound and "
        "ADR-0138 AC-5's explicit failure condition ('fails if code generation is "
        "blocked pending citations'). Refusing everything must not score well."
    ),
    rejects_baselines=(BaselineName.ACCEPT_ALL,),
)

DECOMPOSITION_BOUNDARY_F1 = Bar(
    key="decomposition.boundary_f1",
    value=0.75,
    direction=Direction.AT_LEAST,
    justification=(
        "AC-1 is atomicity, and a single memorable example ('Paris is France's capital "
        "and has 2.1 million residents' yielding two spans) can be special-cased. "
        "Measuring boundary agreement across the whole corpus cannot."
    ),
    rejects_baselines=(BaselineName.NULL,),
)

INTER_LABELLER_KAPPA = Bar(
    key="corpus.cohens_kappa",
    value=0.70,
    direction=Direction.AT_LEAST,
    justification=(
        "A condition on the CORPUS, not the extractor: below this the guidelines are too "
        "ambiguous for the corpus to measure the distinction it claims to, and "
        "ADJUDICATION.md is revised before anything is scored. 'Kappa recorded' is not a "
        "bar — it is satisfied by any number at all."
    ),
    rejects_baselines=(),
)

#: Recall floor applied to each non-exempt class independently.
#:
#: ADR-0138 AC-7 fails when "any single class is below bar" even if the overall figure
#: clears it — reporting per class is explicitly not sufficient. The class that makes
#: this bite is ``factual_bare_predicate``: an entity-triggered extractor posts a
#: respectable overall number while scoring zero there, which is precisely the draft of
#: D1 that review rejected.
PER_CLASS_RECALL_VALUE = 0.85

#: False-positive ceiling applied to each exempt class independently.
#:
#: ADR-0138 AC-3 fails if "code bodies, attributed restatement or arithmetic over cited
#: inputs are classified non-exempt above the false-positive bar". An overall precision
#: figure hides a class that is entirely swept in.
PER_EXEMPT_CLASS_FP_VALUE = 0.15


def _per_class_recall_bar(span_class: SpanClass) -> Bar:
    """Build the recall bar for one non-exempt class."""
    rejects: list[BaselineName] = [BaselineName.NULL, BaselineName.EXEMPT_ALL]
    if span_class is SpanClass.FACTUAL_BARE_PREDICATE:
        rejects.append(BaselineName.ENTITY_TRIGGERED)
    if span_class in {SpanClass.PROSE_IN_FENCE, SpanClass.NL_IN_CODE}:
        rejects.append(BaselineName.FENCE_TRUSTING)
    return Bar(
        key=f"recall.class.{span_class.value}",
        value=PER_CLASS_RECALL_VALUE,
        direction=Direction.AT_LEAST,
        justification=(
            f"A class-shaped hole is invisible in an overall figure. {span_class.value} "
            f"must clear the bar on its own (ADR-0138 AC-7)."
        ),
        rejects_baselines=tuple(rejects),
    )


def _per_exempt_class_fp_bar(span_class: SpanClass) -> Bar:
    """Build the false-positive bar for one exempt class."""
    return Bar(
        key=f"fp_rate.class.{span_class.value}",
        value=PER_EXEMPT_CLASS_FP_VALUE,
        direction=Direction.AT_MOST,
        justification=(
            f"Sweeping {span_class.value} into the contract manufactures refusals the "
            f"user did not deserve (ADR-0138 AC-3, D7)."
        ),
        rejects_baselines=(BaselineName.ACCEPT_ALL,),
    )


def all_bars() -> tuple[Bar, ...]:
    """Every preregistered bar, in report order.

    Returns:
        Overall recall and precision, decomposition, corpus kappa, then the eight
        per-class recall bars and the five per-exempt-class false-positive bars.
    """
    per_class = [
        _per_class_recall_bar(c) for c in sorted(NON_EXEMPT_CLASSES, key=lambda c: c.value)
    ]
    per_exempt = [
        _per_exempt_class_fp_bar(c) for c in sorted(EXEMPT_CLASSES, key=lambda c: c.value)
    ]
    return (
        OVERALL_RECALL,
        OVERALL_PRECISION,
        DECOMPOSITION_BOUNDARY_F1,
        INTER_LABELLER_KAPPA,
        *per_class,
        *per_exempt,
    )


def bars_naming(baseline: BaselineName) -> tuple[Bar, ...]:
    """Bars that claim to reject a given baseline.

    Args:
        baseline: The broken baseline.

    Returns:
        Every bar listing it in ``rejects_baselines``.
    """
    return tuple(bar for bar in all_bars() if baseline in bar.rejects_baselines)


def extractor_bars() -> tuple[Bar, ...]:
    """Bars measuring the extractor, excluding the corpus-admissibility one.

    Returns:
        :func:`all_bars` minus :data:`INTER_LABELLER_KAPPA`, which is a property of the
        corpus and is not produced by a scoring run.
    """
    return tuple(bar for bar in all_bars() if bar is not INTER_LABELLER_KAPPA)


def unmet(results: Sequence[tuple[Bar, float | None]]) -> tuple[Bar, ...]:
    """Bars that a scoring run did not meet.

    A ``None`` measurement counts as unmet: ADR-0138 AC-7 fails when "any class is
    unreported", so an absent number is a failure rather than an abstention.

    Args:
        results: Pairs of bar and observed value.

    Returns:
        The bars that failed or could not be evaluated.
    """
    return tuple(bar for bar, observed in results if bar.holds(observed) is not True)
