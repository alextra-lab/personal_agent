"""Tests for the shared tool-dispatch boundary (ADR-0086 D3).

`dispatch_tool_call` is the single per-call execution path invoked by BOTH the
primary executor loop and the discovery sub-agent loop ("one dispatch path, two
callers"). These tests pin its contract independently of either caller.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.governance.models import GovernanceConfig, SubAgentToolDecision
from personal_agent.orchestrator.tool_dispatch import dispatch_tool_call
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.types import ToolDefinition, ToolResult

_EMPTY_GOVERNANCE_CONFIG = GovernanceConfig(
    modes={}, tools={}, sub_agent_tools={}, mode_constraints={}
)


def _fake_tool_layer(
    result: ToolResult,
    tool_def: ToolDefinition | None = None,
    governance_config: GovernanceConfig | None = None,
) -> Any:
    """Build a ToolExecutionLayer stand-in with execute_tool + registry.get_tool."""
    layer = MagicMock()
    layer.execute_tool = AsyncMock(return_value=result)
    layer.registry = MagicMock()
    layer.registry.get_tool = MagicMock(
        return_value=(tool_def, lambda **_: None) if tool_def is not None else None
    )
    layer.governance_config = governance_config or _EMPTY_GOVERNANCE_CONFIG
    return layer


def _governance_config_with_ceilings(tool_name: str, **ceilings: int) -> GovernanceConfig:
    return GovernanceConfig(
        modes={},
        tools={},
        sub_agent_tools={
            tool_name: SubAgentToolDecision(
                granted=True, reason="test decision", param_ceilings=ceilings
            )
        },
        mode_constraints={},
    )


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

    @pytest.mark.asyncio
    async def test_long_error_hint_is_marked_not_silently_clipped(self) -> None:
        """ADR-0125 D5: the tool-role ``content`` hint is assembled context — a
        long error must carry an explicit marker, not a silent 150-char clip.
        """
        long_error = "the underlying service returned a detailed diagnostic: " * 5  # > 150 chars
        result = ToolResult(
            tool_name="read",
            success=False,
            output={},
            error=long_error,
            latency_ms=1.0,
        )
        layer = _fake_tool_layer(result)

        out = await dispatch_tool_call(
            tool_call_id="tc-3",
            tool_name="read",
            arguments={"path": "/tmp/x"},
            tool_layer=layer,
            trace_ctx=_trace(),
            trace_id="t-1",
            session_id=None,
            loaded_skills=set(),
        )

        assert "...[truncated" in out["content"]
        assert long_error not in out["content"]


class TestPrincipalAwareParamClamping:
    """FRE-1473 — ``principal`` gates the sub-agent-only parameter ceiling."""

    @pytest.mark.asyncio
    async def test_sub_agent_out_of_range_request_is_clamped_before_execute_tool(self) -> None:
        """AC-1 — a sub-agent's over-ceiling request reaches execute_tool already reduced."""
        result = ToolResult(
            tool_name="recall_personal_history",
            success=True,
            output={"turns": []},
            error=None,
            latency_ms=1.0,
        )
        config = _governance_config_with_ceilings("recall_personal_history", days_ago=30, limit=10)
        layer = _fake_tool_layer(result, governance_config=config)

        await dispatch_tool_call(
            tool_call_id="tc-1",
            tool_name="recall_personal_history",
            arguments={"days_ago": 365, "limit": 50},
            tool_layer=layer,
            trace_ctx=_trace(),
            trace_id="t-1",
            session_id="s-1",
            loaded_skills=set(),
            principal="sub_agent",
        )

        sent_args = layer.execute_tool.call_args.args[1]
        assert sent_args == {"days_ago": 30, "limit": 10}

    @pytest.mark.asyncio
    async def test_clamp_is_logged_with_tool_principal_requested_and_applied(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """AC-2 — the clamp is observable when it fires, not just a smaller result."""
        result = ToolResult(
            tool_name="recall_personal_history",
            success=True,
            output={"turns": []},
            error=None,
            latency_ms=1.0,
        )
        config = _governance_config_with_ceilings("recall_personal_history", days_ago=30)
        layer = _fake_tool_layer(result, governance_config=config)
        caplog.set_level("INFO", logger="personal_agent.orchestrator.tool_dispatch")

        await dispatch_tool_call(
            tool_call_id="tc-1",
            tool_name="recall_personal_history",
            arguments={"days_ago": 365},
            tool_layer=layer,
            trace_ctx=_trace(),
            trace_id="t-1",
            session_id="s-1",
            loaded_skills=set(),
            principal="sub_agent",
        )

        assert "sub_agent_tool_param_clamped" in caplog.text
        assert "recall_personal_history" in caplog.text
        assert "sub_agent" in caplog.text
        assert "365" in caplog.text
        assert "30" in caplog.text

    @pytest.mark.asyncio
    async def test_default_principal_never_clamps_even_with_ceilings_configured(self) -> None:
        """AC-3 — the primary's own call (principal omitted) is unaffected by any ceiling."""
        result = ToolResult(
            tool_name="recall_personal_history",
            success=True,
            output={"turns": []},
            error=None,
            latency_ms=1.0,
        )
        config = _governance_config_with_ceilings("recall_personal_history", days_ago=30, limit=10)
        layer = _fake_tool_layer(result, governance_config=config)

        await dispatch_tool_call(
            tool_call_id="tc-1",
            tool_name="recall_personal_history",
            arguments={"days_ago": 365, "limit": 50},
            tool_layer=layer,
            trace_ctx=_trace(),
            trace_id="t-1",
            session_id="s-1",
            loaded_skills=set(),
        )

        sent_args = layer.execute_tool.call_args.args[1]
        assert sent_args == {"days_ago": 365, "limit": 50}

    @pytest.mark.asyncio
    async def test_explicit_primary_principal_also_never_clamps(self) -> None:
        """Same as above, stated explicitly rather than relying only on the default."""
        result = ToolResult(
            tool_name="recall_personal_history",
            success=True,
            output={"turns": []},
            error=None,
            latency_ms=1.0,
        )
        config = _governance_config_with_ceilings("recall_personal_history", days_ago=30)
        layer = _fake_tool_layer(result, governance_config=config)

        await dispatch_tool_call(
            tool_call_id="tc-1",
            tool_name="recall_personal_history",
            arguments={"days_ago": 365},
            tool_layer=layer,
            trace_ctx=_trace(),
            trace_id="t-1",
            session_id="s-1",
            loaded_skills=set(),
            principal="primary",
        )

        sent_args = layer.execute_tool.call_args.args[1]
        assert sent_args == {"days_ago": 365}

    @pytest.mark.asyncio
    async def test_seeded_negative_in_bounds_sub_agent_request_matches_primary(self) -> None:
        """AC-5 — an in-bounds sub-agent call reaches execute_tool identically to a primary call."""
        result = ToolResult(
            tool_name="recall_personal_history",
            success=True,
            output={"turns": []},
            error=None,
            latency_ms=1.0,
        )
        config = _governance_config_with_ceilings("recall_personal_history", days_ago=30, limit=10)
        in_bounds_args = {"days_ago": 5, "limit": 3}

        primary_layer = _fake_tool_layer(result, governance_config=config)
        await dispatch_tool_call(
            tool_call_id="tc-primary",
            tool_name="recall_personal_history",
            arguments=dict(in_bounds_args),
            tool_layer=primary_layer,
            trace_ctx=_trace(),
            trace_id="t-1",
            session_id="s-1",
            loaded_skills=set(),
            principal="primary",
        )

        sub_agent_layer = _fake_tool_layer(result, governance_config=config)
        await dispatch_tool_call(
            tool_call_id="tc-sub-agent",
            tool_name="recall_personal_history",
            arguments=dict(in_bounds_args),
            tool_layer=sub_agent_layer,
            trace_ctx=_trace(),
            trace_id="t-1",
            session_id="s-1",
            loaded_skills=set(),
            principal="sub_agent",
        )

        assert (
            sub_agent_layer.execute_tool.call_args.args[1]
            == primary_layer.execute_tool.call_args.args[1]
            == in_bounds_args
        )
