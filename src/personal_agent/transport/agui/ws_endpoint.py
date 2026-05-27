"""WebSocket transport endpoint (ADR-0075 / FRE-388).

Replaces the SSE endpoint (ADR-0046) with a bidirectional WebSocket
connection.  A single ``GET /ws/{session_id}`` route carries all events
in both directions, eliminating the need for separate POST endpoints
and Future registries.

Key capabilities:
- Reconnect replay via Postgres ``session_events`` table
- Per-connection decision waiter registry (replaces approval_waiter.py)
- Multi-connection eviction (one active socket per session)
- Application-level PING/PONG keepalive
- Inbound rate limiting and message size enforcement

See: docs/architecture_decisions/ADR-0075-websocket-transport.md
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from personal_agent.config.settings import get_settings
from personal_agent.service.auth import RequestUser
from personal_agent.service.database import AsyncSessionLocal
from personal_agent.service.repositories.session_repository import SessionRepository
from personal_agent.service.ws_ticket import consume_ws_ticket
from personal_agent.telemetry import get_logger
from personal_agent.transport.agui.event_buffer import SessionEventBuffer

log = get_logger(__name__)
settings = get_settings()

ws_router = APIRouter(tags=["transport"])

# ── Connection registry ────────────────────────────────────────────────────

WS_CLOSE_SUPERSEDED = 4001


@dataclass
class _ConnectionState:
    """Mutable state for an active WebSocket connection."""

    websocket: WebSocket
    user: RequestUser
    session_id: str
    outbound_queue: asyncio.Queue[dict[str, Any] | None]
    waiters: dict[str, asyncio.Event] = field(default_factory=dict)
    waiter_payloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    waiter_timeouts: dict[str, asyncio.Task[None]] = field(default_factory=dict)


_active_connections: dict[str, _ConnectionState] = {}


def get_active_connection(session_id: str) -> _ConnectionState | None:
    """Return the active connection state for a session, if any."""
    return _active_connections.get(session_id)


# ── Decision waiter API (replaces approval_waiter.py) ──────────────────────


@dataclass(frozen=True)
class ApprovalDecision:
    """Result of a HITL approval round-trip.

    Attributes:
        decision: Outcome of the approval request.
        reason: Optional human-supplied explanation for the decision.
    """

    decision: Literal["approve", "deny", "timeout", "connection_lost"]
    reason: str | None = None


async def register_waiter(
    session_id: str,
    request_id: str,
    timeout_seconds: float,
    default_decision: str = "timeout",
) -> ApprovalDecision:
    """Register a decision waiter and block until resolved or timed out.

    Args:
        session_id: Session the waiter belongs to.
        request_id: Unique request identifier for the pending decision.
        timeout_seconds: Seconds before auto-resolving with *default_decision*.
        default_decision: Decision string to use on timeout.

    Returns:
        The resolved decision.
    """
    conn = _active_connections.get(session_id)
    if conn is None:
        return ApprovalDecision(decision="connection_lost", reason="no active WS connection")

    event = asyncio.Event()
    conn.waiters[request_id] = event
    conn.waiter_payloads[request_id] = {}

    timeout_task = asyncio.create_task(
        _waiter_timeout(session_id, request_id, timeout_seconds, default_decision),
    )
    conn.waiter_timeouts[request_id] = timeout_task

    try:
        await event.wait()
    finally:
        timeout_task.cancel()
        conn.waiters.pop(request_id, None)
        conn.waiter_timeouts.pop(request_id, None)

    payload = conn.waiter_payloads.pop(request_id, {})
    decision_str = payload.get("decision", default_decision)
    reason = payload.get("reason")
    return ApprovalDecision(decision=decision_str, reason=reason)


async def _waiter_timeout(
    session_id: str,
    request_id: str,
    timeout_seconds: float,
    default_decision: str,
) -> None:
    """Background task that resolves a waiter on timeout."""
    await asyncio.sleep(timeout_seconds)
    conn = _active_connections.get(session_id)
    if conn is None:
        return
    evt = conn.waiters.get(request_id)
    if evt is not None and not evt.is_set():
        conn.waiter_payloads[request_id] = {
            "decision": default_decision,
            "reason": f"timed out after {timeout_seconds}s",
        }
        evt.set()
        log.info("ws.waiter_timeout", request_id=request_id, session_id=session_id)


def _resolve_waiter(conn: _ConnectionState, request_id: str, payload: dict[str, Any]) -> None:
    """Resolve a pending waiter with the given payload."""
    evt = conn.waiters.get(request_id)
    if evt is None:
        log.debug("ws.resolve_unknown_waiter", request_id=request_id)
        return
    if evt.is_set():
        log.debug("ws.resolve_already_set", request_id=request_id)
        return
    conn.waiter_payloads[request_id] = payload
    evt.set()
    log.info(
        "ws.waiter_resolved",
        request_id=request_id,
        decision=payload.get("decision"),
        session_id=conn.session_id,
    )


def _cancel_all_waiters(conn: _ConnectionState) -> None:
    """Resolve all pending waiters with connection_lost."""
    for request_id, evt in conn.waiters.items():
        if not evt.is_set():
            conn.waiter_payloads[request_id] = {
                "decision": "connection_lost",
                "reason": "WebSocket disconnected",
            }
            evt.set()
    for task in conn.waiter_timeouts.values():
        task.cancel()
    conn.waiters.clear()
    conn.waiter_timeouts.clear()


# ── Per-session event queues ───────────────────────────────────────────────

_session_queues: dict[str, asyncio.Queue[dict[str, Any] | None]] = {}


def get_event_queue(session_id: str) -> asyncio.Queue[dict[str, Any] | None]:
    """Get or create the bounded event queue for a session.

    Args:
        session_id: Target session.

    Returns:
        Bounded asyncio.Queue (maxsize from settings).
    """
    if session_id not in _session_queues:
        _session_queues[session_id] = asyncio.Queue(maxsize=settings.ws_event_queue_size)
    return _session_queues[session_id]


# ── Authentication helpers ─────────────────────────────────────────────────


async def _authenticate_ws(websocket: WebSocket, session_id_str: str) -> RequestUser | None:
    """Authenticate the WebSocket handshake. Returns None on failure."""
    if not settings.gateway_auth_enabled:
        from personal_agent.service.auth import _get_db_session, _get_user_with_display_name

        email = settings.agent_owner_email
        if not email:
            return None
        async with _get_db_session() as db:
            user_id, display_name = await _get_user_with_display_name(db, email)
        return RequestUser(user_id=user_id, email=email, display_name=display_name)

    ticket_id = websocket.query_params.get("ticket", "")
    if not ticket_id:
        log.warning("ws.missing_ticket", session_id=session_id_str)
        return None

    try:
        session_uuid = UUID(session_id_str)
    except ValueError:
        return None

    return consume_ws_ticket(ticket_id, session_uuid)


def _validate_origin(websocket: WebSocket) -> bool:
    """Check the Origin header against the allowlist."""
    origin = websocket.headers.get("origin", "")
    if not origin:
        return not settings.gateway_auth_enabled
    return origin in settings.allowed_ws_origins


# ── Rate limiter ───────────────────────────────────────────────────────────


class _RateLimiter:
    """Sliding-window rate limiter for inbound messages."""

    def __init__(self, max_per_second: int) -> None:
        self._max = max_per_second
        self._timestamps: list[float] = []

    def check(self) -> bool:
        """Return True if the message is within rate limits."""
        now = time.monotonic()
        self._timestamps = [t for t in self._timestamps if now - t < 1.0]
        if len(self._timestamps) >= self._max:
            return False
        self._timestamps.append(now)
        return True


# ── WebSocket endpoint ─────────────────────────────────────────────────────


@ws_router.websocket("/ws/{session_id}")
async def ws_session(websocket: WebSocket, session_id: str) -> None:
    """WebSocket endpoint for bidirectional AG-UI transport.

    Args:
        websocket: The Starlette WebSocket connection.
        session_id: Target session from the URL path.
    """
    # 1. Auth before accept
    log.info("ws.handshake_started", session_id=session_id)
    user = await _authenticate_ws(websocket, session_id)
    if user is None:
        log.warning("ws.auth_failed", session_id=session_id)
        await websocket.close(code=1008, reason="Unauthorized")
        return

    if not _validate_origin(websocket):
        log.warning(
            "ws.origin_rejected", session_id=session_id, origin=websocket.headers.get("origin", "")
        )
        await websocket.close(code=1008, reason="Origin not allowed")
        return

    # Verify session ownership
    async with AsyncSessionLocal() as db:
        repo = SessionRepository(db)
        session = await repo.get(UUID(session_id), user_id=user.user_id)
    if session is None:
        log.warning("ws.session_not_found", session_id=session_id, user_id=str(user.user_id))
        await websocket.close(code=1008, reason="Session not found")
        return

    # 2. Multi-connection eviction
    old_conn = _active_connections.get(session_id)
    if old_conn is not None:
        _cancel_all_waiters(old_conn)
        try:
            await old_conn.websocket.close(code=WS_CLOSE_SUPERSEDED, reason="Superseded")
        except Exception:
            pass
        log.info("ws.evicted_old_connection", session_id=session_id)

    # 3. Accept
    await websocket.accept()
    log.info("ws.connected", session_id=session_id, user_id=str(user.user_id))

    last_seq = await _receive_connect(websocket, session_id)
    if last_seq is None:
        try:
            await websocket.close(code=1008, reason="CONNECT required")
        except Exception:
            pass
        return

    queue = get_event_queue(session_id)
    conn = _ConnectionState(
        websocket=websocket,
        user=user,
        session_id=session_id,
        outbound_queue=queue,
    )
    _active_connections[session_id] = conn

    sender_task: asyncio.Task[None] | None = None
    receiver_task: asyncio.Task[None] | None = None
    try:
        sender_task = asyncio.create_task(_sender(conn, last_seq))
        receiver_task = asyncio.create_task(_receiver(conn))
        done, pending = await asyncio.wait(
            {sender_task, receiver_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    except Exception:
        log.exception("ws.handler_error", session_id=session_id)
    finally:
        _cancel_all_waiters(conn)
        if _active_connections.get(session_id) is conn:
            _active_connections.pop(session_id, None)
        try:
            await websocket.close()
        except Exception:
            pass
        log.info("ws.disconnected", session_id=session_id)


# ── Sender task ────────────────────────────────────────────────────────────


async def _receive_connect(ws: WebSocket, session_id: str) -> int | None:
    """Read the required CONNECT message before starting concurrent WS tasks."""
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
        msg = json.loads(raw)
    except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
        log.warning("ws.connect_handshake_failed", session_id=session_id)
        return None

    if msg.get("type") != "CONNECT":
        log.warning("ws.expected_connect", session_id=session_id, got=msg.get("type"))
        return None

    last_seq: int = msg.get("last_seq", 0)
    log.debug("ws.connect_received", session_id=session_id, last_seq=last_seq)
    return last_seq


async def _sender(conn: _ConnectionState, last_seq: int) -> None:
    """Drain the outbound queue and send events over the WebSocket."""
    ws = conn.websocket
    queue = conn.outbound_queue
    session_id = conn.session_id
    max_sent_seq = last_seq

    # Replay from Postgres only on reconnect (last_seq > 0).
    # Fresh connections (last_seq == 0) skip replay — events arrive via the
    # live queue. This avoids duplicates caused by fire-and-forget Postgres
    # writes that complete before the replay query runs.
    if last_seq > 0:
        async with AsyncSessionLocal() as db:
            buf = SessionEventBuffer(db)

            oldest = await buf.oldest_available_seq(UUID(session_id))
            if oldest is not None and last_seq < oldest:
                gap_msg = json.dumps(
                    {
                        "type": "REPLAY_GAP",
                        "seq": None,
                        "oldest_available_seq": oldest,
                    }
                )
                try:
                    await ws.send_text(gap_msg)
                except RuntimeError:
                    return
                log.info("ws.replay_gap", session_id=session_id, last_seq=last_seq, oldest=oldest)

            events = await buf.replay(UUID(session_id), after_seq=last_seq)

        for evt in events:
            payload = evt["payload"]
            seq = int(evt["seq"])
            if seq <= max_sent_seq:
                continue
            payload["seq"] = seq
            try:
                await ws.send_text(json.dumps(payload, default=str))
            except RuntimeError:
                return
            max_sent_seq = seq

    # Live drain loop
    while True:
        try:
            item = await queue.get()
        except asyncio.CancelledError:
            return
        if item is None:
            try:
                await ws.send_text(json.dumps({"type": "DONE", "seq": None}))
            except RuntimeError:
                pass
            return
        item_seq = item.get("seq")
        if isinstance(item_seq, int):
            if item_seq <= max_sent_seq:
                log.debug(
                    "ws.skip_duplicate_live_event",
                    session_id=session_id,
                    seq=item_seq,
                    max_sent_seq=max_sent_seq,
                    event_type=item.get("type"),
                )
                continue
            max_sent_seq = item_seq
        try:
            await ws.send_text(json.dumps(item, default=str))
        except RuntimeError:
            return


# ── Receiver task ──────────────────────────────────────────────────────────


async def _receiver(conn: _ConnectionState) -> None:
    """Read inbound WS messages and route to handlers."""
    ws = conn.websocket
    rate_limiter = _RateLimiter(settings.ws_rate_limit_per_second)

    while True:
        try:
            raw = await asyncio.wait_for(
                ws.receive_text(),
                timeout=float(settings.ws_ping_timeout_seconds),
            )
        except asyncio.TimeoutError:
            log.info("ws.inactivity_timeout", session_id=conn.session_id)
            try:
                await ws.close(code=1001, reason="Inactivity timeout")
            except Exception:
                pass
            return
        except WebSocketDisconnect:
            return

        if len(raw) > settings.ws_max_message_size:
            log.warning("ws.message_too_large", session_id=conn.session_id, size=len(raw))
            try:
                await ws.close(code=1008, reason="Message too large")
            except Exception:
                pass
            return

        if not rate_limiter.check():
            log.warning("ws.rate_limit_exceeded", session_id=conn.session_id)
            try:
                await ws.close(code=1008, reason="Rate limit exceeded")
            except Exception:
                pass
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue

        msg_type = msg.get("type", "")
        match msg_type:
            case "PING":
                pong = {"type": "PONG", "seq": None}
                try:
                    conn.outbound_queue.put_nowait(pong)
                except asyncio.QueueFull:
                    pass
            case "APPROVAL_DECISION" | "CONSTRAINT_DECISION" | "INTERRUPT_RESPONSE":
                request_id = msg.get("request_id", "")
                if request_id:
                    _resolve_waiter(conn, request_id, msg)
            case _:
                log.debug("ws.unknown_message_type", msg_type=msg_type, session_id=conn.session_id)


# ── Cleanup task ───────────────────────────────────────────────────────────


async def run_event_cleanup() -> None:
    """Delete expired session_events rows. Called periodically from lifespan."""
    async with AsyncSessionLocal() as db:
        buf = SessionEventBuffer(db)
        await buf.cleanup_expired(ttl_hours=settings.ws_event_ttl_hours)
