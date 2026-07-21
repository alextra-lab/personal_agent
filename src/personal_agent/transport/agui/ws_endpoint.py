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
from collections.abc import Awaitable, Callable
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
class WaiterMetadata:
    """Metadata stored alongside a constraint waiter for decision validation.

    Attributes:
        constraint: Constraint name this waiter is pausing for.
        options: Valid ``action_id`` values accepted from the client.
        default_option: ``action_id`` applied on timeout, disconnect, or an
            invalid incoming decision.
        created_at: Monotonic time the waiter was registered.
    """

    constraint: str
    options: list[str]
    default_option: str
    created_at: float


@dataclass
class _ConnectionState:
    """Mutable state for an active WebSocket connection.

    Note the waiter dicts here serve **approval** waiters only
    (:func:`register_waiter`). Constraint waiters are session-scoped — see
    :data:`_session_constraint_waiters` — because a mobile client's socket drops
    and returns constantly, and a decision must outlive its connection (FRE-928).

    Liveness is not tracked here: the receiver's bounded
    :data:`AppConfig.ws_ping_timeout_seconds` read wait is the single mechanism that
    detects a client which stopped responding without closing (FRE-928 AC-6). A
    second, unread "last seen" field would only invite false trust.
    """

    websocket: WebSocket
    user: RequestUser
    session_id: str
    outbound_queue: asyncio.Queue[dict[str, Any] | None]
    waiters: dict[str, asyncio.Event] = field(default_factory=dict)
    waiter_payloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    waiter_timeouts: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    cancel_requested: bool = False


_active_connections: dict[str, _ConnectionState] = {}


@dataclass
class _ConstraintWaiter:
    """A pending constraint decision, owned by the session rather than a socket.

    Attributes:
        event: Set when the decision resolves (user choice, cancel, or timeout).
        payload: Resolution payload filled in by whichever path resolves it.
        metadata: Options/default used for validation and timeout fallback.
        timeout_task: Background task applying the default when the timeout expires.
    """

    event: asyncio.Event
    payload: dict[str, Any]
    metadata: WaiterMetadata
    timeout_task: asyncio.Task[None] | None = None


#: Pending constraint waiters keyed ``session_id -> request_id -> waiter`` (FRE-928).
#:
#: Session-scoped, not connection-scoped: a brief disconnect is the *normal* condition
#: for a mobile client, so a pending decision must survive one. The transport already
#: persists pause events and replays them from the client's last sequence number, so a
#: client reconnecting inside the timeout receives the card and resolves the original
#: waiter. Only an explicit decision, a Stop press, or the timeout resolves a waiter.
#:
#: This is an in-process registry, which is sound because the service runs as a single
#: uvicorn worker (no ``--workers`` in ``Dockerfile.gateway`` or ``docker-compose.cloud.yml``).
#: Scaling to multiple workers would require moving this to shared state (e.g. Redis).
_session_constraint_waiters: dict[str, dict[str, _ConstraintWaiter]] = {}


def get_active_connection(session_id: str) -> _ConnectionState | None:
    """Return the active connection state for a session, if any."""
    return _active_connections.get(session_id)


def is_cancel_requested(session_id: str) -> bool:
    """Return whether the user requested cancellation for this session (ADR-0076).

    Args:
        session_id: Session to check.

    Returns:
        True if a ``USER_CANCEL`` was received on the active connection and not
        yet cleared. False when no connection is active.
    """
    conn = _active_connections.get(session_id)
    return conn is not None and conn.cancel_requested


def clear_cancel_flag(session_id: str) -> None:
    """Reset the cancellation flag at the start of a new turn (ADR-0076).

    Args:
        session_id: Session whose cancel flag should be cleared.
    """
    conn = _active_connections.get(session_id)
    if conn is not None:
        conn.cancel_requested = False


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


@dataclass(frozen=True)
class ConstraintDecision:
    """Result of a constraint pause round-trip (ADR-0076).

    Separate from :class:`ApprovalDecision` because constraint decisions use
    open-ended ``action_id`` values that vary per constraint type, plus a
    ``remember`` flag for persisting a standing preference.

    Attributes:
        decision: The chosen ``action_id`` (validated against waiter options).
        remember: True if the user asked to remember this choice.
        request_id: Identifier of the pause round-trip.
        resolution: How the decision was reached.
    """

    decision: str
    remember: bool
    request_id: str
    resolution: Literal["user_choice", "timeout_default", "connection_lost", "user_cancel"]


async def register_waiter(
    session_id: str,
    request_id: str,
    timeout_seconds: float,
    default_decision: str = "timeout",
    on_registered: Callable[[], Awaitable[None]] | None = None,
) -> ApprovalDecision:
    """Register a decision waiter and block until resolved or timed out.

    The waiter is registered **before** ``on_registered`` runs, closing the
    race where a client responds to a pushed event before the waiter exists
    (ADR-0076). Callers pass the event-push coroutine as ``on_registered`` so
    the push happens only after the waiter is ready to receive the reply.

    Args:
        session_id: Session the waiter belongs to.
        request_id: Unique request identifier for the pending decision.
        timeout_seconds: Seconds before auto-resolving with *default_decision*.
        default_decision: Decision string to use on timeout.
        on_registered: Optional coroutine run after registration (typically the
            event push). For the no-connection case it still runs so the event
            is persisted for replay, then a ``connection_lost`` decision returns.

    Returns:
        The resolved decision.
    """
    conn = _active_connections.get(session_id)
    if conn is None:
        if on_registered is not None:
            await on_registered()
        return ApprovalDecision(decision="connection_lost", reason="no active WS connection")

    event = asyncio.Event()
    conn.waiters[request_id] = event
    conn.waiter_payloads[request_id] = {}

    timeout_task = asyncio.create_task(
        _waiter_timeout(session_id, request_id, timeout_seconds, default_decision),
    )
    conn.waiter_timeouts[request_id] = timeout_task

    if on_registered is not None:
        await on_registered()

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


async def register_constraint_waiter(
    session_id: str,
    request_id: str,
    timeout_seconds: float,
    metadata: WaiterMetadata,
    on_registered: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Register a constraint-pause waiter and block until resolved (ADR-0076).

    Unlike :func:`register_waiter` (which wraps the result in an
    :class:`ApprovalDecision`), this returns the raw resolution payload so the
    caller can read ``decision`` (action_id), ``resolution``, and ``remember``.
    The waiter is registered before ``on_registered`` runs (race-free push).

    Args:
        session_id: Session the waiter belongs to.
        request_id: Unique identifier for this pause round-trip.
        timeout_seconds: Seconds before auto-resolving with the default option.
        metadata: Constraint metadata (options, default) used for validation
            and timeout resolution.
        on_registered: Optional coroutine run after registration (the pause
            event push). Always invoked — including when no connection is
            active, so the event is persisted and can be replayed on reconnect.

    Returns:
        Resolution payload dict with at least ``decision`` and ``resolution``.

    Raises:
        Exception: Whatever ``on_registered`` raises, after the waiter is
            de-registered (a failed push must not leak a pending waiter).
    """
    bucket = _session_constraint_waiters.setdefault(session_id, {})
    if request_id in bucket:
        # Not reachable today (request_id is a per-pause UUID4), but an overwrite would
        # orphan the first waiter: its timeout task would resolve the survivor, leaving
        # the original awaiting forever with no timeout. Fail loudly instead.
        raise ValueError(f"constraint waiter already registered: {session_id}/{request_id}")

    waiter = _ConstraintWaiter(event=asyncio.Event(), payload={}, metadata=metadata)
    bucket[request_id] = waiter

    # The try opens BEFORE the push and the timeout task: session-scoped waiters
    # lose the accidental sweep that disconnect gave connection-scoped ones, so a
    # cancelled executor or a failing push would otherwise leak the entry forever.
    try:
        waiter.timeout_task = asyncio.create_task(
            _constraint_waiter_timeout(
                session_id, request_id, timeout_seconds, metadata.default_option
            ),
        )

        if on_registered is not None:
            await on_registered()

        await waiter.event.wait()
    finally:
        if waiter.timeout_task is not None:
            waiter.timeout_task.cancel()
        _discard_constraint_waiter(session_id, request_id)

    payload = waiter.payload
    payload.setdefault("decision", metadata.default_option)
    payload.setdefault("resolution", "user_choice")
    return payload


def _discard_constraint_waiter(session_id: str, request_id: str) -> None:
    """Remove a constraint waiter, dropping the session bucket once it empties."""
    bucket = _session_constraint_waiters.get(session_id)
    if bucket is None:
        return
    bucket.pop(request_id, None)
    if not bucket:
        _session_constraint_waiters.pop(session_id, None)


def _get_constraint_waiter(session_id: str, request_id: str) -> _ConstraintWaiter | None:
    """Return the pending constraint waiter for a session/request, if any."""
    return _session_constraint_waiters.get(session_id, {}).get(request_id)


async def _constraint_waiter_timeout(
    session_id: str,
    request_id: str,
    timeout_seconds: float,
    default_option: str,
) -> None:
    """Background task that resolves a constraint waiter on timeout.

    Resolves against the session registry, not the connection: the whole point of
    the timeout is to bound a wait during which no connection may be attached
    (FRE-928 AC-2).
    """
    await asyncio.sleep(timeout_seconds)
    waiter = _get_constraint_waiter(session_id, request_id)
    if waiter is not None and not waiter.event.is_set():
        waiter.payload = {
            "decision": default_option,
            "resolution": "timeout_default",
        }
        waiter.event.set()
        log.info(
            "ws.constraint_waiter_timeout",
            request_id=request_id,
            session_id=session_id,
            default_option=default_option,
        )


def _resolve_waiter(conn: _ConnectionState, request_id: str, payload: dict[str, Any]) -> None:
    """Resolve a pending waiter with the given payload."""
    evt = conn.waiters.get(request_id)
    if evt is None:
        log.debug("ws.resolve_unknown_waiter", request_id=request_id, session_id=conn.session_id)
        return
    if evt.is_set():
        log.debug("ws.resolve_already_set", request_id=request_id, session_id=conn.session_id)
        return
    conn.waiter_payloads[request_id] = payload
    evt.set()
    log.info(
        "ws.waiter_resolved",
        request_id=request_id,
        decision=payload.get("decision"),
        session_id=conn.session_id,
    )


def _resolve_constraint_decision(
    conn: _ConnectionState, request_id: str, msg: dict[str, Any]
) -> None:
    """Validate and resolve an incoming ``CONSTRAINT_DECISION`` (ADR-0076).

    The decision's ``action_id`` is validated against the waiter's registered
    options. An unknown action is logged and substituted with the default
    option. Unknown/already-resolved request IDs are silently dropped by
    :func:`_resolve_waiter` (idempotency).

    The waiter is looked up by **session**, so a decision arriving on a freshly
    reconnected socket resolves the waiter registered before the drop (FRE-928 AC-5).

    Args:
        conn: Connection the decision arrived on (used for its ``session_id``).
        request_id: Pause round-trip identifier.
        msg: Raw inbound message dict (``decision``, ``remember``).
    """
    waiter = _get_constraint_waiter(conn.session_id, request_id)
    if waiter is None:
        log.debug("ws.resolve_unknown_waiter", request_id=request_id, session_id=conn.session_id)
        return
    if waiter.event.is_set():
        log.debug("ws.resolve_already_set", request_id=request_id, session_id=conn.session_id)
        return

    meta = waiter.metadata
    decision = str(msg.get("decision", ""))
    remember = bool(msg.get("remember", False))

    if decision not in meta.options:
        log.warning(
            "ws.constraint_decision_invalid_action",
            request_id=request_id,
            session_id=conn.session_id,
            received=decision,
            substituted=meta.default_option,
            options=meta.options,
        )
        decision = meta.default_option

    waiter.payload = {"decision": decision, "remember": remember, "resolution": "user_choice"}
    waiter.event.set()
    log.info(
        "ws.waiter_resolved",
        request_id=request_id,
        decision=decision,
        session_id=conn.session_id,
    )


def _resolve_all_waiters_user_cancel(conn: _ConnectionState) -> list[str]:
    """Resolve all pending waiters with ``user_cancel`` (ADR-0076 Stop button).

    Each waiter is resolved with its registered default ``action_id`` and a
    ``user_cancel`` resolution, so any executor awaiting a constraint decision
    unblocks immediately and synthesizes from results gathered so far.

    Covers **both** registries — the connection's approval waiters and the session's
    constraint waiters. An explicit Stop is a genuine user decision, so unlike a
    disconnect it must not leave a constraint waiter riding its timeout (FRE-928).

    Args:
        conn: Active connection whose waiters should be cancelled.

    Returns:
        The request IDs that were resolved (for telemetry).
    """
    resolved: list[str] = []
    for request_id, evt in conn.waiters.items():
        if evt.is_set():
            continue
        # Approval waiters carry no per-request metadata; "timeout" is register_waiter's
        # own default_decision contract.
        conn.waiter_payloads[request_id] = {
            "decision": "timeout",
            "resolution": "user_cancel",
        }
        evt.set()
        resolved.append(request_id)

    for request_id, waiter in _session_constraint_waiters.get(conn.session_id, {}).items():
        if waiter.event.is_set():
            continue
        waiter.payload = {
            "decision": waiter.metadata.default_option,
            "resolution": "user_cancel",
        }
        waiter.event.set()
        resolved.append(request_id)

    return resolved


def _cancel_all_waiters(conn: _ConnectionState) -> None:
    """Resolve pending **approval** waiters with connection_lost on disconnect.

    Constraint waiters are deliberately untouched (FRE-928). They are session-scoped
    and ride their own timeout: a mobile socket drops and returns every 30-140s, so
    treating a momentary absence as a permanent one is what discarded live user
    decisions. A client reconnecting inside the window is replayed the pause event and
    resolves the original waiter; a caller with no client at all still falls back when
    the timeout expires.

    Approval waiters keep the old fail-closed-fast behaviour: that is a safety posture
    for HITL tool approval, not the same defect.

    Args:
        conn: The connection being torn down or superseded.
    """
    for request_id, evt in conn.waiters.items():
        if not evt.is_set():
            payload: dict[str, Any] = {
                "decision": "connection_lost",
                "reason": "WebSocket disconnected",
                "resolution": "connection_lost",
            }
            conn.waiter_payloads[request_id] = payload
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
            case "APPROVAL_DECISION" | "INTERRUPT_RESPONSE":
                request_id = msg.get("request_id", "")
                if request_id:
                    _resolve_waiter(conn, request_id, msg)
            case "CONSTRAINT_DECISION":
                request_id = msg.get("request_id", "")
                if request_id:
                    _resolve_constraint_decision(conn, request_id, msg)
            case "USER_CANCEL":
                conn.cancel_requested = True
                cancelled = _resolve_all_waiters_user_cancel(conn)
                log.info(
                    "user_cancel_received",
                    session_id=conn.session_id,
                    pending_constraint_request_ids=cancelled,
                )
            case _:
                log.debug("ws.unknown_message_type", msg_type=msg_type, session_id=conn.session_id)


# ── Cleanup task ───────────────────────────────────────────────────────────


async def run_event_cleanup() -> None:
    """Delete expired session_events rows. Called periodically from lifespan."""
    async with AsyncSessionLocal() as db:
        buf = SessionEventBuffer(db)
        await buf.cleanup_expired(ttl_hours=settings.ws_event_ttl_hours)
