"""Type definitions for the LLM client module.

This module defines the core types used by the LocalLLMClient:
- ModelRole: Enum for different model roles (router, reasoning, coding)
- LLMResponse: Response structure from LLM calls
- ToolCall: Tool call structure for function calling
- Error classes: Hierarchy of LLM client errors
"""

from enum import Enum
from typing import Any

from typing_extensions import TypedDict


class ModelRole(str, Enum):
    """Model roles for different use cases.

    These map to configured models in config/models.yaml.
    """

    ROUTER = "router"
    STANDARD = "standard"
    REASONING = "reasoning"
    CODING = "coding"

    @classmethod
    def from_str(cls, value: str) -> "ModelRole | None":
        """Convert string to ModelRole enum.

        Args:
            value: String representation (case-insensitive).

        Returns:
            ModelRole enum or None if invalid.
        """
        value_lower = value.lower()
        for role in cls:
            if role.value == value_lower:
                return role
        return None


class ToolCall(TypedDict):
    """Tool call structure for function calling.

    Attributes:
        id: Unique identifier for the tool call.
        name: Name of the tool to call.
        arguments: JSON string containing tool arguments.
    """

    id: str
    name: str
    arguments: str  # JSON string


class LLMResponse(TypedDict):
    """Response structure from LLM calls.

    This is a Responses-style interface that normalizes differences between
    different backend APIs (chat_completions vs responses).

    Attributes:
        role: Response role (typically "assistant").
        content: Final natural language content from the model.
        tool_calls: List of tool calls if the model requested tool execution.
        reasoning_trace: Optional reasoning trace from the model (if available).
        usage: Token usage information (prompt_tokens, completion_tokens, etc.).
        response_id: Response ID from /v1/responses API (for stateful conversation).
        raw: Raw response from the backend for debugging.
    """

    role: str  # "assistant"
    content: str
    tool_calls: list[ToolCall]
    reasoning_trace: str | None
    usage: dict[str, Any]
    response_id: str | None
    raw: dict[str, Any]


class LLMStreamEvent(TypedDict):
    """Streaming event structure.

    Attributes:
        type: Event type (token, tool_call, trace, done, error).
        data: Event data (varies by type).
    """

    type: str  # "token" | "tool_call" | "trace" | "done" | "error"
    data: Any


# Error hierarchy


class LLMClientError(Exception):
    """Base exception for all LLM client errors."""

    pass


class LLMTimeout(LLMClientError):
    """Raised when an LLM request times out."""

    pass


class LLMConnectionError(LLMClientError):
    """Raised when connection to LLM server fails."""

    pass


class LLMRateLimit(LLMClientError):
    """Raised when LLM server returns rate limit error."""

    pass


class LLMServerError(LLMClientError):
    """Raised when LLM server returns an error (5xx)."""

    pass


class LLMInvalidResponse(LLMClientError):
    """Raised when LLM server returns invalid or unexpected response format."""

    pass
