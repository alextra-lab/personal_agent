"""The contract wired into the turn path (ADR-0138 D3/D4, FRE-1282).

``step_synthesis`` is where the reply is final and the registry complete, so it is where
the inline checks run and where D4 decides. These tests drive that seam directly: the
observe/enforce split, the retry's return to ``LLM_CALL``, the terminal statement, and the
marker leak that exists in every mode.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

import personal_agent.orchestrator.executor as ex
from personal_agent.captains_log.background import wait_for_background_tasks
from personal_agent.cost_gate import BudgetDenied
from personal_agent.governance.models import Mode
from personal_agent.grounding.source_registry import SourceRegistry
from personal_agent.grounding.spans import (
    NonExemptReason,
    Span,
    SpanExtraction,
    SpanLabel,
)
from personal_agent.grounding.verification import (
    CheckOutcome,
    SpanVerification,
    TurnVerification,
)
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.executor import (
    _strip_markers_from_turn,
    execute_task_safe,
    step_synthesis,
)
from personal_agent.orchestrator.types import ExecutionContext, TaskState

CLAIM = "Paris has 2.1 million residents"


def _ctx(reply: str, registry: SourceRegistry) -> ExecutionContext:
    """A minimal context carrying a finished reply and this turn's registry."""
    ctx = ExecutionContext(
        trace_id=registry.turn_id,
        session_id="session-grounding",
        user_message="How many people live in Paris?",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )
    ctx.final_reply = reply
    ctx.source_registry = registry
    ctx.messages = [
        {"role": "user", "content": "How many people live in Paris?"},
        {"role": "assistant", "content": reply},
    ]
    return ctx


def _extraction_of(reply: str) -> SpanExtraction:
    """One non-exempt span covering the claim inside ``reply``."""
    start = reply.index(CLAIM)
    return SpanExtraction(
        output=reply,
        spans=(
            Span(
                start=start,
                end=start + len(CLAIM),
                text=CLAIM,
                label=SpanLabel.CLAIM_NON_EXEMPT,
                reason=NonExemptReason.CLASSIFIED,
            ),
        ),
    )


def _patched_extractor(reply: str):
    """Patch the span extractor so these tests exercise wiring, not classification."""
    extractor = AsyncMock()
    extractor.extract = AsyncMock(return_value=_extraction_of(reply))
    return patch(
        "personal_agent.grounding.extractor.ModelSpanExtractor", return_value=extractor
    ), patch("personal_agent.llm_client.factory.get_llm_client", return_value=object())


async def _synthesize(ctx: ExecutionContext, reply: str) -> TaskState:
    """Run ``step_synthesis`` with the extractor stubbed and the session manager inert."""
    session_manager = AsyncMock()
    session_manager.update_session = lambda *a, **k: None
    extractor_patch, client_patch = _patched_extractor(reply)
    with extractor_patch, client_patch:
        return await step_synthesis(ctx, session_manager, AsyncMock())


def _entailment_off(cfg: object) -> None:
    """Pin D3(d)'s knobs on a mock settings object (FRE-1286).

    ``patch(...settings)`` hands back a ``MagicMock``, and a mock sampling rate compares
    against a float rather than raising — so leaving these unset would not fail loudly,
    it would silently exercise a configuration that cannot exist. Sampling is off here
    because these tests are about the D3/D4 wiring; the offline arm has its own.
    """
    cfg.grounding_entailment_sample_rate = 0.0  # type: ignore[attr-defined]
    cfg.grounding_entailment_max_inline_checks = 8  # type: ignore[attr-defined]
    cfg.grounding_entailment_latency_budget_ms = 4000  # type: ignore[attr-defined]
    cfg.grounding_entailment_max_excerpt_chars = 6000  # type: ignore[attr-defined]


# ── Marker stripping — every mode, both surfaces ────────────────────────────────────


def test_markers_are_stripped_from_the_reply_and_from_session_history() -> None:
    """Both leaks, not just the visible one.

    ``ctx.messages`` is appended by ``step_llm_call`` before ``final_reply`` is set and is
    what ``step_synthesis`` persists. A marker left there returns next turn as context,
    where a turn-scoped identifier resolves to nothing and would manufacture a refusal.
    """
    registry = SourceRegistry(turn_id="trace-strip")
    registration = registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/paris"},
        content="Paris counts 2,100,000 residents.",
    )
    assert registration.source is not None
    reply = f"{CLAIM} [{registration.source.identifier}]."
    ctx = _ctx(reply, registry)

    _strip_markers_from_turn(ctx)

    assert ctx.final_reply == f"{CLAIM}."
    assert ctx.messages[1]["content"] == f"{CLAIM}."


# ── observe — records everything, blocks nothing ────────────────────────────────────


@pytest.mark.asyncio
async def test_observe_mode_records_the_failure_and_still_delivers() -> None:
    """The default. The pass runs and the outcome is recorded; the turn is not blocked.

    This is what FRE-1284's compliance metric needs to bootstrap, and it is why the
    default is safe to deploy before the extractor's production behaviour is measured.
    """
    registry = SourceRegistry(turn_id="trace-observe")
    reply = f"{CLAIM}."
    ctx = _ctx(reply, registry)

    with patch("personal_agent.orchestrator.executor.settings") as cfg:
        cfg.grounding_verification_mode = "observe"
        cfg.environment = "test"
        _entailment_off(cfg)
        state = await _synthesize(ctx, reply)

    assert state is TaskState.COMPLETED
    assert ctx.final_reply == f"{CLAIM}."
    assert ctx.grounding_disclosure is not None
    assert ctx.grounding_disclosure.startswith("1 of 1 factual statements")
    assert ctx.grounding_record is not None
    assert ctx.grounding_record.mode == "observe"
    assert ctx.grounding_record.no_source_count == 1
    assert ctx.grounding_record.first_generation_compliant is False


@pytest.mark.asyncio
async def test_observe_mode_distinguishes_entitlement_failures_in_the_record() -> None:
    """FRE-1299 AC-4: an entitlement-specific failure is countable on its own, not just
    lumped into ``no_source_count`` alongside an uncited claim (the prior test's case).
    """
    registry = SourceRegistry(turn_id="trace-entitlement")
    registry.register_memory_item({"name": "Paris population", "description": CLAIM})
    reply = f"{CLAIM} [{registry.sources()[0].identifier}]."
    ctx = _ctx(reply, registry)

    with patch("personal_agent.orchestrator.executor.settings") as cfg:
        cfg.grounding_verification_mode = "observe"
        cfg.environment = "test"
        _entailment_off(cfg)
        await _synthesize(ctx, reply)

    assert ctx.grounding_record is not None
    assert ctx.grounding_record.no_source_count == 1
    assert ctx.grounding_record.source_not_entitled_count == 1


# ── observe — the reader sees an unsourced answer (FRE-1325) ────────────────────────


def _span(text: str, outcome: CheckOutcome) -> SpanVerification:
    """One span verdict; offsets are irrelevant to the disclosure."""
    return SpanVerification(text=text, start=0, end=len(text), identifier=None, outcome=outcome)


async def _synthesize_with_verification(
    ctx: ExecutionContext, verification: TurnVerification
) -> TaskState:
    """Run ``step_synthesis`` under ``observe`` with verification's verdict fixed."""
    session_manager = AsyncMock()
    session_manager.update_session = lambda *a, **k: None
    with (
        patch("personal_agent.orchestrator.executor.settings") as cfg,
        patch(
            "personal_agent.orchestrator.executor._verify_grounding",
            new=AsyncMock(return_value=verification),
        ),
    ):
        cfg.grounding_verification_mode = "observe"
        cfg.environment = "test"
        _entailment_off(cfg)
        return await step_synthesis(ctx, session_manager, AsyncMock())


async def _deliver(ctx: ExecutionContext) -> str:
    """Run the public wrapper over an already-finished turn and return the outgoing reply.

    ``execute_task`` is stubbed to hand back ``ctx`` as ``step_synthesis`` left it — the
    capture inside it reads exactly that ``ctx.final_reply``.
    """
    with patch(
        "personal_agent.orchestrator.executor.execute_task", new=AsyncMock(return_value=ctx)
    ):
        result = await execute_task_safe(ctx, MagicMock())
    return result["reply"]


@pytest.mark.asyncio
async def test_observe_a_zero_of_nine_turn_carries_the_unsourced_note() -> None:
    """AC-1: the shape of trace ``dba5b2cba1e0bece6c8b9396465a265c`` — 9 spans, 0 passed.

    That turn was served identically to a fully-cited one. ``result["reply"]`` is what
    ``service/app.py`` pushes to the client and persists, so a line on it is visible at
    the point of reading — live, on reload, and in the CLI. The capture reads
    ``ctx.final_reply`` inside ``execute_task``, which must stay free of the note so
    consolidation never ingests it as model output.
    """
    quoted = [
        "that's the 12.7K-token cold prefill",
        "One truncated query would have saved ~9K tokens",
        "That last spike is step 8's tool result",
    ]
    spans = [_span(text, CheckOutcome.UNCITED) for text in quoted]
    spans += [_span(f"assertion {i}", CheckOutcome.UNCITED) for i in range(6)]
    reply = "Here is the session analysis."
    ctx = _ctx(reply, SourceRegistry(turn_id="dba5b2cba1e0bece6c8b9396465a265c"))

    state = await _synthesize_with_verification(ctx, TurnVerification(spans=tuple(spans)))

    assert state is TaskState.COMPLETED
    assert ctx.final_reply == reply  # what the capture records
    assert await _deliver(ctx) == (
        f"{reply}\n\nNote: 9 of 9 factual statements in this answer are not backed by a "
        "source Seshat verified this turn. No tool and no sub-agent ran this turn. "
        "Check them before you rely on them."
    )
    assert ctx.grounding_record is not None
    assert ctx.grounding_record.passed_count == 0
    assert ctx.grounding_record.first_generation_compliant is False


@pytest.mark.asyncio
async def test_observe_partial_compliance_counts_only_the_settled_failures() -> None:
    """N counts settled failures, M every non-exempt span (ADR-0151 D2)."""
    spans = (
        _span("a", CheckOutcome.PASSED),
        _span("b", CheckOutcome.UNCITED),
        _span("c", CheckOutcome.UNVERIFIABLE_BY_CONTAINMENT),
    )
    ctx = _ctx("Answer.", SourceRegistry(turn_id="trace-partial"))

    await _synthesize_with_verification(ctx, TurnVerification(spans=spans))

    reply = await _deliver(ctx)
    assert "Note: 1 of 3 factual statements" in reply
    assert "Seshat could not check 1 other statement." in reply


@pytest.mark.asyncio
async def test_observe_a_fully_cited_turn_carries_no_note() -> None:
    """AC-2, seeded negative, on the real citation path — not a stubbed verdict.

    Compliance is 0% on every measured turn so far, so an indicator that fired regardless
    would be indistinguishable from this one working. This turn verifies and must be
    delivered byte-for-byte.
    """
    registry = SourceRegistry(turn_id="trace-observe-pass")
    registration = registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/paris"},
        content="Paris counts 2,100,000 residents within the city limits.",
    )
    assert registration.source is not None
    reply = f"{CLAIM} [{registration.source.identifier}]."
    ctx = _ctx(reply, registry)

    with patch("personal_agent.orchestrator.executor.settings") as cfg:
        cfg.grounding_verification_mode = "observe"
        cfg.environment = "test"
        _entailment_off(cfg)
        state = await _synthesize(ctx, reply)

    assert state is TaskState.COMPLETED
    assert ctx.grounding_record is not None
    assert ctx.grounding_record.first_generation_compliant is True
    assert ctx.grounding_disclosure is None
    assert await _deliver(ctx) == f"{CLAIM}."


@pytest.mark.asyncio
async def test_observe_a_turn_with_no_assertions_carries_no_note() -> None:
    """AC-2: a greeting has nothing to cite, so it has nothing to disclose."""
    ctx = _ctx("Hello!", SourceRegistry(turn_id="trace-no-assertions"))

    await _synthesize_with_verification(ctx, TurnVerification())

    assert await _deliver(ctx) == "Hello!"


@pytest.mark.asyncio
async def test_observe_an_unverified_turn_carries_no_note() -> None:
    """Unmeasured is not unsourced: a turn verification could not run on says nothing."""
    ctx = _ctx("Answer.", SourceRegistry(turn_id="trace-observe-unavailable"))

    await _synthesize_with_verification(
        ctx, TurnVerification(unavailable_reason="span extraction failed: RuntimeError")
    )

    assert await _deliver(ctx) == "Answer."


# ── ADR-0151 D2: settled and unsettled statements (FRE-1507 AC-1, AC-3) ─────────────


@pytest.mark.asyncio
async def test_ac1_a_turn_whose_only_failures_are_unsettled_carries_no_note() -> None:
    """A limit of the verifier is not evidence about the statement."""
    spans = tuple(_span(f"s{i}", CheckOutcome.UNVERIFIABLE_BY_CONTAINMENT) for i in range(3))
    ctx = _ctx("Answer.", SourceRegistry(turn_id="trace-unsettled-only"))

    await _synthesize_with_verification(ctx, TurnVerification(spans=spans))

    assert ctx.grounding_disclosure is None
    assert await _deliver(ctx) == "Answer."


@pytest.mark.asyncio
async def test_ac1_the_note_counts_settled_failures_and_states_unsettled_apart() -> None:
    """2 settled and 3 unsettled: the note says 2 of 5, and 3 that Seshat could not check."""
    spans = (
        _span("s1", CheckOutcome.UNCITED),
        _span("s2", CheckOutcome.NOT_CONTAINED),
        _span("u1", CheckOutcome.UNVERIFIABLE_BY_CONTAINMENT),
        _span("u2", CheckOutcome.UNVERIFIABLE_BY_CONTAINMENT),
        _span("u3", CheckOutcome.ENTAILMENT_UNAVAILABLE),
    )
    ctx = _ctx("Answer.", SourceRegistry(turn_id="trace-settled-unsettled"))

    await _synthesize_with_verification(ctx, TurnVerification(spans=spans))

    reply = await _deliver(ctx)
    assert "Note: 2 of 5 factual statements in this answer are not backed by a source" in reply
    assert "Seshat could not check 3 other statements." in reply
    assert reply.endswith("Check them before you rely on them.")


@pytest.mark.asyncio
async def test_ac3_a_turn_with_no_shape_records_null_and_carries_no_note() -> None:
    """No non-exempt statement: ``turn_shape`` is null and nothing is appended."""
    ctx = _ctx("Hello!", SourceRegistry(turn_id="trace-no-shape"))
    mock_log, calls = _capturing_log()

    with patch("personal_agent.orchestrator.executor.log", mock_log):
        await _synthesize_with_verification(ctx, TurnVerification())

    assert _grounding_verification_completed(calls)["turn_shape"] is None
    assert await _deliver(ctx) == "Hello!"


@pytest.mark.asyncio
async def test_an_errored_turn_never_carries_the_note() -> None:
    """The reply of an errored turn may be the classified error, not the verified answer."""
    ctx = _ctx("Partial answer.", SourceRegistry(turn_id="trace-errored"))
    await _synthesize_with_verification(
        ctx, TurnVerification(spans=(_span("x", CheckOutcome.UNCITED),))
    )
    ctx.error = RuntimeError("late failure")

    with patch("personal_agent.orchestrator.executor._emit_classified_error", new=AsyncMock()):
        reply = await _deliver(ctx)

    assert "Note:" not in reply


@pytest.mark.asyncio
async def test_off_mode_runs_nothing_but_still_strips_markers() -> None:
    """The leak predates the checks, so switching them off must not reopen it."""
    registry = SourceRegistry(turn_id="trace-off")
    registration = registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/paris"},
        content="Paris counts 2,100,000 residents.",
    )
    assert registration.source is not None
    reply = f"{CLAIM} [{registration.source.identifier}]."
    ctx = _ctx(reply, registry)

    with patch("personal_agent.orchestrator.executor.settings") as cfg:
        cfg.grounding_verification_mode = "off"
        cfg.environment = "test"
        _entailment_off(cfg)
        state = await _synthesize(ctx, reply)

    assert state is TaskState.COMPLETED
    assert ctx.grounding_record is None
    assert ctx.final_reply == f"{CLAIM}."


# ── enforce — block, retry, refuse ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enforce_blocks_and_returns_to_llm_call_with_retrieval_forced() -> None:
    """D4's first move: block, and go back for another generation that can retrieve.

    The reserved tool iterations are the difference between forcing retrieval and merely
    asking for it — a turn that spent its budget would otherwise be told to retrieve with
    nothing left to retrieve with.
    """
    registry = SourceRegistry(turn_id="trace-enforce")
    reply = f"{CLAIM}."
    ctx = _ctx(reply, registry)

    with patch("personal_agent.orchestrator.executor.settings") as cfg:
        cfg.grounding_verification_mode = "enforce"
        cfg.grounding_max_generation_attempts = 2
        cfg.environment = "test"
        _entailment_off(cfg)
        state = await _synthesize(ctx, reply)

    assert state is TaskState.LLM_CALL
    assert ctx.final_reply is None
    assert ctx.grounding_retry_pending is True
    assert ctx.grounding_retrieval_grant == 2
    assert "Retrieve a source before answering" in ctx.messages[-1]["content"]


@pytest.mark.asyncio
async def test_enforce_reaches_the_terminal_statement_at_the_bound() -> None:
    """AC-5 — the loop ends, and it ends by saying so rather than by going quiet."""
    registry = SourceRegistry(turn_id="trace-terminal")
    reply = f"{CLAIM}."
    ctx = _ctx(reply, registry)
    ctx.grounding_attempts = 1  # a retry already happened
    ctx.retrieval_attempts = ["web_search(paris population)"]

    with patch("personal_agent.orchestrator.executor.settings") as cfg:
        cfg.grounding_verification_mode = "enforce"
        cfg.grounding_max_generation_attempts = 2
        cfg.environment = "test"
        _entailment_off(cfg)
        state = await _synthesize(ctx, reply)

    assert state is TaskState.COMPLETED
    assert ctx.final_reply is not None
    assert "could not find a source" in ctx.final_reply
    assert "web_search(paris population)" in ctx.final_reply
    assert CLAIM not in ctx.final_reply
    # FRE-1325: the terminal statement already says no source was found; the
    # verdict describes the discarded generation, not this text.
    assert ctx.grounding_disclosure is None


@pytest.mark.asyncio
async def test_enforce_delivers_a_turn_that_verified() -> None:
    """The paired positive: enforcement must not be indistinguishable from refusing."""
    registry = SourceRegistry(turn_id="trace-pass")
    registration = registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/paris"},
        content="Paris counts 2,100,000 residents within the city limits.",
    )
    assert registration.source is not None
    reply = f"{CLAIM} [{registration.source.identifier}]."
    ctx = _ctx(reply, registry)

    with patch("personal_agent.orchestrator.executor.settings") as cfg:
        cfg.grounding_verification_mode = "enforce"
        cfg.grounding_max_generation_attempts = 2
        cfg.environment = "test"
        _entailment_off(cfg)
        state = await _synthesize(ctx, reply)

    assert state is TaskState.COMPLETED
    assert ctx.final_reply == f"{CLAIM}."
    assert ctx.grounding_record is not None
    assert ctx.grounding_record.first_generation_compliant is True


@pytest.mark.asyncio
async def test_a_turn_verification_could_not_run_on_is_delivered_and_recorded() -> None:
    """A broken extractor must not refuse the user's turn, and must not pass silently."""
    registry = SourceRegistry(turn_id="trace-unavailable")
    reply = f"{CLAIM}."
    ctx = _ctx(reply, registry)

    session_manager = AsyncMock()
    session_manager.update_session = lambda *a, **k: None
    with (
        patch("personal_agent.orchestrator.executor.settings") as cfg,
        patch(
            "personal_agent.grounding.extractor.ModelSpanExtractor",
            side_effect=RuntimeError("budget reservation denied"),
        ),
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=object()),
    ):
        cfg.grounding_verification_mode = "enforce"
        cfg.grounding_max_generation_attempts = 2
        cfg.environment = "test"
        _entailment_off(cfg)
        state = await step_synthesis(ctx, session_manager, AsyncMock())

    assert state is TaskState.COMPLETED
    assert ctx.final_reply == f"{CLAIM}."
    assert ctx.grounding_record is not None
    assert ctx.grounding_record.available is False
    assert ctx.grounding_record.first_generation_compliant is False


@pytest.mark.asyncio
async def test_a_denied_span_extraction_budget_reservation_is_delivered_and_recorded() -> None:
    """FRE-1312: the ``deliver`` on_denial contract, exercised with the real exception.

    The prior test proves the extractor's broad ``except Exception`` catches *something*;
    this drives the actual ``BudgetDenied`` the split span_extraction lane's ``on_denial:
    deliver`` semantics exist for — a denied reservation is a fact about our accounting,
    not evidence against the model's claim, so the turn still delivers, unverified.
    """
    registry = SourceRegistry(turn_id="trace-budget-denied")
    reply = f"{CLAIM}."
    ctx = _ctx(reply, registry)
    denial = BudgetDenied(
        role="span_extraction",
        time_window="daily",
        current_spend=Decimal("5.00"),
        cap=Decimal("5.00"),
        window_resets_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    session_manager = AsyncMock()
    session_manager.update_session = lambda *a, **k: None
    with (
        patch("personal_agent.orchestrator.executor.settings") as cfg,
        patch(
            "personal_agent.grounding.extractor.ModelSpanExtractor",
            side_effect=denial,
        ),
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=object()),
    ):
        cfg.grounding_verification_mode = "enforce"
        cfg.grounding_max_generation_attempts = 2
        cfg.environment = "test"
        _entailment_off(cfg)
        state = await step_synthesis(ctx, session_manager, AsyncMock())

    assert state is TaskState.COMPLETED
    assert ctx.final_reply == f"{CLAIM}."
    assert ctx.grounding_record is not None
    assert ctx.grounding_record.available is False
    assert ctx.grounding_record.unavailable_reason == "span extraction failed: BudgetDenied"


# ── D3(d)'s sampled offline arm on the turn path (FRE-1286 AC-4) ────────────────────


@pytest.mark.asyncio
async def test_the_offline_arm_runs_after_delivery() -> None:
    """AC-4 end to end: sampling actually happens, and it happens in the background.

    The selector and the scorer are unit-tested on their own; what this pins is that
    ``step_synthesis`` reaches them at all, with a real registry and a real verification —
    the seam a unit test of either half cannot see.
    """
    registry = SourceRegistry(turn_id="trace-sampled")
    registration = registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/paris"},
        content="Paris counts 2,100,000 residents within the city limits.",
    )
    assert registration.source is not None
    reply = f"{CLAIM} [{registration.source.identifier}]."
    ctx = _ctx(reply, registry)
    scored: list[object] = []

    async def _score(samples, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        scored.extend(samples)

    with (
        patch("personal_agent.orchestrator.executor.settings") as cfg,
        patch("personal_agent.grounding.entailment_sampling.score_offline_samples", new=_score),
        patch("personal_agent.orchestrator.executor._entailment_judge", return_value=object()),
    ):
        cfg.grounding_verification_mode = "enforce"
        cfg.grounding_max_generation_attempts = 2
        cfg.environment = "test"
        _entailment_off(cfg)
        cfg.grounding_entailment_sample_rate = 1.0
        state = await _synthesize(ctx, reply)
        await wait_for_background_tasks()

    assert state is TaskState.COMPLETED
    assert [span.text for span in scored] == [CLAIM]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_blocked_turn_is_not_sampled_on_the_generation_that_failed() -> None:
    """The retry branch returns to ``LLM_CALL`` before the scheduling point.

    Sampling a generation D4 threw away would measure text the user never saw, and would
    bill a judge call for it. A turn that retries is sampled once, against its final reply.
    """
    registry = SourceRegistry(turn_id="trace-not-sampled")
    reply = f"{CLAIM}."
    ctx = _ctx(reply, registry)
    scored: list[object] = []

    async def _score(samples, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        scored.extend(samples)

    with (
        patch("personal_agent.orchestrator.executor.settings") as cfg,
        patch("personal_agent.grounding.entailment_sampling.score_offline_samples", new=_score),
        patch("personal_agent.orchestrator.executor._entailment_judge", return_value=object()),
    ):
        cfg.grounding_verification_mode = "enforce"
        cfg.grounding_max_generation_attempts = 2
        cfg.environment = "test"
        _entailment_off(cfg)
        cfg.grounding_entailment_sample_rate = 1.0
        state = await _synthesize(ctx, reply)
        await wait_for_background_tasks()

    assert state is TaskState.LLM_CALL
    assert scored == []


# ── D1's denominator fields on grounding_verification_completed (ADR-0139, FRE-1332) ──


def _capturing_log() -> tuple[MagicMock, list[tuple[str, dict[str, Any]]]]:
    """A mock module logger and the ``(event, kwargs)`` pairs its ``info`` calls carry.

    Patching the module logger rather than ``structlog.testing.capture_logs()`` — see
    ``test_frozen_reset_emit.py`` for why ``capture_logs()`` is unreliable here (FRE-552).
    """
    calls: list[tuple[str, dict[str, Any]]] = []

    def _capture(event: str, **kw: Any) -> None:
        calls.append((event, dict(kw)))

    mock_log = MagicMock()
    mock_log.info.side_effect = _capture
    return mock_log, calls


def _grounding_verification_completed(calls: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    for event, kwargs in calls:
        if event == "grounding_verification_completed":
            return kwargs
    raise AssertionError("grounding_verification_completed was never logged")


@pytest.mark.asyncio
async def test_ac1_an_uncitable_turn_says_so_on_its_own_document() -> None:
    """AC-1: every tool result refused, on a turn with a non-exempt span."""
    registry = SourceRegistry(turn_id="trace-uncitable")
    registry.register_tool_result(
        tool_name="bash",
        arguments={"command": "echo 'Paris has 2.1 million residents'"},
        content="Paris has 2.1 million residents",
    )
    reply = f"{CLAIM}."
    ctx = _ctx(reply, registry)
    mock_log, calls = _capturing_log()

    with (
        patch("personal_agent.orchestrator.executor.settings") as cfg,
        patch("personal_agent.orchestrator.executor.log", mock_log),
    ):
        cfg.grounding_verification_mode = "observe"
        cfg.environment = "test"
        _entailment_off(cfg)
        await _synthesize(ctx, reply)

    fields = _grounding_verification_completed(calls)
    assert fields["turn_evidence_class"] == "uncitable"
    assert fields["tool_results_offered"] == 1
    assert fields["tool_results_admitted"] == 0


@pytest.mark.asyncio
async def test_ac2_a_weights_only_turn_is_citable() -> None:
    """AC-2: no tools called, non-exempt spans exist — stays in the denominator."""
    registry = SourceRegistry(turn_id="trace-weights-only")
    reply = f"{CLAIM}."
    ctx = _ctx(reply, registry)
    mock_log, calls = _capturing_log()

    with (
        patch("personal_agent.orchestrator.executor.settings") as cfg,
        patch("personal_agent.orchestrator.executor.log", mock_log),
    ):
        cfg.grounding_verification_mode = "observe"
        cfg.environment = "test"
        _entailment_off(cfg)
        await _synthesize(ctx, reply)

    fields = _grounding_verification_completed(calls)
    assert fields["turn_evidence_class"] == "citable"
    assert fields["tool_results_offered"] == 0
    assert fields["tool_results_admitted"] == 0


@pytest.mark.asyncio
async def test_an_admitted_source_is_citable() -> None:
    registry = SourceRegistry(turn_id="trace-citable-admitted")
    registration = registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/paris"},
        content="Paris counts 2,100,000 residents within the city limits.",
    )
    assert registration.source is not None
    reply = f"{CLAIM} [{registration.source.identifier}]."
    ctx = _ctx(reply, registry)
    mock_log, calls = _capturing_log()

    with (
        patch("personal_agent.orchestrator.executor.settings") as cfg,
        patch("personal_agent.orchestrator.executor.log", mock_log),
    ):
        cfg.grounding_verification_mode = "observe"
        cfg.environment = "test"
        _entailment_off(cfg)
        await _synthesize(ctx, reply)

    fields = _grounding_verification_completed(calls)
    assert fields["turn_evidence_class"] == "citable"
    assert fields["tool_results_offered"] == 1
    assert fields["tool_results_admitted"] == 1


@pytest.mark.asyncio
async def test_unavailable_verification_leaves_the_new_fields_none() -> None:
    """Verification that never ran has no span list these fields could trust (ADR-0139
    D1: "on every turn where verification ran").
    """
    registry = SourceRegistry(turn_id="trace-d1-unavailable")
    reply = f"{CLAIM}."
    ctx = _ctx(reply, registry)
    mock_log, calls = _capturing_log()

    session_manager = AsyncMock()
    session_manager.update_session = lambda *a, **k: None
    with (
        patch("personal_agent.orchestrator.executor.settings") as cfg,
        patch("personal_agent.orchestrator.executor.log", mock_log),
        patch(
            "personal_agent.grounding.extractor.ModelSpanExtractor",
            side_effect=RuntimeError("budget reservation denied"),
        ),
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=object()),
    ):
        cfg.grounding_verification_mode = "observe"
        cfg.environment = "test"
        _entailment_off(cfg)
        await step_synthesis(ctx, session_manager, AsyncMock())

    fields = _grounding_verification_completed(calls)
    assert fields["turn_evidence_class"] is None
    assert fields["near_miss_markers"] is None
    assert fields["observed_span_outcomes"] is None
    assert fields["invocation_checked_span_outcomes"] is None


@pytest.mark.asyncio
async def test_near_miss_markers_counted_on_the_delivered_reply() -> None:
    """The candidate string still carries its markers when this line is logged —
    ``_strip_markers_from_turn`` runs after ``_record_grounding``, not before.
    """
    registry = SourceRegistry(turn_id="trace-near-miss")
    reply = f"{CLAIM} [S@bash-tempo-trace-dba5b2]."
    ctx = _ctx(reply, registry)
    mock_log, calls = _capturing_log()

    with (
        patch("personal_agent.orchestrator.executor.settings") as cfg,
        patch("personal_agent.orchestrator.executor.log", mock_log),
    ):
        cfg.grounding_verification_mode = "observe"
        cfg.environment = "test"
        _entailment_off(cfg)
        await _synthesize(ctx, reply)

    fields = _grounding_verification_completed(calls)
    assert fields["near_miss_markers"] == {"unresolved": 1}


@pytest.mark.asyncio
async def test_an_uncitable_turn_is_excluded_from_the_compliance_window() -> None:
    """AC-5, on the live turn path: the write to D5's window is skipped, not merely the
    log line's classification.
    """
    registry = SourceRegistry(turn_id="trace-ac5-uncitable")
    registry.register_tool_result(
        tool_name="bash",
        arguments={"command": "echo 'Paris has 2.1 million residents'"},
        content="Paris has 2.1 million residents",
    )
    reply = f"{CLAIM}."
    ctx = _ctx(reply, registry)
    ctx.answering_model_key = "gemma-3-27b"
    mock_log, calls = _capturing_log()

    with (
        patch("personal_agent.orchestrator.executor.settings") as cfg,
        patch("personal_agent.orchestrator.executor.log", mock_log),
    ):
        cfg.grounding_verification_mode = "observe"
        cfg.environment = "test"
        _entailment_off(cfg)
        await _synthesize(ctx, reply)

    fields = _grounding_verification_completed(calls)
    assert fields["compliance_observation"] == "confounded"


# ── D8's ordering signal: which tool was refused, and why (ADR-0140 AC-3, FRE-1359) ──

UNCLASSIFIED_TOOL_NAME = "some_tool_added_next_quarter"


def _all_grounding_verification_completed(
    calls: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Every such event, not the first.

    ``_grounding_verification_completed`` returns the first match, which cannot see a D4
    retry's second document — the very shape the measurement's unit of analysis has to
    collapse.
    """
    return [kwargs for event, kwargs in calls if event == "grounding_verification_completed"]


@pytest.mark.asyncio
async def test_ac1_refused_origins_name_the_tools_and_their_reasons() -> None:
    """AC-1's own seeded shape, read off the single document it names.

    Two refused ``bash`` calls and one refused unclassified call: two distinct origins, in
    refusal order, each paired with the rule that refused it. Without the pairing the
    roadmap cannot tell wrapper demand from a typed tool that returned nothing.
    """
    registry = SourceRegistry(turn_id="trace-refused-origins")
    registry.register_tool_result(
        tool_name="bash",
        arguments={"command": "ls /etc"},
        content="hosts",
    )
    registry.register_tool_result(
        tool_name="bash",
        arguments={"command": "cat /etc/hosts"},
        content="127.0.0.1 localhost",
    )
    registry.register_tool_result(
        tool_name=UNCLASSIFIED_TOOL_NAME,
        arguments={"q": "paris population"},
        content="Paris has 2.1 million residents",
    )
    reply = f"{CLAIM}."
    ctx = _ctx(reply, registry)
    mock_log, calls = _capturing_log()

    with (
        patch("personal_agent.orchestrator.executor.settings") as cfg,
        patch("personal_agent.orchestrator.executor.log", mock_log),
    ):
        cfg.grounding_verification_mode = "observe"
        cfg.environment = "test"
        _entailment_off(cfg)
        await _synthesize(ctx, reply)

    assert len(_all_grounding_verification_completed(calls)) == 1
    fields = _grounding_verification_completed(calls)
    assert fields["turn_evidence_class"] == "uncitable"
    assert fields["refused_tool_origins"] == ["bash", UNCLASSIFIED_TOOL_NAME]
    assert fields["refused_origin_admissibility"] == [
        "bash:model_authored_invocation",
        f"{UNCLASSIFIED_TOOL_NAME}:unclassified_tool",
    ]


@pytest.mark.asyncio
async def test_a_fully_admitted_turn_refuses_nothing() -> None:
    """The paired negative — the field must not fire on a turn that cited everything."""
    registry = SourceRegistry(turn_id="trace-refused-none")
    registration = registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/paris"},
        content="Paris counts 2,100,000 residents within the city limits.",
    )
    assert registration.source is not None
    reply = f"{CLAIM} [{registration.source.identifier}]."
    ctx = _ctx(reply, registry)
    mock_log, calls = _capturing_log()

    with (
        patch("personal_agent.orchestrator.executor.settings") as cfg,
        patch("personal_agent.orchestrator.executor.log", mock_log),
    ):
        cfg.grounding_verification_mode = "observe"
        cfg.environment = "test"
        _entailment_off(cfg)
        await _synthesize(ctx, reply)

    fields = _grounding_verification_completed(calls)
    assert fields["refused_tool_origins"] == []
    assert fields["refused_origin_admissibility"] == []


@pytest.mark.asyncio
async def test_unavailable_verification_still_names_the_refused_origins() -> None:
    """These two fields are properties of the registry, not of the span list.

    They sit with ``tool_results_offered``/``tool_results_admitted``, outside the
    availability gate: verification being *attempted and unavailable* says nothing about
    what the turn refused.
    """
    registry = SourceRegistry(turn_id="trace-refused-unavailable")
    registry.register_tool_result(
        tool_name="bash",
        arguments={"command": "echo 'Paris has 2.1 million residents'"},
        content="Paris has 2.1 million residents",
    )
    reply = f"{CLAIM}."
    ctx = _ctx(reply, registry)
    mock_log, calls = _capturing_log()

    session_manager = AsyncMock()
    session_manager.update_session = lambda *a, **k: None
    with (
        patch("personal_agent.orchestrator.executor.settings") as cfg,
        patch("personal_agent.orchestrator.executor.log", mock_log),
        patch(
            "personal_agent.grounding.extractor.ModelSpanExtractor",
            side_effect=RuntimeError("budget reservation denied"),
        ),
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=object()),
    ):
        cfg.grounding_verification_mode = "observe"
        cfg.environment = "test"
        _entailment_off(cfg)
        await step_synthesis(ctx, session_manager, AsyncMock())

    fields = _grounding_verification_completed(calls)
    assert fields["turn_evidence_class"] is None
    assert fields["refused_tool_origins"] == ["bash"]


@pytest.mark.asyncio
async def test_each_generation_attempt_emits_its_own_document() -> None:
    """Why the measurement counts turns and not documents (FRE-1359 preregistration).

    ``_record_grounding`` runs on every synthesis attempt, and one registry serves the
    whole turn — so a retried trace writes two documents and the second still carries the
    first attempt's refused origins. A terms aggregation over documents would count this
    single turn's ``bash`` twice. The preregistered unit of analysis collapses by
    ``trace_id``, keeping the highest ``attempts``; this test is the behaviour that makes
    that collapse necessary.
    """
    registry = SourceRegistry(turn_id="trace-refused-retry")
    registry.register_tool_result(
        tool_name="bash",
        arguments={"command": "echo 'Paris has 2.1 million residents'"},
        content="Paris has 2.1 million residents",
    )
    reply = f"{CLAIM}."
    ctx = _ctx(reply, registry)
    mock_log, calls = _capturing_log()

    with (
        patch("personal_agent.orchestrator.executor.settings") as cfg,
        patch("personal_agent.orchestrator.executor.log", mock_log),
    ):
        cfg.grounding_verification_mode = "enforce"
        cfg.grounding_max_generation_attempts = 2
        cfg.environment = "test"
        _entailment_off(cfg)
        first = await _synthesize(ctx, reply)
        ctx.final_reply = reply
        await _synthesize(ctx, reply)

    assert first is TaskState.LLM_CALL
    events = _all_grounding_verification_completed(calls)
    assert len(events) == 2
    assert [event["refused_tool_origins"] for event in events] == [["bash"], ["bash"]]


# ── ADR-0151 D1: the turn shape on the paths registry counts miss (FRE-1507 AC-2) ─────
#
# Each seed runs the real step that produces its counts, then synthesis with one uncited
# statement, and reads both the event and the delivered note. A classifier that reads only
# the registry counts gets `tool_results_offered == 0` on the first two seeds, which each
# test asserts, so it cannot tell them from the no-tool seed that must read C.

SHAPE_A = "Tools or sub-agents ran this turn, and Seshat cannot cite their output."
SHAPE_B = "The sources this turn retrieved do not back these statements."
SHAPE_C = "No tool and no sub-agent ran this turn."
_ONE_UNCITED = TurnVerification(spans=(_span("claim", CheckOutcome.UNCITED),))


async def _declare(ctx: ExecutionContext) -> tuple[dict[str, Any], str]:
    """Synthesize over one uncited statement; return the event and the delivered reply."""
    ctx.final_reply = "Answer."
    ctx.messages.append({"role": "assistant", "content": "Answer."})
    mock_log, calls = _capturing_log()
    with patch("personal_agent.orchestrator.executor.log", mock_log):
        await _synthesize_with_verification(ctx, _ONE_UNCITED)
    return _grounding_verification_completed(calls), await _deliver(ctx)


@pytest.fixture
def _tool_step_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """The seams ``test_tool_result_citation.py`` stubs to run ``step_tool_execution``."""
    monkeypatch.setattr(ex, "_get_tool_execution_layer", lambda: object())
    monkeypatch.setattr(ex, "_is_turn_cancelled", lambda _sid: False)
    monkeypatch.setattr(ex, "_report_turn_progress", AsyncMock())


def _assistant_tool_call(name: str, arguments: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "tc-1", "function": {"name": name, "arguments": arguments}}],
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("_tool_step_seams")
async def test_ac2_a_malformed_tool_call_turn_is_shape_a(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatch = AsyncMock()
    monkeypatch.setattr(ex, "dispatch_tool_call", dispatch)
    registry = SourceRegistry(turn_id="trace-shape-malformed")
    ctx = _ctx("Answer.", registry)
    ctx.messages = [ctx.messages[0], _assistant_tool_call("web_search", "{not json")]

    await ex.step_tool_execution(ctx, MagicMock(), AsyncMock())
    fields, reply = await _declare(ctx)

    dispatch.assert_not_called()
    assert fields["tool_results_offered"] == 0
    assert fields["tool_rounds"] == 1
    assert fields["turn_shape"] == "a"
    assert SHAPE_A in reply


@pytest.mark.asyncio
async def test_ac2_a_decompose_turn_whose_workers_all_raise_is_shape_a(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from personal_agent.orchestrator.expansion_controller import (
        ExpansionController,
        ExpansionResult,
        _validate_plan_json,
    )
    from personal_agent.request_gateway.types import DecompositionResult, DecompositionStrategy
    from personal_agent.telemetry.trace import TraceContext
    from tests.personal_agent.orchestrator.test_fanout_incomplete_pause import (
        _ctx as _fanout_ctx,
    )
    from tests.personal_agent.orchestrator.test_fanout_incomplete_pause import (
        _patch_expansion,
        _session_manager,
    )

    plan_json = json.dumps(
        {
            "strategy": "DECOMPOSE",
            "tasks": [
                {"name": f"part_{i}", "goal": f"Goal {i}", "constraints": [], "type": "general"}
                for i in range(2)
            ],
        }
    )
    plan = _validate_plan_json(plan_json, "DECOMPOSE")
    assert plan is not None
    expansion_result = ExpansionResult(plan=plan)
    with patch(
        "personal_agent.orchestrator.expansion_controller.run_sub_agent",
        side_effect=RuntimeError("worker raised"),
    ):
        expansion_result.sub_agent_results = await ExpansionController()._run_dispatch(
            plan=plan,
            llm_client=AsyncMock(),
            trace_id="t1",
            messages=[],
            result=expansion_result,
        )
    _patch_expansion(monkeypatch, expansion_result)
    ctx = _fanout_ctx()
    assert ctx.gateway_output is not None
    ctx.gateway_output = replace(
        ctx.gateway_output,
        decomposition=DecompositionResult(
            strategy=DecompositionStrategy.DECOMPOSE, reason="test", constraints={}
        ),
    )

    await ex.step_init(ctx, _session_manager(), TraceContext(trace_id="t1", session_id="s1"))
    ctx.source_registry = SourceRegistry(turn_id="t1")
    fields, reply = await _declare(ctx)

    assert ctx.sub_agent_results == []
    assert fields["tool_results_offered"] == 0
    assert fields["tool_rounds"] == 0
    assert fields["sub_agents_dispatched"] == 2
    assert fields["turn_shape"] == "a"
    assert SHAPE_A in reply


@pytest.mark.asyncio
async def test_ac2_an_image_attachment_turn_with_no_tool_call_is_shape_c() -> None:
    ctx = _ctx("Answer.", SourceRegistry(turn_id="trace-shape-image"))
    ctx.messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What does this label say?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
            ],
        }
    ]

    fields, reply = await _declare(ctx)

    assert fields["tool_rounds"] == 0
    assert fields["sub_agents_dispatched"] == 0
    assert fields["turn_shape"] == "c"
    assert SHAPE_C in reply


@pytest.mark.asyncio
@pytest.mark.usefixtures("_tool_step_seams")
async def test_ac2_a_turn_with_one_admitted_web_search_source_is_shape_b(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.personal_agent.orchestrator.test_tool_result_citation import _dispatch_returning

    content = json.dumps({"results": [{"content": "Paris counts 2,100,000 residents."}]})
    monkeypatch.setattr(ex, "dispatch_tool_call", _dispatch_returning(content=content))
    ctx = _ctx("Answer.", SourceRegistry(turn_id="trace-shape-web-search"))
    ctx.messages = [ctx.messages[0], _assistant_tool_call("web_search", "{}")]

    await ex.step_tool_execution(ctx, MagicMock(), AsyncMock())
    fields, reply = await _declare(ctx)

    assert fields["tool_results_admitted"] == 1
    assert fields["turn_shape"] == "b"
    assert SHAPE_B in reply


# ── ADR-0151 D4: the declaration stays out of the capture (FRE-1507 AC-4) ─────────────


@pytest.mark.asyncio
async def test_ac4_the_declaration_never_enters_the_capture() -> None:
    """The real capture path: ``execute_task`` writes it before the note is appended."""
    ctx = _ctx("Answer.", SourceRegistry(turn_id="trace-capture-declared"))
    ctx.user_id = uuid4()
    ctx.state = TaskState.SYNTHESIS
    written: list[Any] = []
    session_manager = AsyncMock()
    session_manager.update_session = lambda *a, **k: None

    # The real settings object, three fields pinned: past the capture, execute_task reads
    # numeric limits that a MagicMock cannot compare.
    with (
        patch.object(ex.settings, "grounding_verification_mode", "observe"),
        patch.object(ex.settings, "grounding_entailment_sample_rate", 0.0),
        patch.object(ex.settings, "request_monitoring_enabled", False),
        patch(
            "personal_agent.orchestrator.executor._verify_grounding",
            new=AsyncMock(return_value=_ONE_UNCITED),
        ),
        patch("personal_agent.captains_log.capture.write_capture", side_effect=written.append),
        patch(
            "personal_agent.events.bus.get_event_bus",
            return_value=MagicMock(publish=AsyncMock()),
        ),
        patch(
            "personal_agent.orchestrator.executor._trigger_captains_log_reflection",
            new_callable=AsyncMock,
        ),
    ):
        result = await execute_task_safe(ctx, session_manager)

    assert ctx.error is None, repr(ctx.error)
    assert len(written) == 1
    assert "Check them before you rely on them" not in (written[0].assistant_response or "")
    assert result["reply"].endswith("Check them before you rely on them.")
