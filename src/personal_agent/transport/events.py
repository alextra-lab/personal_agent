"""Internal event types for the transport layer.

These are backend-defined, protocol-agnostic events. The AG-UI adapter
converts them to wire format. Other transport implementations can use
different wire formats for the same events.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class TextDeltaEvent:
    """Streaming text chunk from LLM.

    Attributes:
        text: Partial text token or chunk delivered to the UI.
        session_id: Target session identifier.
    """

    text: str
    session_id: str


@dataclass(frozen=True)
class ToolStartEvent:
    """Tool execution started.

    Attributes:
        tool_name: Name of the tool being invoked.
        args: Arguments passed to the tool.
        session_id: Target session identifier.
    """

    tool_name: str
    args: Mapping[str, Any]
    session_id: str


@dataclass(frozen=True)
class ToolEndEvent:
    """Tool execution completed.

    Attributes:
        tool_name: Name of the tool that finished.
        result_summary: Human-readable summary of the tool result.
        session_id: Target session identifier.
    """

    tool_name: str
    result_summary: str
    session_id: str


@dataclass(frozen=True)
class StateUpdateEvent:
    """Agent state change (e.g., context budget updates).

    Attributes:
        key: State key being updated.
        value: New value for the state key.
        session_id: Target session identifier.
    """

    key: str
    value: Any
    session_id: str


@dataclass(frozen=True)
class InterruptEvent:
    """HITL approval request.

    Attributes:
        context: Description of the decision context presented to the human.
        options: Available response choices (e.g. ``["approve", "reject"]``).
        session_id: Target session identifier.
    """

    context: str
    options: Sequence[str]
    session_id: str


@dataclass(frozen=True)
class ToolApprovalRequestEvent:
    """Tool approval request pushed to the PWA before executing a gated tool.

    The PWA renders this as an approval card.  The agent pauses and awaits
    a ``POST /agui/approval/{request_id}`` response before proceeding.

    Attributes:
        request_id: Unique identifier for this approval round-trip (UUID string).
        trace_id: Trace context identifier for telemetry correlation.
        session_id: Target session identifier (used to route the SSE event).
        tool: Name of the tool awaiting approval.
        args: Arguments that will be passed to the tool if approved.
        risk_level: Qualitative risk label for the PWA to display.
        reason: Human-readable explanation of why approval is required.
        expires_at: ISO-8601 UTC timestamp after which the request times out.
    """

    request_id: str
    trace_id: str
    session_id: str
    tool: str
    args: Mapping[str, Any]
    risk_level: Literal["low", "medium", "high"]
    reason: str
    expires_at: str  # ISO-8601 UTC


# Discriminated union of all internal transport events.
InternalEvent = (
    TextDeltaEvent
    | ToolStartEvent
    | ToolEndEvent
    | StateUpdateEvent
    | InterruptEvent
    | ToolApprovalRequestEvent
)
