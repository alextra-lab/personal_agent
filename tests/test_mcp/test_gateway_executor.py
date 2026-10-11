"""Tests for the MCP gateway adapter's per-tool executor failures (FRE-1575, ADR-0156 D6.6).

No test starts the gateway: the adapter's client is a mock.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.mcp.gateway import MCPGatewayAdapter
from personal_agent.mcp.types import MCPToolError
from personal_agent.telemetry import TraceContext
from personal_agent.tools.contract import legacy_failure_marker
from personal_agent.tools.registry import ToolRegistry

_CTX = TraceContext.new_trace()


def _adapter(client: object | None) -> MCPGatewayAdapter:
    adapter = MCPGatewayAdapter(ToolRegistry())
    adapter.client = client  # type: ignore[assignment]
    return adapter


@pytest.mark.asyncio
async def test_executor_without_a_connected_gateway_is_mcp_unavailable() -> None:
    executor = _adapter(None)._create_executor("search")

    with pytest.raises(MCPToolError) as exc_info:
        await executor(ctx=_CTX, query="x")

    assert exc_info.value.code == "mcp_unavailable"
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_executor_client_failure_is_mcp_call_failed() -> None:
    client = MagicMock()
    client.call_tool = AsyncMock(side_effect=ConnectionResetError("stdio pipe closed"))
    executor = _adapter(client)._create_executor("search")

    with pytest.raises(MCPToolError) as exc_info:
        await executor(ctx=_CTX, query="x")

    assert exc_info.value.code == "mcp_call_failed"
    assert exc_info.value.retryable is False
    assert "ConnectionResetError: stdio pipe closed" in exc_info.value.message
    client.call_tool.assert_awaited_once_with("search", {"query": "x"}, trace_id=_CTX.trace_id)


@pytest.mark.asyncio
async def test_executor_passes_a_tool_error_through_unchanged() -> None:
    original = MCPToolError(
        "mcp_tool_error",
        "MCP tool 'search' returned an error: bad query",
        retryable=False,
        next_action="Correct the arguments as the message states, or use another tool.",
    )
    client = MagicMock()
    client.call_tool = AsyncMock(side_effect=original)
    executor = _adapter(client)._create_executor("search")

    with pytest.raises(MCPToolError) as exc_info:
        await executor(ctx=_CTX, query="x")

    assert exc_info.value is original
    assert exc_info.value.code == "mcp_tool_error"


@pytest.mark.asyncio
async def test_a_third_party_dict_is_kept_under_result() -> None:
    """A server's own ``ok``/``error``/``success`` keys never reach the contract's top level.

    Spread into ``{"ok": true, ...}``, a server's ``ok: false`` would overwrite the contract
    field, and its ``error`` key would turn a call that ran into a ``contract_violation``.
    """
    client = MagicMock()
    server_payload = {"ok": False, "error": "no match", "success": False}
    client.call_tool = AsyncMock(return_value=server_payload)
    executor = _adapter(client)._create_executor("search")

    assert await executor(ctx=_CTX, query="x") == {"result": server_payload}
    assert legacy_failure_marker({"result": server_payload}) is None
