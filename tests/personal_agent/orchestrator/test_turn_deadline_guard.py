"""Tests for the FRE-973 turn-level wall-clock deadline guard.

Verifies:
- step_llm_call skips the LLM call entirely once the turn's wall-clock budget
  (settings.orchestrator_task_timeout_seconds) is already exhausted, salvaging
  from ctx.tool_results instead of attempting a call.
- An in-flight call exceeding the remaining budget is bounded by
  asyncio.wait_for and salvaged the same way — closing the gap that let a
  slow primary generation run to a Cloudflare 524 with nothing salvaged
  (the incident this ticket reports).
- step_tool_execution's iteration-limit gate skips the interactive
  "continue?" pause once the wall-clock deadline is already exceeded, since
  there's no time left to spend asking.
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


@pytest.fixture(autouse=True)
def _restore_executor_tool_globals() -> object:
    """Restore executor's lazily-cached registry globals after each test.

    step_llm_call seeds the module-level ``_tool_registry`` /
    ``_tool_execution_layer`` from whatever ``get_default_registry`` returns
    the first time it's called. The tests below patch it to a stub with no
    tool definitions; without this restore the stub leaks past the patch
    scope and poisons later tests in the same process (mirrors the identical
    fixture in ``test_content_widening.py``).
    """
    saved_registry = ex._tool_registry
    saved_layer = ex._tool_execution_layer
    yield
    ex._tool_registry = saved_registry
    ex._tool_execution_layer = saved_layer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(**overrides: object) -> ExecutionContext:
    defaults: dict[str, object] = {
        "session_id": "sess-deadline-001",
        "trace_id": "trace-deadline-001",
        "user_message": "seven day budget analysis",
        "mode": Mode.NORMAL,
        "channel": Channel.CHAT,
        "messages": [{"role": "user", "content": "seven day budget analysis"}],
    }
    defaults.update(overrides)
    return ExecutionContext(**defaults)  # type: ignore[arg-type]


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
    """Common patch set to drive step_llm_call up to the llm_client.respond() boundary."""
    stack = contextlib.ExitStack()
    stack.enter_context(
        patch("personal_agent.orchestrator.skills.get_skill_bodies",
            return_value=("", ()),
        )
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
# step_llm_call — deadline pre-check (already-expired budget)
# ---------------------------------------------------------------------------


class TestDeadlineAlreadyExpired:
    @pytest.mark.asyncio
    async def test_skips_llm_call_and_salvages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 100)
        ctx = _make_ctx()
        ctx.tool_results.append({"tool_name": "query_es", "success": True})  # type: ignore[attr-defined]
        # Budget is 100s; started 200s ago -> already 100s past deadline.
        ctx.turn_started_monotonic = time.monotonic() - 200

        respond_mock = AsyncMock()
        mock_llm = _mock_llm_client(respond_mock)
        trace_ctx = TraceContext.new_trace()

        with _step_llm_call_patches(mock_llm):
            result = await ex.step_llm_call(ctx, _mock_session(), trace_ctx)  # type: ignore[arg-type]

        assert result == TaskState.SYNTHESIS
        assert not respond_mock.called, "no LLM call should be attempted once already expired"
        assert ctx.final_reply is not None
        assert "stopped early" in ctx.final_reply.lower()
        assert "query_es" in ctx.final_reply

    @pytest.mark.asyncio
    async def test_no_tool_results_still_gets_a_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Edge case: deadline expired before any tool ever ran — still a reply, not a crash."""
        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 100)
        ctx = _make_ctx()
        ctx.turn_started_monotonic = time.monotonic() - 200

        respond_mock = AsyncMock()
        mock_llm = _mock_llm_client(respond_mock)
        trace_ctx = TraceContext.new_trace()

        with _step_llm_call_patches(mock_llm):
            result = await ex.step_llm_call(ctx, _mock_session(), trace_ctx)  # type: ignore[arg-type]

        assert result == TaskState.SYNTHESIS
        assert not respond_mock.called
        assert ctx.final_reply is not None
        assert "stopped early" in ctx.final_reply.lower()


# ---------------------------------------------------------------------------
# step_llm_call — in-flight call bounded by asyncio.wait_for
# ---------------------------------------------------------------------------


class TestInFlightCallBoundedByDeadline:
    @pytest.mark.asyncio
    async def test_slow_call_is_cancelled_and_salvaged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A call that would exceed the remaining budget is cut off — the closed
        version of the FRE-973 incident (a 251s call blowing past the tunnel
        read-timeout with nothing salvaged).
        """
        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 0.05)
        ctx = _make_ctx()
        ctx.tool_results.append({"tool_name": "query_es", "success": True})  # type: ignore[attr-defined]
        # turn_started_monotonic defaults to "now" -> remaining ~0.05s

        completed = {"value": False}

        async def slow_respond(**_kwargs: object) -> dict[str, object]:
            await asyncio.sleep(1.0)
            completed["value"] = (
                True  # must never be reached — proves cancellation, not just a fast return
            )
            return {
                "content": "final answer",
                "tool_calls": [],
                "response_id": None,
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

        respond_mock = AsyncMock(side_effect=slow_respond)
        mock_llm = _mock_llm_client(respond_mock)
        trace_ctx = TraceContext.new_trace()

        with _step_llm_call_patches(mock_llm):
            result = await ex.step_llm_call(ctx, _mock_session(), trace_ctx)  # type: ignore[arg-type]

        assert result == TaskState.SYNTHESIS
        assert completed["value"] is False, "the call must be cancelled, not merely raced"
        assert ctx.final_reply is not None
        assert "stopped early" in ctx.final_reply.lower()
        assert "query_es" in ctx.final_reply

    @pytest.mark.asyncio
    async def test_final_answer_bypass_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A call that would return a real no-tool-call answer past the deadline
        must still be bounded — it cannot bypass the guard through the
        no-tool-calls branch just because it eventually would have answered.
        """
        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 0.05)
        ctx = _make_ctx()

        async def slow_final_answer(**_kwargs: object) -> dict[str, object]:
            await asyncio.sleep(1.0)
            return {
                "content": "the answer",
                "tool_calls": [],
                "response_id": None,
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

        respond_mock = AsyncMock(side_effect=slow_final_answer)
        mock_llm = _mock_llm_client(respond_mock)
        trace_ctx = TraceContext.new_trace()

        with _step_llm_call_patches(mock_llm):
            result = await ex.step_llm_call(ctx, _mock_session(), trace_ctx)  # type: ignore[arg-type]

        # Bounded to SYNTHESIS-via-deadline, never reaches the no-tool-calls
        # "the answer" content path at all.
        assert result == TaskState.SYNTHESIS
        assert ctx.final_reply is not None
        assert "the answer" not in ctx.final_reply


# ---------------------------------------------------------------------------
# step_tool_execution — deadline takes precedence over the interactive pause
# ---------------------------------------------------------------------------


class TestConcurrentCeilingsDeadlinePrecedence:
    @pytest.mark.asyncio
    async def test_deadline_exceeded_skips_interactive_pause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both the iteration cap and the wall-clock deadline are exceeded —
        the turn must not pause to ask the user whether to continue; there's
        no time budget left to spend on that round-trip.
        """
        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 100)
        ctx = _make_ctx()
        ctx.turn_started_monotonic = time.monotonic() - 200  # expired
        ctx.tool_iteration_count = ex._resolve_max_iterations(ctx)  # next increment exceeds

        pause_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", pause_mock)
        trace_ctx = TraceContext.new_trace()

        result = await ex.step_tool_execution(ctx, _mock_session(), trace_ctx)  # type: ignore[arg-type]

        assert not pause_mock.called, "deadline exceeded -> auto-decline, never ask"
        assert result == TaskState.LLM_CALL
        assert ctx.force_synthesis_from_limit is True

    @pytest.mark.asyncio
    async def test_iteration_limit_alone_still_pauses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: iteration limit WITHOUT an exceeded deadline still
        goes through the normal interactive-pause path (unchanged behavior).
        """
        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 900)
        ctx = _make_ctx()
        # turn_started_monotonic defaults to "now" -> plenty of budget remaining.
        ctx.tool_iteration_count = ex._resolve_max_iterations(ctx)

        pause_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", pause_mock)
        trace_ctx = TraceContext.new_trace()

        result = await ex.step_tool_execution(ctx, _mock_session(), trace_ctx)  # type: ignore[arg-type]

        assert pause_mock.called
        assert result == TaskState.LLM_CALL
        assert ctx.force_synthesis_from_limit is True
