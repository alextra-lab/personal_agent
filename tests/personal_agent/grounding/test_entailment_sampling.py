"""The sampled offline arm (ADR-0138 D3(d) / ADR-0087, FRE-1286) — AC-4.

The residue containment cannot see: a correctly-cited token embedded in a claim the source
does not support, or contradicts. It never touches the turn — these assert the selector's
shape and that each scored sample lands as an **adjudicable** row rather than as a bare
counter.
"""

from __future__ import annotations

import asyncio
import random

from personal_agent.grounding.citations import parse_citations
from personal_agent.grounding.entailment import EntailmentJudgement, EntailmentVerdict
from personal_agent.grounding.entailment_sampling import (
    SAMPLE_EVENT,
    score_offline_samples,
    select_offline_samples,
)
from personal_agent.grounding.source_registry import SourceRegistry
from personal_agent.grounding.spans import NonExemptReason, Span, SpanExtraction, SpanLabel
from personal_agent.grounding.verification import (
    CheckOutcome,
    SpanVerification,
    TurnVerification,
    verify_turn,
)
from personal_agent.telemetry.trace import TraceContext

TURN = "trace-sampling-0001"


class _StubJudge:
    """Returns a scripted verdict, recording the passages it read."""

    def __init__(self, verdict: EntailmentVerdict, *, reason: str = "stubbed") -> None:
        self.verdict = verdict
        self.reason = reason
        self.seen: list[tuple[str, str]] = []

    async def judge(
        self, claim: str, source_content: str, *, trace_ctx: TraceContext | None = None
    ) -> EntailmentJudgement:
        self.seen.append((claim, source_content))
        return EntailmentJudgement(verdict=self.verdict, reason=self.reason)


def _passed(text: str, *, entity_free: bool, identifier: str = "S1@abcd") -> SpanVerification:
    """A span that cleared every inline gate."""
    return SpanVerification(
        text=text,
        start=0,
        end=len(text),
        identifier=identifier,
        outcome=CheckOutcome.PASSED,
        entity_free_predicate=entity_free,
    )


# ── The selector ────────────────────────────────────────────────────────────────────


def test_rate_zero_samples_nothing() -> None:
    """The arm is a measurement, and it must be possible to turn a measurement off."""
    verification = TurnVerification(
        spans=tuple(_passed(f"claim {i}", entity_free=False) for i in range(20))
    )
    assert select_offline_samples(verification, rate=0.0) == ()


def test_rate_one_samples_every_eligible_span() -> None:
    """The other extreme, so the selector's range is pinned at both ends."""
    verification = TurnVerification(
        spans=tuple(_passed(f"claim {i}", entity_free=False) for i in range(20))
    )
    assert len(select_offline_samples(verification, rate=1.0)) == 20


def test_the_inline_class_is_excluded() -> None:
    """The two arms are disjoint, and this is where that is enforced.

    Once the inline pass turns a supported escalation into ``PASSED``, only
    ``entity_free_predicate`` still says which arm judged it. Without this exclusion the
    offline arm would re-sample — and re-bill — the class the inline arm already settled.
    """
    verification = TurnVerification(
        spans=(
            _passed("this fish is high in mercury", entity_free=True),
            _passed("Paris has 2.1 million residents", entity_free=False),
        )
    )

    samples = select_offline_samples(verification, rate=1.0)

    assert [span.text for span in samples] == ["Paris has 2.1 million residents"]


def test_only_spans_that_passed_are_sampled() -> None:
    """A span the inline gates already rejected needs no second opinion."""
    verification = TurnVerification(
        spans=(
            _passed("a passing claim", entity_free=False),
            SpanVerification(
                text="a rejected claim",
                start=0,
                end=16,
                identifier="S2@beef",
                outcome=CheckOutcome.NOT_CONTAINED,
            ),
        )
    )

    samples = select_offline_samples(verification, rate=1.0)

    assert [span.text for span in samples] == ["a passing claim"]


def test_the_draw_is_independent_and_at_the_configured_rate() -> None:
    """AC-4's substance: the *configured* rate, not just its extremes.

    Testing only 0.0 and 1.0 would pass for a selector that samples everything above some
    threshold and nothing below it. A per-span Bernoulli draw is what makes the miss rate
    computed from these samples an estimate of the population's.
    """
    verification = TurnVerification(
        spans=tuple(_passed(f"claim {i}", entity_free=False) for i in range(2000))
    )

    drawn = len(select_offline_samples(verification, rate=0.25, rng=random.Random(20260826)))

    assert 440 <= drawn <= 560  # 0.25 ± ~4 sigma on n=2000


# ── The scored row ──────────────────────────────────────────────────────────────────


def _one_turn(claim: str, page: str) -> tuple[TurnVerification, SourceRegistry]:
    """Verify a single-span turn, so the sample carries a resolvable identifier."""
    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name="fetch_url", arguments={"url": "https://example.com/p"}, content=page
    )
    assert registration.source is not None
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
    return verify_turn(extraction, parse_citations(output), registry), registry


def test_a_scored_sample_is_adjudicable_not_just_countable() -> None:
    """The row carries the claim, the source and the excerpt the judge actually read.

    A bare ``miss=true`` cannot be re-adjudicated, and an eval program that inherits only
    a boolean has inherited the judge's opinion rather than the evidence for it.
    """
    verification, registry = _one_turn(
        "Zurich is not sold in France", "Zurich is sold in France and in Belgium."
    )
    judge = _StubJudge(EntailmentVerdict.CONTRADICTED, reason="the page states the positive")
    emitted: list[dict[str, object]] = []

    asyncio.run(
        score_offline_samples(
            verification.spans,
            registry,
            judge,
            answering_model="qwen3.6-35b-thinking",
            judge_model="claude_sonnet",
            max_excerpt_chars=6000,
            emit=lambda event, **fields: emitted.append({"event": event, **fields}),
        )
    )

    assert len(emitted) == 1
    row = emitted[0]
    assert row["event"] == SAMPLE_EVENT
    assert row["verdict"] == EntailmentVerdict.CONTRADICTED.value
    assert row["miss"] is True
    assert row["answering_model"] == "qwen3.6-35b-thinking"
    assert row["judge_model"] == "claude_sonnet"
    assert row["claim"] == "Zurich is not sold in France"
    assert row["excerpt"] == "Zurich is sold in France and in Belgium."
    assert row["identifier"]


def test_a_supported_sample_is_not_a_miss() -> None:
    """The positive arm of the measurement, without which the rate means nothing."""
    verification, registry = _one_turn(
        "Paris has 2.1 million residents", "Paris counts 2,100,000 residents."
    )
    emitted: list[dict[str, object]] = []

    asyncio.run(
        score_offline_samples(
            verification.spans,
            registry,
            _StubJudge(EntailmentVerdict.SUPPORTED),
            answering_model="m",
            judge_model="j",
            max_excerpt_chars=6000,
            emit=lambda event, **fields: emitted.append({"event": event, **fields}),
        )
    )

    assert emitted[0]["miss"] is False


def test_an_undecided_sample_is_not_counted_as_a_miss() -> None:
    """A judge outage must not inflate the residue it is supposed to measure."""
    verification, registry = _one_turn("Paris has 2.1 million residents", "Paris counts 2,100,000.")
    emitted: list[dict[str, object]] = []

    asyncio.run(
        score_offline_samples(
            verification.spans,
            registry,
            _StubJudge(EntailmentVerdict.UNDECIDED),
            answering_model="m",
            judge_model="j",
            max_excerpt_chars=6000,
            emit=lambda event, **fields: emitted.append({"event": event, **fields}),
        )
    )

    assert emitted[0]["miss"] is False
    assert emitted[0]["verdict"] == EntailmentVerdict.UNDECIDED.value


def test_a_failed_sample_is_a_lost_sample_never_a_raised_error() -> None:
    """This runs in a background task after delivery; it has no turn left to fail."""

    class _Exploding:
        async def judge(
            self, claim: str, source_content: str, *, trace_ctx: TraceContext | None = None
        ) -> EntailmentJudgement:
            raise RuntimeError("boom")

    verification, registry = _one_turn("Paris has 2.1 million residents", "Paris counts 2,100,000.")
    emitted: list[dict[str, object]] = []

    asyncio.run(
        score_offline_samples(
            verification.spans,
            registry,
            _Exploding(),
            answering_model="m",
            judge_model="j",
            max_excerpt_chars=6000,
            emit=lambda event, **fields: emitted.append({"event": event, **fields}),
        )
    )

    assert emitted == []


def test_the_turn_s_trace_context_reaches_the_judge() -> None:
    """Sampled spend must stay attributable to the turn that produced it.

    Letting the judge mint its own system context instead costs the session id, and
    ``LiteLLMClient`` then logs ``cost_record_missing_identity`` at ERROR and books the
    call without identity. Found by running the corpus harness — which mints one
    legitimately, having no turn — and noticing the offline arm did the same.
    """

    class _Recording:
        def __init__(self) -> None:
            self.contexts: list[TraceContext | None] = []

        async def judge(
            self, claim: str, source_content: str, *, trace_ctx: TraceContext | None = None
        ) -> EntailmentJudgement:
            self.contexts.append(trace_ctx)
            return EntailmentJudgement(verdict=EntailmentVerdict.SUPPORTED)

    verification, registry = _one_turn("Paris has 2.1 million residents", "Paris counts 2,100,000.")
    judge = _Recording()
    turn_ctx = TraceContext.new_trace(session_id="session-42")
    emitted: list[dict[str, object]] = []

    asyncio.run(
        score_offline_samples(
            verification.spans,
            registry,
            judge,
            answering_model="m",
            judge_model="j",
            max_excerpt_chars=6000,
            trace_ctx=turn_ctx,
            emit=lambda event, **fields: emitted.append({"event": event, **fields}),
        )
    )

    assert judge.contexts == [turn_ctx]
    assert emitted[0]["session_id"] == "session-42"
    assert emitted[0]["trace_id"] == turn_ctx.trace_id
