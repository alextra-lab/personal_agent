"""Tool registry for tool discovery and registration.

This module provides the ToolRegistry class that manages tool definitions
and their executor functions, allowing tools to be registered, discovered,
and filtered by operational mode.
"""

from typing import Any, Callable

from personal_agent.governance.models import Mode
from personal_agent.telemetry import get_logger
from personal_agent.tools.types import ToolDefinition

log = get_logger(__name__)


class ToolRegistry:
    """Central registry of available tools.

    The registry stores tool definitions along with their executor functions,
    enabling tools to be discovered, filtered by mode, and executed.
    """

    def __init__(self) -> None:
        """Initialize empty tool registry."""
        self._tools: dict[str, tuple[ToolDefinition, Callable[..., Any]]] = {}
        log.debug("tool_registry_initialized")

    def register(self, tool_def: ToolDefinition, executor: Callable[..., Any]) -> None:
        """Register a tool with its definition and executor function.

        Args:
            tool_def: Tool definition with metadata.
            executor: Callable that executes the tool. Should accept tool
                parameters as keyword arguments and return a dict with
                tool-specific results.

        Raises:
            ValueError: If tool name already registered.
        """
        if tool_def.name in self._tools:
            raise ValueError(f"Tool '{tool_def.name}' is already registered")

        self._tools[tool_def.name] = (tool_def, executor)
        log.debug(
            "tool_registered",
            tool_name=tool_def.name,
            category=tool_def.category,
            risk_level=tool_def.risk_level,
        )

    def get_tool(self, name: str) -> tuple[ToolDefinition, Callable[..., Any]] | None:
        """Retrieve tool definition and executor.

        Args:
            name: Tool name to retrieve.

        Returns:
            Tuple of (ToolDefinition, executor) if found, None otherwise.
        """
        return self._tools.get(name)

    def list_tools(self, mode: Mode | None = None) -> list[ToolDefinition]:
        """List tools available in the given mode.

        Args:
            mode: Operational mode to filter by. If None, returns all tools.

        Returns:
            List of tool definitions available in the mode.
        """
        if mode is None:
            return [tool_def for tool_def, _ in self._tools.values()]

        mode_str = mode.value
        return [
            tool_def for tool_def, _ in self._tools.values() if mode_str in tool_def.allowed_modes
        ]

    def filter_by_category(self, category: str) -> list[ToolDefinition]:
        """Filter tools by governance category.

        Args:
            category: Category name to filter by.

        Returns:
            List of tool definitions in the category.
        """
        return [tool_def for tool_def, _ in self._tools.values() if tool_def.category == category]

    def list_tool_names(self) -> list[str]:
        """List names of all registered tools.

        Returns:
            List of tool names.
        """
        return list(self._tools.keys())

    def get_tool_definitions_for_llm(self, mode: Mode | None = None) -> list[dict[str, Any]]:
        """Get tool definitions in OpenAI function calling format.

        Args:
            mode: Operational mode to filter by. If None, returns all tools.

        Returns:
            List of tool definitions in OpenAI format (for function calling).
        """
        tools = self.list_tools(mode=mode)
        result = []
        for tool_def in tools:
            # Build properties dict, using full JSON schema for complex types
            properties: dict[str, Any] = {}
            for param in tool_def.parameters:
                if param.json_schema:
                    # Use full JSON schema for complex types (array, object)
                    # This preserves nested structures like items, properties, etc.
                    properties[param.name] = param.json_schema
                else:
                    # Simple types use basic schema
                    properties[param.name] = {
                        "type": param.type,
                        "description": param.description,
                    }

            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool_def.name,
                        "description": tool_def.description,
                        "parameters": {
                            "type": "object",
                            "properties": properties,
                            "required": [
                                param.name for param in tool_def.parameters if param.required
                            ],
                            "additionalProperties": False,
                        },
                    },
                }
            )
        return result
