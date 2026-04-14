"""Tests for SeshatMCPServer routing and handler behaviour.

Covers:
- handle_tool_call routing to correct handler for all 6 tools
- Unknown tool name returns error dict (not exception)
- All handlers return dict responses
- Gateway URL stored but stubs return stub status
- caller_id is accepted without error
"""

from __future__ import annotations

import pytest

from personal_agent.mcp.server.server import SeshatMCPServer

EXPECTED_TOOL_NAMES = frozenset({
    "seshat_search_knowledge",
    "seshat_get_entity",
    "seshat_store_fact",
    "seshat_get_session_context",
    "seshat_query_observations",
    "seshat_delegate",
})


class TestSeshatMCPServerTools:
    """SeshatMCPServer exposes correct tool definitions."""

    def test_tools_property_returns_all_six(self) -> None:
        server = SeshatMCPServer()
        assert len(server.tools) == 6

    def test_tools_property_names_match(self) -> None:
        server = SeshatMCPServer()
        actual = {t.name for t in server.tools}
        assert actual == EXPECTED_TOOL_NAMES


class TestSeshatMCPServerRouting:
    """handle_tool_call routes to correct handlers."""

    @pytest.fixture
    def server(self) -> SeshatMCPServer:
        return SeshatMCPServer()

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self, server: SeshatMCPServer) -> None:
        result = await server.handle_tool_call("nonexistent_tool", {})
        assert "error" in result
        assert "nonexistent_tool" in result["error"]

    @pytest.mark.asyncio
    async def test_search_knowledge_routed(self, server: SeshatMCPServer) -> None:
        result = await server.handle_tool_call(
            "seshat_search_knowledge",
            {"query": "memory module"},
        )
        assert isinstance(result, dict)
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_get_entity_routed(self, server: SeshatMCPServer) -> None:
        result = await server.handle_tool_call(
            "seshat_get_entity",
            {"entity_id": "ent-001"},
        )
        assert isinstance(result, dict)
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_store_fact_routed(self, server: SeshatMCPServer) -> None:
        result = await server.handle_tool_call(
            "seshat_store_fact",
            {"entity": "PersonalAgent", "entity_type": "concept"},
        )
        assert isinstance(result, dict)
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_get_session_context_routed(self, server: SeshatMCPServer) -> None:
        result = await server.handle_tool_call(
            "seshat_get_session_context",
            {"session_id": "sess-abc"},
        )
        assert isinstance(result, dict)
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_query_observations_routed(self, server: SeshatMCPServer) -> None:
        result = await server.handle_tool_call(
            "seshat_query_observations",
            {"limit": 5},
        )
        assert isinstance(result, dict)
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_delegate_routed(self, server: SeshatMCPServer) -> None:
        result = await server.handle_tool_call(
            "seshat_delegate",
            {"task": "Create a Linear issue", "type": "linear_issue"},
        )
        assert isinstance(result, dict)
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_all_tools_routed_successfully(self, server: SeshatMCPServer) -> None:
        """Smoke test: all 6 tools route without errors."""
        tool_calls = [
            ("seshat_search_knowledge", {"query": "test"}),
            ("seshat_get_entity", {"entity_id": "e1"}),
            ("seshat_store_fact", {"entity": "Foo", "entity_type": "concept"}),
            ("seshat_get_session_context", {"session_id": "s1"}),
            ("seshat_query_observations", {}),
            ("seshat_delegate", {"task": "Do X", "type": "decomposition"}),
        ]
        for tool_name, args in tool_calls:
            result = await server.handle_tool_call(tool_name, args)
            assert isinstance(result, dict), f"{tool_name} did not return dict"
            assert "error" not in result, f"{tool_name} returned error: {result}"


class TestSeshatMCPServerInit:
    """SeshatMCPServer initialisation variants."""

    def test_no_gateway_url_by_default(self) -> None:
        server = SeshatMCPServer()
        assert server._gateway_url is None

    def test_gateway_url_stored(self) -> None:
        server = SeshatMCPServer(gateway_url="https://seshat.example.com")
        assert server._gateway_url == "https://seshat.example.com"

    @pytest.mark.asyncio
    async def test_stub_response_has_status_key(self) -> None:
        """Stub responses should include a 'status' key."""
        server = SeshatMCPServer()
        result = await server.handle_tool_call("seshat_search_knowledge", {"query": "x"})
        assert "status" in result

    @pytest.mark.asyncio
    async def test_caller_id_accepted(self) -> None:
        """caller_id parameter should be accepted without error."""
        server = SeshatMCPServer()
        result = await server.handle_tool_call(
            "seshat_search_knowledge",
            {"query": "test"},
            caller_id="claude-code-local",
        )
        assert isinstance(result, dict)
        assert "error" not in result
