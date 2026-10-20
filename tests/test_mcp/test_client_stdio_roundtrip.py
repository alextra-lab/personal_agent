"""FRE-1625 — the MCP client against a real mcp 2.x server over real stdio.

``test_client.py`` mocks every ``ClientSession`` call, so it cannot show what mcp 2.x
actually returns or raises. This module spawns ``_servers/tiny_server.py`` and drives
``MCPClientWrapper`` through the real SDK: the handshake, ``tools/list``, a successful call
and a tool that rejects its arguments.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from personal_agent.mcp.client import MCPClientWrapper
from personal_agent.mcp.types import MCPToolError, mcp_tool_to_definition

_SERVER = Path(__file__).parent / "_servers" / "tiny_server.py"


def _client() -> MCPClientWrapper:
    return MCPClientWrapper([sys.executable, str(_SERVER)], timeout=30)


@pytest.mark.asyncio
async def test_list_tools_keeps_the_wire_spelling_the_converter_reads() -> None:
    async with _client() as client:
        tools = {t["name"]: t for t in await client.list_tools()}

    assert set(tools) == {"add", "reject"}
    assert "inputSchema" in tools["add"], "mcp 2.x dumps snake_case unless by_alias is set"
    definition = mcp_tool_to_definition(tools["add"])
    assert {p.name for p in definition.parameters} == {"a", "b"}


@pytest.mark.asyncio
async def test_call_tool_returns_the_tool_result() -> None:
    async with _client() as client:
        result = await client.call_tool("add", {"a": 2, "b": 3})

    assert result in (5, "5", {"result": 5})


@pytest.mark.asyncio
async def test_a_rejecting_tool_surfaces_as_mcp_tool_error() -> None:
    async with _client() as client:
        with pytest.raises(MCPToolError) as exc_info:
            await client.call_tool("reject", {"team": "Nope"})

    assert exc_info.value.code == "mcp_tool_error"
    assert "team 'Nope' does not exist" in exc_info.value.message
