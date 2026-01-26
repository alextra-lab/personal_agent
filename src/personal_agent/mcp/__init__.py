"""MCP Gateway integration for tool expansion.

This module integrates Docker's MCP Gateway to provide access to
containerized MCP servers while maintaining governance controls.
"""

from personal_agent.mcp.gateway import MCPGatewayAdapter
from personal_agent.mcp.governance import MCPGovernanceManager

__all__ = [
    "MCPGatewayAdapter",
    "MCPGovernanceManager",
]
