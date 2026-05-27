"""AG-UI implementation of UITransportProtocol.

Pushes internal events to a bounded per-session asyncio.Queue and durably
writes them to the Postgres ``session_events`` table via
:class:`~personal_agent.transport.agui.event_buffer.SessionEventBuffer`.
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
from typing import Any, Literal
from uuid import UUID

from personal_agent.service.database import AsyncSessionLocal
from personal_agent.telemetry import get_logger
from personal_agent.transport.agui.adapter import to_agui_event
from personal_agent.transport.agui.event_buffer import SessionEventBuffer
from personal_agent.transport.agui.ws_endpoint import (
    ApprovalDecision,
    get_event_queue,
    register_waiter,
)
from personal_agent.transport.events import (
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
    """Serialize, persist to Postgres, attach seq, and enqueue for WS delivery.

    Events are always written to Postgres (durable path).  If the
    in-memory queue is full (dead/slow client), the queue put is skipped
    and the event will be delivered on reconnect via replay.
    """
    envelope = to_agui_event(event)
    event_type = envelope["type"]

    async with AsyncSessionLocal() as db:
        buf = SessionEventBuffer(db)
        seq = await buf.append(
            session_id=UUID(session_id),
            event_type=event_type,
            payload=envelope,
        )

    envelope["seq"] = seq
    queue = get_event_queue(session_id)
    try:
        queue.put_nowait(envelope)
    except asyncio.QueueFull:
        log.warning(
            "transport.queue_full",
            session_id=session_id,
            event_type=event_type,
            seq=seq,
        )


class AGUITransport:
    """AG-UI streaming transport via WebSocket.

    Satisfies ``UITransportProtocol`` (structural typing).  Pushes typed
    internal events through the dual-write path: Postgres for durability,
    bounded asyncio.Queue for real-time delivery.

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

        decision = await register_waiter(
            session_id=session_id,
            request_id=request_id,
            timeout_seconds=timeout_seconds,
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
