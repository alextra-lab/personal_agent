"""Tool execution layer with governance, validation, and telemetry.

This module provides:
- Tool registry for tool discovery and registration
- Tool execution layer with permission checks and validation
- MVP tools (read_file, system_metrics_snapshot)
"""

from personal_agent.tools.executor import ToolExecutionError, ToolExecutionLayer
from personal_agent.tools.filesystem import (
    list_directory_executor,
    list_directory_tool,
    read_file_executor,
    read_file_tool,
)
from personal_agent.tools.registry import ToolRegistry
from personal_agent.tools.system_health import (
    system_metrics_snapshot_executor,
    system_metrics_snapshot_tool,
)
from personal_agent.tools.types import ToolDefinition, ToolParameter, ToolResult

__all__ = [
    # Core exports
    "ToolRegistry",
    "ToolExecutionLayer",
    "ToolExecutionError",
    "ToolDefinition",
    "ToolParameter",
    "ToolResult",
    # Tool registration function
    "register_mvp_tools",
    "get_default_registry",
]


def register_mvp_tools(registry: ToolRegistry) -> None:
    """Register MVP tools with the registry.

    This function registers the initial set of tools:
    - read_file: Read file contents
    - list_directory: List directory contents
    - system_metrics_snapshot: Get system health metrics

    Args:
        registry: Tool registry to register tools with.
    """
    registry.register(read_file_tool, read_file_executor)
    registry.register(list_directory_tool, list_directory_executor)
    registry.register(system_metrics_snapshot_tool, system_metrics_snapshot_executor)


# Global singleton registry
_default_registry: ToolRegistry | None = None


def get_default_registry() -> ToolRegistry:
    """Get the singleton tool registry with MVP tools pre-registered.

    This ensures all parts of the application share the same registry,
    so MCP tools registered during service initialization are available
    to the orchestrator.

    Returns:
        ToolRegistry singleton with MVP tools (and any dynamically registered tools).
    """
    global _default_registry
    if _default_registry is None:
        _default_registry = ToolRegistry()
        register_mvp_tools(_default_registry)
    return _default_registry
