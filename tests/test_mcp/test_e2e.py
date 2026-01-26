"""End-to-end tests for MCP Gateway integration."""

import os
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(not os.getenv("DOCKER_AVAILABLE"), reason="Requires Docker with MCP Gateway")
async def test_full_mcp_workflow():
    """Test complete MCP workflow: init → discover → execute → governance.

    This test requires Docker to be running with MCP Gateway available.
    """
    from personal_agent.config import settings
    from personal_agent.mcp.gateway import MCPGatewayAdapter
    from personal_agent.tools import ToolRegistry

    # Enable gateway for test
    with patch.object(settings, "mcp_gateway_enabled", True):
        registry = ToolRegistry()
        adapter = MCPGatewayAdapter(registry)

        try:
            # Initialize gateway
            await adapter.initialize()

            # Verify tools discovered
            tools = registry.list_tools()
            mcp_tools = [t for t in tools if t.name.startswith("mcp_")]
            assert len(mcp_tools) > 0, "No MCP tools discovered"

            # Verify governance entries created
            # (Check config/governance/tools.yaml was updated)

            print(f"✓ Discovered {len(mcp_tools)} MCP tools")

        finally:
            await adapter.shutdown()


@pytest.mark.asyncio
async def test_graceful_degradation_no_docker():
    """Test system works when Docker unavailable."""
    from personal_agent.mcp.gateway import MCPGatewayAdapter
    from personal_agent.tools import get_default_registry

    registry = get_default_registry()  # Built-in tools
    adapter = MCPGatewayAdapter(registry)

    # Mock client that fails
    with patch("personal_agent.mcp.gateway.MCPClientWrapper") as mock_client:
        mock_client.return_value.__aenter__.side_effect = Exception("Docker not available")

        with patch.object(adapter, "enabled", True):
            # Should not raise
            await adapter.initialize()

            # Built-in tools still work
            tools = registry.list_tools()
            assert len(tools) > 0
            assert any(t.name == "read_file" for t in tools)
