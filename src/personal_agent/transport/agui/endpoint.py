"""AG-UI SSE streaming endpoint.

Clients connect via ``GET /stream/{session_id}`` and receive real-time
agent events as Server-Sent Events.  Events follow the AG-UI protocol:
``TEXT_DELTA``, ``TOOL_CALL_START``, ``TOOL_CALL_END``, ``STATE_DELTA``,
``INTERRUPT``, and the terminal ``DONE`` sentinel.

The module maintains a per-session :class:`asyncio.Queue` that the
orchestrator pushes events into.  The SSE generator drains the queue and
yields formatted SSE strings until a ``None`` sentinel or client disconnect
is detected.

Keepalive comments (``": keepalive"`` lines) are emitted every 30 s when
the queue is idle, preventing proxies and load-balancers from closing the
connection prematurely.

See: docs/architecture_decisions/ADR-0046.md
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from personal_agent.transport.agui.adapter import serialize_event
from personal_agent.transport.events import InternalEvent

log = structlog.get_logger(__name__)

router = APIRouter(tags=["transport"])

# Per-session event queues.  The orchestrator pushes events here;
# the SSE endpoint reads them and streams to connected clients.
# Type: dict[session_id, Queue[InternalEvent | None]]
_session_queues: dict[str, asyncio.Queue[InternalEvent | None]] = {}


def get_event_queue(session_id: str) -> asyncio.Queue[InternalEvent | None]:
    """Get or create the event queue for a session.

    Idempotent — calling with the same ``session_id`` twice returns the
    same :class:`asyncio.Queue` instance.

    Args:
        session_id: The session to get or create the queue for.

    Returns:
        Async queue for pushing and consuming events.
    """
    if session_id not in _session_queues:
        _session_queues[session_id] = asyncio.Queue()
    return _session_queues[session_id]


def cleanup_session(session_id: str) -> None:
    """Remove the event queue for a session.

    Safe to call even if the session has no queue.

    Args:
        session_id: The session to clean up.
    """
    _session_queues.pop(session_id, None)


async def _event_generator(
    session_id: str,
    request: Request,
) -> AsyncGenerator[str, None]:
    r"""Generate SSE events from the session queue.

    Yields SSE-formatted strings until the session ends (``None`` sentinel)
    or the client disconnects.

    Args:
        session_id: Target session to stream.
        request: FastAPI request, used for disconnect detection.

    Yields:
        SSE-formatted strings (``"data: {...}\n\n"`` or keepalive comments).
    """
    queue = get_event_queue(session_id)
    log.info("sse.client_connected", session_id=session_id)
    try:
        while True:
            if await request.is_disconnected():
                log.info("sse.client_disconnected", session_id=session_id)
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # Send keepalive comment to prevent connection timeout.
                yield ": keepalive\n\n"
                continue
            if event is None:
                # None sentinel signals stream completion.
                yield f"data: {json.dumps({'type': 'DONE'})}\n\n"
                break
            yield f"data: {serialize_event(event)}\n\n"
    finally:
        cleanup_session(session_id)
        log.info("sse.stream_ended", session_id=session_id)


@router.get("/stream/{session_id}")
async def stream_session(session_id: str, request: Request) -> StreamingResponse:
    """AG-UI SSE endpoint.

    Clients connect and receive real-time agent events as Server-Sent Events.
    Events follow AG-UI protocol: ``TEXT_DELTA``, ``TOOL_CALL_START``,
    ``TOOL_CALL_END``, ``STATE_DELTA``, ``INTERRUPT``, ``DONE``.

    The stream closes when:

    * A ``None`` sentinel is pushed to the session queue (normal completion).
    * The client disconnects.

    Args:
        session_id: The session to stream events for.
        request: FastAPI request (used for disconnect detection).

    Returns:
        Streaming SSE response with ``text/event-stream`` media type.
    """
    log.info("sse.stream_requested", session_id=session_id)
    return StreamingResponse(
        _event_generator(session_id, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
