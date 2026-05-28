"""Tests for terminal tool-failure short-circuit (FRE-402).

When a tool declares a non-recoverable (terminal) failure, ``step_tool_execution``
must short-circuit the turn to FAILED with a classified ``tool_failure`` reply
instead of routing the error back through the reasoning model. Non-terminal tool
errors must still loop back to LLM_CALL so the model can attempt recovery.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import personal_agent.orchestrator.executor as ex
from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.types import ExecutionContext, TaskState
from personal_agent.telemetry.trace import TraceContext


def _make_ctx() -> ExecutionContext:
    ctx = ExecutionContext(  # type: ignore[arg-type]
        session_id="sess-fre402",
        trace_id="trace-fre402",
        user_message="draft an artifact",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )
    ctx.messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "tc-1",
                    "function": {"name": "artifact_draft", "arguments": "{}"},
                }
            ],
        }
    ]
    return ctx


def _trace_ctx() -> TraceContext:
    return TraceContext(trace_id="trace-fre402", session_id="sess-fre402")


def _dispatch_returning(*, terminal: bool):
    async def _fake_dispatch(
        tool_call_id, tool_name, arguments, args_hash, gate_result, loop_policy, *a, **k
    ):
        return {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "content": json.dumps({"status": "error", "hint": "sub-agent timed out"}),
            "success": False,
            "latency_ms": 1.0,
            "output_hash": None,
            "gate_result": gate_result,
            "args_hash": args_hash,
            "loop_policy": loop_policy,
            "tool_layer_output": None,
            "tool_layer_error": "HTML generation sub-agent timed out after 120.0s.",
            "terminal": terminal,
            "terminal_reason": (
                "The artifact generator timed out — the document was too complex to build in time."
                if terminal
                else None
            ),
            "terminal_next_step": (
                "Try a simpler artifact, or switch to Cloud for more capacity."
                if terminal
                else None
            ),
        }

    return _fake_dispatch


@pytest.fixture(autouse=True)
def _patch_executor_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ex, "_get_tool_execution_layer", lambda: object())
    monkeypatch.setattr(ex, "_is_turn_cancelled", lambda _sid: False)

    async def _noop_status(_ctx) -> None:
        return None

    monkeypatch.setattr(ex, "_emit_turn_status", _noop_status)


class TestTerminalToolFailureShortCircuit:
    @pytest.mark.asyncio
    async def test_terminal_failure_short_circuits_to_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ex, "_dispatch_tool_call", _dispatch_returning(terminal=True))
        ctx = _make_ctx()

        next_state = await ex.step_tool_execution(ctx, MagicMock(), _trace_ctx())

        assert next_state == TaskState.FAILED
        assert ctx.classified_error is not None
        assert ctx.classified_error.category == "tool_failure"
        assert ctx.final_reply is not None
        assert "timed out" in ctx.final_reply
        assert ctx.error is not None

    @pytest.mark.asyncio
    async def test_non_terminal_error_returns_to_llm_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ex, "_dispatch_tool_call", _dispatch_returning(terminal=False))
        ctx = _make_ctx()

        next_state = await ex.step_tool_execution(ctx, MagicMock(), _trace_ctx())

        assert next_state == TaskState.LLM_CALL
        assert ctx.classified_error is None
        assert ctx.error is None
