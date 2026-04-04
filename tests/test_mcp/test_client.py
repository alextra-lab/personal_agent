"""Tests for MCP client wrapper."""

import asyncio
import builtins
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.mcp.client import MCPClientWrapper, _load_mcp_sdk


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

    # Mock session
    mock_tool = MagicMock()
    mock_tool.model_dump.return_value = {
        "name": "test_tool",
        "description": "Test tool",
        "inputSchema": {"type": "object", "properties": {}},
    }

    mock_result = MagicMock()
    mock_result.tools = [mock_tool]

    client.session = AsyncMock()
    client.session.list_tools = AsyncMock(return_value=mock_result)

    tools = await client.list_tools()
    assert len(tools) == 1
    assert tools[0]["name"] == "test_tool"


@pytest.mark.asyncio
async def test_call_tool():
    """Test tool invocation."""
    client = MCPClientWrapper(["docker", "mcp", "gateway", "run"])

    mock_content = MagicMock()
    mock_content.text = '{"result": "success"}'

    mock_result = MagicMock()
    mock_result.content = [mock_content]
    mock_result.isError = False
    mock_result.structuredContent = None

    client.session = AsyncMock()
    client.session.call_tool = AsyncMock(return_value=mock_result)

    result = await client.call_tool("test_tool", {"arg": "value"})
    assert result == {"result": "success"}


@pytest.mark.asyncio
async def test_client_timeout():
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
