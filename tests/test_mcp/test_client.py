"""Tests for MCP client wrapper."""

import asyncio
import builtins
from unittest.mock import AsyncMock, MagicMock, patch

import mcp.types as mcp_types
import pytest
from mcp.shared.exceptions import MCPError

from personal_agent.mcp.client import MCPClientWrapper, _load_mcp_sdk
from personal_agent.mcp.types import MCPToolError


def test_load_mcp_sdk_raises_clear_error_when_mcp_unavailable() -> None:
    """Regression (FRE-185): error message when optional ``mcp`` SDK is missing."""
    orig_import = builtins.__import__

    def guarded(name: str, globals=None, locals=None, fromlist=(), level: int = 0):
        if name == "mcp" or name.startswith("mcp."):
            raise ImportError("simulated missing mcp")
        return orig_import(name, globals, locals, fromlist, level)

    with patch.object(builtins, "__import__", guarded):
        with pytest.raises(ImportError, match="The 'mcp' package is required"):
            _load_mcp_sdk()


@pytest.mark.asyncio
async def test_client_context_manager():
    """Test client connects and disconnects via context manager."""
    mock_streams = AsyncMock()
    mock_read, mock_write = MagicMock(), MagicMock()
    mock_streams.__aenter__.return_value = (mock_read, mock_write)
    mock_stdio = MagicMock(return_value=mock_streams)

    mock_session = AsyncMock()

    mock_session_class = MagicMock(return_value=mock_session)

    mock_ssp_class = MagicMock()

    def fake_load():
        return mock_session_class, mock_ssp_class, mock_stdio

    with patch("personal_agent.mcp.client._load_mcp_sdk", side_effect=fake_load):
        async with MCPClientWrapper(["docker", "mcp", "gateway", "run"]) as client:
            assert client.session is not None
            mock_session.initialize.assert_called_once()

        mock_session.__aexit__.assert_called_once()


@pytest.mark.asyncio
async def test_list_tools():
    """Test tool listing."""
    client = MCPClientWrapper(["docker", "mcp", "gateway", "run"])

    # Real SDK types (FRE-1625): mcp 2.x renamed attributes to snake_case and
    # model_dump() to snake_case keys, which a MagicMock cannot detect.
    result = mcp_types.ListToolsResult(
        tools=[
            mcp_types.Tool(
                name="test_tool",
                description="Test tool",
                inputSchema={"type": "object", "properties": {"q": {"type": "string"}}},
            )
        ]
    )

    client.session = AsyncMock()
    client.session.list_tools = AsyncMock(return_value=result)

    tools = await client.list_tools()
    assert len(tools) == 1
    assert tools[0]["name"] == "test_tool"
    # The wire spelling that mcp_to_tool_definition reads.
    assert tools[0]["inputSchema"] == {"type": "object", "properties": {"q": {"type": "string"}}}


@pytest.mark.asyncio
async def test_call_tool():
    """Test tool invocation."""
    client = MCPClientWrapper(["docker", "mcp", "gateway", "run"])

    result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text='{"result": "success"}')],
        isError=False,
    )

    client.session = AsyncMock()
    client.session.call_tool = AsyncMock(return_value=result)

    assert await client.call_tool("test_tool", {"arg": "value"}) == {"result": "success"}


@pytest.mark.asyncio
async def test_call_tool_returns_structured_content_first():
    """FRE-1625: ``structured_content`` (``structuredContent`` on the wire) wins over text."""
    client = MCPClientWrapper(["docker", "mcp", "gateway", "run"])

    result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="ignored")],
        structuredContent={"rows": 3},
        isError=False,
    )

    client.session = AsyncMock()
    client.session.call_tool = AsyncMock(return_value=result)

    assert await client.call_tool("test_tool", {}) == {"rows": 3}


@pytest.mark.asyncio
async def test_call_tool_is_error_result_raises_mcp_tool_error():
    """FRE-1575 (ADR-0156 D6.6): an ``isError`` result raises ``mcp_tool_error`` with its text."""
    client = MCPClientWrapper(["docker", "mcp", "gateway", "run"])

    result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="team 'Nope' does not exist")],
        isError=True,
    )

    client.session = AsyncMock()
    client.session.call_tool = AsyncMock(return_value=result)

    with pytest.raises(MCPToolError) as exc_info:
        await client.call_tool("save_issue", {"team": "Nope"}, trace_id="t-1")

    assert exc_info.value.code == "mcp_tool_error"
    assert exc_info.value.retryable is False
    assert "team 'Nope' does not exist" in exc_info.value.message
    assert "save_issue" in exc_info.value.message


@pytest.mark.asyncio
async def test_call_tool_rejection_raised_by_sdk_is_mcp_tool_error():
    """FRE-1625: mcp 2.x raises a tool rejection as ``MCPError`` instead of returning ``isError``.

    The ADR-0156 D6.6 outcome must not change: the model still gets ``mcp_tool_error``
    with the tool's own message.
    """
    client = MCPClientWrapper(["docker", "mcp", "gateway", "run"])

    client.session = AsyncMock()
    client.session.call_tool = AsyncMock(
        side_effect=MCPError(code=-32602, message="team 'Nope' does not exist")
    )

    with pytest.raises(MCPToolError) as exc_info:
        await client.call_tool("save_issue", {"team": "Nope"}, trace_id="t-1")

    assert exc_info.value.code == "mcp_tool_error"
    assert exc_info.value.retryable is False
    assert "team 'Nope' does not exist" in exc_info.value.message
    assert "save_issue" in exc_info.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        mcp_types.CONNECTION_CLOSED,
        mcp_types.REQUEST_TIMEOUT,
        mcp_types.INTERNAL_ERROR,
        mcp_types.METHOD_NOT_FOUND,
    ],
)
async def test_call_tool_transport_failure_is_not_a_tool_rejection(code: int):
    """FRE-1625: mcp 2.x also raises ``MCPError`` for timeouts and closed connections.

    Those are not the tool's verdict on its arguments, so they must keep reaching
    the gateway's ``mcp_call_failed`` mapping rather than tell the model to fix
    its arguments.
    """
    client = MCPClientWrapper(["docker", "mcp", "gateway", "run"])

    client.session = AsyncMock()
    client.session.call_tool = AsyncMock(side_effect=MCPError(code=code, message="gone"))

    with pytest.raises(MCPError):
        await client.call_tool("save_issue", {"team": "Nope"}, trace_id="t-1")
    """Test timeout handling."""
    mock_streams = AsyncMock()
    mock_streams.__aenter__.side_effect = asyncio.TimeoutError()
    mock_stdio = MagicMock(return_value=mock_streams)

    mock_ssp_class = MagicMock()

    def fake_load():
        return MagicMock(), mock_ssp_class, mock_stdio

    with patch("personal_agent.mcp.client._load_mcp_sdk", side_effect=fake_load):
        with pytest.raises(asyncio.TimeoutError):
            async with MCPClientWrapper(["docker", "mcp", "gateway", "run"], timeout=1):
                pass
