"""Tests for FRE-1375: the stop button must abort an in-flight generation.

Verifies:
- step_llm_call races the primary call against the session's cancel event
  (transport.agui.ws_endpoint.get_cancel_event), not just the turn deadline
  — AC-1: a cancel arriving mid-call stops the turn, without waiting for the
  call to finish.
- The cancelled call never completes and the turn never issues another model
  call as a consequence — AC-3.
- A cancel already set before the call starts wins even against an
  instantly-resolving response — the deliberate tie-break in step_llm_call.
- The turn's terminal state is user-visible (CANCELLED event + a real
  final_reply) — AC-4.
- step_tool_execution's between-rounds checkpoint no longer routes back
  through another LLM_CALL.
- step_synthesis skips grounding verification/retry for a stopped-early
  turn, closing the gap that would otherwise let enforce mode's retry path
  issue exactly the extra call AC-3 forbids.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import personal_agent.orchestrator.executor as ex
from personal_agent.config import settings
from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.types import ExecutionContext, TaskState
from personal_agent.telemetry.trace import TraceContext
from personal_agent.transport.agui import ws_endpoint


@pytest.fixture(autouse=True)
def _restore_executor_tool_globals() -> object:
    """Restore executor's lazily-cached registry globals after each test (mirrors
    the identical fixture in test_turn_deadline_guard.py / test_content_widening.py).
    """
    saved_registry = ex._tool_registry
    saved_layer = ex._tool_execution_layer
    yield
    ex._tool_registry = saved_registry
    ex._tool_execution_layer = saved_layer


@pytest.fixture(autouse=True)
def _clean_cancel_events() -> object:
    """Session cancel events are a module-level dict in ws_endpoint — never let
    one test's session_id leak a set event into another test.
    """
    yield
    ws_endpoint._session_cancel_events.clear()


def _make_ctx(**overrides: object) -> ExecutionContext:
    defaults: dict[str, object] = {
        "session_id": "sess-cancel-001",
        "trace_id": "trace-cancel-001",
        "user_message": "seven day budget analysis",
        "mode": Mode.NORMAL,
        "channel": Channel.CHAT,
        "messages": [{"role": "user", "content": "seven day budget analysis"}],
    }
    defaults.update(overrides)
    return ExecutionContext(**defaults)  # type: ignore[arg-type]


def _register_connection(session_id: str) -> ws_endpoint._ConnectionState:
    """Register a minimal active connection — the precondition _get_cancel_event
    checks before racing (only a connected session can ever receive USER_CANCEL).
    """
    conn = ws_endpoint._ConnectionState(
        websocket=MagicMock(),
        user=MagicMock(),
        session_id=session_id,
        outbound_queue=asyncio.Queue(),
    )
    ws_endpoint._active_connections[session_id] = conn
    return conn


def _mock_llm_client(respond: AsyncMock) -> MagicMock:
    mock_client = MagicMock()
    mock_client.respond = respond
    mock_client.model_configs = {}
    return mock_client


def _mock_session() -> MagicMock:
    mock_session = MagicMock()
    mock_session.add_message = AsyncMock()
    mock_session.get_messages = AsyncMock(return_value=[])
    return mock_session


def _step_llm_call_patches(mock_llm: MagicMock) -> contextlib.ExitStack:
    stack = contextlib.ExitStack()
    stack.enter_context(
        patch("personal_agent.orchestrator.skills.get_skill_bodies", return_value=("", ()))
    )
    stack.enter_context(
        patch("personal_agent.orchestrator.skills.assemble_skill_index", return_value="")
    )
    stack.enter_context(
        patch(
            "personal_agent.orchestrator.skills.assemble_skill_index_directive",
            return_value="",
        )
    )
    stack.enter_context(
        patch(
            "personal_agent.orchestrator.skills.assemble_skill_usage_directives",
            return_value="",
        )
    )
    stack.enter_context(patch("personal_agent.orchestrator.skills.get_all_skills", return_value={}))
    stack.enter_context(
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=mock_llm)
    )
    stack.enter_context(
        patch(
            "personal_agent.orchestrator.executor.get_default_registry",
            return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
        )
    )
    return stack


# ---------------------------------------------------------------------------
# step_llm_call — mid-call cancellation (AC-1, AC-3, AC-4)
# ---------------------------------------------------------------------------


class TestMidCallCancellation:
    @pytest.fixture(autouse=True)
    def _active_connection(self) -> object:
        """These tests exercise the race path, which requires a live connection
        (FRE-1375's fix for the phantom-cancel-on-unrelated-tests regression).
        """
        _register_connection("sess-cancel-001")
        yield
        ws_endpoint._active_connections.pop("sess-cancel-001", None)

    @pytest.mark.asyncio
    async def test_cancel_mid_call_stops_without_waiting_for_the_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cancel fired partway through a slow call aborts it immediately —
        the call is never allowed to complete, unlike the pre-fix behaviour
        where the flag was only read between tool rounds.
        """
        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 30)
        ctx = _make_ctx()
        ctx.tool_results.append({"tool_name": "query_es", "success": True})  # type: ignore[attr-defined]

        completed = {"value": False}
        cancel_event = ws_endpoint.get_cancel_event(ctx.session_id)

        async def slow_respond(**_kwargs: object) -> dict[str, object]:
            await asyncio.sleep(0.05)
            cancel_event.set()  # simulate USER_CANCEL arriving mid-generation
            await asyncio.sleep(5.0)
            completed["value"] = True  # must never be reached
            return {
                "content": "final answer",
                "tool_calls": [],
                "response_id": None,
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

        respond_mock = AsyncMock(side_effect=slow_respond)
        mock_llm = _mock_llm_client(respond_mock)
        trace_ctx = TraceContext.new_trace()

        with (
            _step_llm_call_patches(mock_llm),
            patch(
                "personal_agent.orchestrator.executor._emit_turn_cancelled",
                new=AsyncMock(),
            ) as emit_mock,
        ):
            result = await asyncio.wait_for(
                ex.step_llm_call(ctx, _mock_session(), trace_ctx),  # type: ignore[arg-type]
                timeout=5.0,
            )

        assert result == TaskState.SYNTHESIS
        assert completed["value"] is False, "the call must be aborted, not allowed to finish"
        assert emit_mock.await_count == 1, "AC-4: the cancellation must be emitted"
        assert ctx.final_reply is not None
        assert "stopped" in ctx.final_reply.lower()
        assert "query_es" in ctx.final_reply
        assert ctx.turn_stopped_early is True

    @pytest.mark.asyncio
    async def test_cancel_already_set_wins_even_against_a_fast_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deliberate tie-break: once Stop was pressed, a response landing in
        the same instant must not be delivered.
        """
        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 30)
        ctx = _make_ctx()
        ws_endpoint.get_cancel_event(ctx.session_id).set()  # cancelled before the call starts

        respond_mock = AsyncMock(
            return_value={
                "content": "instant answer",
                "tool_calls": [],
                "response_id": None,
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )
        mock_llm = _mock_llm_client(respond_mock)
        trace_ctx = TraceContext.new_trace()

        with _step_llm_call_patches(mock_llm):
            result = await ex.step_llm_call(ctx, _mock_session(), trace_ctx)  # type: ignore[arg-type]

        assert result == TaskState.SYNTHESIS
        assert ctx.final_reply is not None
        assert "instant answer" not in ctx.final_reply

    @pytest.mark.asyncio
    async def test_no_cancel_falls_back_to_normal_delivery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: an uncancelled turn with a session_id (so the race
        path IS taken) still delivers the model's real answer.
        """
        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 30)
        ctx = _make_ctx()

        respond_mock = AsyncMock(
            return_value={
                "content": "the real answer",
                "tool_calls": [],
                "response_id": None,
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )
        mock_llm = _mock_llm_client(respond_mock)
        trace_ctx = TraceContext.new_trace()

        with _step_llm_call_patches(mock_llm):
            result = await ex.step_llm_call(ctx, _mock_session(), trace_ctx)  # type: ignore[arg-type]

        assert result == TaskState.SYNTHESIS
        assert ctx.final_reply == "the real answer"
        assert ctx.turn_stopped_early is False

    @pytest.mark.asyncio
    async def test_no_session_id_uses_plain_wait_for(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No session_id (e.g. a background/system call) — no cancel source is
        possible, so the original single-timeout path runs, unchanged.
        """
        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 30)
        ctx = _make_ctx(session_id=None)

        respond_mock = AsyncMock(
            return_value={
                "content": "no session answer",
                "tool_calls": [],
                "response_id": None,
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )
        mock_llm = _mock_llm_client(respond_mock)
        trace_ctx = TraceContext.new_trace()

        with _step_llm_call_patches(mock_llm):
            result = await ex.step_llm_call(ctx, _mock_session(), trace_ctx)  # type: ignore[arg-type]

        assert result == TaskState.SYNTHESIS
        assert ctx.final_reply == "no session answer"


class TestNoActiveConnectionDoesNotTouchCancelMachinery:
    """Regression guard: a session_id with no live WS connection (almost every
    orchestrator unit test, and any non-interactive/background call) must never
    create or race an asyncio.Event for cancellation.

    This is what protected the OLD single-source asyncio.wait_for path from ever
    seeing cross-test contamination. The FIRST version of this fix (gating only
    on ``ctx.session_id`` truthiness) broke it: pytest-asyncio's per-test event
    loops made a reused, never-cleared ``asyncio.Event`` from a prior test's
    closed loop resolve as spuriously "done" the instant a later, wholly
    unrelated test raced it again with the same session_id — surfacing as a
    phantom cancellation with no cancel ever sent. Discovered via
    tests/test_orchestrator/test_executor.py failing only when run after another
    test using the same "test-session" id, never in isolation.
    """

    @pytest.mark.asyncio
    async def test_uncancellable_session_delivers_normally_and_touches_no_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 30)
        session_id = "sess-no-connection"
        assert ws_endpoint.get_active_connection(session_id) is None
        ctx = _make_ctx(session_id=session_id)

        respond_mock = AsyncMock(
            return_value={
                "content": "delivered without a connection",
                "tool_calls": [],
                "response_id": None,
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )
        mock_llm = _mock_llm_client(respond_mock)
        trace_ctx = TraceContext.new_trace()

        with _step_llm_call_patches(mock_llm):
            result = await ex.step_llm_call(ctx, _mock_session(), trace_ctx)  # type: ignore[arg-type]

        assert result == TaskState.SYNTHESIS
        assert ctx.final_reply == "delivered without a connection"
        assert session_id not in ws_endpoint._session_cancel_events, (
            "no connection means no possible cancel source — the race must never "
            "be entered, so no Event should be created for this session at all"
        )


# ---------------------------------------------------------------------------
# step_tool_execution — between-rounds checkpoint no longer re-enters LLM_CALL
# ---------------------------------------------------------------------------


class TestToolExecutionCancelCheckpoint:
    @pytest.mark.asyncio
    async def test_cancel_between_rounds_goes_straight_to_synthesis(self) -> None:
        ctx = _make_ctx(session_id="sess-cancel-checkpoint")
        ctx.tool_results.append({"tool_name": "query_es", "success": True})  # type: ignore[attr-defined]
        conn = ws_endpoint._ConnectionState(
            websocket=MagicMock(),
            user=MagicMock(),
            session_id=ctx.session_id,
            outbound_queue=asyncio.Queue(),
            cancel_requested=True,
        )
        ws_endpoint._active_connections[ctx.session_id] = conn
        try:
            with patch(
                "personal_agent.orchestrator.executor._emit_turn_cancelled",
                new=AsyncMock(),
            ):
                result = await ex.step_tool_execution(
                    ctx, _mock_session(), TraceContext.new_trace()
                )  # type: ignore[arg-type]
        finally:
            ws_endpoint._active_connections.pop(ctx.session_id, None)

        assert result == TaskState.SYNTHESIS
        assert ctx.force_synthesis_from_limit is False
        assert ctx.turn_stopped_early is True
        assert ctx.final_reply is not None


# ---------------------------------------------------------------------------
# step_synthesis — grounding is skipped for a stopped-early turn
# ---------------------------------------------------------------------------


class TestGroundingSkippedWhenStoppedEarly:
    @pytest.mark.asyncio
    async def test_enforce_mode_does_not_retry_a_stopped_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard for the codex plan-review finding: enforce mode's
        retry path (back to TaskState.LLM_CALL) must never fire for a turn
        that was stopped early — that would silently reintroduce the extra
        model call AC-3 forbids.
        """
        monkeypatch.setattr(settings, "grounding_verification_mode", "enforce")
        ctx = _make_ctx()
        ctx.final_reply = "Stopped before gathering any results."
        ctx.turn_stopped_early = True

        verify_mock = AsyncMock()
        session_manager = MagicMock()
        session_manager.update_session = MagicMock()

        with patch("personal_agent.orchestrator.executor._verify_grounding", verify_mock):
            result = await ex.step_synthesis(ctx, session_manager, TraceContext.new_trace())  # type: ignore[arg-type]

        assert result == TaskState.COMPLETED
        assert not verify_mock.called
        assert ctx.final_reply == "Stopped before gathering any results."
