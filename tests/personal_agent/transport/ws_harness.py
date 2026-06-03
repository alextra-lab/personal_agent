"""WS integration test harness — minimal FastAPI app with mocked dependencies (FRE-400).

Usage::

    def test_something(monkeypatch):
        app, fake_buf = build_ws_test_app(monkeypatch)
        with TestClient(app, raise_server_exceptions=False) as client:
            with ws_connect(client, session_id) as ws:
                client.post("/__test/text_delta", params={...})
                msg = ws.receive_json()
                ...

Design
------
The real ``ws_endpoint`` uses module-level dicts (``_active_connections``,
``_session_queues``) and accesses ``AsyncSessionLocal`` / ``SessionEventBuffer``
directly. Rather than spinning up a real Postgres instance, this harness
patches those symbols using ``pytest.MonkeyPatch`` (auto-restored after each
test) and replaces them with:

* ``FakeSessionEventBuffer`` — in-memory list with a monotonic counter.
* ``_FakeSessionRepository`` — always reports a found session.
* Patched ``_authenticate_ws`` — returns a fixed ``RequestUser``.

A test-only ``/__test/*`` router is mounted alongside ``ws_router`` so that
event injection (text deltas, constraint pauses, etc.) runs *inside* the
app's event loop — the correct way to feed events to an asyncio.Queue.

**Important:** Long-running coroutines (constraint pause waiters) are scheduled
with ``asyncio.create_task()`` rather than FastAPI ``BackgroundTasks``.
In Starlette's ``TestClient``, ``BackgroundTasks`` run synchronously before
the HTTP response is returned to the test thread, which causes the HTTP call
to block until the task completes — the wrong behaviour for tests that need
the HTTP call to return immediately so they can drive the WS.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from personal_agent.service.auth import RequestUser

# Tracks in-flight constraint tasks so they are not garbage-collected before
# they finish.  Entries are removed via the task's done callback.
_background_tasks: set[asyncio.Task[None]] = set()

# Close code sent when a new connection supersedes an existing one (ADR-0075).
WS_CLOSE_SUPERSEDED: int = 4001

# Timeout (seconds) for constraint-pause round-trips in tests.  Patched to a
# small value (e.g. 0.05) in timeout-specific tests via monkeypatch.
DEFAULT_CONSTRAINT_TIMEOUT_S: float = 5.0

_TEST_USER_ID: UUID = uuid4()
_TEST_USER = RequestUser(
    user_id=_TEST_USER_ID,
    email="test@example.com",
    display_name="Test User",
)


# ── Fake implementations ───────────────────────────────────────────────────────


class FakeSessionEventBuffer:
    """In-memory stand-in for ``SessionEventBuffer`` (no Postgres required).

    Implements the same async interface as the real buffer.  Tests can
    pre-populate ``_store`` directly to simulate prior events for
    reconnect / replay tests.

    Attributes:
        _store: Mapping of ``str(session_id)`` → list of stored event dicts.
        _counter: Monotonically increasing sequence counter.
    """

    def __init__(self) -> None:
        """Initialise with empty storage."""
        self._store: dict[str, list[dict[str, Any]]] = {}
        self._counter: int = 0

    async def append(
        self,
        session_id: UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
        """Store a copy of *payload* and return the next sequence number.

        Args:
            session_id: Target session.
            event_type: AG-UI event type string (e.g. ``TEXT_DELTA``).
            payload: Full JSON envelope to store.

        Returns:
            Monotonically increasing integer sequence number.
        """
        self._counter += 1
        key = str(session_id)
        if key not in self._store:
            self._store[key] = []
        # Store a shallow copy so subsequent mutations of the live envelope
        # (e.g. ``envelope["seq"] = seq`` in ``_push_event``) don't affect us.
        self._store[key].append(
            {"seq": self._counter, "event_type": event_type, "payload": dict(payload)}
        )
        return self._counter

    async def replay(self, session_id: UUID, after_seq: int) -> list[dict[str, Any]]:
        """Return stored events with seq > *after_seq* in insertion order.

        Args:
            session_id: Target session.
            after_seq: Exclusive lower bound.

        Returns:
            List of dicts with ``seq`` and ``payload`` keys.
        """
        key = str(session_id)
        return [
            {"seq": e["seq"], "payload": e["payload"]}
            for e in self._store.get(key, [])
            if e["seq"] > after_seq
        ]

    async def oldest_available_seq(self, session_id: UUID) -> int | None:
        """Return the smallest retained seq, or ``None`` if no events exist.

        Args:
            session_id: Target session.

        Returns:
            Smallest seq or ``None``.
        """
        key = str(session_id)
        events = self._store.get(key, [])
        return events[0]["seq"] if events else None

    async def cleanup_expired(self, ttl_hours: int = 24) -> int:
        """No-op; the fake buffer never expires events.

        Args:
            ttl_hours: Unused.

        Returns:
            Always 0.
        """
        return 0


class _FakeSessionRepository:
    """Session repository stub that always reports a found session.

    This satisfies the ownership check in ``ws_session`` without hitting
    Postgres.
    """

    def __init__(self, db: Any) -> None:
        """Accept and discard the db session parameter.

        Args:
            db: Unused database session (accepted to match real API).
        """

    async def get(self, session_id: UUID, user_id: UUID | None = None) -> object:
        """Return a non-``None`` sentinel indicating the session exists.

        Args:
            session_id: Unused; all sessions are treated as found.
            user_id: Unused.

        Returns:
            An opaque non-``None`` object.
        """
        return object()


@asynccontextmanager
async def _fake_async_session() -> AsyncGenerator[None, None]:
    """Async context manager that yields ``None`` in place of a real DB session."""
    yield None


def _fake_session_local_factory() -> Any:
    """Return a callable that acts like ``AsyncSessionLocal()``.

    Returns:
        A zero-argument callable returning the fake async context manager.
    """

    def _factory() -> Any:
        return _fake_async_session()

    return _factory


# ── App builder ────────────────────────────────────────────────────────────────


def build_ws_test_app(
    mp: pytest.MonkeyPatch,
) -> tuple[FastAPI, FakeSessionEventBuffer]:
    """Build a minimal FastAPI app wired for WS integration tests.

    The returned app mounts:

    * ``ws_router`` — the real ``/ws/{session_id}`` endpoint from
      ``personal_agent.transport.agui.ws_endpoint``.
    * ``test_router`` — test-only ``/__test/*`` endpoints for injecting events
      from within the app's event loop.

    All database dependencies are replaced with in-memory fakes via
    ``mp`` (auto-restored after the test).

    Args:
        mp: ``pytest.MonkeyPatch`` fixture for automatic teardown.

    Returns:
        ``(app, fake_buf)`` — the FastAPI app and the shared
        :class:`FakeSessionEventBuffer` instance.
    """
    import tests.personal_agent.transport.ws_harness as _this_module
    from personal_agent.transport.agui import transport as _transport
    from personal_agent.transport.agui import ws_endpoint as _wsep
    from personal_agent.transport.agui.ws_endpoint import ws_router

    fake_buf = FakeSessionEventBuffer()
    fake_session_local = _fake_session_local_factory()

    # ── Patch AsyncSessionLocal ─────────────────────────────────────────────
    # Both modules import it at the top level and reference it by name at
    # call time, so we patch the symbol in each module's namespace.
    mp.setattr(_transport, "AsyncSessionLocal", fake_session_local)
    mp.setattr(_wsep, "AsyncSessionLocal", fake_session_local)

    # ── Patch SessionEventBuffer ────────────────────────────────────────────
    # Both modules create a new SessionEventBuffer(db) on each call; patching
    # the class-level reference makes every instantiation return our singleton.
    mp.setattr(_transport, "SessionEventBuffer", lambda _db: fake_buf)
    mp.setattr(_wsep, "SessionEventBuffer", lambda _db: fake_buf)

    # ── Patch SessionRepository ─────────────────────────────────────────────
    mp.setattr(_wsep, "SessionRepository", _FakeSessionRepository)

    # ── Patch authentication ────────────────────────────────────────────────
    async def _fake_authenticate(websocket: Any, session_id_str: str) -> RequestUser:
        return _TEST_USER

    mp.setattr(_wsep, "_authenticate_ws", _fake_authenticate)

    # Ensure the origin check passes (origin is empty in TestClient → allowed
    # when gateway_auth_enabled=False, which is the default in test env).
    from personal_agent.config import settings as _settings

    mp.setattr(_settings, "gateway_auth_enabled", False)

    # ── Test-only event injection router ────────────────────────────────────
    test_router = APIRouter()

    @test_router.post("/__test/text_delta")
    async def _inject_text_delta(session_id: str, text: str) -> dict[str, str]:
        from personal_agent.transport.agui.transport import AGUITransport

        await AGUITransport().send_text_delta(text=text, session_id=session_id)
        return {"ok": "sent"}

    @test_router.post("/__test/done")
    async def _inject_done(session_id: str) -> dict[str, str]:
        from personal_agent.transport.agui.ws_endpoint import get_event_queue

        await get_event_queue(session_id).put(None)  # None sentinel → DONE frame
        return {"ok": "sent"}

    @test_router.post("/__test/turn_status")
    async def _inject_turn_status(
        session_id: str,
        context_tokens: int = 10000,
        context_max: int = 100000,
        tool_iteration: int = 1,
        tool_iteration_max: int = 10,
        turn_cost_usd: float = 0.01,
    ) -> dict[str, str]:
        from personal_agent.transport.agui.transport import emit_turn_status

        await emit_turn_status(
            session_id=session_id,
            value={
                "context_tokens": context_tokens,
                "context_max": context_max,
                "tool_iteration": tool_iteration,
                "tool_iteration_max": tool_iteration_max,
                "turn_cost_usd": turn_cost_usd,
            },
        )
        return {"ok": "sent"}

    @test_router.post("/__test/classified_error")
    async def _inject_classified_error(
        session_id: str,
        category: str = "model_server",
        reason: str = "Test error",
        next_step: str = "Retry",
        trace_id: str = "test-trace",
    ) -> dict[str, str]:
        from personal_agent.error_classification import ClassifiedError
        from personal_agent.transport.agui.transport import emit_classified_error

        classified = ClassifiedError(
            category=category,  # type: ignore[arg-type]
            reason=reason,
            next_step=next_step,
            actions=("retry",),
            partial=False,
        )
        await emit_classified_error(
            session_id=session_id,
            trace_id=trace_id,
            classified=classified,
        )
        return {"ok": "sent"}

    @test_router.post("/__test/cancelled")
    async def _inject_cancelled(session_id: str, trace_id: str = "test-trace") -> dict[str, str]:
        from personal_agent.transport.agui.transport import emit_cancelled

        await emit_cancelled(session_id=session_id, trace_id=trace_id)
        return {"ok": "sent"}

    @test_router.post("/__test/constraint_pause")
    async def _inject_constraint_pause(
        session_id: str,
        request_id: str,
        constraint: str,
    ) -> dict[str, str]:
        """Start a constraint pause concurrently and return immediately.

        Uses ``asyncio.create_task`` rather than FastAPI ``BackgroundTasks``
        because in Starlette's ``TestClient``, ``BackgroundTasks`` run
        synchronously before the HTTP response reaches the test thread.  That
        would block the test for the entire waiter lifetime (up to
        ``DEFAULT_CONSTRAINT_TIMEOUT_S`` seconds) before it could send the
        ``CONSTRAINT_DECISION``.  ``asyncio.create_task`` schedules the
        coroutine on the running event loop without blocking the response.
        """
        from personal_agent.orchestrator.constraint_options import CONSTRAINT_OPTIONS
        from personal_agent.transport.agui.transport import (
            emit_constraint_resolved,
            register_and_push_constraint,
        )
        from personal_agent.transport.agui.ws_endpoint import WaiterMetadata
        from personal_agent.transport.events import ConstraintPauseEvent

        opts = CONSTRAINT_OPTIONS[constraint]
        option_ids = [o.action_id for o in opts]
        default = option_ids[-1]
        expires_at = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()

        metadata = WaiterMetadata(
            constraint=constraint,
            options=option_ids,
            default_option=default,
            created_at=time.monotonic(),
        )
        pause_event = ConstraintPauseEvent(
            request_id=request_id,
            session_id=session_id,
            trace_id="test-trace",
            constraint=constraint,  # type: ignore[arg-type]
            context="Test constraint pause",
            options=option_ids,
            default_option=default,
            expires_at=expires_at,
        )

        # Read module-level timeout so tests can patch DEFAULT_CONSTRAINT_TIMEOUT_S.
        timeout_s: float = _this_module.DEFAULT_CONSTRAINT_TIMEOUT_S

        async def _run() -> None:
            result = await register_and_push_constraint(
                session_id=session_id,
                request_id=request_id,
                event=pause_event,
                metadata=metadata,
                timeout_seconds=timeout_s,
            )
            await emit_constraint_resolved(
                request_id=request_id,
                session_id=session_id,
                constraint=constraint,
                action_id=result["decision"],
                resolution=result["resolution"],
            )

        task = asyncio.create_task(_run())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return {"ok": "started"}

    app = FastAPI()
    app.include_router(ws_router)
    app.include_router(test_router)
    return app, fake_buf


# ── WS connection helper ───────────────────────────────────────────────────────


@contextmanager
def ws_connect(
    client: TestClient,
    session_id: str,
    last_seq: int = 0,
) -> Generator[Any, None, None]:
    """Open a WebSocket connection and perform the mandatory CONNECT handshake.

    The server requires the first client message to be::

        {"type": "CONNECT", "last_seq": N}

    This helper sends it automatically so test code can go straight to
    asserting events.

    Args:
        client: Starlette ``TestClient`` wrapping the test app.
        session_id: Target session ID path parameter.
        last_seq: Last event seq the client has seen (0 for a fresh connection;
            a positive value triggers replay of events with ``seq > last_seq``).

    Yields:
        The connected ``WebSocketTestSession``.
    """
    with client.websocket_connect(f"/ws/{session_id}") as ws:
        ws.send_json({"type": "CONNECT", "last_seq": last_seq})
        yield ws
