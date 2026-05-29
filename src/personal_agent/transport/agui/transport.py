"""AG-UI implementation of UITransportProtocol.

Durably writes internal events to the Postgres ``session_events`` table via
:class:`~personal_agent.transport.agui.event_buffer.SessionEventBuffer`, then
pushes the sequenced envelopes to a bounded per-session asyncio.Queue.
The WebSocket endpoint in
:mod:`personal_agent.transport.agui.ws_endpoint` drains the queue and
streams events to the connected client; on reconnect, events are replayed
from Postgres.

This class satisfies :class:`~personal_agent.transport.protocols.UITransportProtocol`
via structural typing — no explicit base class is required.

See: docs/architecture_decisions/ADR-0075-websocket-transport.md
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

if TYPE_CHECKING:
    from personal_agent.error_classification import ClassifiedError

from personal_agent.service.database import AsyncSessionLocal
from personal_agent.telemetry import get_logger
from personal_agent.transport.agui.adapter import to_agui_event
from personal_agent.transport.agui.event_buffer import SessionEventBuffer
from personal_agent.transport.agui.ws_endpoint import (
    ApprovalDecision,
    WaiterMetadata,
    get_event_queue,
    register_constraint_waiter,
    register_waiter,
)
from personal_agent.transport.events import (
    CancelledEvent,
    ClassifiedErrorEvent,
    ConstraintPauseEvent,
    ConstraintResolvedEvent,
    InternalEvent,
    InterruptEvent,
    StateUpdateEvent,
    TextDeltaEvent,
    ToolApprovalRequestEvent,
    ToolEndEvent,
    ToolStartEvent,
)

log = get_logger(__name__)


async def _push_event(event: InternalEvent, session_id: str) -> None:
    """Persist an event, then enqueue the sequenced envelope for live WS delivery."""
    envelope = to_agui_event(event)
    event_type = envelope["type"]

    try:
        async with AsyncSessionLocal() as db:
            buf = SessionEventBuffer(db)
            seq = await buf.append(
                session_id=UUID(session_id),
                event_type=event_type,
                payload=envelope,
            )
        envelope["seq"] = seq
    except Exception:
        log.exception(
            "transport.persist_event_failed", session_id=session_id, event_type=event_type
        )
        return

    queue = get_event_queue(session_id)
    try:
        queue.put_nowait(envelope)
    except asyncio.QueueFull:
        log.warning(
            "transport.queue_full",
            session_id=session_id,
            event_type=event_type,
        )


async def register_and_push_constraint(
    *,
    session_id: str,
    request_id: str,
    event: ConstraintPauseEvent,
    metadata: WaiterMetadata,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Register a constraint waiter, push the pause event, await the decision.

    Registration happens before the push (race-free, ADR-0076). When no
    WebSocket connection is active, the pause event is **not** persisted — a
    client that will never see it should not get a replayed pause — and the
    default option is returned with ``resolution="connection_lost"``.

    Args:
        session_id: Target session identifier.
        request_id: Unique identifier for this pause round-trip.
        event: The ``ConstraintPauseEvent`` to deliver once registered.
        metadata: Waiter metadata (options, default) for validation/timeout.
        timeout_seconds: Seconds before the default option auto-applies.

    Returns:
        Resolution payload dict with ``decision``, ``resolution``, and an
        optional ``remember`` flag.
    """

    async def _push() -> None:
        await _push_event(event, session_id)

    return await register_constraint_waiter(
        session_id=session_id,
        request_id=request_id,
        timeout_seconds=timeout_seconds,
        metadata=metadata,
        on_registered=_push,
    )


async def emit_constraint_resolved(
    *,
    request_id: str,
    session_id: str,
    constraint: str,
    action_id: str,
    resolution: str,
) -> None:
    """Persist + enqueue a ``CONSTRAINT_RESOLVED`` event (ADR-0076)."""
    await _push_event(
        ConstraintResolvedEvent(
            request_id=request_id,
            session_id=session_id,
            constraint=constraint,
            action_id=action_id,
            resolution=resolution,  # type: ignore[arg-type]
        ),
        session_id,
    )


async def emit_cancelled(*, session_id: str, trace_id: str, reason: str = "user_cancel") -> None:
    """Persist + enqueue a ``CANCELLED`` event (ADR-0076 Stop button)."""
    await _push_event(
        CancelledEvent(session_id=session_id, trace_id=trace_id, reason=reason),
        session_id,
    )


async def emit_classified_error(
    *,
    session_id: str,
    trace_id: str,
    classified: ClassifiedError,
) -> None:
    """Persist + enqueue a ``RUN_ERROR`` event (FRE-398).

    Args:
        session_id: Target session identifier.
        trace_id: Trace context identifier for telemetry correlation.
        classified: Structured error description from the classifier.
    """
    await _push_event(
        ClassifiedErrorEvent(
            session_id=session_id,
            trace_id=trace_id,
            category=classified.category,
            reason=classified.reason,
            next_step=classified.next_step,
            actions=list(classified.actions),
            partial=classified.partial,
        ),
        session_id,
    )


async def emit_turn_status(*, session_id: str, value: Mapping[str, Any]) -> None:
    """Persist + enqueue a ``turn_status`` STATE_DELTA event (ADR-0076).

    Args:
        session_id: Target session identifier.
        value: Turn metrics payload (context tokens, tool iteration, cost).
    """
    await _push_event(
        StateUpdateEvent(key="turn_status", value=dict(value), session_id=session_id),
        session_id,
    )


async def emit_session_profile(*, session_id: str, profile: str) -> None:
    """Persist + enqueue a ``session_profile`` STATE_DELTA event (ADR-0079).

    Best-effort live notification to the active client that the session's
    server-owned execution profile changed. Correctness does not depend on
    delivery — other clients converge via hydration. See ADR-0079 §5-6.

    Args:
        session_id: Target session identifier.
        profile: The session's resolved execution profile (e.g. ``"cloud"``).
    """
    await _push_event(
        StateUpdateEvent(key="session_profile", value=profile, session_id=session_id),
        session_id,
    )


class AGUITransport:
    """AG-UI streaming transport via WebSocket.

    Satisfies ``UITransportProtocol`` (structural typing).  Pushes typed
    internal events through the sequenced dual-write path: Postgres for
    durability, bounded asyncio.Queue for real-time delivery.

    Decision round-trips (tool approvals, constraint pauses, HITL interrupts)
    use the per-connection waiter registry in
    :mod:`personal_agent.transport.agui.ws_endpoint` instead of the retired
    Future registry.
    """

    async def send_text_delta(self, text: str, session_id: str) -> None:
        """Stream an incremental text chunk to the UI.

        Args:
            text: Partial text token or chunk to deliver.
            session_id: Target session identifier.
        """
        await _push_event(TextDeltaEvent(text=text, session_id=session_id), session_id)

    async def send_tool_event(
        self, event: ToolStartEvent | ToolEndEvent | dict[str, Any], session_id: str
    ) -> None:
        """Deliver a tool lifecycle event to the UI (start or end).

        Args:
            event: Tool event payload — either a typed event or a dict with
                ``tool_name`` and optional ``args``/``result_summary``.
            session_id: Target session identifier.
        """
        if isinstance(event, (ToolStartEvent, ToolEndEvent)):
            await _push_event(event, session_id)
        elif isinstance(event, dict):
            tool_name = str(event.get("tool_name", "unknown"))
            if "result_summary" in event:
                await _push_event(
                    ToolEndEvent(
                        tool_name=tool_name,
                        result_summary=str(event["result_summary"]),
                        session_id=session_id,
                    ),
                    session_id,
                )
            else:
                await _push_event(
                    ToolStartEvent(
                        tool_name=tool_name,
                        args=event.get("args", {}),
                        session_id=session_id,
                    ),
                    session_id,
                )
        else:
            log.warning(
                "transport.send_tool_event_unknown_type",
                session_id=session_id,
                event_type=type(event).__name__,
            )

    async def send_state(self, state: Mapping[str, Any], session_id: str) -> None:
        """Push agent state key-value pairs to the UI.

        Args:
            state: JSON-serialisable state mapping.
            session_id: Target session identifier.
        """
        for key, value in state.items():
            await _push_event(
                StateUpdateEvent(key=key, value=value, session_id=session_id),
                session_id,
            )

    async def send_interrupt(self, context: Any, session_id: str) -> Any:
        """Push an interrupt event to the UI.

        Args:
            context: Either an InterruptEvent or a value to wrap.
            session_id: Target session identifier.

        Returns:
            None — response handling via WS is implemented in request_tool_approval.
        """
        if isinstance(context, InterruptEvent):
            await _push_event(context, session_id)
        else:
            await _push_event(
                InterruptEvent(
                    context=str(context),
                    options=["approve", "reject"],
                    session_id=session_id,
                ),
                session_id,
            )
        return None

    async def request_tool_approval(
        self,
        *,
        request_id: str,
        trace_id: str,
        session_id: str,
        tool: str,
        args: Mapping[str, Any],
        risk_level: Literal["low", "medium", "high"],
        reason: str,
        timeout_seconds: float = 60.0,
    ) -> ApprovalDecision:
        """Push an approval request event and await the human's decision.

        Pushes a ToolApprovalRequestEvent through the dual-write path so
        the PWA renders an approval card, then blocks on the per-connection
        waiter registry until the client sends an APPROVAL_DECISION message
        or the timeout elapses.

        Args:
            request_id: Unique identifier for this round-trip (UUID string).
            trace_id: Trace context identifier for telemetry correlation.
            session_id: Target session identifier.
            tool: Name of the tool awaiting approval.
            args: Arguments that will be passed to the tool if approved.
            risk_level: Qualitative risk label for the PWA approval card.
            reason: Human-readable explanation of why approval is required.
            timeout_seconds: Seconds before auto-returning a timeout decision.

        Returns:
            ApprovalDecision with the human's verdict or a timeout/disconnect.
        """
        expires_at = (datetime.now(UTC) + timedelta(seconds=timeout_seconds)).isoformat()

        async def _push() -> None:
            await _push_event(
                ToolApprovalRequestEvent(
                    request_id=request_id,
                    trace_id=trace_id,
                    session_id=session_id,
                    tool=tool,
                    args=args,
                    risk_level=risk_level,
                    reason=reason,
                    expires_at=expires_at,
                ),
                session_id,
            )
            log.info(
                "transport.approval_request_queued",
                request_id=request_id,
                session_id=session_id,
                trace_id=trace_id,
                tool=tool,
                risk_level=risk_level,
                timeout_seconds=timeout_seconds,
            )

        # Register the waiter BEFORE pushing the event so a fast client reply
        # can never arrive before the waiter exists (ADR-0076 race fix).
        decision = await register_waiter(
            session_id=session_id,
            request_id=request_id,
            timeout_seconds=timeout_seconds,
            on_registered=_push,
        )
        log.info(
            "transport.approval_decision_received",
            request_id=request_id,
            session_id=session_id,
            trace_id=trace_id,
            tool=tool,
            decision=decision.decision,
        )
        return decision
