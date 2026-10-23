"""Tests for ADR-0143 D4 — the pre-step lifetime check in the state-driver loop (FRE-1409).

Verifies:
- AC-1: every non-terminal state except SYNTHESIS stops at the cap, and the turn ends with a reply.
- AC-2: SYNTHESIS runs after the cap and does not loop.
- AC-3: a tool in flight is not cancelled by the cap.
- AC-4: the setting's description names the four bind points and points to the service bound.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

import personal_agent.orchestrator.executor as ex
from personal_agent.config import settings
from personal_agent.config.settings import AppConfig
from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.types import ExecutionContext, TaskState
from personal_agent.telemetry.trace import TraceContext

_CAP_SECONDS = 100

_NON_SYNTHESIS_STATES = [
    TaskState.INIT,
    TaskState.PLANNING,
    TaskState.LLM_CALL,
    TaskState.TOOL_EXECUTION,
]


class _FakeClock:
    """Stands in for ``executor.time``: a controllable ``monotonic``, real everything else.

    Patching ``time.monotonic`` globally would also move the asyncio loop's clock.
    """

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name: str) -> object:
        return getattr(time, name)


@asynccontextmanager
async def _no_topology(_ctx: ExecutionContext) -> AsyncIterator[None]:
    yield


def _ctx(state: TaskState, *, started: float) -> ExecutionContext:
    ctx = ExecutionContext(  # type: ignore[arg-type]
        session_id="sess-fre1409",
        trace_id="trace-fre1409",
        user_message="hi",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )
    ctx.state = state
    ctx.turn_started_monotonic = started
    return ctx


class _Harness:
    """Records which steps the driver loop dispatches and every LLM call."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.dispatched: list[TaskState] = []
        self.llm = MagicMock()
        self.llm.respond = AsyncMock()
        self.session_manager = MagicMock()
        self.stop_helper_calls = 0
        self._monkeypatch = monkeypatch

        monkeypatch.setattr(settings, "orchestrator_turn_lifetime_seconds", _CAP_SECONDS)
        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 900)
        monkeypatch.setattr(settings, "request_monitoring_enabled", False)
        monkeypatch.setattr(settings, "grounding_verification_mode", "off")
        monkeypatch.setattr(ex, "observe_topology", _no_topology)
        # Post-completion side effects: capture, event bus, reflection.
        monkeypatch.setattr(ex, "_write_task_capture", MagicMock())
        monkeypatch.setattr(ex, "_trigger_captains_log_reflection", MagicMock())
        monkeypatch.setattr("personal_agent.captains_log.background.run_in_background", MagicMock())
        monkeypatch.setattr("personal_agent.events.bus.get_event_bus", MagicMock())

        real_stop = ex._stop_turn_for_lifetime_cap

        def counting_stop(ctx: ExecutionContext) -> None:
            self.stop_helper_calls += 1
            real_stop(ctx)

        monkeypatch.setattr(ex, "_stop_turn_for_lifetime_cap", counting_stop)

        real_synthesis = ex.step_synthesis

        async def recording_synthesis(
            ctx: ExecutionContext, sm: object, trace_ctx: TraceContext
        ) -> TaskState:
            self.dispatched.append(TaskState.SYNTHESIS)
            return await real_synthesis(ctx, sm, trace_ctx)  # type: ignore[arg-type]

        monkeypatch.setattr(ex, "step_synthesis", recording_synthesis)
        for name, state in (
            ("step_init", TaskState.INIT),
            ("step_planning", TaskState.PLANNING),
            ("step_llm_call", TaskState.LLM_CALL),
            ("step_tool_execution", TaskState.TOOL_EXECUTION),
        ):
            self.script(name, state, lambda _ctx: TaskState.SYNTHESIS)

    def script(
        self,
        name: str,
        state: TaskState,
        next_state: Callable[[ExecutionContext], TaskState],
    ) -> None:
        """Replace a step: record the dispatch, call the LLM if it is LLM_CALL, move on."""

        async def step(ctx: ExecutionContext, _sm: object, _trace: TraceContext) -> TaskState:
            self.dispatched.append(state)
            if state is TaskState.LLM_CALL:
                await self.llm.respond()
            return next_state(ctx)

        self._monkeypatch.setattr(ex, name, step)

    async def run(self, ctx: ExecutionContext) -> ExecutionContext:
        # Bounded: a loop that fails to end the turn must fail the test, not hang it.
        return await asyncio.wait_for(ex.execute_task(ctx, self.session_manager), timeout=10)


@pytest.mark.asyncio
class TestEveryStateStopsAtTheCap:
    """AC-1: no non-terminal state dispatches its step after the cap."""

    @pytest.mark.parametrize("state", _NON_SYNTHESIS_STATES, ids=lambda s: s.value)
    async def test_state_is_not_dispatched_after_the_cap(
        self, monkeypatch: pytest.MonkeyPatch, state: TaskState
    ) -> None:
        """Cap passed before dispatch: only SYNTHESIS runs, with a reply and no LLM call."""
        clock = _FakeClock()
        monkeypatch.setattr(ex, "time", clock)
        harness = _Harness(monkeypatch)
        ctx = _ctx(state, started=clock.now)
        clock.now += _CAP_SECONDS + 1

        result = await harness.run(ctx)

        assert result.state is TaskState.COMPLETED
        assert result.turn_stopped_early is True
        assert result.final_reply
        assert "lifetime cap" in result.final_reply
        assert harness.dispatched == [TaskState.SYNTHESIS]
        harness.llm.respond.assert_not_called()

    async def test_cap_passing_between_two_steps_stops_before_the_next(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INIT runs inside the cap and spends it; LLM_CALL is then never dispatched."""
        clock = _FakeClock()
        monkeypatch.setattr(ex, "time", clock)
        harness = _Harness(monkeypatch)

        def init_spends_the_cap(_ctx: ExecutionContext) -> TaskState:
            clock.now += _CAP_SECONDS + 1
            return TaskState.LLM_CALL

        harness.script("step_init", TaskState.INIT, init_spends_the_cap)
        ctx = _ctx(TaskState.INIT, started=clock.now)

        result = await harness.run(ctx)

        assert result.state is TaskState.COMPLETED
        assert result.turn_stopped_early is True
        assert harness.dispatched == [TaskState.INIT, TaskState.SYNTHESIS]
        harness.llm.respond.assert_not_called()

    async def test_open_step_span_is_closed_when_the_check_skips_tool_execution(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LLM_CALL leaves its span open for TOOL_EXECUTION. The skip must still close it."""
        clock = _FakeClock()
        monkeypatch.setattr(ex, "time", clock)
        harness = _Harness(monkeypatch)
        opened: list[object] = []
        closed: list[object] = []
        real_open, real_close = ex.open_step_span, ex.close_step_span

        def spy_open(**kw: object) -> tuple[object, object]:
            span_and_token = real_open(**kw)  # type: ignore[arg-type]
            opened.append(span_and_token[0])
            return span_and_token

        def spy_close(span: object, token: object, **kw: object) -> None:
            closed.append(span)
            real_close(span, token, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(ex, "open_step_span", spy_open)
        monkeypatch.setattr(ex, "close_step_span", spy_close)

        def llm_call_spends_the_cap(_ctx: ExecutionContext) -> TaskState:
            clock.now += _CAP_SECONDS + 1
            return TaskState.TOOL_EXECUTION

        harness.script("step_llm_call", TaskState.LLM_CALL, llm_call_spends_the_cap)
        ctx = _ctx(TaskState.LLM_CALL, started=clock.now)

        result = await harness.run(ctx)

        assert result.state is TaskState.COMPLETED
        assert harness.dispatched == [TaskState.LLM_CALL, TaskState.SYNTHESIS]
        assert len(opened) == 1
        assert closed == opened

    async def test_inside_the_cap_every_step_still_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The check must not fire early: with time left, the scripted path runs unchanged."""
        clock = _FakeClock()
        monkeypatch.setattr(ex, "time", clock)
        harness = _Harness(monkeypatch)
        harness.script("step_init", TaskState.INIT, lambda _ctx: TaskState.LLM_CALL)
        harness.script("step_llm_call", TaskState.LLM_CALL, lambda _ctx: TaskState.SYNTHESIS)
        ctx = _ctx(TaskState.INIT, started=clock.now)

        result = await harness.run(ctx)

        assert result.state is TaskState.COMPLETED
        assert result.turn_stopped_early is False
        assert harness.dispatched == [TaskState.INIT, TaskState.LLM_CALL, TaskState.SYNTHESIS]
        assert harness.stop_helper_calls == 0


@pytest.mark.asyncio
class TestSynthesisIsExempt:
    """AC-2: SYNTHESIS runs after the cap, once, and the check does not rewrite its reply."""

    async def test_synthesis_runs_once_with_the_cap_exhausted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _FakeClock()
        monkeypatch.setattr(ex, "time", clock)
        harness = _Harness(monkeypatch)
        ctx = _ctx(TaskState.SYNTHESIS, started=clock.now)
        ctx.final_reply = "the real answer"
        clock.now += _CAP_SECONDS + 1

        result = await harness.run(ctx)

        assert result.state is TaskState.COMPLETED
        assert harness.dispatched == [TaskState.SYNTHESIS]
        assert harness.stop_helper_calls == 0
        assert result.turn_stopped_early is False
        assert result.final_reply == "the real answer"

    async def test_exhausted_turn_reaches_synthesis_once_and_does_not_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stop helper runs once for the whole turn, not once per loop pass."""
        clock = _FakeClock()
        monkeypatch.setattr(ex, "time", clock)
        harness = _Harness(monkeypatch)
        ctx = _ctx(TaskState.LLM_CALL, started=clock.now)
        clock.now += _CAP_SECONDS + 1

        result = await harness.run(ctx)

        assert result.state is TaskState.COMPLETED
        assert harness.dispatched.count(TaskState.SYNTHESIS) == 1
        assert harness.stop_helper_calls == 1


@pytest.mark.asyncio
class TestToolInFlightIsNotCancelled:
    """AC-3: the cap bounds the next step. It does not cancel a tool already running."""

    async def test_slow_tool_completes_and_its_result_is_salvaged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(monkeypatch)
        # Real step_tool_execution, real clock: the cap expires 0.1 s into a 0.5 s tool.

        async def recording_tool_step(
            ctx: ExecutionContext, sm: object, trace_ctx: TraceContext
        ) -> TaskState:
            harness.dispatched.append(TaskState.TOOL_EXECUTION)
            return await _REAL_STEP_TOOL_EXECUTION(ctx, sm, trace_ctx)

        monkeypatch.setattr(ex, "step_tool_execution", recording_tool_step)
        harness.script("step_llm_call", TaskState.LLM_CALL, lambda _ctx: TaskState.SYNTHESIS)
        monkeypatch.setattr(ex, "_get_tool_execution_layer", lambda: object())
        monkeypatch.setattr(ex, "_is_turn_cancelled", lambda _sid: False)
        monkeypatch.setattr(ex, "_report_turn_progress", AsyncMock())

        tool_events: list[str] = []

        async def slow_dispatch(
            tool_call_id: str,
            tool_name: str,
            arguments: object,
            args_hash: str,
            gate_result: object,
            loop_policy: object,
            **_kw: object,
        ) -> dict[str, object]:
            tool_events.append("started")
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                tool_events.append("cancelled")
                raise
            tool_events.append("finished")
            return {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "content": json.dumps({"status": "ok"}),
                "success": True,
                "latency_ms": 500.0,
                "output_hash": None,
                "gate_result": gate_result,
                "args_hash": args_hash,
                "loop_policy": loop_policy,
                "tool_layer_output": None,
                "tool_layer_error": None,
            }

        monkeypatch.setattr(ex, "dispatch_tool_call", slow_dispatch)

        ctx = _ctx(TaskState.TOOL_EXECUTION, started=time.monotonic() - (_CAP_SECONDS - 0.1))
        ctx.messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tc-1", "function": {"name": "slow_tool", "arguments": "{}"}}
                ],
            }
        ]

        result = await harness.run(ctx)

        assert tool_events == ["started", "finished"]
        assert [r["tool_name"] for r in result.tool_results] == ["slow_tool"]
        assert result.state is TaskState.COMPLETED
        assert result.turn_stopped_early is True
        assert result.final_reply is not None
        assert "slow_tool: success" in result.final_reply
        assert harness.dispatched == [TaskState.TOOL_EXECUTION, TaskState.SYNTHESIS]
        harness.llm.respond.assert_not_called()


_REAL_STEP_TOOL_EXECUTION: Callable[
    [ExecutionContext, object, TraceContext], Awaitable[TaskState]
] = ex.step_tool_execution


class TestDescriptionNamesTheBindPoints:
    """AC-4: the description says what the code does."""

    def test_names_all_four_bind_points_and_the_service_bound(self) -> None:
        description = AppConfig.model_fields["orchestrator_turn_lifetime_seconds"].description
        assert description is not None
        for bind_point in (
            "in-flight primary call",
            "constraint pause",
            "tool-iteration-limit gate",
            "pre-step check",
        ):
            assert bind_point in description
        assert "service_request_task_lifetime_seconds" in description

    def test_no_unqualified_cap_on_a_turn_claim(self) -> None:
        description = AppConfig.model_fields["orchestrator_turn_lifetime_seconds"].description
        assert description is not None
        assert "cap on a turn" not in description
