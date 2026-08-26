"""D3(d) inline — the entity-free predicate class, decided (ADR-0138, FRE-1286).

AC-1, AC-2, AC-3's routing half, and AC-5. The judge is stubbed throughout: what a model
*decides* is measured over a labelled corpus by ``scripts/eval/fre1286_entailment``, while
what the *contract* does with a decision is what these assert.

AC-2 comes first on purpose. AC-1 is trivially satisfied by a judge that rejects
everything, and the positive control is the criterion that makes rejecting everything fail.
"""

from __future__ import annotations

import asyncio

from personal_agent.grounding.citations import parse_citations
from personal_agent.grounding.enforcement import TurnDecision, decide
from personal_agent.grounding.entailment import (
    EntailmentJudge,
    EntailmentJudgement,
    EntailmentVerdict,
)
from personal_agent.grounding.source_registry import SourceRegistry
from personal_agent.grounding.spans import NonExemptReason, Span, SpanExtraction, SpanLabel
from personal_agent.grounding.verification import (
    CheckOutcome,
    TurnVerification,
    apply_entailment,
    build_grounding_record,
    verify_turn,
)
from personal_agent.telemetry.trace import TraceContext

TURN = "trace-entailment-0001"

MERCURY_CLAIM = "this fish is high in mercury"
MERCURY_PAGE = (
    "Bonito del norte is line-caught in the Bay of Biscay. Testing found this fish is "
    "high in mercury, above the advisory level for weekly consumption."
)


class _StubJudge:
    """Returns a scripted verdict, recording every claim it was asked about."""

    def __init__(self, verdict: EntailmentVerdict, *, reason: str = "stubbed") -> None:
        self.verdict = verdict
        self.reason = reason
        self.claims: list[str] = []

    async def judge(
        self, claim: str, source_content: str, *, trace_ctx: TraceContext | None = None
    ) -> EntailmentJudgement:
        self.claims.append(claim)
        return EntailmentJudgement(verdict=self.verdict, reason=self.reason)


class _SlowJudge:
    """A judge that takes measurable wall-clock time."""

    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.concurrent = 0
        self.max_concurrent = 0

    async def judge(
        self, claim: str, source_content: str, *, trace_ctx: TraceContext | None = None
    ) -> EntailmentJudgement:
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        await asyncio.sleep(self.delay_s)
        self.concurrent -= 1
        return EntailmentJudgement(verdict=EntailmentVerdict.SUPPORTED)


def _mercury_turn() -> tuple[SpanExtraction, SourceRegistry, str]:
    """One entity-free, figure-free span citing a page that states the predicate."""
    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name="fetch_url", arguments={"url": "https://example.com/fish"}, content=MERCURY_PAGE
    )
    assert registration.source is not None
    output = f"{MERCURY_CLAIM} [{registration.source.identifier}]."
    start = output.index(MERCURY_CLAIM)
    extraction = SpanExtraction(
        output=output,
        spans=(
            Span(
                start=start,
                end=start + len(MERCURY_CLAIM),
                text=MERCURY_CLAIM,
                label=SpanLabel.CLAIM_NON_EXEMPT,
                reason=NonExemptReason.CLASSIFIED,
            ),
        ),
    )
    return extraction, registry, output


def _judged(
    judge: EntailmentJudge,
    *,
    max_checks: int = 8,
    budget_ms: int = 4000,
    checks_already_used: int = 0,
) -> TurnVerification:
    """Verify the mercury turn and run the inline entailment pass over it."""
    extraction, registry, output = _mercury_turn()
    verification = verify_turn(extraction, parse_citations(output), registry)
    assert [span.outcome for span in verification.spans] == [CheckOutcome.ENTAILMENT_REQUIRED]
    return asyncio.run(
        apply_entailment(
            verification,
            registry,
            judge,
            max_checks=max_checks,
            budget_ms=budget_ms,
            checks_already_used=checks_already_used,
        )
    )


# ── AC-2 — the positive control, first ──────────────────────────────────────────────


def test_a_genuinely_supporting_source_passes() -> None:
    """Without this arm, a reject-everything judge would ace AC-1."""
    result = _judged(_StubJudge(EntailmentVerdict.SUPPORTED))

    assert [span.outcome for span in result.spans] == [CheckOutcome.PASSED]
    assert result.compliant is True
    assert decide(result, attempt=1, max_attempts=2).decision is TurnDecision.DELIVER


# ── AC-1 — the class containment cannot reach ───────────────────────────────────────


def test_a_non_supporting_source_is_rejected_and_blocks() -> None:
    """The ADR's own example: a page mentioning mercury is not support for the claim.

    Containment passes here — every content word of the claim is in the page — which is
    exactly why D3 escalates the class. The rejection has to come from D3(d) or from
    nowhere.
    """
    result = _judged(_StubJudge(EntailmentVerdict.NOT_SUPPORTED, reason="only mentions mercury"))

    assert [span.outcome for span in result.spans] == [CheckOutcome.NOT_ENTAILED]
    assert result.compliant is False
    assert "only mentions mercury" in result.spans[0].detail
    assert (
        decide(result, attempt=1, max_attempts=2).decision
        is TurnDecision.RETRY_WITH_FORCED_RETRIEVAL
    )


def test_the_judge_sees_the_claim_without_its_citation_marker() -> None:
    """A marker is protocol; letting it reach the judge would make it part of the claim."""
    judge = _StubJudge(EntailmentVerdict.SUPPORTED)
    _judged(judge)

    assert judge.claims == [MERCURY_CLAIM]


# ── AC-3 — contradiction is recorded as itself ──────────────────────────────────────


def test_contradiction_is_kept_distinct_from_plain_non_support() -> None:
    """A source stating the negation and a source saying nothing are different facts.

    Blurring them would put the residue ADR-0138 assigns to this ticket — "not sold in
    France" containing every token of "sold in France" — behind the same counter as an
    off-topic page.
    """
    result = _judged(_StubJudge(EntailmentVerdict.CONTRADICTED, reason="states the negation"))

    assert [span.outcome for span in result.spans] == [CheckOutcome.CONTRADICTED_BY_SOURCE]
    assert result.compliant is False


# ── Failure families (FRE-1282 AC-6 still holds) ────────────────────────────────────


def test_rejections_are_citation_theatre_not_no_source() -> None:
    """``NOT_ENTAILED`` belongs with ``NOT_CONTAINED``, in neither family.

    ``true_no_source`` means the turn had no admissible source at all. A source that
    exists, is entitled, is reachable and contains every token but does not support the
    claim is not that — it is what ``NOT_CONTAINED`` already is, caught one gate later.
    Filing it under ``no_source_count`` would quietly change what that counter means.
    """
    for verdict in (EntailmentVerdict.NOT_SUPPORTED, EntailmentVerdict.CONTRADICTED):
        result = _judged(_StubJudge(verdict))
        assert result.true_no_source == ()
        assert result.unverifiable == ()


def test_a_judge_outage_reads_as_our_malfunction_not_as_honesty() -> None:
    """``UNDECIDED`` is machine-undecided, and it still blocks.

    Blocking is the status quo being preserved, not a new policy: before FRE-1286 this
    class blocked as ``ENTAILMENT_REQUIRED``. What must not happen is a wave of judge
    outages reading as the model becoming candid, which is what the family placement buys.
    """
    result = _judged(_StubJudge(EntailmentVerdict.UNDECIDED, reason="provider timed out"))

    assert [span.outcome for span in result.spans] == [CheckOutcome.ENTAILMENT_UNAVAILABLE]
    assert len(result.unverifiable) == 1
    assert result.true_no_source == ()
    assert result.compliant is False


def test_a_raising_judge_does_not_lose_the_turn() -> None:
    """An exception out of the judge is a verdict of undecided, not a failed turn."""

    class _Exploding:
        async def judge(
            self, claim: str, source_content: str, *, trace_ctx: TraceContext | None = None
        ) -> EntailmentJudgement:
            raise RuntimeError("boom")

    result = _judged(_Exploding())

    assert [span.outcome for span in result.spans] == [CheckOutcome.ENTAILMENT_UNAVAILABLE]


# ── AC-5 — the latency bound ────────────────────────────────────────────────────────


def test_spans_are_judged_concurrently() -> None:
    """N escalated spans cost one round-trip, not N.

    This is the first of AC-5's bounds: without it, added latency scales with assertions
    per turn — the cost that got Option 5 rejected.
    """
    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name="fetch_url", arguments={"url": "https://example.com/fish"}, content=MERCURY_PAGE
    )
    assert registration.source is not None
    ident = registration.source.identifier
    output = " ".join(f"{MERCURY_CLAIM} [{ident}]." for _ in range(4))
    spans = []
    cursor = 0
    for _ in range(4):
        start = output.index(MERCURY_CLAIM, cursor)
        spans.append(
            Span(
                start=start,
                end=start + len(MERCURY_CLAIM),
                text=MERCURY_CLAIM,
                label=SpanLabel.CLAIM_NON_EXEMPT,
                reason=NonExemptReason.CLASSIFIED,
            )
        )
        cursor = start + len(MERCURY_CLAIM)

    verification = verify_turn(
        SpanExtraction(output=output, spans=tuple(spans)), parse_citations(output), registry
    )
    judge = _SlowJudge(0.05)
    result = asyncio.run(
        apply_entailment(verification, registry, judge, max_checks=8, budget_ms=4000)
    )

    assert judge.max_concurrent == 4
    assert result.entailment_checks == 4
    assert result.entailment_latency_ms is not None
    assert result.entailment_latency_ms < 150


def test_budget_excess_is_recorded_and_surfaced() -> None:
    """Exceeding the budget is a fact on the record, not a silent slowdown.

    The pass is deliberately not aborted mid-flight: with a per-call timeout already
    bounding the worst case, aborting would only convert a slow provider into a refusal
    the user did not deserve.
    """
    result = _judged(_StubJudge(EntailmentVerdict.SUPPORTED), budget_ms=0)

    assert result.entailment_budget_exceeded is True
    record = build_grounding_record(result, mode="enforce")
    assert record.entailment_budget_exceeded is True
    assert record.entailment_checks == 1
    assert record.entailment_latency_ms is not None


def test_checks_are_capped_and_the_cap_spans_d4_retries() -> None:
    """The cap is cumulative, because a per-pass cap bounds nothing when the pass repeats.

    D4 may generate up to ``grounding_max_generation_attempts`` times, each with its own
    verification pass. A budget of "8 per pass" is a budget of 8 × attempts.
    """
    judge = _StubJudge(EntailmentVerdict.SUPPORTED)
    result = _judged(judge, max_checks=1, checks_already_used=1)

    assert judge.claims == []
    assert [span.outcome for span in result.spans] == [CheckOutcome.ENTAILMENT_UNAVAILABLE]
    assert "budget" in result.spans[0].detail


def test_a_turn_with_nothing_escalated_costs_no_judge_call() -> None:
    """The common case must not pay for this at all."""
    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/paris"},
        content="Paris counts 2,100,000 residents within the city limits.",
    )
    assert registration.source is not None
    claim = "Paris has 2.1 million residents"
    output = f"{claim} [{registration.source.identifier}]."
    start = output.index(claim)
    extraction = SpanExtraction(
        output=output,
        spans=(
            Span(
                start=start,
                end=start + len(claim),
                text=claim,
                label=SpanLabel.CLAIM_NON_EXEMPT,
                reason=NonExemptReason.CLASSIFIED,
            ),
        ),
    )
    verification = verify_turn(extraction, parse_citations(output), registry)
    judge = _StubJudge(EntailmentVerdict.NOT_SUPPORTED)

    result = asyncio.run(
        apply_entailment(verification, registry, judge, max_checks=8, budget_ms=4000)
    )

    assert judge.claims == []
    assert result.entailment_checks == 0
    assert result.entailment_latency_ms is None
    assert [span.outcome for span in result.spans] == [CheckOutcome.PASSED]
