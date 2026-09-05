"""Internal event types for the transport layer.

These are backend-defined, protocol-agnostic events. The AG-UI adapter
converts them to wire format. Other transport implementations can use
different wire formats for the same events.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal


class Phase(str, Enum):
    """Closed set of inference phases the transport announces (ADR-0123 §1-§2).

    Tool phases are **not** here — they continue to derive from
    :class:`ToolStartEvent` / :class:`ToolEndEvent` and must not be duplicated.
    These name the inference and human-wait boundaries that were previously
    invisible to the client (telemetry-only).

    Members:
        PLANNING: The first primary-inference round of a turn — the model
            deciding what to do (``step_planning_started``, ADR §2).
        SYNTHESIS: A post-tool primary-inference round — the model writing the
            response from gathered results (ADR §2 "final synthesis inference").
        ARTIFACT_BUILD: The artifact-draft sub-agent HTML build (the multi-minute
            silence of the measured turn two).
        EXPANSION: The parent phase spanning a concurrent sub-agent fan-out.
        SUB_AGENT: One child activity within an ``EXPANSION`` parent.
        WAITING_FOR_CHOICE: A human pause (builder card, tool approval, cost
            gate). Blocked on the user, not working — excluded from the AC-2 gap
            clock (ADR §1).
    """

    PLANNING = "planning"
    SYNTHESIS = "synthesis"
    ARTIFACT_BUILD = "artifact_build"
    EXPANSION = "expansion"
    SUB_AGENT = "sub_agent"
    WAITING_FOR_CHOICE = "waiting_for_choice"


#: The governed constraints a ``ConstraintPauseEvent`` may carry (ADR-0076 +
#: ADR-0122 §3). Widened by FRE-881 from the original closed pair to (a) admit
#: ``artifact_builder`` — the first computed-options decision type — and (b) close
#: the pre-existing ``attachment_cost`` drift: that constraint was already passed
#: at the executor's attachment-cost gate yet was absent from the literal, riding a
#: ``# type: ignore[arg-type]`` at the pause helper. Both are now first-class.
ConstraintName = Literal[
    "tool_iteration_limit",
    "context_compression",
    "attachment_cost",
    "artifact_builder",
]


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
class PhaseStartEvent:
    """An inference / human-wait phase began (ADR-0123 §2).

    Emitted alongside — never duplicating — the tool events, on the best-effort
    ``_push_event`` path. The ``started_at`` server timestamp is what the client
    computes advancing elapsed from, so a reconnect mid-phase does not reset the
    counter (ADR §3/§6).

    Attributes:
        phase: Which phase began (see :class:`Phase`).
        phase_id: Unique id for this phase instance. Pairs with the matching
            :class:`PhaseEndEvent` and distinguishes concurrent children that
            share a ``phase`` value.
        session_id: Target session identifier (routes the event).
        started_at: ISO-8601 UTC server timestamp of the phase start.
        detail: Optional human-readable qualifier (e.g. a sub-agent task name).
        parent_id: When set, the ``phase_id`` of the parent phase this is a child
            of — the concurrent-children model (ADR §1, AC-8).
    """

    phase: Phase
    phase_id: str
    session_id: str
    started_at: str  # ISO-8601 UTC
    detail: str | None = None
    parent_id: str | None = None


@dataclass(frozen=True)
class PhaseEndEvent:
    """An inference / human-wait phase ended (ADR-0123 §2).

    Attributes:
        phase: Which phase ended (see :class:`Phase`).
        phase_id: Id of the phase instance that ended (pairs with its start).
        session_id: Target session identifier (routes the event).
        parent_id: The parent's ``phase_id`` when this ended a child, so the
            client can resolve the child against its parent without a lookup.
        ok: ``False`` when the phase ended because the wrapped work raised
            (FRE-936 / AC-9(b)). ``phase_span``'s pairing guarantee means a
            ``PhaseEndEvent`` fires on *every* exit, success or exception —
            without this flag a failed phase is wire-identical to a
            successful one, and the client would render a green check for
            work that just failed.
        ended_at: ISO-8601 UTC server timestamp of the phase end (ADR-0142,
            FRE-1391). Paired with :class:`PhaseStartEvent`'s ``started_at`` so a
            ``WAITING_FOR_CHOICE`` phase has a measurable duration — without it,
            end minus start cannot be computed for a human pause at all.
    """

    phase: Phase
    phase_id: str
    session_id: str
    parent_id: str | None = None
    ok: bool = True
    ended_at: str | None = None  # ISO-8601 UTC


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
        session_id: Target session identifier (used to route the event).
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
    compression, attachment cost, or the artifact-builder selection) is reached
    and no standing preference resolves it. The PWA renders a ``DecisionCard``;
    the agent awaits a ``CONSTRAINT_DECISION`` response (or the ``expires_at``
    timeout) before proceeding (ADR-0076 / ADR-0122 §3).

    Attributes:
        request_id: Unique identifier for this pause round-trip (UUID string).
        session_id: Target session identifier (used to route the event).
        trace_id: Trace context identifier for telemetry correlation.
        constraint: Which constraint is firing (see :data:`ConstraintName`). For a
            computed-options constraint the ``options`` are derived from the
            ADR-0121 catalog rather than the static registry.
        context: Human-readable description of the situation.
        options: Valid ``action_id`` values the user may choose from.
        default_option: ``action_id`` applied on timeout or disconnect.
        expires_at: ISO-8601 UTC timestamp after which the default fires.
    """

    request_id: str
    session_id: str
    trace_id: str
    constraint: ConstraintName
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


@dataclass(frozen=True)
class ClassifiedErrorEvent:
    """A turn failed with a classified, actionable error (FRE-398).

    Pushed when a turn ends in failure so the PWA can render a distinct
    error surface with guidance and optional action buttons.  One-way
    (no client round-trip); the PWA renders the reason and next_step text,
    and the ``actions`` list drives future button labels in PR2.

    Attributes:
        session_id: Target session identifier (used to route the event).
        trace_id: Trace context identifier for telemetry correlation.
        category: Machine-readable failure class (mirrors
            :class:`~personal_agent.error_classification.ClassifiedError`).
        reason: Human-readable explanation of what happened.
        next_step: Concrete guidance for what the user can do next.
        actions: Stable action ids for PWA buttons (e.g. ``retry``).
        partial: ``True`` when a partial reply was salvaged from gathered work.
    """

    session_id: str
    trace_id: str
    category: Literal[
        "model_server",
        "timeout",
        "connection",
        "rate_limit",
        "budget_denied",
        "tool_failure",
        "attachment_unsupported",
        "generic",
    ]
    reason: str
    next_step: str
    actions: Sequence[str]
    partial: bool


# Discriminated union of all internal transport events.
InternalEvent = (
    TextDeltaEvent
    | ToolStartEvent
    | ToolEndEvent
    | PhaseStartEvent
    | PhaseEndEvent
    | StateUpdateEvent
    | InterruptEvent
    | ToolApprovalRequestEvent
    | ConstraintPauseEvent
    | ConstraintResolvedEvent
    | CancelledEvent
    | ClassifiedErrorEvent
)
