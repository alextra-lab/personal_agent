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
from typing import Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.service.auth import RequestUser, get_request_user
from personal_agent.service.database import get_db_session
from personal_agent.service.repositories.session_repository import SessionRepository
from personal_agent.transport.agui.adapter import serialize_event
from personal_agent.transport.agui.approval_waiter import (
    ApprovalDecision,
    get_waiter_session_id,
    resolve_approval,
)
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
    log.debug("sse.client_connected", session_id=session_id)
    try:
        while True:
            if await request.is_disconnected():
                log.debug("sse.client_disconnected", session_id=session_id)
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
        log.debug("sse.stream_ended", session_id=session_id)


@router.get("/stream/{session_id}")
async def stream_session(
    session_id: str,
    request: Request,
    request_user: RequestUser = Depends(get_request_user),  # noqa: B008
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> StreamingResponse:
    """AG-UI SSE endpoint.

    Clients connect and receive real-time agent events as Server-Sent Events.
    Events follow AG-UI protocol: ``TEXT_DELTA``, ``TOOL_CALL_START``,
    ``TOOL_CALL_END``, ``STATE_DELTA``, ``INTERRUPT``, ``DONE``.

    The stream closes when:

    * A ``None`` sentinel is pushed to the session queue (normal completion).
    * The client disconnects.

    Returns 404 (not 403) when the session does not exist or belongs to
    another user — do not confirm existence of other users' sessions.

    Args:
        session_id: The session to stream events for.
        request: FastAPI request (used for disconnect detection).
        request_user: Resolved user identity (injected by FastAPI).
        db: Database session (injected by FastAPI).

    Returns:
        Streaming SSE response with ``text/event-stream`` media type.

    Raises:
        HTTPException: 404 if session not found or owned by another user.
    """
    repo = SessionRepository(db)
    session = await repo.get(UUID(session_id), user_id=request_user.user_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    log.debug("sse.stream_requested", session_id=session_id, user_id=str(request_user.user_id))
    return StreamingResponse(
        _event_generator(session_id, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class ApprovalResponseBody(BaseModel):
    """Request body for the tool-approval decision endpoint.

    Attributes:
        session_id: The session this approval card belongs to. Required (FRE-378)
            — the waiter is registered keyed by session_id, so the endpoint must
            compare against the same value the waiter holds. The PWA derives it
            from its active session; the endpoint verifies ownership against
            ``request_user.user_id`` before trusting it (FRE-378).
        decision: The human's verdict on the pending tool call.
        reason: Optional free-text explanation for the decision.
    """

    session_id: str
    decision: Literal["approve", "deny"]
    reason: str | None = None


@router.post("/approval/{request_id}")
async def submit_approval(
    request_id: str,
    body: ApprovalResponseBody,
    request_user: RequestUser = Depends(get_request_user),  # noqa: B008
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> dict[str, str]:
    """Submit a tool-approval decision for a pending approval request.

    The agent pauses tool execution and waits for a decision delivered via
    this endpoint.  The ``request_id`` must correspond to an active approval
    request **registered against the same session that the caller owns**.

    FRE-378 fix: previously the endpoint compared ``request_user.user_id``
    against the waiter's registered ``session_id`` — those values are never
    equal, so every approval returned 404 "session_mismatch". The body now
    carries an explicit ``session_id`` which is (a) verified to belong to
    the calling user via ``SessionRepository.get(sid, user_id=...)`` and
    (b) passed to ``resolve_approval()`` as the correct comparison value.

    Returns 404 (not 403) when the ``request_id`` is unknown or belongs to
    another session — do not confirm existence of other users' approval
    requests.

    Args:
        request_id: UUID string identifying the pending approval request.
        body: The approval decision, reason, and the caller's session_id.
        request_user: Resolved user identity (injected by FastAPI).
        db: Database session (injected by FastAPI) — used to verify session
            ownership before trusting the body's ``session_id``.

    Returns:
        ``{"status": "ok"}`` on success.

    Raises:
        HTTPException: 422 if ``body.session_id`` is not a valid UUID.
        HTTPException: 404 if the session does not exist, is owned by a
            different user, the request_id is unknown, or the request_id's
            registered session does not match.
    """
    # 1. Validate session_id shape.
    try:
        session_uuid = UUID(body.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="session_id must be a valid UUID") from exc

    # 2. Verify session ownership — PWA-supplied session_id cannot be trusted
    #    on its own. repo.get returns None on either "does not exist" or
    #    "owned by another user"; both collapse to 404 to avoid confirming
    #    existence of other users' sessions.
    repo = SessionRepository(db)
    owned = await repo.get(session_uuid, user_id=request_user.user_id)
    if owned is None:
        log.warning(
            "approval_endpoint.session_ownership_mismatch",
            request_id=request_id,
            requested_session_id=body.session_id,
            user_id=str(request_user.user_id),
        )
        raise HTTPException(status_code=404, detail="Approval request not found")

    # 3. Confirm the waiter exists and was registered against this session.
    waiter_session_id = get_waiter_session_id(request_id)
    if waiter_session_id is None:
        log.warning(
            "approval_endpoint.unknown_request_id",
            request_id=request_id,
            session_id=body.session_id,
            user_id=str(request_user.user_id),
        )
        raise HTTPException(status_code=404, detail="Approval request not found")

    # 4. Resolve. resolve_approval re-checks session_id against the waiter's
    #    registered value as a final guard; same-value passes through.
    decision = ApprovalDecision(decision=body.decision, reason=body.reason)
    resolved = resolve_approval(request_id, decision, body.session_id)
    if not resolved:
        log.warning(
            "approval_endpoint.resolve_failed",
            request_id=request_id,
            session_id=body.session_id,
            user_id=str(request_user.user_id),
            waiter_session_id=waiter_session_id,
        )
        raise HTTPException(status_code=404, detail="Approval request not found")

    log.info(
        "approval_endpoint.decision_submitted",
        request_id=request_id,
        session_id=body.session_id,
        decision=body.decision,
        user_id=str(request_user.user_id),
    )
    return {"status": "ok"}
