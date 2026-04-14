"""Tests for Seshat MCP server tool definitions.

Verifies that:
- All 6 expected tools are defined in SESHAT_TOOLS
- Each tool has a valid JSON Schema (type=object, properties dict, required list)
- SESHAT_TOOLS_BY_NAME index contains all 6 tools
- MCPToolDefinition is frozen (immutable)
"""

from __future__ import annotations

import pytest

from personal_agent.mcp.server.tools import (
    MCPToolDefinition,
    SESHAT_TOOLS,
    SESHAT_TOOLS_BY_NAME,
)

EXPECTED_TOOL_NAMES = frozenset({
    "seshat_search_knowledge",
    "seshat_get_entity",
    "seshat_store_fact",
    "seshat_get_session_context",
    "seshat_query_observations",
    "seshat_delegate",
})


class TestSeshatToolCount:
    """All 6 expected tools must be defined."""

    def test_tool_count(self) -> None:
        assert len(SESHAT_TOOLS) == 6

    def test_all_expected_tools_present(self) -> None:
        actual_names = {t.name for t in SESHAT_TOOLS}
        assert actual_names == EXPECTED_TOOL_NAMES

    def test_by_name_index_complete(self) -> None:
        assert set(SESHAT_TOOLS_BY_NAME.keys()) == EXPECTED_TOOL_NAMES

    def test_by_name_values_match_tools(self) -> None:
        for tool in SESHAT_TOOLS:
            assert SESHAT_TOOLS_BY_NAME[tool.name] is tool


class TestMCPToolDefinitionFrozen:
    """MCPToolDefinition must be immutable."""

    def test_frozen_name(self) -> None:
        tool = SESHAT_TOOLS[0]
        with pytest.raises((AttributeError, TypeError)):
            tool.name = "mutated"  # type: ignore[misc]

    def test_frozen_description(self) -> None:
        tool = SESHAT_TOOLS[0]
        with pytest.raises((AttributeError, TypeError)):
            tool.description = "mutated"  # type: ignore[misc]


class TestToolSchemas:
    """Each tool must have a valid JSON Schema with required fields defined."""

    @pytest.mark.parametrize("tool_name", list(EXPECTED_TOOL_NAMES))
    def test_schema_is_object_type(self, tool_name: str) -> None:
        tool = SESHAT_TOOLS_BY_NAME[tool_name]
        assert tool.input_schema.get("type") == "object"

    @pytest.mark.parametrize("tool_name", list(EXPECTED_TOOL_NAMES))
    def test_schema_has_properties(self, tool_name: str) -> None:
        tool = SESHAT_TOOLS_BY_NAME[tool_name]
        assert "properties" in tool.input_schema
        assert isinstance(tool.input_schema["properties"], dict)

    def test_search_knowledge_requires_query(self) -> None:
        tool = SESHAT_TOOLS_BY_NAME["seshat_search_knowledge"]
        assert "query" in tool.input_schema.get("required", [])

    def test_get_entity_requires_entity_id(self) -> None:
        tool = SESHAT_TOOLS_BY_NAME["seshat_get_entity"]
        assert "entity_id" in tool.input_schema.get("required", [])

    def test_store_fact_requires_entity_and_type(self) -> None:
        tool = SESHAT_TOOLS_BY_NAME["seshat_store_fact"]
        required = tool.input_schema.get("required", [])
        assert "entity" in required
        assert "entity_type" in required

    def test_get_session_context_requires_session_id(self) -> None:
        tool = SESHAT_TOOLS_BY_NAME["seshat_get_session_context"]
        assert "session_id" in tool.input_schema.get("required", [])

    def test_query_observations_has_no_required(self) -> None:
        """seshat_query_observations has all optional params."""
        tool = SESHAT_TOOLS_BY_NAME["seshat_query_observations"]
        # required key absent OR is an empty list
        required = tool.input_schema.get("required", [])
        assert required == []

    def test_delegate_requires_task_and_type(self) -> None:
        tool = SESHAT_TOOLS_BY_NAME["seshat_delegate"]
        required = tool.input_schema.get("required", [])
        assert "task" in required
        assert "type" in required

    def test_delegate_type_is_enum(self) -> None:
        tool = SESHAT_TOOLS_BY_NAME["seshat_delegate"]
        type_prop = tool.input_schema["properties"]["type"]
        assert "enum" in type_prop
        assert "linear_issue" in type_prop["enum"]

    def test_all_tools_have_non_empty_description(self) -> None:
        for tool in SESHAT_TOOLS:
            assert len(tool.description) > 10, f"{tool.name} description is too short"
