"""FRE-1773: assistant message metadata names the model that ran the turn.

The executor resolves the turn's primary deployment from the per-turn selection
contextvar (``resolve_role_target("primary", model_key=get_current_selection("primary"))``).
The stored assistant message's ``metadata.model`` must come from that same resolution,
not from the static ``primary`` binding — on every write path: the ``/chat/stream`` direct
append, the ``/chat`` background append, and the ``cg:session-writer`` consumer, which
runs outside the turn's context and so reads the model off ``request.completed``.
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

from personal_agent.config.model_loader import (
    is_selectable_binding,
    load_model_config,
    resolve_role_target,
)
from personal_agent.config.selection import reset_current_selection, set_current_selection
from personal_agent.events.bus import NoOpBus, get_event_bus, set_global_event_bus
from personal_agent.events.models import RequestCompletedEvent
from personal_agent.events.redis_backend import RedisStreamBus
from personal_agent.events.request_completed_handlers import build_session_writer_handler
from tests._helpers.request_tasks import run_turn

_TEST_USER_ID = uuid4()


def _bound_and_other() -> tuple[str, str, str]:
    """Return the bound primary model id, another selectable key, and that key's model id."""
    bound_key, bound_def = resolve_role_target("primary")
    assert bound_def is not None
    config = load_model_config()
    for key in config.models:
        if key == bound_key or not is_selectable_binding("primary", key, config):
            continue
        _, other_def = resolve_role_target("primary", model_key=key)
        if other_def is not None and other_def.id != bound_def.id:
            return bound_def.id, key, other_def.id
    pytest.skip("catalog has no second selectable primary deployment")


@pytest.fixture(autouse=True)
def _no_turn_registry_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """``open_turn`` reads Postgres; a unit test of the chat task must not."""
    monkeypatch.setattr(
        "personal_agent.transport.agui.transport.open_turn", AsyncMock(return_value=None)
    )


@pytest.fixture(autouse=True)
def _restore_event_bus() -> Iterator[None]:
    """Each test owns the global event bus singleton; restore it after."""
    original = get_event_bus()
    yield
    set_global_event_bus(original)


@asynccontextmanager
async def _fake_db_session(_mock_db: MagicMock) -> AsyncIterator[MagicMock]:
    yield _mock_db


def _mock_orchestrator() -> MagicMock:
    session_manager = MagicMock()
    session_manager.get_session.return_value = None
    orchestrator = MagicMock()
    orchestrator.session_manager = session_manager
    orchestrator.handle_user_request = AsyncMock(
        return_value={"reply": "hi there", "trace_id": "trace-1"}
    )
    return orchestrator


def _mock_repo() -> MagicMock:
    session = SimpleNamespace(session_id=uuid4(), messages=[], execution_profile="local")
    repo = MagicMock()
    repo.get = AsyncMock(return_value=session)
    repo.append_message = AsyncMock(return_value=None)
    return repo


def _assistant_metadata(repo: MagicMock) -> dict[str, object]:
    appends = [
        c.args[1]
        for c in repo.append_message.await_args_list
        if c.args[1].get("role") == "assistant"
    ]
    assert len(appends) == 1
    metadata: dict[str, object] = appends[0]["metadata"]
    return metadata


async def _run_stream_turn(
    mock_session_local: MagicMock,
    mock_repo_cls: MagicMock,
    mock_orchestrator_cls: MagicMock,
    *,
    primary_selection_key: str | None,
) -> MagicMock:
    repo = _mock_repo()
    mock_repo_cls.return_value = repo
    mock_session_local.side_effect = lambda: _fake_db_session(MagicMock())
    mock_orchestrator_cls.return_value = _mock_orchestrator()
    await run_turn(
        session_id=str(uuid4()),
        message="hi",
        user_id=_TEST_USER_ID,
        trace_id="trace-1",
        primary_selection_key=primary_selection_key,
    )
    return repo


@pytest.fixture(autouse=True)
def _no_transport_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chat task's attachment check and WS pushes are not under test here."""
    monkeypatch.setattr(
        "personal_agent.service.app._validate_attachments", AsyncMock(return_value=[])
    )
    for name in ("emit_done", "_push_event", "emit_session_selection"):
        monkeypatch.setattr(f"personal_agent.transport.agui.transport.{name}", AsyncMock())


@pytest.mark.asyncio
@patch("personal_agent.orchestrator.Orchestrator")
@patch("personal_agent.service.app.SessionRepository")
@patch("personal_agent.service.app.AsyncSessionLocal")
async def test_ac1_direct_append_stores_the_selected_deployments_model(
    mock_session_local: MagicMock,
    mock_repo_cls: MagicMock,
    mock_orchestrator_cls: MagicMock,
) -> None:
    """AC-1: a session switched to another deployment stores that deployment's model id."""
    bound_id, other_key, other_id = _bound_and_other()
    set_global_event_bus(NoOpBus())

    repo = await _run_stream_turn(
        mock_session_local, mock_repo_cls, mock_orchestrator_cls, primary_selection_key=other_key
    )

    metadata = _assistant_metadata(repo)
    assert metadata["model"] == other_id
    assert metadata["model"] != bound_id


@pytest.mark.asyncio
@patch("personal_agent.orchestrator.Orchestrator")
@patch("personal_agent.service.app.SessionRepository")
@patch("personal_agent.service.app.AsyncSessionLocal")
async def test_ac2_direct_append_without_selection_stores_the_bound_model(
    mock_session_local: MagicMock,
    mock_repo_cls: MagicMock,
    mock_orchestrator_cls: MagicMock,
) -> None:
    """AC-2: a turn with no session selection stores the bound primary model."""
    bound_id, _, _ = _bound_and_other()
    set_global_event_bus(NoOpBus())

    repo = await _run_stream_turn(
        mock_session_local, mock_repo_cls, mock_orchestrator_cls, primary_selection_key=None
    )

    assert _assistant_metadata(repo)["model"] == bound_id


@pytest.mark.asyncio
@patch("personal_agent.orchestrator.Orchestrator")
@patch("personal_agent.service.app.SessionRepository")
@patch("personal_agent.service.app.AsyncSessionLocal")
async def test_ac1_redis_publish_carries_the_selected_deployments_model(
    mock_session_local: MagicMock,
    mock_repo_cls: MagicMock,
    mock_orchestrator_cls: MagicMock,
) -> None:
    """AC-1: request.completed carries the turn's model, for the out-of-context consumer."""
    _, other_key, other_id = _bound_and_other()
    redis_client = AsyncMock(spec=aioredis.Redis)
    redis_client.xadd = AsyncMock(return_value="1-0")
    set_global_event_bus(RedisStreamBus(redis_client))

    await _run_stream_turn(
        mock_session_local, mock_repo_cls, mock_orchestrator_cls, primary_selection_key=other_key
    )

    payload = orjson.loads(redis_client.xadd.call_args[0][1]["data"])
    assert payload["primary_model"] == other_id


@pytest.mark.asyncio
@patch("personal_agent.service.app.SessionRepository")
@patch("personal_agent.service.app.AsyncSessionLocal")
async def test_ac3_background_append_uses_the_turns_selection(
    mock_session_local: MagicMock,
    mock_repo_cls: MagicMock,
) -> None:
    """AC-3: the /chat background append reads the same per-turn source as the direct one."""
    from personal_agent.service.app import _append_assistant_message_background

    _, other_key, other_id = _bound_and_other()
    repo = _mock_repo()
    mock_repo_cls.return_value = repo
    mock_session_local.side_effect = lambda: _fake_db_session(MagicMock())

    token = set_current_selection({"primary": other_key})
    try:
        # /chat creates this task after setting the selection; the task copies the context.
        await asyncio.create_task(
            _append_assistant_message_background(uuid4(), "hi there", "trace-1")
        )
    finally:
        reset_current_selection(token)

    assert _assistant_metadata(repo)["model"] == other_id


@pytest.mark.asyncio
@pytest.mark.parametrize("carried", [True, False])
async def test_ac3_session_writer_stores_the_events_model_else_the_binding(
    carried: bool,
) -> None:
    """AC-3: the consumer stores the model the event carries; an old event falls back."""
    bound_id, _, other_id = _bound_and_other()
    repo = _mock_repo()
    event = RequestCompletedEvent(
        trace_id="trace-1",
        session_id=str(uuid4()),
        assistant_response="hi there",
        source_component="service.app",
        primary_model=other_id if carried else None,
    )
    with (
        patch(
            "personal_agent.events.request_completed_handlers.AsyncSessionLocal",
            side_effect=lambda: _fake_db_session(MagicMock()),
        ),
        patch(
            "personal_agent.events.request_completed_handlers.SessionRepository",
            return_value=repo,
        ),
    ):
        await build_session_writer_handler()(event)

    assert _assistant_metadata(repo)["model"] == (other_id if carried else bound_id)
