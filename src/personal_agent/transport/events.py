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


@dataclass(frozen=True)
class ConstraintPauseEvent:
    """Harness constraint about to fire — pause and request a user decision.

    Pushed when a governed constraint (tool iteration limit, context
    compression) is reached and no standing preference resolves it. The PWA
    renders a ``DecisionCard``; the agent awaits a ``CONSTRAINT_DECISION``
    response (or the ``expires_at`` timeout) before proceeding (ADR-0076).

    Attributes:
        request_id: Unique identifier for this pause round-trip (UUID string).
        session_id: Target session identifier (used to route the event).
        trace_id: Trace context identifier for telemetry correlation.
        constraint: Which constraint is firing.
        context: Human-readable description of the situation.
        options: Valid ``action_id`` values the user may choose from.
        default_option: ``action_id`` applied on timeout or disconnect.
        expires_at: ISO-8601 UTC timestamp after which the default fires.
    """

    request_id: str
    session_id: str
    trace_id: str
    constraint: Literal["tool_iteration_limit", "context_compression"]
    context: str
    options: Sequence[str]
    default_option: str
    expires_at: str  # ISO-8601 UTC


@dataclass(frozen=True)
class ConstraintResolvedEvent:
    """A constraint pause has been resolved — decision applied (ADR-0076).

    Only emitted when a ``CONSTRAINT_PAUSE`` was sent (``request_id`` is always
    set). The preference-applied path does not emit this event — it logs
    ``constraint_preference_applied`` via structlog instead, since there was no
    pause to resolve.

    Attributes:
        request_id: Identifier of the resolved pause round-trip.
        session_id: Target session identifier (used to route the event).
        constraint: Which constraint was resolved.
        action_id: Stable action identifier that was applied.
        resolution: How the decision was reached.
    """

    request_id: str
    session_id: str
    constraint: str
    action_id: str
    resolution: Literal["user_choice", "timeout_default", "connection_lost", "user_cancel"]


@dataclass(frozen=True)
class CancelledEvent:
    """Turn cancelled by the user via the Stop button (ADR-0076).

    Attributes:
        session_id: Target session identifier (used to route the event).
        trace_id: Trace context identifier for telemetry correlation.
        reason: Cancellation reason (e.g. ``"user_cancel"``).
    """

    session_id: str
    trace_id: str
    reason: str


# Discriminated union of all internal transport events.
InternalEvent = (
    TextDeltaEvent
    | ToolStartEvent
    | ToolEndEvent
    | StateUpdateEvent
    | InterruptEvent
    | ToolApprovalRequestEvent
    | ConstraintPauseEvent
    | ConstraintResolvedEvent
    | CancelledEvent
)
