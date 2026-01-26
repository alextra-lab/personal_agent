"""Integration tests for MCP Gateway (requires Docker)."""

from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.mcp.gateway import MCPGatewayAdapter
from personal_agent.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_gateway_initialization():
    """Test gateway initialization and tool discovery."""
    registry = ToolRegistry()
    adapter = MCPGatewayAdapter(registry)

    # Mock client
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()

    # Mock tool discovery
    mock_client.list_tools = AsyncMock(
        return_value=[
            {
                "name": "test_tool",
                "description": "Test tool",
                "inputSchema": {
                    "type": "object",
                    "properties": {"arg1": {"type": "string", "description": "Argument 1"}},
                    "required": ["arg1"],
                },
            }
        ]
    )

    with patch("personal_agent.mcp.gateway.MCPClientWrapper", return_value=mock_client):
        with patch("personal_agent.mcp.gateway.MCPGovernanceManager"):
            with patch.object(adapter, "enabled", True):
                await adapter.initialize()

    # Verify tool registered
    tools = registry.list_tools()
    assert len(tools) > 0
    assert any(t.name == "mcp_test_tool" for t in tools)


@pytest.mark.asyncio
async def test_graceful_degradation():
    """Test system continues if gateway unavailable."""
    registry = ToolRegistry()
    adapter = MCPGatewayAdapter(registry)

    # Mock client that fails
    mock_client = AsyncMock()
    mock_client.__aenter__.side_effect = Exception("Gateway unavailable")

    with patch("personal_agent.mcp.gateway.MCPClientWrapper", return_value=mock_client):
        with patch.object(adapter, "enabled", True):
            # Should not raise, just log warning
            await adapter.initialize()

    # Adapter should be disabled
    assert adapter.enabled is False
