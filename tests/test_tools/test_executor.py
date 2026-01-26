"""Tests for ToolExecutionLayer."""

from pathlib import Path

import pytest

from personal_agent.brainstem.mode_manager import ModeManager
from personal_agent.config.governance_loader import load_governance_config
from personal_agent.governance.models import Mode
from personal_agent.telemetry import TraceContext
from personal_agent.tools.executor import ToolExecutionLayer
from personal_agent.tools.registry import ToolRegistry
from personal_agent.tools.types import ToolDefinition, ToolParameter, ToolResult


@pytest.fixture
def trace_ctx() -> TraceContext:
    """Fixture for trace context."""
    return TraceContext.new_trace()


@pytest.fixture
def governance_config():
    """Fixture for governance config."""
    return load_governance_config()


@pytest.fixture
def mode_manager(governance_config) -> ModeManager:
    """Fixture for mode manager."""
    return ModeManager(governance_config=governance_config)


@pytest.fixture
def registry() -> ToolRegistry:
    """Fixture for tool registry with test tool."""
    reg = ToolRegistry()

    def test_executor(path: str) -> dict:
        return {"success": True, "content": "test content"}

    tool_def = ToolDefinition(
        name="test_tool",
        description="Test tool",
        category="read_only",
        parameters=[ToolParameter(name="path", type="string", description="Path", required=True)],
        risk_level="low",
        allowed_modes=["NORMAL", "ALERT"],
    )

    reg.register(tool_def, test_executor)
    return reg


@pytest.fixture
def execution_layer(registry, governance_config, mode_manager) -> ToolExecutionLayer:
    """Fixture for tool execution layer."""
    return ToolExecutionLayer(
        registry=registry, governance_config=governance_config, mode_manager=mode_manager
    )


@pytest.mark.asyncio
async def test_execute_tool_success(execution_layer, trace_ctx) -> None:
    """Test successful tool execution."""
    result = await execution_layer.execute_tool("test_tool", {"path": "/test/path"}, trace_ctx)

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.tool_name == "test_tool"
    assert result.output == {"success": True, "content": "test content"}
    assert result.error is None
    assert result.latency_ms >= 0


@pytest.mark.asyncio
async def test_execute_nonexistent_tool(execution_layer, trace_ctx) -> None:
    """Test executing nonexistent tool returns error result."""
    result = await execution_layer.execute_tool("nonexistent_tool", {}, trace_ctx)

    assert result.success is False
    assert result.tool_name == "nonexistent_tool"
    assert "not found" in result.error.lower()
    assert result.output == {}


@pytest.mark.asyncio
async def test_execute_tool_mode_blocked(execution_layer, mode_manager, trace_ctx) -> None:
    """Test tool execution blocked by mode."""
    # Switch to DEGRADED mode first (valid transition), then to LOCKDOWN
    # test_tool only allowed in NORMAL, ALERT
    mode_manager.transition_to(Mode.DEGRADED, "Test transition", {"cpu": 90.0})
    mode_manager.transition_to(Mode.LOCKDOWN, "Test", {})

    result = await execution_layer.execute_tool("test_tool", {"path": "/test/path"}, trace_ctx)

    assert result.success is False
    assert "permission denied" in result.error.lower()
    assert "LOCKDOWN" in result.error


@pytest.mark.asyncio
async def test_execute_tool_executor_exception(execution_layer, trace_ctx) -> None:
    """Test tool execution handles executor exceptions."""

    # Register a tool that raises an exception
    def failing_executor() -> dict:
        raise ValueError("Test error")

    failing_tool = ToolDefinition(
        name="failing_tool",
        description="Failing tool",
        category="read_only",
        parameters=[],
        risk_level="low",
        allowed_modes=["NORMAL"],
    )

    execution_layer.registry.register(failing_tool, failing_executor)

    result = await execution_layer.execute_tool("failing_tool", {}, trace_ctx)

    assert result.success is False
    assert result.error is not None
    assert "test error" in result.error.lower()
    assert result.latency_ms >= 0


@pytest.mark.asyncio
async def test_execute_tool_with_path_validation(
    execution_layer, governance_config, trace_ctx
) -> None:
    """Test path validation for file tools."""

    # Create a test file tool with path restrictions
    def file_executor(path: str) -> dict:
        return {"success": True, "content": "file content"}

    file_tool = ToolDefinition(
        name="restricted_file_tool",
        description="File tool with restrictions",
        category="read_only",
        parameters=[ToolParameter(name="path", type="string", description="Path", required=True)],
        risk_level="low",
        allowed_modes=["NORMAL"],
    )

    execution_layer.registry.register(file_tool, file_executor)

    # Add tool policy with path restrictions
    from personal_agent.governance.models import ToolPolicy

    tool_policy = ToolPolicy(
        category="read_only",
        allowed_in_modes=["NORMAL"],
        forbidden_paths=["/System/**", "/Library/**"],
        allowed_paths=["$HOME/**"],
    )
    governance_config.tools["restricted_file_tool"] = tool_policy

    # Test forbidden path
    result = await execution_layer.execute_tool(
        "restricted_file_tool", {"path": "/System/Library"}, trace_ctx
    )
    assert result.success is False
    assert "forbidden" in result.error.lower()

    # Test allowed path (using home directory)
    home_path = str(Path.home() / "test_file.txt")
    result = await execution_layer.execute_tool(
        "restricted_file_tool", {"path": home_path}, trace_ctx
    )
    assert result.success is True


def test_get_default_registry() -> None:
    """Test get_default_registry returns registry with MVP tools."""
    from personal_agent.tools import get_default_registry

    registry = get_default_registry()

    assert "read_file" in registry.list_tool_names()
    assert "system_metrics_snapshot" in registry.list_tool_names()


def test_register_mvp_tools() -> None:
    """Test register_mvp_tools registers all MVP tools."""
    from personal_agent.tools import register_mvp_tools
    from personal_agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    register_mvp_tools(registry)

    assert "read_file" in registry.list_tool_names()
    assert "system_metrics_snapshot" in registry.list_tool_names()
