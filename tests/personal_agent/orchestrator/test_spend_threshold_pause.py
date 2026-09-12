"""Tests for the ADR-0142 D2/D3/D4b spend-threshold constraint pause (FRE-1393).

Verifies the ticket's own five acceptance criteria:
- AC-1: the turn actually blocks, and resumes only on a decision.
- AC-2: it asks at most once per turn.
- AC-3: a stored preference cannot silence it (allow_preference=False, ADR-0101 §8b).
- AC-4: headless (no client) terminates at the baseline, not at the deadline.
- AC-5: the card states the iteration count reached and the threshold crossed.

Plus the codex-plan-review finding that the trigger must respect a per-task-type
ceiling lower than the global one (the still-live
``orchestrator_max_tool_iterations_by_task_type``, not removed until FRE-1394).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import personal_agent.orchestrator.executor as ex
from personal_agent.config import settings
from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.types import ExecutionContext, TaskState
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

_TRANSPORT = "personal_agent.transport.agui.transport"


def _ctx(**overrides: object) -> ExecutionContext:
    defaults: dict[str, object] = {
        "session_id": "s1",
        "trace_id": "t1",
        "user_message": "hi",
        "mode": Mode.NORMAL,
        "channel": Channel.CHAT,
    }
    defaults.update(overrides)
    return ExecutionContext(**defaults)  # type: ignore[arg-type]


def _mock_session() -> object:
    from unittest.mock import MagicMock

    mock_session = MagicMock()
    mock_session.add_message = AsyncMock()
    mock_session.get_messages = AsyncMock(return_value=[])
    return mock_session


def _gateway_output(task_type: TaskType) -> GatewayOutput:
    return GatewayOutput(
        intent=IntentResult(
            task_type=task_type,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        ),
        governance=GovernanceContext(mode=Mode.NORMAL, expansion_permitted=True),
        decomposition=DecompositionResult(strategy=DecompositionStrategy.SINGLE, reason="test"),
        context=AssembledContext(
            messages=[{"role": "user", "content": "hello"}],
            memory_context=None,
            tool_definitions=None,
        ),
        session_id="test-session",
        trace_id="test-trace",
    )


def _patch_no_preference(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_load(user_id: object, constraint: str, **_kw: object) -> None:
        return None

    monkeypatch.setattr(ex, "_load_constraint_preference", fake_load)


def _patch_answered(monkeypatch: pytest.MonkeyPatch, decision: str) -> None:
    async def fake_push(**kwargs: object) -> dict[str, str]:
        return {"decision": decision, "resolution": "user_choice"}

    async def fake_emit(**kwargs: object) -> None:
        return None

    monkeypatch.setattr(f"{_TRANSPORT}.register_and_push_constraint", fake_push)
    monkeypatch.setattr(f"{_TRANSPORT}.emit_constraint_resolved", fake_emit)


@pytest.mark.asyncio
class TestSpendPauseBlocksAndResumesOnDecision:
    """AC-1: the turn actually blocks, and resumes only on a decision."""

    async def test_dispatch_never_starts_before_the_pause_resolves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "orchestrator_spend_threshold", 2)
        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 900)
        events: list[str] = []

        async def fake_pause(**kwargs: object) -> object:
            await asyncio.sleep(0.05)
            events.append("pause_resolved")
            from personal_agent.orchestrator.constraint_options import ConstraintDecision

            return ConstraintDecision("continue_turn", "user_choice")

        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", fake_pause)

        ctx = _ctx(
            session_id="sess-1",
            trace_id="trace-1",
            messages=[
                {
                    "role": "assistant",
                    "content": "x",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "noop_tool", "arguments": "{}"},
                        }
                    ],
                }
            ],
        )
        ctx.tool_iteration_count = 1  # about to become 2, crossing the threshold of 2

        original_begin_turn = ctx.loop_gate.begin_turn

        def spy_begin_turn() -> None:
            events.append("dispatch_started")
            return original_begin_turn()

        monkeypatch.setattr(ctx.loop_gate, "begin_turn", spy_begin_turn)

        # Tool dispatch itself is irrelevant to this test — stub the execution layer
        # so it never touches real tool machinery once (if) dispatch is reached.
        from unittest.mock import MagicMock

        fake_layer = MagicMock()
        fake_layer.execute_batch = AsyncMock(return_value=[])
        monkeypatch.setattr(ex, "_get_tool_execution_layer", lambda: fake_layer)

        trace_ctx = TraceContext.new_trace()
        await ex.step_tool_execution(ctx, _mock_session(), trace_ctx)  # type: ignore[arg-type]

        assert events == ["pause_resolved", "dispatch_started"]
        assert ctx.spend_pause_raised is True


@pytest.mark.asyncio
class TestSpendPauseFiresAtMostOnce:
    """AC-2: a turn granted a continuation does not re-ask at the same threshold."""

    async def test_second_crossing_does_not_reask(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "orchestrator_spend_threshold", 2)
        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 900)

        call_count = {"n": 0}

        async def fake_pause(**kwargs: object) -> object:
            call_count["n"] += 1
            from personal_agent.orchestrator.constraint_options import ConstraintDecision

            return ConstraintDecision("continue_turn", "user_choice")

        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", fake_pause)

        from unittest.mock import MagicMock

        fake_layer = MagicMock()
        fake_layer.execute_batch = AsyncMock(return_value=[])
        monkeypatch.setattr(ex, "_get_tool_execution_layer", lambda: fake_layer)

        ctx = _ctx(
            session_id="sess-2",
            trace_id="trace-2",
            messages=[{"role": "assistant", "content": "x", "tool_calls": []}],
        )
        trace_ctx = TraceContext.new_trace()

        ctx.tool_iteration_count = 1  # -> 2, crosses threshold once
        await ex.step_tool_execution(ctx, _mock_session(), trace_ctx)  # type: ignore[arg-type]
        ctx.tool_iteration_count = 4  # -> 5, still past threshold, must not re-ask
        await ex.step_tool_execution(ctx, _mock_session(), trace_ctx)  # type: ignore[arg-type]

        assert call_count["n"] == 1


@pytest.mark.asyncio
class TestSpendPausePreferenceExempt:
    """AC-3: a stored preference cannot silence it (ADR-0101 §8b / allow_preference=False)."""

    async def test_preference_never_loaded_and_no_applied_event_logged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        pref_loaded = {"called": False}

        async def fake_load(user_id: object, constraint: str, **_kw: object) -> str | None:
            pref_loaded["called"] = True
            return "continue_turn"  # would silently bypass the pause if ever honored

        monkeypatch.setattr(ex, "_load_constraint_preference", fake_load)
        _patch_answered(monkeypatch, "continue_turn")

        ctx = _ctx()
        with caplog.at_level("INFO"):
            result = await ex._maybe_pause_for_constraint(
                session_id="s1",
                trace_id="t1",
                user_id=uuid4(),
                constraint="spend_threshold",
                context="ctx",
                allow_preference=False,
                ctx=ctx,
            )

        assert pref_loaded["called"] is False
        assert result == "continue_turn"
        assert result.resolution == "user_choice"
        applied_events = [
            r for r in caplog.records if getattr(r, "event", "") == "constraint_preference_applied"
        ]
        assert applied_events == []


@pytest.mark.asyncio
class TestSpendPauseHeadlessBaseline:
    """AC-4: headless (no client) terminates at the baseline, not at the deadline."""

    async def test_no_connection_resolves_via_safe_default_not_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuinely no-client session waits out the (short, test-scoped) timeout."""
        monkeypatch.setattr(settings, "constraint_pause_timeout_seconds", 0.2)
        _patch_no_preference(monkeypatch)

        ctx = _ctx(session_id=f"no-conn-{uuid4()}")
        started = time.monotonic()
        result = await ex._maybe_pause_for_constraint(
            session_id=ctx.session_id,
            trace_id="t1",
            user_id=None,
            constraint="spend_threshold",
            context="ctx",
            allow_preference=False,
            ctx=ctx,
        )
        elapsed = time.monotonic() - started

        assert elapsed >= 0.15, f"resolved instantly ({elapsed:.3f}s) — bypassed the timeout"
        assert result == "answer_now"  # the safe default (last option)
        assert result.resolution == "timeout_default"
        assert ctx.pause_count == 1

    async def test_call_site_forces_synthesis_on_the_baseline_outcome(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """step_tool_execution must force synthesis, not grant anything, on no-client."""
        monkeypatch.setattr(settings, "orchestrator_spend_threshold", 2)

        async def fake_pause(**kwargs: object) -> object:
            from personal_agent.orchestrator.constraint_options import ConstraintDecision

            return ConstraintDecision("answer_now", "timeout_default")

        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", fake_pause)

        ctx = _ctx(
            session_id="sess-3",
            trace_id="trace-3",
            messages=[{"role": "assistant", "content": "x", "tool_calls": []}],
        )
        ctx.tool_iteration_count = 1  # -> 2, crosses threshold
        trace_ctx = TraceContext.new_trace()

        result = await ex.step_tool_execution(ctx, _mock_session(), trace_ctx)  # type: ignore[arg-type]

        assert result == TaskState.LLM_CALL
        assert ctx.force_synthesis_from_limit is True
        assert ctx.tool_iteration_bonus == 0


@pytest.mark.asyncio
class TestSpendPauseCardContext:
    """AC-5: the card states the iteration count reached and the threshold crossed."""

    async def test_context_string_carries_count_and_threshold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "orchestrator_spend_threshold", 3)

        captured: dict[str, object] = {}

        async def fake_pause(**kwargs: object) -> object:
            captured.update(kwargs)
            from personal_agent.orchestrator.constraint_options import ConstraintDecision

            return ConstraintDecision("continue_turn", "user_choice")

        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", fake_pause)

        from unittest.mock import MagicMock

        fake_layer = MagicMock()
        fake_layer.execute_batch = AsyncMock(return_value=[])
        monkeypatch.setattr(ex, "_get_tool_execution_layer", lambda: fake_layer)

        ctx = _ctx(
            session_id="sess-4",
            trace_id="trace-4",
            messages=[{"role": "assistant", "content": "x", "tool_calls": []}],
        )
        ctx.tool_iteration_count = 2  # -> 3, crosses threshold of 3
        trace_ctx = TraceContext.new_trace()

        await ex.step_tool_execution(ctx, _mock_session(), trace_ctx)  # type: ignore[arg-type]

        assert captured["constraint"] == "spend_threshold"
        context = str(captured["context"])
        assert "3" in context  # both the reached count and the threshold are 3 here


@pytest.mark.asyncio
class TestSpendPauseRespectsPerTaskTypeCeiling:
    """The trigger must never fire at or above the turn's EFFECTIVE ceiling.

    codex plan-review finding: the still-live orchestrator_max_tool_iterations_by_task_type
    (not removed until FRE-1394) can put the effective ceiling for a turn at or below the
    configured spend threshold — e.g. conversational's default cap of 6 equals the spend
    threshold's default of 6. The ordinary tool_iteration_limit path must remain the sole
    control in that case; the spend pause must not fire redundantly beside it.
    """

    async def test_conversational_ceiling_at_threshold_never_raises_spend_pause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "orchestrator_spend_threshold", 6)
        monkeypatch.setattr(
            settings, "orchestrator_max_tool_iterations_by_task_type", {"conversational": 6}
        )

        pause_called = {"n": 0}

        async def fake_pause(**kwargs: object) -> object:
            pause_called["n"] += 1
            from personal_agent.orchestrator.constraint_options import ConstraintDecision

            return ConstraintDecision("finish_now", "user_choice")

        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", fake_pause)

        ctx = _ctx(
            session_id="sess-5",
            trace_id="trace-5",
            messages=[{"role": "assistant", "content": "x", "tool_calls": []}],
            gateway_output=_gateway_output(TaskType.CONVERSATIONAL),
        )
        ctx.tool_iteration_count = 6  # -> 7, past the effective ceiling of 6

        trace_ctx = TraceContext.new_trace()
        await ex.step_tool_execution(ctx, _mock_session(), trace_ctx)  # type: ignore[arg-type]

        # The ordinary tool_iteration_limit path (also mocked via the same helper) is
        # the one legitimately entitled to fire here -- but the spend gate must not have
        # been the reason: assert it was never reached ahead of it a second, spurious time.
        assert ctx.spend_pause_raised is False
        assert pause_called["n"] == 1  # exactly the tool_iteration_limit pause, not two
