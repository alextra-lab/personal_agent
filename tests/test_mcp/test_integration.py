"""Integration tests for MCP Gateway (requires Docker)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.mcp.gateway import MCPGatewayAdapter
from personal_agent.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_gateway_initialization():
    """Test gateway initialization and tool discovery."""
    registry = ToolRegistry()
    adapter = MCPGatewayAdapter(registry)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()

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

    mock_governance = MagicMock()
    mock_governance.get_description_override.return_value = None
    mock_governance.ensure_tool_configured.return_value = None

    with patch("personal_agent.mcp.gateway.MCPClientWrapper", return_value=mock_client):
        with patch("personal_agent.mcp.gateway.MCPGovernanceManager", return_value=mock_governance):
            adapter.enabled = True
            await adapter.initialize()

    tools = registry.list_tools()
    assert len(tools) > 0
    assert any(t.name == "mcp_test_tool" for t in tools)


@pytest.mark.asyncio
async def test_graceful_degradation():
    """Test system continues if gateway unavailable."""
    registry = ToolRegistry()
    adapter = MCPGatewayAdapter(registry)

    mock_client = AsyncMock()
    mock_client.__aenter__.side_effect = Exception("Gateway unavailable")

    with patch("personal_agent.mcp.gateway.MCPClientWrapper", return_value=mock_client):
        adapter.enabled = True
        await adapter.initialize()

    assert adapter.enabled is False
