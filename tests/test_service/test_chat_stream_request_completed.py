"""FRE-1033: `_process_chat_stream_background` must publish `request.completed`.

Live PWA chat traffic goes through `/chat/stream` -> `_process_chat_stream_background`,
not either of the two publish sites FRE-1033's ticket text names -- and that function
had zero publish logic for this event. Naively adding a publish call would double-append
the assistant message, because the `cg:session-writer` consumer
(`build_session_writer_handler`) unconditionally appends `event.assistant_response` on
every `RequestCompletedEvent`, and this function already appends that same message
itself. These tests prove: the Redis branch publishes (once) instead of appending
directly, the NoOp branch keeps its direct append, a publish failure falls back to
direct persistence rather than silently losing the turn, and a cancelled task still
releases its waiter.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import orjson
import pytest
import redis.asyncio as aioredis

from personal_agent.events import session_write_waiter as sww
from personal_agent.events.bus import NoOpBus, get_event_bus, set_global_event_bus
from personal_agent.events.redis_backend import RedisStreamBus
from personal_agent.service.app import _process_chat_stream_background

_TEST_USER_ID = uuid4()


@asynccontextmanager
async def _fake_db_session(_mock_db: MagicMock) -> AsyncIterator[MagicMock]:
    yield _mock_db


@pytest.fixture(autouse=True)
def _clean_waiters() -> Iterator[None]:
    """No waiter state leaks between tests (mirrors test_session_write_waiter.py)."""
    sww._session_write_waiters.clear()
    yield
    sww._session_write_waiters.clear()


@pytest.fixture(autouse=True)
def _restore_event_bus() -> Iterator[None]:
    """Each test owns the global event bus singleton; restore it after."""
    original = get_event_bus()
    yield
    set_global_event_bus(original)


@pytest.fixture
def mock_redis() -> AsyncMock:
    """Mocked redis.asyncio.Redis client (mirrors tests/personal_agent/events/test_consumer.py)."""
    client = AsyncMock(spec=aioredis.Redis)
    client.xadd = AsyncMock(return_value="1-0")
    return client


def _mock_orchestrator() -> MagicMock:
    """An Orchestrator stand-in whose handle_user_request returns a fixed reply."""
    session_manager = MagicMock()
    session_manager.get_session.return_value = None
    orchestrator = MagicMock()
    orchestrator.session_manager = session_manager

    async def _handle_user_request(**_kwargs: object) -> dict[str, str]:
        return {"reply": "hi there", "trace_id": "trace-1"}

    orchestrator.handle_user_request = AsyncMock(side_effect=_handle_user_request)
    return orchestrator


@pytest.mark.asyncio
@patch("personal_agent.service.app._validate_attachments", new_callable=AsyncMock)
@patch("personal_agent.transport.agui.transport.emit_done", new_callable=AsyncMock)
@patch("personal_agent.transport.agui.transport._push_event", new_callable=AsyncMock)
@patch("personal_agent.orchestrator.Orchestrator")
@patch("personal_agent.service.app.SessionRepository")
@patch("personal_agent.service.app.AsyncSessionLocal")
async def test_redis_branch_publishes_instead_of_double_appending(
    mock_session_local: MagicMock,
    mock_repo_cls: MagicMock,
    mock_orchestrator_cls: MagicMock,
    mock_push_event: AsyncMock,
    mock_emit_done: AsyncMock,
    mock_validate_attachments: AsyncMock,
    mock_redis: AsyncMock,
) -> None:
    """Redis bus active: publish once with user_id; no direct assistant append."""
    session_id = uuid4()
    session = SimpleNamespace(session_id=session_id, messages=[], execution_profile="local")
    mock_repo = MagicMock()
    mock_repo.get = AsyncMock(return_value=session)
    mock_repo.append_message = AsyncMock(return_value=None)
    mock_repo_cls.return_value = mock_repo
    mock_session_local.side_effect = lambda: _fake_db_session(MagicMock())
    mock_validate_attachments.return_value = []
    mock_orchestrator_cls.return_value = _mock_orchestrator()

    set_global_event_bus(RedisStreamBus(mock_redis))

    await _process_chat_stream_background(
        session_id=str(session_id),
        message="Tell me about Python",
        user_id=_TEST_USER_ID,
        trace_id="trace-1",
    )

    # Exactly one publish (XADD) to the request.completed stream.
    mock_redis.xadd.assert_called_once()
    stream_name, fields = mock_redis.xadd.call_args[0][0], mock_redis.xadd.call_args[0][1]
    assert stream_name == "stream:request.completed"
    payload = orjson.loads(fields["data"])
    assert payload["user_id"] == str(_TEST_USER_ID)
    assert payload["source_component"] == "service.app"
    # ADR-0129 D3 / FRE-1067: RequestTimer is retired — the event carries no
    # timing payload; span timing now lives in the OTel span tree.
    assert "trace_summary" not in payload
    assert "trace_breakdown" not in payload

    # Only the user-message append happened synchronously; the assistant append
    # is the session-writer consumer's job now, not this function's.
    assistant_appends = [
        call
        for call in mock_repo.append_message.await_args_list
        if call.args[1].get("role") == "assistant"
    ]
    assert assistant_appends == [], (
        "the Redis branch must not append the assistant message itself -- "
        "cg:session-writer does that; a direct append here double-writes it"
    )


@pytest.mark.asyncio
@patch("personal_agent.service.app._validate_attachments", new_callable=AsyncMock)
@patch("personal_agent.transport.agui.transport.emit_done", new_callable=AsyncMock)
@patch("personal_agent.transport.agui.transport._push_event", new_callable=AsyncMock)
@patch("personal_agent.orchestrator.Orchestrator")
@patch("personal_agent.service.app.SessionRepository")
@patch("personal_agent.service.app.AsyncSessionLocal")
async def test_noop_branch_still_appends_directly(
    mock_session_local: MagicMock,
    mock_repo_cls: MagicMock,
    mock_orchestrator_cls: MagicMock,
    mock_push_event: AsyncMock,
    mock_emit_done: AsyncMock,
    mock_validate_attachments: AsyncMock,
) -> None:
    """NoOp bus (no consumers running): direct append must still happen (regression guard)."""
    session_id = uuid4()
    session = SimpleNamespace(session_id=session_id, messages=[], execution_profile="local")
    mock_repo = MagicMock()
    mock_repo.get = AsyncMock(return_value=session)
    mock_repo.append_message = AsyncMock(return_value=None)
    mock_repo_cls.return_value = mock_repo
    mock_session_local.side_effect = lambda: _fake_db_session(MagicMock())
    mock_validate_attachments.return_value = []
    mock_orchestrator_cls.return_value = _mock_orchestrator()

    set_global_event_bus(NoOpBus())

    await _process_chat_stream_background(
        session_id=str(session_id),
        message="Tell me about Python",
        user_id=_TEST_USER_ID,
        trace_id="trace-1",
    )

    assistant_appends = [
        call
        for call in mock_repo.append_message.await_args_list
        if call.args[1].get("role") == "assistant"
    ]
    assert len(assistant_appends) == 1
    assert assistant_appends[0].args[1]["content"] == "hi there"


@pytest.mark.asyncio
@patch("personal_agent.service.app._validate_attachments", new_callable=AsyncMock)
@patch("personal_agent.transport.agui.transport.emit_done", new_callable=AsyncMock)
@patch("personal_agent.transport.agui.transport._push_event", new_callable=AsyncMock)
@patch("personal_agent.orchestrator.Orchestrator")
@patch("personal_agent.service.app.SessionRepository")
@patch("personal_agent.service.app.AsyncSessionLocal")
async def test_publish_failure_falls_back_to_direct_append_and_releases_waiter(
    mock_session_local: MagicMock,
    mock_repo_cls: MagicMock,
    mock_orchestrator_cls: MagicMock,
    mock_push_event: AsyncMock,
    mock_emit_done: AsyncMock,
    mock_validate_attachments: AsyncMock,
    mock_redis: AsyncMock,
) -> None:
    """A publish failure must not silently drop the already-streamed answer."""
    session_id = uuid4()
    session = SimpleNamespace(session_id=session_id, messages=[], execution_profile="local")
    mock_repo = MagicMock()
    mock_repo.get = AsyncMock(return_value=session)
    mock_repo.append_message = AsyncMock(return_value=None)
    mock_repo_cls.return_value = mock_repo
    mock_session_local.side_effect = lambda: _fake_db_session(MagicMock())
    mock_validate_attachments.return_value = []
    mock_orchestrator_cls.return_value = _mock_orchestrator()

    mock_redis.xadd = AsyncMock(side_effect=ConnectionError("redis down"))
    set_global_event_bus(RedisStreamBus(mock_redis))

    await _process_chat_stream_background(
        session_id=str(session_id),
        message="Tell me about Python",
        user_id=_TEST_USER_ID,
        trace_id="trace-1",
    )

    assistant_appends = [
        call
        for call in mock_repo.append_message.await_args_list
        if call.args[1].get("role") == "assistant"
    ]
    assert len(assistant_appends) == 1, (
        "publish failure must fall back to a direct append, not silently drop the turn"
    )
    assert sww._session_write_waiters == {}, "the waiter must be released on publish failure"


@pytest.mark.asyncio
@patch("personal_agent.service.app._validate_attachments", new_callable=AsyncMock)
@patch("personal_agent.transport.agui.transport.emit_done", new_callable=AsyncMock)
@patch("personal_agent.transport.agui.transport._push_event", new_callable=AsyncMock)
@patch("personal_agent.orchestrator.Orchestrator")
@patch("personal_agent.service.app.SessionRepository")
@patch("personal_agent.service.app.AsyncSessionLocal")
async def test_cancellation_after_waiter_registered_releases_it(
    mock_session_local: MagicMock,
    mock_repo_cls: MagicMock,
    mock_orchestrator_cls: MagicMock,
    mock_push_event: AsyncMock,
    mock_emit_done: AsyncMock,
    mock_validate_attachments: AsyncMock,
    mock_redis: AsyncMock,
) -> None:
    """A cancelled task must release its waiter rather than leak it until timeout."""
    session_id = uuid4()
    session = SimpleNamespace(session_id=session_id, messages=[], execution_profile="local")
    mock_repo = MagicMock()
    mock_repo.get = AsyncMock(return_value=session)
    mock_repo.append_message = AsyncMock(return_value=None)
    mock_repo_cls.return_value = mock_repo
    mock_session_local.side_effect = lambda: _fake_db_session(MagicMock())
    mock_validate_attachments.return_value = []
    mock_orchestrator_cls.return_value = _mock_orchestrator()

    publish_started = asyncio.Event()

    async def _hanging_publish(*_args: object, **_kwargs: object) -> None:
        publish_started.set()
        await asyncio.sleep(60)

    bus = RedisStreamBus(mock_redis)
    bus.publish = AsyncMock(side_effect=_hanging_publish)  # type: ignore[method-assign]
    set_global_event_bus(bus)

    task = asyncio.create_task(
        _process_chat_stream_background(
            session_id=str(session_id),
            message="Tell me about Python",
            user_id=_TEST_USER_ID,
            trace_id="trace-1",
        )
    )
    await asyncio.wait_for(publish_started.wait(), timeout=1.0)
    assert sww._session_write_waiters, "waiter must be registered before publish resolves"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sww._session_write_waiters == {}, "cancellation must not leak the waiter"
