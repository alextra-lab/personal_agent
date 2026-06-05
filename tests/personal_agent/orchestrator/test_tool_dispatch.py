"""Tests for the shared tool-dispatch boundary (ADR-0086 D3).

`dispatch_tool_call` is the single per-call execution path invoked by BOTH the
primary executor loop and the discovery sub-agent loop ("one dispatch path, two
callers"). These tests pin its contract independently of either caller.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from personal_agent.orchestrator.tool_dispatch import dispatch_tool_call

from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.types import ToolDefinition, ToolResult


def _fake_tool_layer(result: ToolResult, tool_def: ToolDefinition | None = None) -> Any:
    """Build a ToolExecutionLayer stand-in with execute_tool + registry.get_tool."""
    layer = MagicMock()
    layer.execute_tool = AsyncMock(return_value=result)
    layer.registry = MagicMock()
    layer.registry.get_tool = MagicMock(
        return_value=(tool_def, lambda **_: None) if tool_def is not None else None
    )
    return layer


def _trace() -> TraceContext:
    return TraceContext(trace_id="t-1", session_id="s-1")


class TestDispatchToolCall:
    @pytest.mark.asyncio
    async def test_success_returns_contract_dict(self) -> None:
        result = ToolResult(
            tool_name="read",
            success=True,
            output={"body": "hello"},
            error=None,
            latency_ms=4.2,
        )
        layer = _fake_tool_layer(result)

        out = await dispatch_tool_call(
            tool_call_id="tc-1",
            tool_name="read",
            arguments={"path": "/tmp/x"},
            tool_layer=layer,
            trace_ctx=_trace(),
            trace_id="t-1",
            session_id="s-1",
            loaded_skills=set(),
        )

        assert out["tool_call_id"] == "tc-1"
        assert out["tool_name"] == "read"
        assert out["success"] is True
        assert out["tool_layer_output"] == {"body": "hello"}
        assert out["output_hash"] is not None
        # Optional gate fields echo through as None when omitted.
        assert out["gate_result"] is None
        assert out["loop_policy"] is None

    @pytest.mark.asyncio
    async def test_execution_failure_is_formatted(self) -> None:
        result = ToolResult(
            tool_name="read",
            success=False,
            output={},
            error="boom",
            latency_ms=1.0,
        )
        layer = _fake_tool_layer(result)

        out = await dispatch_tool_call(
            tool_call_id="tc-2",
            tool_name="read",
            arguments={"path": "/tmp/x"},
            tool_layer=layer,
            trace_ctx=_trace(),
            trace_id="t-1",
            session_id=None,
            loaded_skills=set(),
        )

        assert out["success"] is False
        assert out["output_hash"] is None
        assert "error" in out["content"]
