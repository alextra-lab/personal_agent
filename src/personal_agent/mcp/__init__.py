"""MCP Gateway integration for tool expansion.

This module integrates Docker's MCP Gateway to provide access to
containerized MCP servers while maintaining governance controls.

Package exports are lazy: importing ``personal_agent.mcp.governance`` must not
pull in ``gateway`` (and the ``mcp`` SDK). Use ``from personal_agent.mcp.gateway import ...``
when you need the adapter.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "MCPGatewayAdapter",
    "MCPGovernanceManager",
]


def __getattr__(name: str) -> Any:
    if name == "MCPGatewayAdapter":
        from personal_agent.mcp.gateway import MCPGatewayAdapter

        return MCPGatewayAdapter
    if name == "MCPGovernanceManager":
        from personal_agent.mcp.governance import MCPGovernanceManager

        return MCPGovernanceManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
