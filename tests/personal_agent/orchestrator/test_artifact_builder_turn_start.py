"""ADR-0122 T5 / FRE-930 — the artifact-builder ask is raised at TURN START.

Proves the decision is resolved in ``step_init`` (before the first LLM call), that it
lands on the execution context and the tool-boundary carrier (AC-10a), that it is
ordered after — and gated by — the attachment-cost pause (AC-14 a/d), and that no
turn-start ask runs without the ``artifact_build_intent`` signal. The build-boundary
consumption (AC-1/AC-11/AC-10c) is proved in ``tests/personal_agent/tools/test_artifact_tools.py``,
where the ``artifact_draft`` fakes live.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest

from personal_agent.governance.models import Mode
from personal_agent.orchestrator import executor as executor_mod
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.constraint_options import (
    ConstraintDecision,
    get_artifact_builder_resolution,
    reset_artifact_builder_resolution,
    set_artifact_builder_resolution,
)
from personal_agent.orchestrator.session import SessionManager
from personal_agent.orchestrator.types import AttachmentRef, ExecutionContext, TaskState
from personal_agent.request_gateway.types import (
    AssembledContext,
    Complexity,
    DecompositionResult,
    DecompositionStrategy,
    GatewayOutput,
    GovernanceContext,
    IntentResult,
    TaskType,
)
from personal_agent.telemetry.trace import TraceContext

pytestmark = pytest.mark.asyncio

_IMAGE_BLOCK = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


@pytest.fixture(autouse=True)
def _reset_builder_carrier() -> Iterator[None]:
    """Token-reset the resolution ContextVar around every test (isolation)."""
    token = set_artifact_builder_resolution(None)
    try:
        yield
    finally:
        reset_artifact_builder_resolution(token)


def _gw(signals: list[str]) -> GatewayOutput:
    """A minimal SINGLE-strategy GatewayOutput carrying ``signals``."""
    return GatewayOutput(
        intent=IntentResult(
            task_type=TaskType.TOOL_USE,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=signals,
        ),
        governance=GovernanceContext(mode=Mode.NORMAL, expansion_permitted=True),
        decomposition=DecompositionResult(strategy=DecompositionStrategy.SINGLE, reason="test"),
        context=AssembledContext(messages=[], memory_context=None, tool_definitions=None),
        session_id="test-session",
        trace_id="test-trace",
    )


def _ctx(
    sm: SessionManager, *, signals: list[str], user_message: str = "build me a dashboard"
) -> ExecutionContext:
    session_id = sm.create_session(Mode.NORMAL, Channel.CHAT)
    trace = TraceContext.new_trace()
    ctx = ExecutionContext(
        session_id=session_id,
        trace_id=trace.trace_id,
        user_message=user_message,
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        eval_mode=True,
    )
    ctx.gateway_output = _gw(signals)
    return ctx


# ── AC-10(a): resolved at turn start, on the execution context + the carrier ──────


async def test_step_init_populates_builder_resolution_when_signal_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-10(a): after step_init the resolution is on ctx AND the carrier; fails on old code.

    The old build-boundary placement never resolves in step_init, so ctx.
    artifact_builder_resolution would be None here — the discriminating check.
    """
    sm = SessionManager()
    ctx = _ctx(sm, signals=["tool_intent_pattern", "artifact_build_intent"])
    decision = ConstraintDecision("claude_sonnet", "user_choice")
    pause = AsyncMock(return_value=decision)
    monkeypatch.setattr(executor_mod, "_maybe_pause_for_constraint", pause)

    next_state = await executor_mod.step_init(ctx, sm, TraceContext.new_trace())

    assert next_state == TaskState.LLM_CALL  # gateway SINGLE path
    assert ctx.artifact_builder_resolution == decision
    assert ctx.artifact_builder_resolution.resolution == "user_choice"
    pause.assert_awaited_once()
    assert pause.await_args.kwargs["constraint"] == "artifact_builder"
    # The tool-boundary carrier mirrors it (what artifact_draft reads).
    assert get_artifact_builder_resolution() == decision


async def test_step_init_populates_planning_note_with_resolved_deployment_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0122 §5/T6: the note names the resolved deployment's effective budget.

    A non-default pick (claude_haiku, declared max_tokens 4096) so the note cannot
    coincidentally match the configured default (claude_sonnet, 32768).
    """
    sm = SessionManager()
    ctx = _ctx(sm, signals=["tool_intent_pattern", "artifact_build_intent"])
    decision = ConstraintDecision("claude_haiku", "user_choice")
    pause = AsyncMock(return_value=decision)
    monkeypatch.setattr(executor_mod, "_maybe_pause_for_constraint", pause)
    # Bypass the live provider-availability check (no ANTHROPIC_API_KEY in the test
    # env would otherwise fail closed to the default) — mirrors the same patch used
    # throughout tests/personal_agent/tools/test_artifact_tools.py.
    monkeypatch.setattr(
        "personal_agent.orchestrator.constraint_options.resolve_artifact_builder_key",
        lambda selected_key, config, **_kw: selected_key,
    )

    await executor_mod.step_init(ctx, sm, TraceContext.new_trace())

    assert ctx.artifact_builder_planning_note is not None
    assert "claude_haiku" in ctx.artifact_builder_planning_note
    assert "4096" in ctx.artifact_builder_planning_note
    assert "200000" in ctx.artifact_builder_planning_note  # claude_haiku's context_length


async def test_step_init_no_planning_note_without_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No artifact_build_intent signal → no turn-start ask → no planning note either."""
    sm = SessionManager()
    ctx = _ctx(sm, signals=["tool_intent_pattern"], user_message="search the web for X")
    pause = AsyncMock()
    monkeypatch.setattr(executor_mod, "_maybe_pause_for_constraint", pause)

    await executor_mod.step_init(ctx, sm, TraceContext.new_trace())

    assert ctx.artifact_builder_planning_note is None
    pause.assert_not_awaited()


async def test_step_init_no_resolution_without_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No artifact_build_intent signal → the ask never runs; the carrier stays None.

    A build that nonetheless reaches artifact_draft then reads None and logs a tunable
    miss (§3b/AC-11).
    """
    sm = SessionManager()
    ctx = _ctx(sm, signals=["tool_intent_pattern"], user_message="search the web for X")
    pause = AsyncMock()
    monkeypatch.setattr(executor_mod, "_maybe_pause_for_constraint", pause)

    await executor_mod.step_init(ctx, sm, TraceContext.new_trace())

    assert ctx.artifact_builder_resolution is None
    pause.assert_not_awaited()
    assert get_artifact_builder_resolution() is None


# ── AC-14(a)/(d): attachment gate precedes and gates the builder ──────────────────


def _with_attachment(ctx: ExecutionContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the turn one resolved image block so the attachment-cost gate is entered."""
    ctx.attachments = (
        AttachmentRef(artifact_id="a1", content_type="image/png", title="pic.png", r2_key="k1"),
    )
    from personal_agent.orchestrator.attachment_resolution import ResolvedAttachments

    monkeypatch.setattr(
        "personal_agent.orchestrator.attachment_resolution.resolve_attachments",
        AsyncMock(return_value=ResolvedAttachments(blocks=(_IMAGE_BLOCK,), disclosures=())),
    )


async def test_declining_attachment_gate_short_circuits_before_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-14(d): declining the attachment gate returns SYNTHESIS and no builder ask runs."""
    sm = SessionManager()
    ctx = _ctx(sm, signals=["tool_intent_pattern", "artifact_build_intent"])
    _with_attachment(ctx, monkeypatch)

    order: list[str] = []

    async def fake_attach(
        _ctx: ExecutionContext, _blocks: object, native_pdf_page_count: int = 0
    ) -> bool:
        order.append("attachment")
        return False  # user declined → short-circuit

    async def fake_pause(**_kw: object) -> ConstraintDecision:
        order.append("builder")
        return ConstraintDecision("claude_sonnet", "user_choice")

    monkeypatch.setattr(executor_mod, "_maybe_confirm_attachment_cost", fake_attach)
    monkeypatch.setattr(executor_mod, "_maybe_pause_for_constraint", fake_pause)

    next_state = await executor_mod.step_init(ctx, sm, TraceContext.new_trace())

    assert next_state == TaskState.SYNTHESIS
    assert order == ["attachment"]  # builder never raised — no build follows
    assert ctx.artifact_builder_resolution is None


async def test_attachment_gate_precedes_builder_when_proceeding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-14(a)/(b): the attachment gate is raised before the builder, its own waiter."""
    sm = SessionManager()
    ctx = _ctx(sm, signals=["tool_intent_pattern", "artifact_build_intent"])
    _with_attachment(ctx, monkeypatch)

    order: list[str] = []

    async def fake_attach(
        _ctx: ExecutionContext, _blocks: object, native_pdf_page_count: int = 0
    ) -> bool:
        order.append("attachment")
        return True  # confirmed → turn proceeds

    async def fake_pause(**kw: object) -> ConstraintDecision:
        order.append(f"builder:{kw['constraint']}")
        return ConstraintDecision("claude_sonnet", "user_choice")

    monkeypatch.setattr(executor_mod, "_maybe_confirm_attachment_cost", fake_attach)
    monkeypatch.setattr(executor_mod, "_maybe_pause_for_constraint", fake_pause)

    await executor_mod.step_init(ctx, sm, TraceContext.new_trace())

    assert order == ["attachment", "builder:artifact_builder"]
    assert ctx.artifact_builder_resolution == ConstraintDecision("claude_sonnet", "user_choice")


# ── The helper in isolation: unconditional carrier set ────────────────────────────


async def test_helper_leaves_carrier_none_when_no_gateway_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No gateway_output at all → resolution stays None, no pause."""
    sm = SessionManager()
    ctx = _ctx(sm, signals=[], user_message="a message with no gateway")
    ctx.gateway_output = None
    pause = AsyncMock()
    monkeypatch.setattr(executor_mod, "_maybe_pause_for_constraint", pause)

    await executor_mod._maybe_resolve_artifact_builder(ctx)

    pause.assert_not_awaited()
    assert get_artifact_builder_resolution() is None
    assert ctx.artifact_builder_resolution is None
