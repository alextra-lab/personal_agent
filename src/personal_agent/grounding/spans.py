"""Span domain types for the grounding contract (ADR-0138 D1, FRE-1281).

ADR-0138 D1 inverts the obvious rule: rather than enumerating what must be cited, which
is unbounded, it enumerates what need not be, which is finite. Everything here follows
from that inversion.

**Why there are three labels and not two.** ``NOT_A_CLAIM`` looks redundant next to
"exempt" — both mean "no citation needed" — but they are different statements and the
difference is the point. ``CLAIM_EXEMPT`` says *this asserts something, and D1 names a
region that excuses it*. ``NOT_A_CLAIM`` says *this asserts nothing*. Collapsing them
would lose the ability to tell an extractor that examined a sentence and judged it inert
from one that never looked, and that distinction is what the coverage contract in
:mod:`personal_agent.grounding.span_policy` rests on: an extractor which simply omits
text produces no segment at all, and a claim would leave the contract with no record.

**Why the enum of exempt regions is closed.** Each member is a region named in D1's table.
A new member is a change to the contract, and adding one without amending the ADR would
silently widen what may go uncited — which is the failure mode the whole document exists
to prevent.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator
from typing_extensions import Self


class SpanLabel(StrEnum):
    """What the extractor decided about one segment of output.

    See the module docstring for why ``NOT_A_CLAIM`` is not folded into
    ``CLAIM_EXEMPT``.
    """

    CLAIM_EXEMPT = "claim_exempt"
    CLAIM_NON_EXEMPT = "claim_non_exempt"
    NOT_A_CLAIM = "not_a_claim"


class ExemptRegion(StrEnum):
    """The finite list of regions D1 excuses from citation.

    ``AMBIGUOUS`` is not a region — it is the classifier's way of declining to decide,
    and :mod:`personal_agent.grounding.span_policy` converts it to
    :attr:`SpanLabel.CLAIM_NON_EXEMPT`. D1: "Ambiguous classification resolves to
    assertion." It is a member here so the classifier has a way to say it, rather than
    guessing and being believed.
    """

    CODE = "code"
    DERIVED_ARITHMETIC = "derived_arithmetic"
    ATTRIBUTED_RESTATEMENT = "attributed_restatement"
    CONNECTIVE_EVALUATIVE = "connective_evaluative"
    SYSTEM_RECORD = "system_record"
    AMBIGUOUS = "ambiguous"


class NonExemptReason(StrEnum):
    """Why a span carries a citation obligation.

    Recorded because the remedies differ and because ADR-0138 requires failure modes to
    stay distinguishable in telemetry rather than blurring into one another. The three
    ``*_PIN`` and ``COVERAGE_GAP`` members are produced by the deterministic post-pass
    rather than by the classifier, so a reader can always tell which layer decided.
    """

    CLASSIFIED = "classified"
    AMBIGUITY_PIN = "ambiguity_pin"
    DEPENDENCY_PIN = "dependency_pin"
    CHECKABLE_PREDICATE_PIN = "checkable_predicate_pin"
    OVERLAP_PRECEDENCE = "overlap_precedence"
    COVERAGE_GAP = "coverage_gap"
    UNANCHORABLE = "unanchorable"


class Span(BaseModel):
    """One atomic segment of model output, with its grounding decision.

    Attributes:
        start: Character offset into the output being classified.
        end: End offset, exclusive, so ``output[start:end]`` is the span's text.
        text: The span's text, carried alongside the offsets for readability.
        label: The grounding decision.
        region: Which exempt region excuses it. Set only for ``CLAIM_EXEMPT``.
        reason: Why it is non-exempt. Set only for ``CLAIM_NON_EXEMPT``.
    """

    model_config = ConfigDict(frozen=True)

    start: int
    end: int
    text: str
    label: SpanLabel
    region: ExemptRegion | None = None
    reason: NonExemptReason | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        """Reject internally contradictory spans.

        Returns:
            The validated span.

        Raises:
            ValueError: If offsets are inverted, or a label carries the wrong annotation.
        """
        if self.end <= self.start:
            raise ValueError(f"span end {self.end} must exceed start {self.start}")
        if self.label is SpanLabel.CLAIM_EXEMPT and self.region is None:
            raise ValueError(f"exempt span {self.text!r} names no region")
        if self.label is not SpanLabel.CLAIM_EXEMPT and self.region is not None:
            raise ValueError(f"span {self.text!r} is {self.label.value} but names a region")
        if self.label is SpanLabel.CLAIM_NON_EXEMPT and self.reason is None:
            raise ValueError(f"non-exempt span {self.text!r} names no reason")
        if self.label is not SpanLabel.CLAIM_NON_EXEMPT and self.reason is not None:
            raise ValueError(f"span {self.text!r} is {self.label.value} but names a reason")
        return self

    @property
    def is_claim(self) -> bool:
        """Whether this span asserts anything at all."""
        return self.label is not SpanLabel.NOT_A_CLAIM

    def overlaps(self, other: Span) -> bool:
        """Whether two spans share at least one character.

        Args:
            other: The span to compare against.

        Returns:
            ``True`` if the half-open ranges intersect.
        """
        return self.start < other.end and other.start < self.end


class SpanExtraction(BaseModel):
    """The result of classifying one model output.

    Attributes:
        output: The text that was classified.
        spans: Non-overlapping spans in document order, tiling ``output``.
        degraded: Set when the deterministic post-pass had to fail closed — a coverage
            gap, an unanchorable quote, a malformed classifier reply. The extraction is
            still usable and still fails safe; the flag exists so a wave of degradations
            is visible as a malfunction rather than read as the model becoming cautious.
    """

    model_config = ConfigDict(frozen=True)

    output: str
    spans: tuple[Span, ...]
    degraded: bool = False

    @property
    def non_exempt(self) -> tuple[Span, ...]:
        """Spans requiring a citation — what D3 will verify."""
        return tuple(s for s in self.spans if s.label is SpanLabel.CLAIM_NON_EXEMPT)

    @property
    def claims(self) -> tuple[Span, ...]:
        """Spans that assert something, exempt or not."""
        return tuple(s for s in self.spans if s.is_claim)


def assert_non_overlapping(spans: Sequence[Span]) -> None:
    """Assert D1's non-overlap invariant over a span sequence.

    Args:
        spans: Spans in any order.

    Raises:
        ValueError: On the first overlapping pair, naming both spans.
    """
    ordered = sorted(spans, key=lambda s: (s.start, s.end))
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if later.start < earlier.end:
            raise ValueError(
                f"spans overlap: {earlier.text!r} [{earlier.start}:{earlier.end}] and "
                f"{later.text!r} [{later.start}:{later.end}]"
            )
