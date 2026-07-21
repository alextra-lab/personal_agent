"""Tests for WS endpoint waiter registry and connection state (ADR-0075 / FRE-388)."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from personal_agent.service.auth import RequestUser
from personal_agent.transport.agui.ws_endpoint import (
    _active_connections,
    _cancel_all_waiters,
    _ConnectionState,
    _receiver,
    _resolve_waiter,
    get_event_queue,
    register_waiter,
)


def _make_user() -> RequestUser:
    return RequestUser(user_id=uuid4(), email="test@example.com", display_name="Test")


def _make_conn(session_id: str) -> _ConnectionState:
    """Create a minimal ConnectionState for testing (no real WebSocket)."""
    return _ConnectionState(
        websocket=None,  # type: ignore[arg-type]
        user=_make_user(),
        session_id=session_id,
        outbound_queue=asyncio.Queue(maxsize=100),
    )


class TestEventQueue:
    """Tests for the per-session event queue."""

    def test_get_or_create(self) -> None:
        sid = f"test-{uuid4()}"
        q1 = get_event_queue(sid)
        q2 = get_event_queue(sid)
        assert q1 is q2

    def test_bounded_queue(self) -> None:
        sid = f"test-bounded-{uuid4()}"
        q = get_event_queue(sid)
        assert q.maxsize > 0


class TestResolveWaiter:
    """Tests for the per-connection waiter resolve logic."""

    def test_resolve_sets_event(self) -> None:
        conn = _make_conn("s1")
        evt = asyncio.Event()
        conn.waiters["req-1"] = evt
        conn.waiter_payloads["req-1"] = {}

        _resolve_waiter(conn, "req-1", {"decision": "approve"})

        assert evt.is_set()
        assert conn.waiter_payloads["req-1"]["decision"] == "approve"

    def test_resolve_unknown_waiter_no_error(self) -> None:
        conn = _make_conn("s1")
        _resolve_waiter(conn, "nonexistent", {"decision": "deny"})

    def test_resolve_already_set_no_error(self) -> None:
        conn = _make_conn("s1")
        evt = asyncio.Event()
        evt.set()
        conn.waiters["req-2"] = evt
        conn.waiter_payloads["req-2"] = {"decision": "timeout"}

        _resolve_waiter(conn, "req-2", {"decision": "approve"})
        assert conn.waiter_payloads["req-2"]["decision"] == "timeout"


class TestCancelAllWaiters:
    """Tests for disconnect cleanup."""

    def test_cancels_all_pending(self) -> None:
        conn = _make_conn("s1")
        evt_a = asyncio.Event()
        evt_b = asyncio.Event()
        conn.waiters["a"] = evt_a
        conn.waiters["b"] = evt_b
        conn.waiter_payloads["a"] = {}
        conn.waiter_payloads["b"] = {}

        _cancel_all_waiters(conn)

        assert evt_a.is_set()
        assert evt_b.is_set()
        assert conn.waiter_payloads["a"]["decision"] == "connection_lost"
        assert conn.waiter_payloads["b"]["decision"] == "connection_lost"
        assert len(conn.waiters) == 0


class TestRegisterWaiter:
    """Tests for the register_waiter coroutine."""

    @pytest.mark.asyncio
    async def test_no_connection_returns_connection_lost(self) -> None:
        sid = f"no-conn-{uuid4()}"
        _active_connections.pop(sid, None)
        result = await register_waiter(sid, "req-x", timeout_seconds=1.0)
        assert result.decision == "connection_lost"

    @pytest.mark.asyncio
    async def test_waiter_resolved_by_external_set(self) -> None:
        sid = f"resolve-test-{uuid4()}"
        conn = _make_conn(sid)
        _active_connections[sid] = conn

        async def resolve_after_delay() -> None:
            await asyncio.sleep(0.05)
            _resolve_waiter(conn, "req-y", {"decision": "approve", "reason": "ok"})

        task = asyncio.create_task(resolve_after_delay())
        result = await register_waiter(sid, "req-y", timeout_seconds=5.0)
        await task

        assert result.decision == "approve"
        assert result.reason == "ok"
        _active_connections.pop(sid, None)

    @pytest.mark.asyncio
    async def test_waiter_times_out(self) -> None:
        sid = f"timeout-test-{uuid4()}"
        conn = _make_conn(sid)
        _active_connections[sid] = conn

        result = await register_waiter(sid, "req-z", timeout_seconds=0.1)

        assert result.decision == "timeout"
        _active_connections.pop(sid, None)


class _HangingWebSocket:
    """A client that stops responding without ever closing cleanly (half-open).

    ``receive_text`` never returns, which is exactly what a half-open TCP socket
    looks like to the server: no data, no disconnect, no error.
    """

    def __init__(self) -> None:
        self.closed_with: tuple[int, str] | None = None

    async def receive_text(self) -> str:
        await asyncio.Event().wait()  # never resolves
        raise AssertionError("unreachable")

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)


class TestLivenessDetection:
    """FRE-928 AC-6 — a dead connection is detected from missed pings, not at handshake."""

    @pytest.mark.asyncio
    async def test_half_open_connection_closed_within_ping_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A client that goes silent is torn down on the bounded receive wait.

        The server must not hold a half-open connection until the client's next
        handshake — that window is what let a pause be pushed into a socket with no
        reader. The bound is ``ws_ping_timeout_seconds``; the PWA pings every 25s, so
        the shipped 60s default is ~2.4 missed intervals.
        """
        from personal_agent.transport.agui import ws_endpoint as we

        monkeypatch.setattr(we.settings, "ws_ping_timeout_seconds", 0.3, raising=False)

        sid = f"halfopen-{uuid4()}"
        ws = _HangingWebSocket()
        conn = _ConnectionState(
            websocket=ws,  # type: ignore[arg-type]
            user=_make_user(),
            session_id=sid,
            outbound_queue=asyncio.Queue(maxsize=100),
        )

        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(_receiver(conn), timeout=5.0)
        elapsed = loop.time() - started

        assert ws.closed_with is not None, "half-open connection was held, never closed"
        assert ws.closed_with[0] == 1001
        assert elapsed < 2.0, f"detection took {elapsed:.2f}s — not bounded by the ping timeout"
