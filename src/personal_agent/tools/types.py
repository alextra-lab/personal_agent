"""Type definitions for tool execution layer.

This module defines the Pydantic models for tool definitions, parameters,
and results used by the tool execution system.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolParameter(BaseModel):
    """Parameter definition for a tool."""

    name: str = Field(..., description="Parameter name")
    type: Literal["string", "number", "boolean", "object", "array"] = Field(
        ..., description="Parameter type"
    )
    description: str = Field(..., description="Parameter description for LLM")
    required: bool = Field(True, description="Whether parameter is required")
    default: Any | None = Field(None, description="Default value if not required")
    # Full JSON Schema for complex types (array items, object properties, etc.)
    json_schema: dict[str, Any] | None = Field(
        None, description="Full JSON Schema for complex nested types"
    )


class ToolDefinition(BaseModel):
    """OpenAI-style tool definition for LLM function calling.

    Each tool is defined with its name, description, parameters, and
    governance metadata (risk level, allowed modes, etc.).
    """

    name: str = Field(..., description="Tool name (e.g., 'read_file', 'system_metrics_snapshot')")
    description: str = Field(..., description="Clear description for LLM")
    category: str = Field(..., description="Tool category from governance (e.g., 'read_only')")
    parameters: list[ToolParameter] = Field(default_factory=list, description="Tool parameters")

    # Governance metadata
    risk_level: Literal["low", "medium", "high"] = Field(..., description="Risk level")
    allowed_modes: list[str] = Field(..., description="Which operational modes allow this tool")
    requires_approval: bool = Field(False, description="Whether tool always requires approval")
    requires_sandbox: bool = Field(False, description="Whether tool requires sandboxing")

    # Execution metadata
    timeout_seconds: int = Field(30, ge=1, description="Execution timeout in seconds")
    rate_limit_per_hour: int | None = Field(None, ge=0, description="Rate limit per hour")


class ToolResult(BaseModel):
    """Result from tool execution."""

    tool_name: str = Field(..., description="Name of the executed tool")
    success: bool = Field(..., description="Whether tool execution succeeded")
    output: str | dict[str, Any] = Field(..., description="Tool-specific output")
    error: str | None = Field(None, description="Error message if failed")
    latency_ms: float = Field(..., ge=0, description="Execution latency in milliseconds")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Extra context (e.g., files touched)"
    )
