"""Converts internal events to AG-UI wire format (JSON for SSE).

The AG-UI adapter maps backend-agnostic :mod:`personal_agent.transport.events`
types to the AG-UI protocol JSON envelope.  Each event type maps one-to-one
to an AG-UI event kind:

* ``TEXT_DELTA``     — streaming text chunk
* ``TOOL_CALL_START`` — tool invocation begun
* ``TOOL_CALL_END``  — tool invocation complete
* ``STATE_DELTA``    — agent state change
* ``INTERRUPT``      — HITL approval request

See: docs/architecture_decisions/ADR-0046.md
"""

from __future__ import annotations

import json
from typing import Any

from personal_agent.transport.events import (
    InternalEvent,
    InterruptEvent,
    StateUpdateEvent,
    TextDeltaEvent,
    ToolApprovalRequestEvent,
    ToolEndEvent,
    ToolStartEvent,
)


def to_agui_event(event: InternalEvent) -> dict[str, Any]:
    """Convert an internal event to AG-UI wire format.

    AG-UI event types:

    * ``TEXT_DELTA``: streaming text
    * ``TOOL_CALL_START``: tool invocation begun
    * ``TOOL_CALL_END``: tool invocation complete
    * ``STATE_DELTA``: agent state change
    * ``INTERRUPT``: HITL approval request

    Args:
        event: Internal event to convert.

    Returns:
        Dict ready for JSON serialization as SSE data field.

    Examples:
        >>> from personal_agent.transport.events import TextDeltaEvent
        >>> to_agui_event(TextDeltaEvent(text="hello", session_id="s1"))
        {'type': 'TEXT_DELTA', 'data': {'text': 'hello'}, 'session_id': 's1'}
    """
    match event:
        case TextDeltaEvent(text=text, session_id=sid):
            return {"type": "TEXT_DELTA", "data": {"text": text}, "session_id": sid}
        case ToolStartEvent(tool_name=name, args=args, session_id=sid):
            return {
                "type": "TOOL_CALL_START",
                "data": {"tool_name": name, "args": dict(args)},
                "session_id": sid,
            }
        case ToolEndEvent(tool_name=name, result_summary=summary, session_id=sid):
            return {
                "type": "TOOL_CALL_END",
                "data": {"tool_name": name, "result": summary},
                "session_id": sid,
            }
        case StateUpdateEvent(key=key, value=value, session_id=sid):
            return {"type": "STATE_DELTA", "data": {"key": key, "value": value}, "session_id": sid}
        case InterruptEvent(context=ctx, options=opts, session_id=sid):
            return {
                "type": "INTERRUPT",
                "data": {"context": ctx, "options": list(opts)},
                "session_id": sid,
            }
        case ToolApprovalRequestEvent(
            request_id=request_id,
            trace_id=trace_id,
            session_id=_sid,
            tool=tool,
            args=args,
            risk_level=risk_level,
            reason=reason,
            expires_at=expires_at,
        ):
            return {
                "type": "tool_approval_request",
                "request_id": request_id,
                "trace_id": trace_id,
                "tool": tool,
                "args": dict(args),
                "risk_level": risk_level,
                "reason": reason,
                "expires_at": expires_at,
            }
        case _:
            raise ValueError(f"Unhandled event type: {type(event).__name__}")


def serialize_event(event: InternalEvent) -> str:
    """Serialize an internal event to JSON string for SSE data field.

    Args:
        event: Internal event to serialize.

    Returns:
        JSON-encoded string of the AG-UI envelope.

    Examples:
        >>> from personal_agent.transport.events import TextDeltaEvent
        >>> import json
        >>> raw = serialize_event(TextDeltaEvent(text="hi", session_id="s1"))
        >>> json.loads(raw)["type"]
        'TEXT_DELTA'
    """
    return json.dumps(to_agui_event(event))
