"""Type definitions for tool execution layer.

This module defines the Pydantic models for tool definitions, parameters,
and results used by the tool execution system.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Longest error message the model sees (ADR-0156 D6.2).
ERROR_MESSAGE_LIMIT = 2000

#: No tool result exceeds this many tokens (ADR-0156 D6.4, S4).
RESULT_TOKEN_CEILING = 25_000


class ToolErrorDetail(BaseModel):
    """The failure half of the one tool result shape (ADR-0156 D6.1).

    The model sees it as ``{"ok": false, "error": {code, message, retryable, next_action}}``.
    """

    model_config = ConfigDict(frozen=True)

    code: str = Field(..., description="Stable code from the tool's closed code set")
    message: str = Field(
        ..., max_length=ERROR_MESSAGE_LIMIT, description="What failed, bounded at 2,000 chars"
    )
    retryable: bool = Field(..., description="Whether the same call can succeed if repeated")
    next_action: str = Field(
        ...,
        max_length=ERROR_MESSAGE_LIMIT,
        description="The next call that can work, or that none exists",
    )


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

    # Tool contract (ADR-0156 D6)
    error_codes: frozenset[str] = Field(
        default_factory=frozenset,
        description=(
            "The tool's closed failure-code set, read from its error class with "
            "personal_agent.tools.contract.error_codes(). A failure with another code is a "
            "contract violation."
        ),
    )
    max_result_tokens: int = Field(
        RESULT_TOKEN_CEILING,
        ge=1,
        le=RESULT_TOKEN_CEILING,
        description="Token bound on the model-facing result; a longer result is truncated.",
    )
    truncation_next_action: str = Field(
        "Narrow the request (fewer results or a smaller range) and call again.",
        description="Told to the model when a result is truncated at max_result_tokens.",
    )

    worker_only: bool = Field(
        False,
        description=(
            "True for a tool only a sub-agent worker is offered (FRE-1564). The registry omits "
            "it from every listing the primary reads; a worker's tool loop opts in."
        ),
    )

    # Grounding metadata (ADR-0098 Amendment A2)
    referent_parameter: str | None = Field(
        None,
        description=(
            "Name of the parameter whose value is the single external referent this tool "
            "retrieves (e.g. 'url' for fetch_url). None if the tool addresses a query "
            "rather than one referent (e.g. web_search)."
        ),
    )


class ToolResult(BaseModel):
    """Result from tool execution."""

    tool_name: str = Field(..., description="Name of the executed tool")
    success: bool = Field(..., description="Whether tool execution succeeded")
    output: str | dict[str, Any] = Field(..., description="Tool-specific output")
    error: str | None = Field(None, description="Error message if failed")
    error_detail: ToolErrorDetail | None = Field(
        None, description="The model-facing failure (ADR-0156 D6.1); set on every failure"
    )
    latency_ms: float = Field(..., ge=0, description="Execution latency in milliseconds")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Extra context (e.g., files touched)"
    )
