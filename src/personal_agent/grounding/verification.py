"""The inline checks every assertion span must pass (ADR-0138 D3, FRE-1282).

D3: every assertion span must carry a citation passing **all three** of resolution,
reachability and containment, and all three run "inline and blocking, on every turn, at
every enforcement level". This module runs them and reports; deciding what follows from a
failure — block, retry, refuse — is D4 and lives in
:mod:`personal_agent.grounding.enforcement`.

**There are four gates here, not three, and the fourth is D2's not a new one.** D3
presupposes that what is *in* the registry belongs there; D2 decides that. The gap between
those two sentences was found live rather than in review, on 2026-08-26 (session
``a1a496fa``): a date the agent hallucinated in an earlier session, written to the KG by
entity extraction and recalled as an admissible memory source, passes resolution (it is in
the registry), passes reachability (a memory node has no external referent), and passes
containment (**the source is the false claim**). Three greens on an eight-week error.

Verification confirms a claim was *copied from a source*. It never confirms the source was
*entitled to make it*. So :class:`~personal_agent.grounding.source_registry.Entitlement` is
checked first, and an agent-derived source fails with
:attr:`CheckOutcome.SOURCE_NOT_ENTITLED` — recorded distinctly, because "the system cited
itself" and "the source did not contain the claim" call for entirely different remedies.

**Reachability is decided from the record, never from a re-fetch.** ``fetch_url`` raises on
non-2xx after redirects, so a failed fetch registers no source at all: non-2xx is already
unreachable by construction, and a page fetched seconds ago in this same turn was
demonstrably reachable. Re-fetching would measure only whether it broke in the intervening
seconds, at the cost of an inline network round-trip per citation and a verdict that is not
deterministic. D2 already rules that verification "resolves against the recorded result,
never against a re-execution"; extending that to the fetched page is the consistent reading.
What a check *can* still add is the residual the fetch could not see — a soft-404 or an auth
wall served with HTTP 200 — and that is what :func:`check_reachability` looks for.

Which sources have an external referent at all is
:data:`~personal_agent.grounding.source_registry.REFERENT_ARGUMENTS`' decision, carried on
the source itself. Everything else passes **vacuously**, which is D2 stated literally: for
the user's words, memory nodes and turn-local tool evidence "the recorded result *is* the
durable artifact", and reachability is not-applicable rather than failed.
"""

from __future__ import annotations

import asyncio
import re
import time
from enum import StrEnum

import structlog
from pydantic import BaseModel, ConfigDict

from personal_agent.captains_log.turn_evidence import GroundedSpanRecord, GroundingRecord
from personal_agent.grounding.citations import CitationParse, strip_citation_markers
from personal_agent.grounding.containment import ContainmentOutcome, check_containment
from personal_agent.grounding.entailment import (
    EntailmentJudge,
    EntailmentJudgement,
    EntailmentVerdict,
)
from personal_agent.grounding.source_registry import Entitlement, RegisteredSource, SourceRegistry
from personal_agent.grounding.spans import Span, SpanExtraction
from personal_agent.telemetry.trace import TraceContext

log = structlog.get_logger(__name__)

SOFT_FAILURE_MAX_CHARS = 600
"""Longest extracted body a soft-404 or auth-wall pattern may condemn.

The length bound is what keeps this from manufacturing refusals: a long article that
happens to contain the words "page not found" somewhere in its body is an article, while a
200-response whose entire content is four hundred characters of "Sign in to continue" is a
wall. Without the bound the pattern alone would reject legitimate pages, which under D4
costs a refusal the user did not deserve.
"""

_SOFT_FAILURE_PATTERN = re.compile(
    r"\b(?:"
    r"page\s+not\s+found|not\s+found|404\s+error|no\s+longer\s+available|"
    r"sign\s+in\s+to\s+continue|please\s+log\s+in|log\s+in\s+to\s+continue|"
    r"subscribe\s+to\s+read|access\s+denied|enable\s+javascript"
    r")\b",
    re.IGNORECASE,
)


class Reachability(StrEnum):
    """What D3(b) decided about one source.

    ``NOT_APPLICABLE`` is a **pass**, not an abstention: D2 grants it to every source with
    no external referent, and D3's "all three" holds literally because of it.
    """

    NOT_APPLICABLE = "not_applicable"
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"


class CheckOutcome(StrEnum):
    """What verification decided about one assertion span.

    Every member names **which** gate fired, because AC-1 requires the reason to
    distinguish the checks and AC-6 requires two of these never to blur:
    ``UNVERIFIABLE_BY_CONTAINMENT`` is a limit of our normalizer, while ``UNCITED`` and
    ``UNRESOLVED`` are true no-source outcomes. A wave of the first is a malfunction; a
    wave of the second is the contract working.
    """

    PASSED = "passed"
    UNCITED = "uncited"
    UNRESOLVED = "unresolved"
    SOURCE_NOT_ENTITLED = "source_not_entitled"
    UNREACHABLE = "unreachable"
    NOT_CONTAINED = "not_contained"
    UNVERIFIABLE_BY_CONTAINMENT = "unverifiable_by_containment"
    ENTAILMENT_REQUIRED = "entailment_required"
    NOT_ENTAILED = "not_entailed"
    CONTRADICTED_BY_SOURCE = "contradicted_by_source"
    ENTAILMENT_UNAVAILABLE = "entailment_unavailable"


_TRUE_NO_SOURCE: frozenset[CheckOutcome] = frozenset(
    {CheckOutcome.UNCITED, CheckOutcome.UNRESOLVED, CheckOutcome.SOURCE_NOT_ENTITLED}
)
"""Outcomes meaning the turn genuinely had no admissible source for the span.

``SOURCE_NOT_ENTITLED`` belongs here rather than with the containment limits: the system
citing its own earlier utterance *is* having no source, however well the tokens matched.

**Not here: the rejection outcomes.** ``NOT_CONTAINED``, ``NOT_ENTAILED`` and
``CONTRADICTED_BY_SOURCE`` are a third thing — a source that exists, is entitled and is
reachable, caught not supporting the claim. That is the contract catching citation theatre,
not the turn having nothing to stand on, and folding it in here would quietly change what
``no_source_count`` means (FRE-1286 plan review).
"""

_MACHINE_UNDECIDED: frozenset[CheckOutcome] = frozenset(
    {
        CheckOutcome.UNVERIFIABLE_BY_CONTAINMENT,
        CheckOutcome.ENTAILMENT_REQUIRED,
        CheckOutcome.ENTAILMENT_UNAVAILABLE,
    }
)
"""Outcomes our own machinery could not settle.

A normalizer that could not decide, an escalation no judge ran on, a judge that timed out
or returned nothing. Named as a set beside :data:`_TRUE_NO_SOURCE` rather than left as a
literal in one property, so the two families stay symmetrical as members are added.
"""


class SpanVerification(BaseModel):
    """One assertion span's verdict.

    Attributes:
        text: The span's text, citation markers removed.
        start: Offset into the model output.
        end: End offset, exclusive.
        identifier: The citation identifier bound to it, or None when it carried none.
        outcome: Which gate decided it.
        reachability: What D3(b) found, for the record even when a later gate decided.
        missing: Required tokens absent from the source, when containment ran.
        entity_free_predicate: Whether containment placed this span in D3(d)'s escalated
            class. Carried past the entailment pass on purpose: once
            :func:`apply_entailment` turns a supported escalation into ``PASSED``, nothing
            else distinguishes it from a span that passed containment outright, and the
            offline arm would re-sample the class the inline arm already judged
            (FRE-1286 plan review).
        detail: One line naming the rule that fired, for the turn record and for a reader
            of a turn that refused.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    start: int
    end: int
    identifier: str | None
    outcome: CheckOutcome
    reachability: Reachability = Reachability.NOT_APPLICABLE
    missing: tuple[str, ...] = ()
    entity_free_predicate: bool = False
    detail: str = ""

    @property
    def passed(self) -> bool:
        """Whether this span may be delivered as it stands."""
        return self.outcome is CheckOutcome.PASSED


class TurnVerification(BaseModel):
    """What verification decided about one turn's output.

    Attributes:
        spans: One verdict per non-exempt span, in output order.
        degraded_extraction: Whether span extraction had to fail closed. Carried so a wave
            of refusals traceable to a malfunctioning extractor is not read as the model
            becoming honest.
        unavailable_reason: Set when verification could not run at all — an extractor
            failure, a denied budget reservation. Distinct from every span outcome,
            because it is a fact about Seshat's own machinery rather than about the claim.
        entailment_checks: Judge calls D3(d) made on this pass (FRE-1286).
        entailment_latency_ms: Wall-clock the inline entailment pass cost, or None when it
            did not run. The common turn escalates nothing and pays nothing.
        entailment_budget_exceeded: Whether that wall-clock exceeded the configured
            budget. Recorded rather than acted on: with a per-call timeout already
            bounding the worst case, aborting mid-flight would only convert a slow
            provider into a refusal the user did not deserve (AC-5).
    """

    model_config = ConfigDict(frozen=True)

    spans: tuple[SpanVerification, ...] = ()
    degraded_extraction: bool = False
    unavailable_reason: str | None = None
    entailment_checks: int = 0
    entailment_latency_ms: float | None = None
    entailment_budget_exceeded: bool = False

    @property
    def available(self) -> bool:
        """Whether verification ran at all."""
        return self.unavailable_reason is None

    @property
    def failures(self) -> tuple[SpanVerification, ...]:
        """Every span that may not be delivered as it stands."""
        return tuple(span for span in self.spans if not span.passed)

    @property
    def compliant(self) -> bool:
        """Whether every non-exempt span carried a citation passing every gate.

        FRE-1284's compliance numerator reads this. A turn where verification could not
        run is **not** compliant: an unmeasured turn must never count as a passing one.
        """
        return self.available and not self.failures

    @property
    def true_no_source(self) -> tuple[SpanVerification, ...]:
        """Failures where the turn genuinely had no admissible source (AC-6)."""
        return tuple(span for span in self.spans if span.outcome in _TRUE_NO_SOURCE)

    @property
    def unverifiable(self) -> tuple[SpanVerification, ...]:
        """Failures our own machinery could not decide (AC-6).

        Kept apart from :attr:`true_no_source` so a wave of false refusals can never read
        as honest not-knowing. FRE-1286 widened this from the normalizer alone to
        :data:`_MACHINE_UNDECIDED`: a judge that timed out is the same kind of fact about
        Seshat as a normalizer that could not settle a paraphrase.
        """
        return tuple(span for span in self.spans if span.outcome in _MACHINE_UNDECIDED)


def check_reachability(source: RegisteredSource) -> Reachability:
    """Run D3(b) against one source's recorded retrieval.

    Args:
        source: The cited source.

    Returns:
        ``NOT_APPLICABLE`` when the source has no external referent — a vacuous pass under
        D2. Otherwise ``UNREACHABLE`` when the recorded body is a short soft-404 or auth
        wall, and ``REACHABLE`` otherwise, since a non-2xx fetch never registered a source
        in the first place.
    """
    if source.referent is None:
        return Reachability.NOT_APPLICABLE
    body = source.content.strip()
    if len(body) <= SOFT_FAILURE_MAX_CHARS and _SOFT_FAILURE_PATTERN.search(body):
        return Reachability.UNREACHABLE
    return Reachability.REACHABLE


def _identifier_for(span: Span, parse: CitationParse) -> str | None:
    """Return the citation identifier bound to one span, if any.

    Binding is by overlap between the extractor's span and the region a marker binds.
    Both sets of offsets index the *same* model output, so this joins the two halves of
    the contract — what needed a citation, and what carried one — without either having
    to know about the other.

    Args:
        span: A non-exempt span.
        parse: The citation parse of the same output.

    Returns:
        The identifier, or None when no marker binds this region.
    """
    for cited in parse.spans:
        if cited.start < span.end and span.start < cited.end:
            return cited.identifier
    return None


def _verify_span(span: Span, parse: CitationParse, registry: SourceRegistry) -> SpanVerification:
    """Run every gate against one non-exempt span.

    Args:
        span: The span requiring a citation.
        parse: The citation parse of the output it came from.
        registry: This turn's registry.

    Returns:
        The verdict, naming the first gate that failed.
    """
    text = strip_citation_markers(span.text)
    identifier = _identifier_for(span, parse)

    if identifier is None:
        return SpanVerification(
            text=text,
            start=span.start,
            end=span.end,
            identifier=None,
            outcome=CheckOutcome.UNCITED,
            detail="the span carries no citation marker (ADR-0138 D1)",
        )

    source = registry.resolve(identifier)
    if source is None:
        return SpanVerification(
            text=text,
            start=span.start,
            end=span.end,
            identifier=identifier,
            outcome=CheckOutcome.UNRESOLVED,
            detail=f"{identifier} resolves to no source in this turn's registry (D3(a))",
        )

    if source.entitlement is Entitlement.AGENT_DERIVED:
        return SpanVerification(
            text=text,
            start=span.start,
            end=span.end,
            identifier=identifier,
            outcome=CheckOutcome.SOURCE_NOT_ENTITLED,
            detail=(
                f"{identifier} is an agent-derived {source.kind.value} source, so citing it "
                "grounds the claim in the system's own earlier assertion (D2)"
            ),
        )

    reachability = check_reachability(source)
    if reachability is Reachability.UNREACHABLE:
        return SpanVerification(
            text=text,
            start=span.start,
            end=span.end,
            identifier=identifier,
            outcome=CheckOutcome.UNREACHABLE,
            reachability=reachability,
            detail=f"{source.referent} returned a soft failure or an auth wall (D3(b))",
        )

    containment = check_containment(text, source.content)
    outcome = {
        ContainmentOutcome.CONTAINED: CheckOutcome.PASSED,
        ContainmentOutcome.NOT_CONTAINED: CheckOutcome.NOT_CONTAINED,
        ContainmentOutcome.UNVERIFIABLE: CheckOutcome.UNVERIFIABLE_BY_CONTAINMENT,
        ContainmentOutcome.ENTAILMENT_REQUIRED: CheckOutcome.ENTAILMENT_REQUIRED,
    }[containment.outcome]

    details = {
        CheckOutcome.PASSED: "",
        CheckOutcome.NOT_CONTAINED: (
            f"{identifier} does not contain {', '.join(containment.missing)} (D3(c))"
        ),
        CheckOutcome.UNVERIFIABLE_BY_CONTAINMENT: (
            f"{identifier} states the claim's entities and figures but not "
            f"{', '.join(containment.missing)}; the difference may be paraphrase (D3(c))"
        ),
        CheckOutcome.ENTAILMENT_REQUIRED: (
            "the span names no entity and states no figure, so containment cannot settle "
            "it and D3(d) must (FRE-1286)"
        ),
    }

    return SpanVerification(
        text=text,
        start=span.start,
        end=span.end,
        identifier=identifier,
        outcome=outcome,
        reachability=reachability,
        missing=containment.missing,
        entity_free_predicate=containment.entity_free_predicate,
        detail=details[outcome],
    )


def verify_turn(
    extraction: SpanExtraction,
    parse: CitationParse,
    registry: SourceRegistry,
) -> TurnVerification:
    """Run D3 over every non-exempt span in one turn's output.

    Args:
        extraction: What span extraction decided needed a citation (FRE-1281).
        parse: What the citation format expresses about the same output (FRE-1280).
        registry: This turn's source registry.

    Returns:
        One verdict per non-exempt span. Exempt spans and non-claims are absent by
        construction — D1 excuses them, and inventing a verdict for them would put the
        contract's coverage back in this module's hands instead of the extractor's.
    """
    return TurnVerification(
        spans=tuple(_verify_span(span, parse, registry) for span in extraction.non_exempt),
        degraded_extraction=extraction.degraded,
    )


_VERDICT_OUTCOMES: dict[EntailmentVerdict, CheckOutcome] = {
    EntailmentVerdict.SUPPORTED: CheckOutcome.PASSED,
    EntailmentVerdict.NOT_SUPPORTED: CheckOutcome.NOT_ENTAILED,
    EntailmentVerdict.CONTRADICTED: CheckOutcome.CONTRADICTED_BY_SOURCE,
    EntailmentVerdict.UNDECIDED: CheckOutcome.ENTAILMENT_UNAVAILABLE,
}


def _entailed_span(span: SpanVerification, judgement: EntailmentJudgement) -> SpanVerification:
    """Return one escalated span resolved by a verdict.

    Args:
        span: The span carrying ``ENTAILMENT_REQUIRED``.
        judgement: What the judge decided.

    Returns:
        The resolved verdict. The judge's own reason is carried into the detail: under D4
        it reaches the retry directive, and "the page only mentions mercury" tells the
        model something that "not entailed" does not.
    """
    outcome = _VERDICT_OUTCOMES[judgement.verdict]
    if outcome is CheckOutcome.PASSED:
        return span.model_copy(update={"outcome": outcome, "detail": ""})
    reason = judgement.reason or "the judge gave no reason"
    return span.model_copy(
        update={
            "outcome": outcome,
            "detail": f"{span.identifier} does not support the claim: {reason} (D3(d))",
        }
    )


async def apply_entailment(
    verification: TurnVerification,
    registry: SourceRegistry,
    judge: EntailmentJudge,
    *,
    max_checks: int,
    budget_ms: int,
    checks_already_used: int = 0,
    trace_ctx: TraceContext | None = None,
) -> TurnVerification:
    """Settle D3(d)'s escalated class inline (ADR-0138 D3(d), FRE-1286).

    Only spans carrying :attr:`CheckOutcome.ENTAILMENT_REQUIRED` are judged — the ones
    containment reported it cannot decide, because they name no entity and state no
    figure. Every other span is returned untouched, so a turn escalating nothing costs no
    model call at all.

    **Latency is bounded by construction, not by the measurement.** All escalated spans go
    out in one :func:`asyncio.gather`, so the added cost is one round-trip rather than one
    per assertion — the scaling that got ADR-0138's Option 5 rejected. The per-call timeout
    lives on the judge; this function only records what the pass cost.

    Args:
        verification: What the deterministic gates decided.
        registry: This turn's registry, for resolving each span's source.
        judge: The entailment judge.
        max_checks: Bound on judge calls, **cumulative across D4 attempts**. A per-pass
            bound bounds nothing when D4 may run the pass again.
        budget_ms: The latency budget the elapsed time is recorded against.
        checks_already_used: Judge calls earlier attempts on this turn already spent.
        trace_ctx: The turn's trace context, threaded into every judge call.

    Returns:
        The verification with each escalated span resolved. A span past the cap, and a
        span whose judge could not answer, becomes ``ENTAILMENT_UNAVAILABLE`` — which
        still blocks under D4, deliberately: before this ticket the same class blocked as
        ``ENTAILMENT_REQUIRED``, so fail-closed is the behaviour being preserved.
    """
    pending = [
        index
        for index, span in enumerate(verification.spans)
        if span.outcome is CheckOutcome.ENTAILMENT_REQUIRED
    ]
    if not pending:
        return verification

    allowance = max(0, max_checks - checks_already_used)
    judged, over_cap = pending[:allowance], pending[allowance:]

    started = time.perf_counter()
    results: list[EntailmentJudgement | BaseException] = []
    if judged:
        results = list(
            await asyncio.gather(
                *(
                    judge.judge(
                        verification.spans[index].text,
                        _source_content(registry, verification.spans[index]),
                        trace_ctx=trace_ctx,
                    )
                    for index in judged
                ),
                return_exceptions=True,
            )
        )
    elapsed_ms = (time.perf_counter() - started) * 1000

    spans = list(verification.spans)
    for index, result in zip(judged, results, strict=True):
        if isinstance(result, BaseException):
            # The judge is documented never to raise, so this is a defect in it rather
            # than a provider failure. It still must not cost the user the turn.
            log.warning(
                "entailment_judge_raised",
                error_type=type(result).__name__,
                trace_id=trace_ctx.trace_id if trace_ctx else None,
            )
            result = EntailmentJudgement(
                verdict=EntailmentVerdict.UNDECIDED,
                reason=f"the entailment judge raised {type(result).__name__}",
            )
        spans[index] = _entailed_span(spans[index], result)

    for index in over_cap:
        spans[index] = spans[index].model_copy(
            update={
                "outcome": CheckOutcome.ENTAILMENT_UNAVAILABLE,
                "detail": (
                    f"this turn's inline entailment budget of {max_checks} checks was "
                    "exhausted before this span (D3(d))"
                ),
            }
        )

    return verification.model_copy(
        update={
            "spans": tuple(spans),
            "entailment_checks": len(judged),
            "entailment_latency_ms": elapsed_ms,
            "entailment_budget_exceeded": elapsed_ms > budget_ms,
        }
    )


def _source_content(registry: SourceRegistry, span: SpanVerification) -> str:
    """Return the cited source's content for one escalated span.

    Args:
        registry: This turn's registry.
        span: The escalated span, whose identifier already resolved once.

    Returns:
        The content, or the empty string when the identifier no longer resolves — which
        the judge reads as an unintelligible passage and reports as undecided, the
        fail-closed direction.
    """
    source = registry.resolve(span.identifier) if span.identifier else None
    return source.content if source is not None else ""


def unavailable(reason: str) -> TurnVerification:
    """Return the verdict for a turn verification could not run on.

    Args:
        reason: What prevented it — a denied budget reservation, an extractor failure.

    Returns:
        A verification carrying no span verdicts and naming the reason. Recorded rather
        than silent: a budget denial is a fact about Seshat's accounting, not evidence
        about the model's claim, and a wave of these reads as the infrastructure
        malfunction it is rather than as a turn that verified cleanly.
    """
    return TurnVerification(unavailable_reason=reason)


__all__ = [
    "SOFT_FAILURE_MAX_CHARS",
    "CheckOutcome",
    "Reachability",
    "SpanVerification",
    "TurnVerification",
    "apply_entailment",
    "build_grounding_record",
    "check_reachability",
    "unavailable",
    "verify_turn",
]


def build_grounding_record(
    verification: TurnVerification,
    *,
    mode: str,
    attempts: int = 1,
    retrieval_forced: bool = False,
) -> GroundingRecord:
    """Render one turn's verification as the ADR-0125 output-side record (AC-6).

    Args:
        verification: What the inline checks decided.
        mode: The verification mode the turn ran under.
        attempts: Generation attempts D4 made.
        retrieval_forced: Whether retrieval was forced before this generation.

    Returns:
        The record. The two failure families are counted apart, which is the whole of
        AC-6: a normalizer limit and an honest no-source outcome must never be reachable
        from the same number.
    """
    return GroundingRecord(
        mode=mode,
        available=verification.available,
        unavailable_reason=verification.unavailable_reason,
        non_exempt_count=len(verification.spans),
        passed_count=sum(1 for span in verification.spans if span.passed),
        unverifiable_count=len(verification.unverifiable),
        no_source_count=len(verification.true_no_source),
        source_not_entitled_count=sum(
            1 for span in verification.spans if span.outcome is CheckOutcome.SOURCE_NOT_ENTITLED
        ),
        degraded_extraction=verification.degraded_extraction,
        entailment_checks=verification.entailment_checks,
        entailment_latency_ms=verification.entailment_latency_ms,
        entailment_budget_exceeded=verification.entailment_budget_exceeded,
        attempts=attempts,
        retrieval_forced=retrieval_forced,
        first_generation_compliant=verification.compliant and attempts == 1,
        spans=[
            GroundedSpanRecord(
                text=span.text,
                identifier=span.identifier,
                outcome=span.outcome.value,
                detail=span.detail,
            )
            for span in verification.spans
        ],
    )
