"""AG-UI implementation of UITransportProtocol.

Pushes internal events to the per-session SSE queue maintained by
:mod:`personal_agent.transport.agui.endpoint`.  The SSE endpoint streams
those events to any connected client.

This class satisfies :class:`~personal_agent.transport.protocols.UITransportProtocol`
via structural typing — no explicit base class is required.

See: docs/architecture_decisions/ADR-0046.md
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog

from personal_agent.transport.agui.endpoint import get_event_queue
from personal_agent.transport.events import (
    InterruptEvent,
    StateUpdateEvent,
    TextDeltaEvent,
    ToolEndEvent,
    ToolStartEvent,
)

log = structlog.get_logger(__name__)


class AGUITransport:
    """AG-UI streaming transport via SSE.

    Satisfies ``UITransportProtocol`` (structural typing).  Pushes typed
    internal events to the per-session async queue; the SSE endpoint in
    :mod:`personal_agent.transport.agui.endpoint` streams those events to
    the connected frontend.

    Note:
        ``send_interrupt`` currently pushes the interrupt event and returns
        ``None`` immediately.  Actual HITL response handling will be
        implemented when the PWA (FRE-209) is ready.
    """

    async def send_text_delta(self, text: str, session_id: str) -> None:
        """Stream an incremental text chunk to the UI.

        Args:
            text: Partial text token or chunk to deliver.
            session_id: Target session identifier.
        """
        queue = get_event_queue(session_id)
        await queue.put(TextDeltaEvent(text=text, session_id=session_id))
        log.debug("transport.text_delta_queued", session_id=session_id, length=len(text))

    async def send_tool_event(self, event: Any, session_id: str) -> None:
        """Deliver a tool lifecycle event to the UI (start or end).

        Accepts both :class:`~personal_agent.transport.events.ToolStartEvent`
        and :class:`~personal_agent.transport.events.ToolEndEvent`.  If the
        caller passes a raw :class:`~personal_agent.transport.events.ToolStartEvent`
        or :class:`~personal_agent.transport.events.ToolEndEvent` with a
        matching ``session_id``, it is pushed as-is; otherwise a
        ``ToolStartEvent`` is constructed from the payload.

        Args:
            event: Tool event payload — either a
                :class:`~personal_agent.transport.events.ToolStartEvent`,
                :class:`~personal_agent.transport.events.ToolEndEvent`,
                or a dict with ``tool_name`` and optional ``args``/``result_summary``.
            session_id: Target session identifier.
        """
        queue = get_event_queue(session_id)
        if isinstance(event, (ToolStartEvent, ToolEndEvent)):
            await queue.put(event)
        elif isinstance(event, dict):
            tool_name = str(event.get("tool_name", "unknown"))
            if "result_summary" in event:
                await queue.put(
                    ToolEndEvent(
                        tool_name=tool_name,
                        result_summary=str(event["result_summary"]),
                        session_id=session_id,
                    )
                )
            else:
                await queue.put(
                    ToolStartEvent(
                        tool_name=tool_name,
                        args=event.get("args", {}),
                        session_id=session_id,
                    )
                )
        else:
            log.warning(
                "transport.send_tool_event_unknown_type",
                session_id=session_id,
                event_type=type(event).__name__,
            )

    async def send_state(self, state: Mapping[str, Any], session_id: str) -> None:
        """Push agent state key-value pairs to the UI.

        Each key-value pair in ``state`` is emitted as a separate
        :class:`~personal_agent.transport.events.StateUpdateEvent`.

        Args:
            state: JSON-serialisable state mapping (e.g. mode, memory summary).
            session_id: Target session identifier.
        """
        queue = get_event_queue(session_id)
        for key, value in state.items():
            await queue.put(StateUpdateEvent(key=key, value=value, session_id=session_id))
        log.debug("transport.state_queued", session_id=session_id, keys=list(state.keys()))

    async def send_interrupt(self, context: Any, session_id: str) -> Any:
        """Suspend execution and signal a human decision request.

        Pushes an :class:`~personal_agent.transport.events.InterruptEvent` to
        the session queue.  Returns ``None`` immediately — actual HITL
        response handling (awaiting the human reply) will be implemented
        when the PWA (FRE-209) is ready.

        Args:
            context: Either an
                :class:`~personal_agent.transport.events.InterruptEvent`
                (pushed as-is) or any value that will be converted to a
                string context with default options ``["approve", "reject"]``.
            session_id: Target session identifier.

        Returns:
            ``None`` — placeholder until FRE-209 implements response handling.
        """
        queue = get_event_queue(session_id)
        if isinstance(context, InterruptEvent):
            await queue.put(context)
        else:
            await queue.put(
                InterruptEvent(
                    context=str(context),
                    options=["approve", "reject"],
                    session_id=session_id,
                )
            )
        log.info("transport.interrupt_queued", session_id=session_id)
        return None  # Response handling deferred to FRE-209
