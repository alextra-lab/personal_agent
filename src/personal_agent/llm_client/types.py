"""Type definitions for the LLM client module.

This module defines the core types used by the LLM clients:
- ModelRole: Two-tier model taxonomy (PRIMARY + SUB_AGENT) — ADR-0033
- LLMResponse: Response structure from LLM calls
- ToolCall: Tool call structure for function calling
- Error classes: Hierarchy of LLM client errors
"""

from enum import Enum
from typing import Any

from typing_extensions import NotRequired, TypedDict


class ModelRole(str, Enum):
    """Model roles identifying what a given LLM call is for (FRE-1037).

    Tier 1 (Primary): The orchestrator brain — reasoning, tool calling, decomposition.
    Tier 2 (Sub-Agent): Focused single-task completion — no thinking, fast inference.
    Compressor: Lightweight summarization of evicted context turns (ADR-0038).

    See ADR-0033 for the two-tier taxonomy rationale.

    The remaining members mirror ``config/model_roles.yaml``'s ``bindings:`` block
    (ADR-0099/ADR-0121's single generative source of truth for role→model
    assignment) — ``tests/test_llm_client/test_types.py::test_model_role_matches_bindings_matrix``
    asserts every matrix role is representable here, so the two can't drift apart
    again the way they had (FRE-1037). ``SKILL_ROUTING`` and ``STUDY`` are the two
    documented exceptions: real, live background roles that resolve outside the
    matrix (a dedicated ``AppConfig`` field and a script-local convention,
    respectively) rather than through ``config/model_roles.yaml``.
    """

    PRIMARY = "primary"  # Tier 1: orchestrator brain
    SUB_AGENT = "sub_agent"  # Tier 2: focused task completion
    COMPRESSOR = "compressor"  # Context compression / summarization
    ARTIFACT_BUILDER = "artifact_builder"  # HTML artifact generation (ADR-0118 T1)
    ENTITY_EXTRACTION = "entity_extraction"
    CAPTAINS_LOG = "captains_log"
    SESSION_SUMMARY = "session_summary"
    INSIGHTS = "insights"
    EMBEDDING = "embedding"
    RERANKER = "reranker"
    RERANKER_FALLBACK = "reranker_fallback"
    VISION = "vision"
    SPAN_EXTRACTION = "span_extraction"  # ADR-0138 D1 span classifier (FRE-1281)
    ENTAILMENT = "entailment"  # ADR-0138 D3(d) entailment judge (FRE-1286)
    SKILL_ROUTING = "skill_routing"  # matrix-independent — AppConfig.skill_routing_model_key
    STUDY = "study"  # matrix-independent — scripts/study/categorizer.py

    @classmethod
    def from_str(cls, value: str) -> "ModelRole | None":
        """Convert string to ModelRole enum (case-insensitive).

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

    @classmethod
    def required(cls, value: str) -> "ModelRole":
        """Convert string to ModelRole, raising rather than silently defaulting.

        Use this at call-time role *assignment* boundaries (a caller determining
        which role a live call belongs to). ``from_str`` stays available for
        reconstructing a role from persisted history, where a stale/corrupt value
        is a data-resilience concern, not a live misassignment (FRE-1037 step 3).

        Args:
            value: String representation (case-insensitive).

        Returns:
            The matching ModelRole.

        Raises:
            ValueError: If ``value`` does not match any ModelRole member.
        """
        role = cls.from_str(value)
        if role is None:
            raise ValueError(f"{value!r} is not a valid ModelRole")
        return role


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
        cost_usd: Per-call cost in USD. Populated on paid (cloud) calls so the
            executor can accumulate ``turn_cost_usd`` for the live status bar;
            omitted on self-hosted (free) local calls (read as 0.0 by callers).
        finish_reason: Why generation stopped, in OpenAI's vocabulary (``stop``,
            ``length``, ``tool_calls``, …). Populated on cloud calls; **absent**
            elsewhere rather than defaulted, because ``"stop"`` and "we did not look"
            must stay distinguishable — a caller that cannot tell a truncated reply
            from a complete one reads a sizing fault as a format fault (FRE-996).
    """

    role: str  # "assistant"
    content: str
    tool_calls: list[ToolCall]
    reasoning_trace: str | None
    usage: dict[str, Any]
    response_id: str | None
    raw: dict[str, Any]
    cost_usd: NotRequired[float]
    finish_reason: NotRequired[str | None]


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
