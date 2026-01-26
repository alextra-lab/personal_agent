"""Tests for MCP client wrapper."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.mcp.client import MCPClientWrapper


@pytest.mark.asyncio
async def test_client_context_manager():
    """Test client connects and disconnects via context manager."""
    with patch("personal_agent.mcp.client.stdio_client") as mock_stdio:
        # Mock context manager chain
        mock_streams = AsyncMock()
        mock_read, mock_write = MagicMock(), MagicMock()
        mock_streams.__aenter__.return_value = (mock_read, mock_write)
        mock_stdio.return_value = mock_streams

        mock_session = AsyncMock()

        with patch("personal_agent.mcp.client.ClientSession") as mock_session_class:
            mock_session_class.return_value = mock_session

            # Test context manager
            async with MCPClientWrapper(["docker", "mcp", "gateway", "run"]) as client:
                assert client.session is not None
                mock_session.initialize.assert_called_once()

            # Verify cleanup
            mock_session.__aexit__.assert_called_once()


@pytest.mark.asyncio
async def test_list_tools():
    """Test tool listing."""
    with patch("personal_agent.mcp.client.stdio_client"):
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
    with patch("personal_agent.mcp.client.stdio_client"):
        client = MCPClientWrapper(["docker", "mcp", "gateway", "run"])

        # Mock session
        mock_content = MagicMock()
        mock_content.text = '{"result": "success"}'

        mock_result = MagicMock()
        mock_result.content = [mock_content]

        client.session = AsyncMock()
        client.session.call_tool = AsyncMock(return_value=mock_result)

        result = await client.call_tool("test_tool", {"arg": "value"})
        assert result == {"result": "success"}


@pytest.mark.asyncio
async def test_client_timeout():
    """Test timeout handling."""
    with patch("personal_agent.mcp.client.stdio_client") as mock_stdio:
        # Mock slow initialization
        mock_streams = AsyncMock()
        mock_streams.__aenter__.side_effect = asyncio.TimeoutError()
        mock_stdio.return_value = mock_streams

        with pytest.raises(asyncio.TimeoutError):
            async with MCPClientWrapper(["docker", "mcp", "gateway", "run"], timeout=1):
                pass
