"""Type conversions between MCP and tool execution formats."""

from typing import Any, Literal

from personal_agent.telemetry import get_logger
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)


def mcp_tool_to_definition(
    mcp_tool: dict[str, Any], description_override: str | None = None
) -> ToolDefinition:
    """Convert MCP tool schema to ToolDefinition.

    Args:
        mcp_tool: MCP tool schema from list_tools().
        Format: {
            "name": "github_search",
            "description": "Search GitHub repositories",
            "inputSchema": {
                "type": "object",
                "properties": {...},
                "required": [...]
            }
        }
        description_override: Optional description to use instead of MCP description.
            Useful for improving tool selection by providing clearer descriptions.

    Returns:
        ToolDefinition with mcp_ prefix and governance metadata.
    """
    # Extract metadata
    name = mcp_tool.get("name", "")
    # Use override description if provided, otherwise use MCP description
    description = description_override if description_override else mcp_tool.get("description", "")
    input_schema = mcp_tool.get("inputSchema", {})

    # Convert parameters
    parameters = []
    properties = input_schema.get("properties", {})
    required_fields = input_schema.get("required", [])

    for param_name, param_schema in properties.items():
        param_type = param_schema.get("type", "string")

        # Map JSON Schema types to tool parameter types
        type_mapping = {
            "string": "string",
            "number": "number",
            "integer": "number",
            "boolean": "boolean",
            "object": "object",
            "array": "array",
        }

        # For complex types (array, object), preserve full JSON schema
        # This is critical for MCP tools with nested schemas like Perplexity
        json_schema: dict[str, Any] | None = None
        if param_type in ("array", "object"):
            # Store the full parameter schema for complex types
            json_schema = param_schema

        parameters.append(
            ToolParameter(
                name=param_name,
                type=type_mapping.get(param_type, "string"),
                description=param_schema.get("description", ""),
                required=param_name in required_fields,
                default=param_schema.get("default"),
                json_schema=json_schema,
            )
        )

    # Infer risk level from name (used as default, overridden by governance)
    risk_level = _infer_risk_level(name)

    # Create ToolDefinition with mcp_ prefix
    return ToolDefinition(
        name=f"mcp_{name}",  # Always prefix to avoid conflicts
        description=description,
        category="mcp",
        parameters=parameters,
        risk_level=risk_level,
        allowed_modes=["NORMAL", "DEGRADED"],  # Default, overridden by governance
        requires_approval=risk_level == "high",  # Auto-approval for low/medium
        requires_sandbox=False,  # MCP servers already containerized
        timeout_seconds=30,
    )


def _infer_risk_level(tool_name: str) -> Literal["low", "medium", "high"]:
    """Infer risk level from tool name keywords.

    Args:
        tool_name: MCP tool name (without mcp_ prefix).

    Returns:
        Risk level: "low", "medium", or "high".
    """
    name_lower = tool_name.lower()

    # High risk keywords
    high_risk = ["write", "delete", "execute", "send", "create", "modify", "update", "remove"]
    if any(keyword in name_lower for keyword in high_risk):
        return "high"

    # Low risk keywords
    low_risk = ["read", "get", "list", "search", "query", "view", "show"]
    if any(keyword in name_lower for keyword in low_risk):
        return "low"

    # Default to medium
    return "medium"


def mcp_result_to_tool_result(
    tool_name: str, mcp_result: Any, latency_ms: float, error: str | None = None
) -> dict[str, Any]:
    """Convert MCP tool result to ToolResult format.

    Args:
        tool_name: Name of tool called (with mcp_ prefix).
        mcp_result: Result from MCP client.call_tool().
        latency_ms: Execution latency in milliseconds.
        error: Error message if call failed.

    Returns:
        Dict matching ToolResult structure.
    """
    return {
        "tool_name": tool_name,
        "success": error is None,
        "output": mcp_result if error is None else {},
        "error": error,
        "latency_ms": latency_ms,
        "metadata": {"source": "mcp_gateway"},
    }
