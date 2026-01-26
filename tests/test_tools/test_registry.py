"""Tests for ToolRegistry."""

import pytest

from personal_agent.governance.models import Mode
from personal_agent.tools.registry import ToolRegistry
from personal_agent.tools.types import ToolDefinition, ToolParameter


def test_registry_initialization() -> None:
    """Test ToolRegistry initializes empty."""
    registry = ToolRegistry()
    assert len(registry.list_tool_names()) == 0
    assert len(registry.list_tools()) == 0


def test_register_tool() -> None:
    """Test registering a tool."""
    registry = ToolRegistry()

    def dummy_executor(path: str) -> dict:
        return {"success": True}

    tool_def = ToolDefinition(
        name="test_tool",
        description="Test tool",
        category="read_only",
        parameters=[ToolParameter(name="path", type="string", description="Path", required=True)],
        risk_level="low",
        allowed_modes=["NORMAL", "ALERT"],
    )

    registry.register(tool_def, dummy_executor)

    assert "test_tool" in registry.list_tool_names()
    assert len(registry.list_tools()) == 1

    tool_def_result, executor_result = registry.get_tool("test_tool")
    assert tool_def_result == tool_def
    assert executor_result == dummy_executor


def test_register_duplicate_tool_raises_error() -> None:
    """Test registering duplicate tool raises ValueError."""
    registry = ToolRegistry()

    def dummy_executor() -> dict:
        return {"success": True}

    tool_def = ToolDefinition(
        name="test_tool",
        description="Test tool",
        category="read_only",
        parameters=[],
        risk_level="low",
        allowed_modes=["NORMAL"],
    )

    registry.register(tool_def, dummy_executor)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(tool_def, dummy_executor)


def test_get_nonexistent_tool() -> None:
    """Test getting nonexistent tool returns None."""
    registry = ToolRegistry()
    assert registry.get_tool("nonexistent") is None


def test_list_tools_filtered_by_mode() -> None:
    """Test listing tools filtered by mode."""
    registry = ToolRegistry()

    def dummy_executor() -> dict:
        return {"success": True}

    # Tool allowed in NORMAL and ALERT
    tool1 = ToolDefinition(
        name="tool1",
        description="Tool 1",
        category="read_only",
        parameters=[],
        risk_level="low",
        allowed_modes=["NORMAL", "ALERT"],
    )

    # Tool only allowed in NORMAL
    tool2 = ToolDefinition(
        name="tool2",
        description="Tool 2",
        category="read_only",
        parameters=[],
        risk_level="low",
        allowed_modes=["NORMAL"],
    )

    registry.register(tool1, dummy_executor)
    registry.register(tool2, dummy_executor)

    # All tools in NORMAL mode
    normal_tools = registry.list_tools(mode=Mode.NORMAL)
    assert len(normal_tools) == 2
    assert {t.name for t in normal_tools} == {"tool1", "tool2"}

    # Only tool1 in ALERT mode
    alert_tools = registry.list_tools(mode=Mode.ALERT)
    assert len(alert_tools) == 1
    assert alert_tools[0].name == "tool1"

    # No tools in LOCKDOWN mode
    lockdown_tools = registry.list_tools(mode=Mode.LOCKDOWN)
    assert len(lockdown_tools) == 0


def test_list_tools_without_mode() -> None:
    """Test listing all tools when mode is None."""
    registry = ToolRegistry()

    def dummy_executor() -> dict:
        return {"success": True}

    tool1 = ToolDefinition(
        name="tool1",
        description="Tool 1",
        category="read_only",
        parameters=[],
        risk_level="low",
        allowed_modes=["NORMAL"],
    )
    tool2 = ToolDefinition(
        name="tool2",
        description="Tool 2",
        category="read_only",
        parameters=[],
        risk_level="low",
        allowed_modes=["ALERT"],
    )

    registry.register(tool1, dummy_executor)
    registry.register(tool2, dummy_executor)

    all_tools = registry.list_tools(mode=None)
    assert len(all_tools) == 2


def test_filter_by_category() -> None:
    """Test filtering tools by category."""
    registry = ToolRegistry()

    def dummy_executor() -> dict:
        return {"success": True}

    read_tool = ToolDefinition(
        name="read_tool",
        description="Read tool",
        category="read_only",
        parameters=[],
        risk_level="low",
        allowed_modes=["NORMAL"],
    )
    write_tool = ToolDefinition(
        name="write_tool",
        description="Write tool",
        category="system_write",
        parameters=[],
        risk_level="high",
        allowed_modes=["NORMAL"],
    )

    registry.register(read_tool, dummy_executor)
    registry.register(write_tool, dummy_executor)

    read_tools = registry.filter_by_category("read_only")
    assert len(read_tools) == 1
    assert read_tools[0].name == "read_tool"

    write_tools = registry.filter_by_category("system_write")
    assert len(write_tools) == 1
    assert write_tools[0].name == "write_tool"


def test_get_tool_definitions_for_llm() -> None:
    """Test getting tool definitions in OpenAI format."""
    registry = ToolRegistry()

    def dummy_executor(path: str, max_size: int = 10) -> dict:
        return {"success": True}

    tool_def = ToolDefinition(
        name="test_tool",
        description="Test tool description",
        category="read_only",
        parameters=[
            ToolParameter(name="path", type="string", description="Path", required=True),
            ToolParameter(
                name="max_size", type="number", description="Max size", required=False, default=10
            ),
        ],
        risk_level="low",
        allowed_modes=["NORMAL"],
    )

    registry.register(tool_def, dummy_executor)

    llm_tools = registry.get_tool_definitions_for_llm()

    assert len(llm_tools) == 1
    llm_tool = llm_tools[0]

    assert llm_tool["type"] == "function"
    assert llm_tool["function"]["name"] == "test_tool"
    assert llm_tool["function"]["description"] == "Test tool description"
    assert "path" in llm_tool["function"]["parameters"]["properties"]
    assert "max_size" in llm_tool["function"]["parameters"]["properties"]
    assert "path" in llm_tool["function"]["parameters"]["required"]
    assert "max_size" not in llm_tool["function"]["parameters"]["required"]
