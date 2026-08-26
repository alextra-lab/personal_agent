"""The contract wired into the turn path (ADR-0138 D3/D4, FRE-1282).

``step_synthesis`` is where the reply is final and the registry complete, so it is where
the inline checks run and where D4 decides. These tests drive that seam directly: the
observe/enforce split, the retry's return to ``LLM_CALL``, the terminal statement, and the
marker leak that exists in every mode.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.captains_log.background import wait_for_background_tasks
from personal_agent.governance.models import Mode
from personal_agent.grounding.source_registry import SourceRegistry
from personal_agent.grounding.spans import (
    NonExemptReason,
    Span,
    SpanExtraction,
    SpanLabel,
)
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.executor import _strip_markers_from_turn, step_synthesis
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
