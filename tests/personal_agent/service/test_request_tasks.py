"""FRE-1408 / ADR-0143 D1 + D2: the request-task registry and the stage-one deadline.

The chat turn task used to be a bare ``asyncio.create_task`` with no reference, no callback
and no outer bound. These tests prove the registry owns it (AC-1, AC-2), stage one ends it
on an absolute deadline and classifies the ending honestly (AC-3), and the settings are
ordered at startup (AC-4).

AC-5: the tests for AC-1 (bypass), AC-2, AC-3(a) and AC-4 are written so that they reach
their assertion on ``main`` too. They drive the production endpoint, read the registry
through ``_registry_or_none`` and set the new settings through ``_set_setting``, so on
``main`` they fail on behaviour (no ``task_ended`` log, no bound) and not on an import error.

Stage one is cooperative (ADR-0143 D2). The error report that follows a timeout awaits
``_push_event`` again, so a resource that stays blocked can still hold the task. D3 (FRE-1410)
bounds that. The blocked push in these tests blocks once.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator, MutableMapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from personal_agent.config import settings
from personal_agent.config.settings import AppConfig
from personal_agent.events.bus import NoOpBus, get_event_bus, set_global_event_bus
from personal_agent.service.auth import RequestUser
from personal_agent.service.idempotency import DeduplicationResult
from tests._helpers.log_capture import capture_log_records

_USER_ID = UUID("1f7cc4bc-3f83-4f21-88e5-96b4b08b116a")
_SESSION_ID = "35d379be-b465-4b46-86b3-ff6db56c57ab"

Records = list[MutableMapping[str, Any]]


# ── Baseline-compatible helpers (must not import the new module at top level) ─────────────


def _registry_or_none() -> Any | None:
    """Return the registry singleton, or ``None`` on a tree that has no registry (``main``)."""
    try:
        from personal_agent.service.request_tasks import get_request_task_registry
    except ImportError:
        return None
    return get_request_task_registry()


def _live_records() -> list[Any]:
    """Return the registry's records; empty when there is no registry."""
    registry = _registry_or_none()
    return list(registry.records()) if registry is not None else []


def _set_setting(monkeypatch: pytest.MonkeyPatch, name: str, value: int) -> None:
    """Set a setting; skip it only on a tree that has no registry (``main``)."""
    try:
        monkeypatch.setattr(settings, name, value)
    except AttributeError:
        if _registry_or_none() is not None:
            raise  # the registry exists, so a missing setting is a typo and not a baseline tree


def _ended(records: Records, trace_id: str) -> MutableMapping[str, Any] | None:
    """Return the ``chat_stream.task_ended`` record for a trace, if one was logged."""
    return next(
        (
            r
            for r in records
            if r.get("event") == "chat_stream.task_ended" and r.get("trace_id") == trace_id
        ),
        None,
    )


async def _wait_for(predicate: Any, *, timeout: float) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` seconds pass."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return bool(predicate())


# ── Mocks for the real turn body ────────────────────────────────────────────────────────


@dataclass
class _BodyMocks:
    """Handles on the doubles that replace the substrate the turn body touches."""

    session_local: MagicMock
    push_event: AsyncMock
    emit_done: AsyncMock
    open_turn: AsyncMock
    orchestrator: MagicMock
    release: asyncio.Event = field(default_factory=asyncio.Event)
    gates: dict[str, asyncio.Event] = field(default_factory=dict)


@asynccontextmanager
async def _fake_db_session() -> AsyncIterator[MagicMock]:
    yield MagicMock()


@pytest.fixture
def body_mocks(monkeypatch: pytest.MonkeyPatch) -> Iterator[_BodyMocks]:
    """Replace the substrate the real ``_process_chat_stream_background`` touches.

    Yields:
        Handles on the doubles. ``release`` frees every blocked double at teardown, so a test
        that fails on ``main`` (where nothing ends the blocked task) leaves no task behind.
    """
    session = SimpleNamespace(session_id=uuid4(), messages=[], execution_profile="local")
    repo = MagicMock()
    repo.get = AsyncMock(return_value=session)
    repo.append_message = AsyncMock(return_value=None)
    orchestrator = MagicMock()
    orchestrator.session_manager = MagicMock()
    orchestrator.session_manager.get_session.return_value = None
    orchestrator.handle_user_request = AsyncMock(return_value={"reply": "hi"})

    original_bus = get_event_bus()
    set_global_event_bus(NoOpBus())
    with (
        patch("personal_agent.service.app.AsyncSessionLocal") as session_local,
        patch("personal_agent.service.app.SessionRepository", return_value=repo),
        patch("personal_agent.service.app._validate_attachments", new=AsyncMock(return_value=[])),
        patch("personal_agent.service.app.run_gateway_pipeline", new=AsyncMock(return_value=None)),
        patch("personal_agent.service.app._record_first_token", new=AsyncMock()),
        patch("personal_agent.orchestrator.Orchestrator", return_value=orchestrator),
        patch("personal_agent.transport.agui.transport._push_event", new=AsyncMock()) as push,
        patch("personal_agent.transport.agui.transport.emit_done", new=AsyncMock()) as emit_done,
        patch("personal_agent.transport.agui.transport.open_turn", new=AsyncMock()) as open_turn,
    ):
        session_local.side_effect = lambda: _fake_db_session()
        push.return_value = True
        mocks = _BodyMocks(
            session_local=session_local,
            push_event=push,
            emit_done=emit_done,
            open_turn=open_turn,
            orchestrator=orchestrator,
        )
        yield mocks
        mocks.release.set()
        for gate in mocks.gates.values():
            gate.set()
    set_global_event_bus(original_bus)


@pytest.fixture(autouse=True)
def _registry_is_empty_afterwards() -> Iterator[None]:
    """A test must leave no record in the registry singleton."""
    yield
    registry = _registry_or_none()
    if registry is None:
        return
    left = registry.records()
    for record in left:
        if record.task is not None:
            record.task.cancel()
    assert left == (), f"records left in the registry singleton: {[r.trace_id for r in left]}"


async def _post(message: str, *, session_id: str = _SESSION_ID) -> None:
    """Call the production ``/chat/stream`` endpoint, with its three lookups stubbed."""
    from personal_agent.service.app import chat_stream_endpoint

    dedup = MagicMock()
    dedup.check_and_record.return_value = DeduplicationResult(is_duplicate=False)
    with (
        patch("personal_agent.service.app.get_deduplicator", return_value=dedup),
        patch(
            "personal_agent.service.app._resolve_session_selection",
            new=AsyncMock(return_value=("anthropic-sonnet", "default")),
        ),
        patch(
            "personal_agent.service.app._resolve_session_effort",
            new=AsyncMock(return_value=("standard", "default")),
        ),
    ):
        await chat_stream_endpoint(
            message=message,
            session_id=session_id,
            primary_selection=None,
            seshat_effort=None,
            client_msg_id=None,
            attachments=None,
            request_user=RequestUser(user_id=_USER_ID, email="o@example.test"),
        )


def _launched_trace_ids(records: Records) -> list[str]:
    """Return the trace ids that ``chat_stream_endpoint`` launched, in launch order."""
    return [r["trace_id"] for r in records if r.get("event") == "chat_stream.launched"]


def _block_the_first_push(mocks: _BodyMocks) -> None:
    """Make the reply push block once, on a cooperative wait; later pushes return."""
    calls = 0

    async def _push(*_args: object, **_kwargs: object) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            await mocks.release.wait()
        return True

    mocks.push_event.side_effect = _push


# ── AC-1: a bypass raises, a registry creation does not ────────────────────────────────────


@pytest.mark.asyncio
async def test_bare_create_task_raises_entry_error_before_any_side_effect(
    body_mocks: _BodyMocks,
) -> None:
    """A turn body started with a bare ``create_task`` raises before it does anything."""
    from personal_agent.service.app import _process_chat_stream_background

    with capture_log_records() as records:
        task = asyncio.create_task(
            _process_chat_stream_background(
                session_id=_SESSION_ID, message="m", user_id=_USER_ID, trace_id="bypass-1"
            )
        )
        with pytest.raises(Exception) as excinfo:  # noqa: PT011 - the class is new on this branch
            await task

    assert type(excinfo.value).__name__ == "RequestTaskEntryError"
    body_mocks.session_local.assert_not_called()
    body_mocks.open_turn.assert_not_called()
    body_mocks.push_event.assert_not_called()
    assert [r for r in records if r.get("trace_id") == "bypass-1"] == []


@pytest.mark.asyncio
async def test_a_child_task_inside_a_registered_turn_cannot_run_the_body(
    body_mocks: _BodyMocks,
) -> None:
    """The context marker is inherited by a child task; the task identity check refuses it."""
    from personal_agent.service.app import _process_chat_stream_background
    from personal_agent.service.request_tasks import (
        RequestTaskEntryError,
        get_request_task_registry,
    )

    async def parent() -> None:
        child = asyncio.create_task(
            _process_chat_stream_background(
                session_id=_SESSION_ID, message="m", user_id=_USER_ID, trace_id="parent-1"
            )
        )
        with pytest.raises(RequestTaskEntryError):
            await child

    record = get_request_task_registry().spawn(
        parent(), trace_id="parent-1", session_id=_SESSION_ID
    )
    assert record.task is not None
    await record.task
    body_mocks.session_local.assert_not_called()


@pytest.mark.asyncio
async def test_registry_spawn_runs_the_body(body_mocks: _BodyMocks) -> None:
    """The same body, created through the registry, runs to completion."""
    from tests._helpers.request_tasks import run_turn

    with capture_log_records() as records:
        await run_turn(
            session_id=_SESSION_ID, message="m", user_id=_USER_ID, trace_id="registered-1"
        )

    body_mocks.session_local.assert_called()
    body_mocks.push_event.assert_called()
    ended = _ended(records, "registered-1")
    assert ended is not None and ended["outcome"] == "completed"


# ── AC-2: two turns on one session are two records, and both endings are logged ───────────


@pytest.mark.asyncio
async def test_two_turns_on_one_session_are_two_records_and_both_endings_are_logged(
    body_mocks: _BodyMocks,
) -> None:
    """Two concurrent turns on one session do not overwrite each other, and each ending is logged."""

    async def _handle(**kwargs: Any) -> dict[str, str]:
        gate = asyncio.Event()
        body_mocks.gates[kwargs["trace_id"]] = gate
        await gate.wait()
        return {"reply": "hi"}

    body_mocks.orchestrator.handle_user_request.side_effect = _handle

    with capture_log_records() as records:
        await _post("first")
        await _post("second")
        first, second = _launched_trace_ids(records)
        assert await _wait_for(lambda: len(body_mocks.gates) == 2, timeout=2.0)

        live = _live_records()
        assert len(live) == 2, "two turns on one session must be two registry records"
        assert {r.trace_id for r in live} == {first, second}
        assert {r.session_id for r in live} == {_SESSION_ID}

        body_mocks.gates[first].set()
        by_trace = {r.trace_id: r for r in live}
        by_trace[second].task.cancel()
        assert await _wait_for(
            lambda: _ended(records, first) is not None and _ended(records, second) is not None,
            timeout=2.0,
        ), "both turn endings must be logged as chat_stream.task_ended"

    assert _ended(records, first)["outcome"] == "completed"  # type: ignore[index]
    assert _ended(records, second)["outcome"] == "cancelled"  # type: ignore[index]
    assert _ended(records, first)["session_id"] == _SESSION_ID  # type: ignore[index]
    assert _live_records() == []


# ── AC-3: stage one fires on the absolute deadline and classifies honestly ────────────────


@pytest.mark.asyncio
async def test_stage_one_times_out_a_cooperative_block_after_the_executor(
    body_mocks: _BodyMocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) A block after the executor returns ends at the deadline as ``timed_out``."""
    _set_setting(monkeypatch, "orchestrator_turn_lifetime_seconds", 1)
    _set_setting(monkeypatch, "service_request_task_lifetime_seconds", 2)
    _set_setting(monkeypatch, "service_request_close_timeout_seconds", 1)
    _block_the_first_push(body_mocks)

    with capture_log_records() as records:
        await _post("hello")
        (trace_id,) = _launched_trace_ids(records)
        assert await _wait_for(lambda: _ended(records, trace_id) is not None, timeout=3.0), (
            "the turn task must end by 3 s after started_at"
        )

    ended = _ended(records, trace_id)
    assert ended is not None
    assert ended["outcome"] == "timed_out"
    assert 1.9 <= ended["elapsed_seconds"] <= 3.0
    assert _live_records() == []
    # Cancellation runs the body's `finally`: the stream still gets its DONE.
    body_mocks.emit_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_inner_timeout_error_before_the_deadline_is_failed(
    body_mocks: _BodyMocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) A ``TimeoutError`` raised below the bound, at 0.2 s, is ``failed`` and not ``timed_out``."""
    _set_setting(monkeypatch, "orchestrator_turn_lifetime_seconds", 1)
    _set_setting(monkeypatch, "service_request_task_lifetime_seconds", 2)
    _set_setting(monkeypatch, "service_request_close_timeout_seconds", 1)

    async def _tool_times_out(**_kwargs: Any) -> dict[str, str]:
        await asyncio.sleep(0.2)
        raise asyncio.TimeoutError("a tool gave up")

    body_mocks.orchestrator.handle_user_request.side_effect = _tool_times_out

    with capture_log_records() as records:
        await _post("hello")
        (trace_id,) = _launched_trace_ids(records)
        assert await _wait_for(lambda: _ended(records, trace_id) is not None, timeout=3.0)

    ended = _ended(records, trace_id)
    assert ended is not None
    assert ended["outcome"] == "failed"
    assert ended["elapsed_seconds"] < 1.5


@pytest.mark.asyncio
async def test_an_escaping_timeout_error_before_the_deadline_is_failed(
    body_mocks: _BodyMocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) A ``TimeoutError`` that reaches the outer handler before the deadline is ``failed``."""
    _set_setting(monkeypatch, "orchestrator_turn_lifetime_seconds", 1)
    _set_setting(monkeypatch, "service_request_task_lifetime_seconds", 2)

    def _summary_times_out(*_args: object, **_kwargs: object) -> None:
        raise asyncio.TimeoutError("a downstream call gave up")

    with (
        patch("personal_agent.service.app.build_turn_summary", side_effect=_summary_times_out),
        capture_log_records() as records,
    ):
        await _post("hello")
        (trace_id,) = _launched_trace_ids(records)
        assert await _wait_for(lambda: _ended(records, trace_id) is not None, timeout=3.0)

    ended = _ended(records, trace_id)
    assert ended is not None
    assert ended["outcome"] == "failed"
    assert ended["elapsed_seconds"] < 1.5


@pytest.mark.asyncio
async def test_scheduling_delay_is_charged_to_the_turn(
    body_mocks: _BodyMocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) A task that starts 1.5 s late, with a 2 s lifetime, still ends at about 2 s."""
    _set_setting(monkeypatch, "orchestrator_turn_lifetime_seconds", 1)
    _set_setting(monkeypatch, "service_request_task_lifetime_seconds", 2)
    _set_setting(monkeypatch, "service_request_close_timeout_seconds", 1)
    _block_the_first_push(body_mocks)

    with capture_log_records() as records:
        await _post("hello")
        time.sleep(1.5)  # noqa: ASYNC251 - blocks the loop on purpose: the task cannot start
        (trace_id,) = _launched_trace_ids(records)
        assert await _wait_for(lambda: _ended(records, trace_id) is not None, timeout=3.0)

    ended = _ended(records, trace_id)
    assert ended is not None
    assert ended["outcome"] == "timed_out"
    assert 1.9 <= ended["elapsed_seconds"] <= 2.6, (
        "the deadline counts from insertion, not from start"
    )


# ── AC-4: the validator rejects a misorder at startup ─────────────────────────────────────


def test_lifetime_not_above_orchestrator_cap_is_rejected() -> None:
    """A service lifetime at or below the orchestrator cap does not construct."""
    with pytest.raises(ValidationError) as excinfo:
        AppConfig(
            orchestrator_turn_lifetime_seconds=100,
            service_request_task_lifetime_seconds=100,
        )
    message = str(excinfo.value)
    assert "service_request_task_lifetime_seconds" in message
    assert "orchestrator_turn_lifetime_seconds" in message


def test_lifetime_defaults_to_the_orchestrator_cap_plus_300() -> None:
    """The default leaves the orchestrator cap to fire first on a long but live turn."""
    config = AppConfig(orchestrator_turn_lifetime_seconds=600)
    assert config.service_request_task_lifetime_seconds == 900
    assert config.service_request_close_timeout_seconds == 10


# ── The registry itself: shutdown, leaks, duplicates, the timer stamp ─────────────────────


@pytest.fixture
def registry() -> Any:
    """A registry that is private to the test (the endpoint uses the singleton)."""
    from personal_agent.service.request_tasks import RequestTaskRegistry

    return RequestTaskRegistry()


@pytest.mark.asyncio
async def test_shutdown_cancels_live_tasks_and_logs_their_ending(
    registry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown cancels every live task, waits, and leaves an empty registry."""
    monkeypatch.setattr(settings, "service_request_close_timeout_seconds", 1)

    with capture_log_records() as records:
        record = registry.spawn(asyncio.Event().wait(), trace_id="sd-1", session_id=_SESSION_ID)
        await registry.shutdown()

    assert registry.records() == ()
    assert record.task is not None and record.task.cancelled()
    ended = _ended(records, "sd-1")
    assert ended is not None and ended["outcome"] == "cancelled"
    assert ended["was_leaked"] is False


@pytest.mark.asyncio
async def test_a_task_that_ignores_cancellation_is_leaked_and_kept(
    registry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A survivor of shutdown is marked leaked, stays enumerable, and reports ``was_leaked``."""
    monkeypatch.setattr(settings, "service_request_close_timeout_seconds", 1)
    release = asyncio.Event()

    async def stubborn() -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    with capture_log_records() as records:
        record = registry.spawn(stubborn(), trace_id="leak-1", session_id=_SESSION_ID)
        await asyncio.sleep(
            0
        )  # let the body start: a task cancelled before its first step never runs
        await registry.shutdown()

        assert record.state == "leaked"
        assert registry.records() == (record,)
        assert registry.counts() == (0, 1)
        assert any(
            r.get("event") == "chat_stream.task_leaked" and r.get("trace_id") == "leak-1"
            for r in records
        )
        assert any(
            r.get("event") == "request_tasks_gauge" and r.get("live") == 0 and r.get("leaked") == 1
            for r in records
        )

        release.set()
        assert record.task is not None
        await record.task
        await asyncio.sleep(0)

    assert registry.records() == ()
    ended = _ended(records, "leak-1")
    assert ended is not None and ended["was_leaked"] is True


@pytest.mark.asyncio
async def test_a_second_record_with_the_same_trace_id_is_refused(registry: Any) -> None:
    """Overwriting a live record would drop the strong reference to its task."""
    first = registry.spawn(asyncio.Event().wait(), trace_id="dup-1", session_id=_SESSION_ID)
    with pytest.raises(ValueError, match="dup-1"):
        registry.spawn(asyncio.Event().wait(), trace_id="dup-1", session_id=_SESSION_ID)
    assert registry.records() == (first,)
    assert first.task is not None
    first.task.cancel()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_the_timer_stamps_timed_out_on_a_task_still_running_at_the_deadline(
    registry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timer alone stamps ``timed_out``; a task that ends later still reports it."""
    monkeypatch.setattr(settings, "service_request_task_lifetime_seconds", 1)
    release = asyncio.Event()

    async def stubborn() -> None:
        await release.wait()

    with capture_log_records() as records:
        record = registry.spawn(stubborn(), trace_id="stamp-1", session_id=_SESSION_ID)
        assert await _wait_for(lambda: record.outcome == "timed_out", timeout=2.0)
        release.set()
        assert record.task is not None
        await record.task
        await asyncio.sleep(0)

    ended = _ended(records, "stamp-1")
    assert ended is not None and ended["outcome"] == "timed_out"


@pytest.mark.asyncio
async def test_an_exception_that_escapes_a_stamped_task_is_still_logged(registry: Any) -> None:
    """A stamped outcome does not hide the exception: its type is logged and it is retrieved."""

    async def boom() -> None:
        raise ValueError("escaped the body")

    with capture_log_records() as records:
        record = registry.spawn(boom(), trace_id="esc-1", session_id=_SESSION_ID)
        record.stamp_failed()
        assert record.task is not None
        with pytest.raises(ValueError, match="escaped the body"):
            await record.task
        await asyncio.sleep(0)

    ended = _ended(records, "esc-1")
    assert ended is not None
    assert ended["outcome"] == "failed"
    assert ended["error_type"] == "ValueError"


# ── FRE-1697: an output-cap stop is a failed turn, not a completed one ─────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        ({"reply": "The model used its whole output limit.", "error_category": "output_cap"}, "failed"),
        ({"reply": "The model server returned an error.", "error_category": "model_server"}, "completed"),
        ({"reply": "hi"}, "completed"),
    ],
)
async def test_an_output_cap_result_ends_the_task_as_failed(
    body_mocks: _BodyMocks, result: dict[str, str], outcome: str
) -> None:
    """The orchestrator returns normally; only the output-cap category stamps ``failed``."""
    body_mocks.orchestrator.handle_user_request.return_value = result

    with capture_log_records() as records:
        await _post("hello")
        (trace_id,) = _launched_trace_ids(records)
        assert await _wait_for(lambda: _ended(records, trace_id) is not None, timeout=3.0)

    ended = _ended(records, trace_id)
    assert ended is not None
    assert ended["outcome"] == outcome
