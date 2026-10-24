"""FRE-1410 / ADR-0143 D3: the close is a deadline, not a wait.

Every test runs the production turn body (``_process_chat_stream_background``) through the
request-task registry, with the real ``emit_done``, the real ``_sender`` and a fake socket. The
substrate is replaced at its edge: a fake ``SessionEventBuffer`` stands in for Postgres and the
orchestrator is a mock. "Terminal signal" means the sender dequeued the close sentinel and sent a
``DONE`` frame, or the degraded path closed the socket.

The tests that must fail on ``main`` (ticket AC-6: AC-2(c), AC-3, AC-4(a)) reach their assertion
there. They import no symbol that is new on this branch and fail on behaviour: no ``DONE`` frame,
no log line, no leaked record.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import AsyncIterator, Iterator, MutableMapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio

from personal_agent.events.bus import NoOpBus, get_event_bus, set_global_event_bus
from personal_agent.service.auth import RequestUser
from personal_agent.service.request_tasks import get_request_task_registry
from tests._helpers.log_capture import capture_log_records
from tests.personal_agent.service.test_request_tasks import (
    _ended,
    _launched_trace_ids,
    _live_records,
    _post,
    _set_setting,
    _wait_for,
)

Records = list[MutableMapping[str, Any]]

_GENERATIONS = itertools.count(100)


# ── Fakes at the substrate edge ────────────────────────────────────────────────────────────


class _Buffer:
    """Stands in for ``SessionEventBuffer``. Can block the ``DONE`` append cooperatively."""

    rows: list[str] = []
    block_done: asyncio.Event | None = None

    def __init__(self, db: Any) -> None:
        """Accept and ignore the db handle."""

    async def append(self, session_id: Any, event_type: str, payload: dict[str, Any]) -> int:
        """Persist a row, or wait on ``block_done`` first when the row is ``DONE``."""
        if event_type == "DONE" and type(self).block_done is not None:
            await type(self).block_done.wait()
        type(self).rows.append(event_type)
        return len(type(self).rows)


@asynccontextmanager
async def _fake_db() -> AsyncIterator[None]:
    yield None


class _FakeWebSocket:
    """Records what the sender sends and whether anything closed the socket."""

    def __init__(self) -> None:
        """Create an open socket."""
        self.sent: list[dict[str, Any]] = []
        self.closed = asyncio.Event()

    async def send_text(self, text: str) -> None:
        """Record one frame."""
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Record the close."""
        self.closed.set()


@dataclass
class _Client:
    """One fake connected client: a socket, its connection state, and its sender task."""

    ws: _FakeWebSocket
    conn: Any
    sender: asyncio.Task[None] | None

    def got_done(self, trace_id: str) -> bool:
        """Whether the sender sent a ``DONE`` frame for the trace."""
        return any(m.get("type") == "DONE" and m.get("trace_id") == trace_id for m in self.ws.sent)


@dataclass
class _Env:
    """Handles on the doubles that replace the substrate around the turn body."""

    orchestrator: MagicMock
    record_first_token: AsyncMock
    release: asyncio.Event = field(default_factory=asyncio.Event)
    gates: dict[str, asyncio.Event] = field(default_factory=dict)
    clients: list[_Client] = field(default_factory=list)
    sessions: list[str] = field(default_factory=list)
    executor_returned_at: float = 0.0

    def new_session(self) -> str:
        """Return a fresh session id that the env cleans up."""
        session_id = str(uuid4())
        self.sessions.append(session_id)
        return session_id

    def connect(self, session_id: str, *, run_sender: bool = True) -> _Client:
        """Register a fake connection for the session, with a real sender unless held."""
        from personal_agent.transport.agui import ws_endpoint

        ws = _FakeWebSocket()
        conn = ws_endpoint._ConnectionState(
            websocket=ws,  # type: ignore[arg-type]
            user=RequestUser(user_id=uuid4(), email="o@example.test"),
            session_id=session_id,
            outbound_queue=ws_endpoint.get_event_queue(session_id),
        )
        conn.generation = next(_GENERATIONS)  # type: ignore[attr-defined]
        ws_endpoint._active_connections[session_id] = conn
        sender = asyncio.create_task(ws_endpoint._sender(conn, 0)) if run_sender else None
        conn.sender_task = sender
        client = _Client(ws=ws, conn=conn, sender=sender)
        self.clients.append(client)
        return client

    def disconnect(self, session_id: str) -> None:
        """Drop the active connection and stop its sender, as a closed socket would."""
        from personal_agent.transport.agui import ws_endpoint

        conn = ws_endpoint._active_connections.pop(session_id, None)
        if conn is not None and conn.sender_task is not None:
            conn.sender_task.cancel()

    def suppress_cancellation_for(self, trace_ids: set[str] | None = None) -> None:
        """Make the executor a block that swallows cancellation, for the given traces (or all)."""

        async def _handle(**kwargs: Any) -> dict[str, str]:
            if trace_ids is not None and kwargs["trace_id"] not in trace_ids:
                return {"reply": "hi"}
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    continue
            return {"reply": "hi"}

        self.orchestrator.handle_user_request.side_effect = _handle

    def block_first_push(self) -> None:
        """Make the first reply push wait cooperatively (``release`` frees it)."""
        from personal_agent.transport.agui import transport

        original = transport._push_event
        calls = 0

        async def _push(event: Any, session_id: str) -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                await self.release.wait()
            return await original(event, session_id)

        patcher = patch.object(transport, "_push_event", _push)
        patcher.start()
        self._patchers.append(patcher)

    _patchers: list[Any] = field(default_factory=list)


@pytest_asyncio.fixture
async def env() -> AsyncIterator[_Env]:
    """Replace the substrate around the real turn body; keep the real transport and sender."""
    session = SimpleNamespace(session_id=uuid4(), messages=[], execution_profile="local")
    repo = MagicMock()
    repo.get = AsyncMock(return_value=session)
    repo.append_message = AsyncMock(return_value=None)
    orchestrator = MagicMock()
    orchestrator.session_manager = MagicMock()
    orchestrator.session_manager.get_session.return_value = None
    orchestrator.handle_user_request = AsyncMock(return_value={"reply": "hi"})
    _Buffer.rows = []
    _Buffer.block_done = None

    original_bus = get_event_bus()
    set_global_event_bus(NoOpBus())
    with (
        patch("personal_agent.service.app.AsyncSessionLocal") as app_session_local,
        patch("personal_agent.service.app.SessionRepository", return_value=repo),
        patch("personal_agent.service.app._validate_attachments", new=AsyncMock(return_value=[])),
        patch("personal_agent.service.app.run_gateway_pipeline", new=AsyncMock(return_value=None)),
        patch("personal_agent.service.app._record_first_token", new=AsyncMock()) as first_token,
        patch("personal_agent.orchestrator.Orchestrator", return_value=orchestrator),
        patch(
            "personal_agent.transport.agui.transport.open_turn", new=AsyncMock(return_value=None)
        ),
        patch("personal_agent.transport.agui.transport.SessionEventBuffer", _Buffer),
        patch("personal_agent.transport.agui.transport.AsyncSessionLocal", lambda: _fake_db()),
    ):
        app_session_local.side_effect = lambda: _fake_db()
        handle = _Env(orchestrator=orchestrator, record_first_token=first_token)
        handle._patchers = []
        yield handle
        # Free every blocked double while the loop still runs: the loop's own shutdown cancels
        # the tasks that are left, and a task that suppresses cancellation would hang it.
        handle.release.set()
        if _Buffer.block_done is not None:
            _Buffer.block_done.set()
        for gate in handle.gates.values():
            gate.set()
        await asyncio.sleep(0.1)
        for patcher in handle._patchers:
            patcher.stop()
    set_global_event_bus(original_bus)

    from personal_agent.transport.agui import transport, ws_endpoint

    for client in handle.clients:
        if client.sender is not None:
            client.sender.cancel()
    for session_id in handle.sessions:
        ws_endpoint._active_connections.pop(session_id, None)
        ws_endpoint._session_queues.pop(session_id, None)
        lock = transport._session_emit_locks.pop(session_id, None)
        if lock is not None and lock.locked():
            lock.release()


@pytest.fixture(autouse=True)
def _registry_is_empty_afterwards() -> Iterator[None]:
    """A test must leave no record in the registry singleton."""
    yield
    left = get_request_task_registry().records()
    for record in left:
        if record.task is not None:
            record.task.cancel()
    assert left == (), f"records left in the registry singleton: {[r.trace_id for r in left]}"


def _degraded(records: Records, trace_id: str, reason: str) -> bool:
    """Whether ``transport.done_degraded`` was logged for the trace with the reason."""
    return any(
        r.get("event") == "transport.done_degraded"
        and r.get("trace_id") == trace_id
        and r.get("reason") == reason
        for r in records
    )


def _leaked_logged(records: Records, trace_id: str) -> bool:
    return any(
        r.get("event") == "chat_stream.task_leaked" and r.get("trace_id") == trace_id
        for r in records
    )


def _settings(monkeypatch: pytest.MonkeyPatch, orchestrator: int, service: int, close: int) -> None:
    _set_setting(monkeypatch, "orchestrator_turn_lifetime_seconds", orchestrator)
    _set_setting(monkeypatch, "service_request_task_lifetime_seconds", service)
    _set_setting(monkeypatch, "service_request_close_timeout_seconds", close)


def _stamp_executor_return(env: _Env) -> None:
    """Record when the executor returns, so a test can measure the close from that moment."""

    async def _handle(**_kwargs: Any) -> dict[str, str]:
        env.executor_returned_at = asyncio.get_running_loop().time()
        return {"reply": "hi"}

    env.orchestrator.handle_user_request.side_effect = _handle


# ── AC-1: stage one's own close wins when the task cooperates ─────────────────────────────


@pytest.mark.asyncio
async def test_stage_one_close_wins_when_the_task_cooperates(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cooperative block ends at stage one, and the task's own ``emit_done`` closes the stream."""
    _settings(monkeypatch, 1, 2, 1)
    session_id = env.new_session()
    client = env.connect(session_id)
    env.block_first_push()
    loop = asyncio.get_running_loop()

    with capture_log_records() as records:
        t0 = loop.time()
        await _post("hello", session_id=session_id)
        (trace_id,) = _launched_trace_ids(records)
        assert await _wait_for(lambda: client.got_done(trace_id), timeout=4.0), (
            "the sender must dequeue the sentinel by 4 s after started_at"
        )
        assert loop.time() - t0 < 4.0
        assert await _wait_for(lambda: _ended(records, trace_id) is not None, timeout=5.0)

    ended = _ended(records, trace_id)
    assert ended is not None
    assert ended["outcome"] == "timed_out"
    assert ended.get("closed_by") == "emit_done", "the task's own emit_done must win close_once"
    assert not _leaked_logged(records, trace_id)
    assert _live_records() == []


# ── AC-2: lock, persist and queue failures each close within one close budget ─────────────


async def _run_to_executor_return(
    env: _Env, session_id: str, records: Records
) -> tuple[str, float]:
    await _post("hello", session_id=session_id)
    (trace_id,) = _launched_trace_ids(records)
    assert await _wait_for(lambda: env.executor_returned_at > 0, timeout=3.0)
    return trace_id, env.executor_returned_at


@pytest.mark.asyncio
async def test_a_held_emit_lock_closes_within_one_budget(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) The lock is held after the reply push: the sentinel arrives with ``lock_timeout``."""
    from personal_agent.transport.agui.transport import _get_emit_lock

    _settings(monkeypatch, 30, 60, 1)
    session_id = env.new_session()
    client = env.connect(session_id)
    _stamp_executor_return(env)

    async def _hold_the_lock(*_args: object, **_kwargs: object) -> None:
        await _get_emit_lock(session_id).acquire()

    env.record_first_token.side_effect = _hold_the_lock
    loop = asyncio.get_running_loop()

    with capture_log_records() as records:
        trace_id, returned = await _run_to_executor_return(env, session_id, records)
        assert await _wait_for(lambda: client.got_done(trace_id), timeout=2.0)
        assert loop.time() - returned < 2.0
        assert await _wait_for(lambda: _ended(records, trace_id) is not None, timeout=3.0)

    assert _degraded(records, trace_id, "lock_timeout")
    ended = _ended(records, trace_id)
    assert ended is not None and ended["outcome"] == "completed"
    assert _live_records() == []


@pytest.mark.asyncio
async def test_a_blocked_done_append_closes_within_one_budget(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) The ``DONE`` append blocks: the sentinel arrives with ``persist_timeout``."""
    _settings(monkeypatch, 30, 60, 1)
    session_id = env.new_session()
    client = env.connect(session_id)
    _stamp_executor_return(env)
    _Buffer.block_done = asyncio.Event()
    loop = asyncio.get_running_loop()

    with capture_log_records() as records:
        trace_id, returned = await _run_to_executor_return(env, session_id, records)
        assert await _wait_for(lambda: client.got_done(trace_id), timeout=2.0)
        assert loop.time() - returned < 2.0
        assert await _wait_for(lambda: _ended(records, trace_id) is not None, timeout=3.0)

    assert _degraded(records, trace_id, "persist_timeout")
    ended = _ended(records, trace_id)
    assert ended is not None and ended["outcome"] == "completed"
    assert _live_records() == []


@pytest.mark.asyncio
async def test_a_full_queue_closes_the_socket_within_one_budget(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) The queue is full and the sender is held: the socket closes with ``queue_timeout``."""
    from personal_agent.transport.agui.ws_endpoint import DONE_SENTINEL_TYPE, get_event_queue

    _settings(monkeypatch, 30, 60, 1)
    _set_setting(monkeypatch, "ws_event_queue_size", 5)
    session_id = env.new_session()
    client = env.connect(session_id, run_sender=False)
    queue = get_event_queue(session_id)
    while not queue.full():
        queue.put_nowait({"type": "filler", "seq": queue.qsize() + 1000})
    _stamp_executor_return(env)
    loop = asyncio.get_running_loop()

    with capture_log_records() as records:
        trace_id, returned = await _run_to_executor_return(env, session_id, records)
        assert await _wait_for(client.ws.closed.is_set, timeout=2.0), (
            "with no sender draining, only a raw close can end the stream"
        )
        assert loop.time() - returned < 2.0
        assert await _wait_for(lambda: _ended(records, trace_id) is not None, timeout=3.0)

    assert _degraded(records, trace_id, "queue_timeout")
    assert not any(m.get("type") == "DONE" for m in client.ws.sent)
    queued = [queue.get_nowait() for _ in range(queue.qsize())]
    assert not any(item.get("type") == DONE_SENTINEL_TYPE for item in queued)
    ended = _ended(records, trace_id)
    assert ended is not None and ended["outcome"] == "completed"
    assert _live_records() == []


# ── AC-3: stage two closes around a task that suppresses cancellation, and keeps the leak ─


@pytest.mark.asyncio
async def test_stage_two_closes_around_a_suppressing_task_and_keeps_the_leak(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline closes the stream, the record stays ``leaked``, and a late end is ``timed_out``."""
    _settings(monkeypatch, 1, 3, 1)
    session_id = env.new_session()
    client = env.connect(session_id)
    env.suppress_cancellation_for()
    loop = asyncio.get_running_loop()

    with capture_log_records() as records:
        t0 = loop.time()
        await _post("hello", session_id=session_id)
        (trace_id,) = _launched_trace_ids(records)
        assert await _wait_for(lambda: client.got_done(trace_id), timeout=5.0), (
            "a terminal signal must reach the client by lifetime + 2 x close = 5 s"
        )
        assert loop.time() - t0 < 5.0
        await asyncio.sleep(max(0.0, 6.0 - (loop.time() - t0)))

        (record,) = _live_records()
        assert record.trace_id == trace_id
        assert record.state == "leaked"
        assert record.task is not None and not record.task.done()
        assert _leaked_logged(records, trace_id)
        assert any(
            r.get("event") == "request_tasks_gauge" and r.get("leaked") == 1 for r in records
        )

        env.release.set()
        assert await _wait_for(lambda: _ended(records, trace_id) is not None, timeout=3.0)

    ended = _ended(records, trace_id)
    assert ended is not None
    assert ended["outcome"] == "timed_out"
    assert ended["was_leaked"] is True
    assert _live_records() == []
    gauges = [r for r in records if r.get("event") == "request_tasks_gauge"]
    assert gauges[-1]["leaked"] == 0
    assert sum(1 for m in client.ws.sent if m.get("type") == "DONE") == 1


# ── AC-4: a stale deadline never closes a connection it does not own ──────────────────────


@pytest.mark.asyncio
async def test_a_stale_deadline_yields_to_a_newer_turn_on_the_session(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) Turn A leaks while turn B owns the session: B's socket stays open and B gets its DONE."""
    _settings(monkeypatch, 1, 2, 1)
    session_id = env.new_session()
    client = env.connect(session_id)
    suppressed = asyncio.Event()

    async def _handle(**kwargs: Any) -> dict[str, str]:
        trace_id = kwargs["trace_id"]
        if not suppressed.is_set():
            suppressed.set()  # the first turn to reach the executor is A
            while not env.release.is_set():
                try:
                    await env.release.wait()
                except asyncio.CancelledError:
                    continue
            return {"reply": "a"}
        gate = env.gates[trace_id] = asyncio.Event()
        await gate.wait()
        return {"reply": "b"}

    env.orchestrator.handle_user_request.side_effect = _handle

    with capture_log_records() as records:
        await _post("turn a", session_id=session_id)
        (a_trace,) = _launched_trace_ids(records)
        assert await _wait_for(suppressed.is_set, timeout=2.0)
        # B outlives A's deadline: B's own bound is long.
        _set_setting(monkeypatch, "service_request_task_lifetime_seconds", 30)
        await _post("turn b", session_id=session_id)
        b_trace = _launched_trace_ids(records)[1]
        assert await _wait_for(lambda: b_trace in env.gates, timeout=2.0)

        assert await _wait_for(lambda: _leaked_logged(records, a_trace), timeout=4.0)
        assert not client.ws.closed.is_set(), "A's stale deadline closed B's socket"
        assert _degraded(records, a_trace, "superseded")
        assert not client.got_done(a_trace)

        env.gates[b_trace].set()
        assert await _wait_for(lambda: client.got_done(b_trace), timeout=2.0)
        assert not client.ws.closed.is_set()
        env.release.set()
        assert await _wait_for(
            lambda: _ended(records, a_trace) is not None and _ended(records, b_trace) is not None,
            timeout=3.0,
        )

    b_ended = _ended(records, b_trace)
    assert b_ended is not None and b_ended["outcome"] == "completed"


@pytest.mark.asyncio
async def test_a_reconnect_between_capture_and_close_is_not_closed(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) A reconnect after the capture leaves the new socket open and logs a mismatch."""
    from personal_agent.transport.agui import ws_endpoint

    _settings(monkeypatch, 1, 2, 1)
    session_id = env.new_session()
    replacement: list[_Client] = []

    class _ReconnectingQueue(asyncio.Queue[Any]):
        """A full queue whose blocked ``put`` is the moment the client reconnects."""

        async def put(self, item: Any) -> None:
            if not replacement:
                replacement.append(env.connect(session_id, run_sender=False))
            await super().put(item)

    queue = _ReconnectingQueue(maxsize=1)
    queue.put_nowait({"type": "filler", "seq": 1})
    ws_endpoint._session_queues[session_id] = queue
    env.connect(session_id, run_sender=False)
    env.suppress_cancellation_for()

    with capture_log_records() as records:
        await _post("hello", session_id=session_id)
        (trace_id,) = _launched_trace_ids(records)
        assert await _wait_for(lambda: _leaked_logged(records, trace_id), timeout=4.0)
        assert await _wait_for(
            lambda: any(r.get("event") == "ws.close_skipped_generation_mismatch" for r in records),
            timeout=2.0,
        )
        env.release.set()
        assert await _wait_for(lambda: _ended(records, trace_id) is not None, timeout=3.0)

    assert replacement, "the capture must have happened before the reconnect"
    assert not replacement[0].ws.closed.is_set(), "a stale deadline closed the new connection"


@pytest.mark.asyncio
async def test_a_deadline_with_no_connection_enqueues_nothing(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) After a disconnect, no sentinel is queued and the next connection is not closed."""
    from personal_agent.transport.agui.ws_endpoint import DONE_SENTINEL_TYPE, get_event_queue

    _settings(monkeypatch, 1, 2, 1)
    session_id = env.new_session()
    env.connect(session_id)
    env.suppress_cancellation_for()

    with capture_log_records() as records:
        await _post("hello", session_id=session_id)
        (trace_id,) = _launched_trace_ids(records)
        await asyncio.sleep(0.3)
        env.disconnect(session_id)
        assert await _wait_for(lambda: _leaked_logged(records, trace_id), timeout=4.0)
        assert await _wait_for(lambda: _degraded(records, trace_id, "no_connection"), timeout=2.0)

        queue = get_event_queue(session_id)
        queued = [queue.get_nowait() for _ in range(queue.qsize())]
        assert not any(item.get("type") == DONE_SENTINEL_TYPE for item in queued)
        for item in queued:
            queue.put_nowait(item)
        reconnected = env.connect(session_id)
        await asyncio.sleep(0.3)
        assert reconnected.sender is not None and not reconnected.sender.done()
        assert not reconnected.ws.closed.is_set()
        env.release.set()
        assert await _wait_for(lambda: _ended(records, trace_id) is not None, timeout=3.0)


# ── AC-5: shutdown closes streams first, then ends what it can ────────────────────────────


@pytest.mark.asyncio
async def test_shutdown_closes_streams_first_then_ends_what_it_can(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown returns inside two close budgets; both clients get a terminal signal."""
    _settings(monkeypatch, 30, 60, 1)
    cooperative_session, suppressing_session = env.new_session(), env.new_session()
    cooperative = env.connect(cooperative_session)
    suppressing = env.connect(suppressing_session)
    in_executor: set[str] = set()
    cooperative_gate = asyncio.Event()

    async def _handle(**kwargs: Any) -> dict[str, str]:
        in_executor.add(kwargs["session_id"])
        if kwargs["session_id"] == cooperative_session:
            await cooperative_gate.wait()
        else:
            while not env.release.is_set():
                try:
                    await env.release.wait()
                except asyncio.CancelledError:
                    continue
        return {"reply": "hi"}

    env.orchestrator.handle_user_request.side_effect = _handle
    loop = asyncio.get_running_loop()

    with capture_log_records() as records:
        await _post("one", session_id=cooperative_session)
        await _post("two", session_id=suppressing_session)
        first, second = _launched_trace_ids(records)
        assert await _wait_for(lambda: len(in_executor) == 2, timeout=2.0)

        started = loop.time()
        await get_request_task_registry().shutdown()
        assert loop.time() - started < 2.0, "shutdown must return inside two close budgets"

        assert cooperative.got_done(first) and suppressing.got_done(second)
        cooperative_ended = _ended(records, first)
        assert cooperative_ended is not None and cooperative_ended["outcome"] == "cancelled"
        assert _leaked_logged(records, second)
        (leaked,) = _live_records()
        assert leaked.trace_id == second and leaked.state == "leaked"

        env.release.set()
        assert await _wait_for(lambda: _ended(records, second) is not None, timeout=3.0)


# ── The arbiter and the late emit_done ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_once_returns_true_exactly_once_per_trace() -> None:
    """Three claims on one trace: one wins. A trace with no record is not arbitrated."""
    registry = get_request_task_registry()
    record = registry.spawn(asyncio.Event().wait(), trace_id="arb-1", session_id=str(uuid4()))
    assert registry.close_once("arb-1", "deadline") is True
    assert registry.close_once("arb-1", "emit_done") is False
    assert registry.close_once("arb-1", "shutdown") is False
    assert registry.close_once("no-such-trace", "emit_done") is True
    assert record.task is not None
    record.task.cancel()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_emit_done_after_a_won_close_pushes_no_sentinel(env: _Env) -> None:
    """If the deadline already closed the stream, ``emit_done`` persists DONE and pushes nothing."""
    from personal_agent.transport.agui.transport import emit_done
    from personal_agent.transport.agui.ws_endpoint import get_event_queue

    registry = get_request_task_registry()
    session_id = env.new_session()
    record = registry.spawn(asyncio.Event().wait(), trace_id="late-1", session_id=session_id)
    assert registry.close_once("late-1", "deadline") is True

    with capture_log_records() as records:
        await emit_done(session_id, "late-1")

    assert "DONE" in _Buffer.rows
    assert get_event_queue(session_id).empty()
    assert any(r.get("event") == "transport.done_after_close" for r in records)
    assert record.task is not None
    record.task.cancel()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_sender_skips_a_degraded_sentinel_decided_for_another_connection(
    env: _Env,
) -> None:
    """A sentinel queued behind a reconnect must not close the connection that replaced it."""
    from personal_agent.transport.agui.ws_endpoint import DONE_SENTINEL_TYPE

    client = env.connect(env.new_session())
    queue = client.conn.outbound_queue
    generation = client.conn.generation
    queue.put_nowait({"type": DONE_SENTINEL_TYPE, "trace_id": "stale", "generation": generation + 1})
    queue.put_nowait({"type": DONE_SENTINEL_TYPE, "trace_id": "own", "generation": generation})

    assert client.sender is not None
    await asyncio.wait_for(client.sender, timeout=2.0)

    assert client.got_done("own")
    assert not client.got_done("stale")
