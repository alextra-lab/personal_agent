"""Tests for WS endpoint waiter registry and connection state (ADR-0075 / FRE-388)."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from personal_agent.service.auth import RequestUser
from personal_agent.transport.agui.ws_endpoint import (
    _active_connections,
    _approval_gap_expired,
    _ApprovalWaiter,
    _ConnectionState,
    _get_or_create_cancel_event,
    _receiver,
    _resolve_all_waiters_user_cancel,
    _resolve_waiter,
    _session_approval_waiters,
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


def _mark_connected_before(session_id: str) -> None:
    """Make ``session_id`` look like a session whose socket opened and later dropped."""
    _get_or_create_cancel_event(session_id)
    _active_connections.pop(session_id, None)


def _no_registry_left(session_id: str) -> bool:
    return session_id not in _session_approval_waiters


class TestResolveWaiter:
    """Tests for the session-scoped waiter resolve logic."""

    def test_resolve_sets_event(self) -> None:
        sid = f"resolve-{uuid4()}"
        conn = _make_conn(sid)
        waiter = _ApprovalWaiter(event=asyncio.Event(), payload={})
        _session_approval_waiters[sid] = {"req-1": waiter}
        try:
            _resolve_waiter(conn, "req-1", {"decision": "approve"})

            assert waiter.event.is_set()
            assert waiter.payload["decision"] == "approve"
        finally:
            _session_approval_waiters.pop(sid, None)

    def test_resolve_unknown_waiter_no_error(self) -> None:
        conn = _make_conn("s1")
        _resolve_waiter(conn, "nonexistent", {"decision": "deny"})

    def test_resolve_already_set_no_error(self) -> None:
        sid = f"resolve-set-{uuid4()}"
        conn = _make_conn(sid)
        waiter = _ApprovalWaiter(event=asyncio.Event(), payload={"decision": "timeout"})
        waiter.event.set()
        _session_approval_waiters[sid] = {"req-2": waiter}
        try:
            _resolve_waiter(conn, "req-2", {"decision": "approve"})
            assert waiter.payload["decision"] == "timeout"
        finally:
            _session_approval_waiters.pop(sid, None)

    def test_a_reconnected_socket_resolves_the_waiter_registered_before_the_drop(self) -> None:
        """The lookup is by session, so a new connection object answers the old waiter."""
        sid = f"resolve-reconnect-{uuid4()}"
        waiter = _ApprovalWaiter(event=asyncio.Event(), payload={})
        _session_approval_waiters[sid] = {"req-3": waiter}
        try:
            _resolve_waiter(_make_conn(sid), "req-3", {"decision": "approve"})
            assert waiter.event.is_set()
        finally:
            _session_approval_waiters.pop(sid, None)

    def test_user_cancel_resolves_every_pending_waiter_of_the_session(self) -> None:
        sid = f"cancel-{uuid4()}"
        conn = _make_conn(sid)
        a = _ApprovalWaiter(event=asyncio.Event(), payload={})
        b = _ApprovalWaiter(event=asyncio.Event(), payload={})
        _session_approval_waiters[sid] = {"a": a, "b": b}
        try:
            resolved = _resolve_all_waiters_user_cancel(conn)

            assert sorted(resolved) == ["a", "b"]
            assert a.event.is_set() and b.event.is_set()
            assert a.payload["decision"] == "timeout"
            assert a.payload["resolution"] == "user_cancel"
        finally:
            _session_approval_waiters.pop(sid, None)


class TestRegisterWaiter:
    """Tests for the register_waiter coroutine."""

    @pytest.mark.asyncio
    async def test_never_connected_session_fails_fast(self) -> None:
        """No PWA ever opened a socket (eval, /chat): refuse at once, as before FRE-1677."""
        sid = f"no-conn-{uuid4()}"
        _active_connections.pop(sid, None)
        pushed = False

        async def on_registered() -> None:
            nonlocal pushed
            pushed = True

        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await register_waiter(sid, "req-x", 5.0, on_registered=on_registered)

        assert result.decision == "connection_lost"
        assert loop.time() - started < 0.5
        assert pushed, "the event must still be persisted for replay"
        assert _no_registry_left(sid)

    @pytest.mark.asyncio
    async def test_no_socket_at_request_waits_for_the_reconnected_client(self) -> None:
        """FRE-1677 AC-2: a session that connected before waits for its client to return.

        Reproduces 12:49:26: the socket had dropped, ``register_waiter`` found no
        connection and returned ``connection_lost`` at once.
        """
        sid = f"gap-{uuid4()}"
        _mark_connected_before(sid)
        pushed = False

        async def on_registered() -> None:
            nonlocal pushed
            pushed = True

        async def client_returns() -> None:
            await asyncio.sleep(0.05)
            returned = _make_conn(sid)
            _active_connections[sid] = returned
            _resolve_waiter(returned, "req-gap", {"decision": "approve"})

        task = asyncio.create_task(client_returns())
        try:
            result = await register_waiter(sid, "req-gap", 5.0, on_registered=on_registered)
            await task
        finally:
            _active_connections.pop(sid, None)

        assert result.decision == "approve"
        assert pushed
        assert _no_registry_left(sid)

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
        assert _no_registry_left(sid)
        _active_connections.pop(sid, None)

    @pytest.mark.asyncio
    async def test_waiter_survives_the_connection_leaving_and_being_replaced(self) -> None:
        sid = f"survive-{uuid4()}"
        _active_connections[sid] = _make_conn(sid)

        async def drop_then_replace() -> None:
            await asyncio.sleep(0.03)
            _active_connections.pop(sid, None)
            await asyncio.sleep(0.03)
            replacement = _make_conn(sid)
            _active_connections[sid] = replacement
            await asyncio.sleep(0.03)
            _resolve_waiter(replacement, "req-s", {"decision": "approve"})

        task = asyncio.create_task(drop_then_replace())
        try:
            result = await register_waiter(sid, "req-s", 5.0)
            await task
        finally:
            _active_connections.pop(sid, None)

        assert result.decision == "approve"

    @pytest.mark.asyncio
    async def test_no_socket_for_the_whole_wait_times_out_after_the_timeout(self) -> None:
        sid = f"gap-timeout-{uuid4()}"
        _mark_connected_before(sid)

        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await register_waiter(sid, "req-t", 0.3)

        assert result.decision == "timeout"
        assert loop.time() - started >= 0.25, "returned before the timeout"
        assert _no_registry_left(sid)
        _approval_gap_expired.discard(sid)

    @pytest.mark.asyncio
    async def test_after_one_gap_timeout_later_approvals_fail_fast(self) -> None:
        """Approvals are serialized per session; one gap must not cost the timeout each."""
        sid = f"gap-memo-{uuid4()}"
        _mark_connected_before(sid)
        try:
            first = await register_waiter(sid, "req-1", 0.2)
            loop = asyncio.get_running_loop()
            started = loop.time()
            second = await register_waiter(sid, "req-2", 5.0)

            assert first.decision == "timeout"
            assert second.decision == "connection_lost"
            assert loop.time() - started < 0.5
        finally:
            _approval_gap_expired.discard(sid)

    @pytest.mark.asyncio
    async def test_a_live_connection_ignores_the_gap_memo(self) -> None:
        sid = f"gap-memo-live-{uuid4()}"
        conn = _make_conn(sid)
        _active_connections[sid] = conn
        _approval_gap_expired.add(sid)

        async def answer() -> None:
            await asyncio.sleep(0.05)
            _resolve_waiter(conn, "req-l", {"decision": "approve"})

        task = asyncio.create_task(answer())
        try:
            result = await register_waiter(sid, "req-l", 5.0)
            await task
        finally:
            _active_connections.pop(sid, None)
            _approval_gap_expired.discard(sid)

        assert result.decision == "approve"

    @pytest.mark.asyncio
    async def test_cancel_while_waiting_leaves_no_registry_entry(self) -> None:
        sid = f"cancel-wait-{uuid4()}"
        _mark_connected_before(sid)

        task = asyncio.create_task(register_waiter(sid, "req-c", 5.0))
        await asyncio.sleep(0.05)
        assert sid in _session_approval_waiters
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert _no_registry_left(sid)

    @pytest.mark.asyncio
    async def test_cancel_during_the_push_leaves_no_registry_entry(self) -> None:
        sid = f"cancel-push-{uuid4()}"
        _mark_connected_before(sid)
        in_push = asyncio.Event()

        async def slow_push() -> None:
            in_push.set()
            await asyncio.sleep(5.0)

        task = asyncio.create_task(register_waiter(sid, "req-p", 5.0, on_registered=slow_push))
        await in_push.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert _no_registry_left(sid)

    @pytest.mark.asyncio
    async def test_a_failing_push_leaves_no_registry_entry(self) -> None:
        sid = f"push-fails-{uuid4()}"
        _mark_connected_before(sid)

        async def failing_push() -> None:
            raise RuntimeError("persist failed")

        with pytest.raises(RuntimeError, match="persist failed"):
            await register_waiter(sid, "req-f", 5.0, on_registered=failing_push)

        assert _no_registry_left(sid)


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
