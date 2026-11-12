"""FRE-1776 — Stop must end a turn while sub-agents run.

FRE-1375 raced the session's cancel event against the primary's own model call
only. During a HYBRID/DECOMPOSE fan-out the executor awaited the whole expansion
with no race, so a Stop waited until every sub-agent finished (87 s observed).

These tests drive ``step_init`` through the real ``ExpansionController`` and,
for AC-2, the real ``run_sub_agent``; only the model clients are faked.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import structlog.testing

import personal_agent.orchestrator.executor as ex
from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.sub_agent_types import SubAgentResult
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
from personal_agent.transport.agui import ws_endpoint

_SESSION = "0f6c1d2e-1776-4c00-9a00-000000001776"
_TRACE = "trace-fre1776"
_DEADLINE_S = 2.0  # AC-1: the turn ends within 2 seconds of the cancel.


@pytest.fixture(autouse=True)
def _clean_cancel_state() -> object:
    """Never let one test's cancel event or connection leak into another."""
    yield
    ws_endpoint._session_cancel_events.clear()
    ws_endpoint._active_connections.pop(_SESSION, None)


def _register_connection() -> asyncio.Event:
    """Register a connection and its cancel event, as the WS connect handler does."""
    conn = ws_endpoint._ConnectionState(
        websocket=MagicMock(),
        user=MagicMock(),
        session_id=_SESSION,
        outbound_queue=asyncio.Queue(),
    )
    ws_endpoint._active_connections[_SESSION] = conn
    return ws_endpoint._get_or_create_cancel_event(_SESSION)


def _ctx() -> ExecutionContext:
    gateway_output = GatewayOutput(
        intent=IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        ),
        governance=GovernanceContext(mode=Mode.NORMAL, expansion_permitted=True),
        decomposition=DecompositionResult(
            strategy=DecompositionStrategy.HYBRID,
            reason="test",
            constraints={"max_sub_agents": 2},
        ),
        context=AssembledContext(
            messages=[{"role": "user", "content": "research these two things"}],
            memory_context=None,
            tool_definitions=None,
        ),
        session_id=_SESSION,
        trace_id=_TRACE,
    )
    return ExecutionContext(
        session_id=_SESSION,
        trace_id=_TRACE,
        user_message="research these two things",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        gateway_output=gateway_output,
        user_id=uuid4(),
    )


def _session_manager() -> MagicMock:
    session_manager = MagicMock()
    session_manager.get_session = MagicMock(return_value=None)
    return session_manager


def _plan_json(tasks: int = 2) -> str:
    return json.dumps(
        {
            "strategy": "HYBRID",
            "tasks": [
                {"name": f"task_{i}", "goal": f"Goal for task {i}", "type": "general"}
                for i in range(tasks)
            ],
        }
    )


def _sub_result(task_name: str) -> SubAgentResult:
    return SubAgentResult(
        task_id=uuid4(),
        spec_task=task_name,
        summary="the findings",
        full_output="the findings",
        tools_used=[],
        token_count=10,
        duration_ms=1.0,
        success=True,
        tool_iterations=3,
        tool_result_chars_absorbed=1000,
    )


def _patch_clients(monkeypatch: pytest.MonkeyPatch, sub_agent_client: Any) -> None:
    """The planner (PRIMARY) returns a two-task plan; SUB_AGENT gets ``sub_agent_client``."""
    planner = AsyncMock()
    planner.respond = AsyncMock(return_value={"content": _plan_json(), "cost_usd": 0.0})

    def _get_llm_client(role_name: str | None = None, mode: str | None = None) -> Any:
        return sub_agent_client if role_name == "sub_agent" else planner

    monkeypatch.setattr("personal_agent.llm_client.factory.get_llm_client", _get_llm_client)


class _PendingSubAgents:
    """A fake ``run_sub_agent`` whose first call sets the cancel event, then hangs."""

    def __init__(self, cancel_event: asyncio.Event | None) -> None:
        self.cancel_event = cancel_event
        self.started: list[str] = []
        self.cancelled: list[str] = []
        self.completed: list[str] = []

    async def __call__(self, *, spec: Any, **_kwargs: Any) -> SubAgentResult:
        self.started.append(spec.task)
        try:
            if self.cancel_event is not None:
                self.cancel_event.set()  # USER_CANCEL arrives while the call is pending
            await asyncio.sleep(30.0)
        except asyncio.CancelledError:
            self.cancelled.append(spec.task)
            raise
        self.completed.append(spec.task)
        return _sub_result(spec.task)


async def _run_step_init(ctx: ExecutionContext) -> TaskState:
    return await asyncio.wait_for(
        ex.step_init(ctx, _session_manager(), TraceContext(trace_id=_TRACE, session_id=_SESSION)),
        timeout=_DEADLINE_S,
    )


class TestAC1CancelDuringSubAgentEndsTheTurn:
    """AC-1 — a cancel during a pending sub-agent call ends the turn as cancelled."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fan_out_limit", [1, 2], ids=["serial", "side_by_side"])
    async def test_cancel_mid_sub_agent_stops_the_turn(
        self, monkeypatch: pytest.MonkeyPatch, fan_out_limit: int
    ) -> None:
        """Turn ends cancelled; in-flight sub-agents cancelled; queued ones never start."""
        cancel_event = _register_connection()
        fake = _PendingSubAgents(cancel_event)
        monkeypatch.setattr("personal_agent.orchestrator.expansion_controller.run_sub_agent", fake)
        monkeypatch.setattr(
            "personal_agent.orchestrator.expansion_controller._fan_out_limit",
            lambda *_a, **_k: fan_out_limit,
        )
        _patch_clients(monkeypatch, MagicMock())
        emit_mock = AsyncMock()
        monkeypatch.setattr(ex, "_emit_turn_cancelled", emit_mock)
        ctx = _ctx()

        with structlog.testing.capture_logs() as logs:
            state = await _run_step_init(ctx)

        assert state == TaskState.SYNTHESIS
        assert emit_mock.await_count == 1
        assert ctx.turn_stopped_early is True
        assert ctx.final_reply is not None and "stopped" in ctx.final_reply.lower()
        assert fake.completed == []
        assert fake.started and sorted(fake.cancelled) == sorted(fake.started), (
            "in-flight cancelled"
        )
        if fan_out_limit == 1:
            assert len(fake.started) == 1, "a not-yet-started serial task never starts"
        events = [e for e in logs if e["event"] == "user_cancel_during_sub_agents"]
        assert len(events) == 1
        assert events[0]["trace_id"] == _TRACE
        assert events[0]["session_id"] == _SESSION


class TestAC2CancelledSubAgentReleasesSlotAndAudits:
    """AC-2 — a cancelled sub-agent writes its audit record and releases its slot."""

    @pytest.mark.asyncio
    async def test_audit_record_written_and_slot_released(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real ``run_sub_agent`` cancel handler runs; the held slot is freed."""
        import personal_agent.orchestrator.sub_agent as sa

        cancel_event = _register_connection()
        captured: list[Any] = []
        monkeypatch.setattr(sa, "write_sub_agent_capture", lambda cap: captured.append(cap))

        slot = asyncio.Semaphore(1)
        calls: list[int] = []

        async def _respond(**_kwargs: Any) -> dict[str, Any]:
            async with slot:  # the client's concurrency slot is held during the call
                calls.append(1)
                cancel_event.set()
                await asyncio.sleep(30.0)
            return {"content": "never", "tool_calls": [], "cost_usd": 0.0}

        sub_client = MagicMock()
        sub_client.respond = _respond
        sub_client.model_configs = {}
        _patch_clients(monkeypatch, sub_client)
        monkeypatch.setattr(ex, "_emit_turn_cancelled", AsyncMock())
        ctx = _ctx()

        state = await _run_step_init(ctx)

        assert state == TaskState.SYNTHESIS
        assert len(calls) == 1, "only the first sub-agent started"
        assert slot.locked() is False, "the cancelled call released its slot"
        assert len(captured) == 1
        assert captured[0].success is False
        assert "cancel" in (captured[0].error or "").lower()


class TestAC3NoCancelKeepsTodaysResult:
    """AC-3 — with no cancel, the fan-out runs to the end and the turn continues."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("connected", [True, False], ids=["event_exists", "no_event"])
    async def test_no_cancel_runs_every_sub_agent(
        self, monkeypatch: pytest.MonkeyPatch, connected: bool
    ) -> None:
        """``connected=False``: ``get_cancel_event`` returns None — the path is unchanged."""
        if connected:
            _register_connection()

        async def _fast(*, spec: Any, **_kwargs: Any) -> SubAgentResult:
            return _sub_result(spec.task)

        monkeypatch.setattr("personal_agent.orchestrator.expansion_controller.run_sub_agent", _fast)
        _patch_clients(monkeypatch, MagicMock())
        emit_mock = AsyncMock()
        monkeypatch.setattr(ex, "_emit_turn_cancelled", emit_mock)
        ctx = _ctx()

        state = await _run_step_init(ctx)

        assert state == TaskState.LLM_CALL
        assert emit_mock.await_count == 0
        assert ctx.turn_stopped_early is False
        assert len(ctx.sub_agent_results) == 2
        assert ctx.synthesis_appended is True
        if not connected:
            assert ws_endpoint.get_cancel_event(_SESSION) is None, "never created"
