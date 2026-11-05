"""Session message writes on real rows of the test Postgres (FRE-1733 AC-1, AC-5).

``_chat_impl`` loads the session and then waits for the prior turn's write. These tests land a write
during that wait, and they run concurrent appends.

Marker: ``integration`` — excluded from ``make test`` because it needs a live DB.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from personal_agent.config import settings as app_settings
from personal_agent.events.redis_backend import RedisStreamBus
from personal_agent.events.session_write_waiter import release_session_write_wait
from personal_agent.service.app import chat
from personal_agent.service.auth import RequestUser
from personal_agent.service.repositories.session_repository import SessionRepository

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_FIRST_USER = {"role": "user", "content": "first question"}
_PRIOR_REPLY = {
    "role": "assistant",
    "content": "the prior turn's reply",
    "model": "m",
    "model_role": "primary",
    "model_config_path": "c",
}


@pytest_asyncio.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Return a session factory on the test Postgres, and refuse any other database (AC-5)."""
    assert ":5433/" in app_settings.database_url, "this test writes, so it runs on the test stack"
    engine = create_async_engine(app_settings.database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[tuple[UUID, UUID]]:
    """Seed one user and one session that holds the first user message."""
    user_id, session_id = uuid4(), uuid4()
    now = datetime.now(timezone.utc)
    async with maker() as db:
        await db.execute(
            text("INSERT INTO users (user_id, email) VALUES (:uid, :email)"),
            {"uid": user_id, "email": f"fre1733-{user_id}@test.invalid"},
        )
        await db.execute(
            text(
                "INSERT INTO sessions (session_id, user_id, created_at, last_active_at, messages)"
                ' VALUES (:sid, :uid, :now, :now, \'[{"role": "user", "content": "first question"}]\')'
            ),
            {"sid": session_id, "uid": user_id, "now": now},
        )
        await db.commit()
    yield user_id, session_id
    async with maker() as db:
        await db.execute(text("DELETE FROM sessions WHERE session_id = :sid"), {"sid": session_id})
        await db.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": user_id})
        await db.commit()


async def _stored_messages(
    maker: async_sessionmaker[AsyncSession], session_id: UUID
) -> list[dict[str, Any]]:
    async with maker() as db:
        row = await db.execute(
            text("SELECT messages FROM sessions WHERE session_id = :sid"), {"sid": session_id}
        )
        messages: list[dict[str, Any]] = row.scalar_one()
        return messages


async def test_a_write_during_the_wait_reaches_the_history_and_the_table(
    maker: async_sessionmaker[AsyncSession], seeded: tuple[UUID, UUID]
) -> None:
    """AC-1: a prior reply that lands during the wait is not dropped, and the next turn sees it."""
    user_id, session_id = seeded

    async def _write_during_the_wait(sid: str, *, trace_id: str) -> None:
        async with maker() as other:
            await SessionRepository(other).append_message(UUID(sid), dict(_PRIOR_REPLY))

    session_manager = MagicMock()
    session_manager.get_session.return_value = None
    orchestrator = SimpleNamespace(
        session_manager=session_manager,
        handle_user_request=AsyncMock(return_value={"reply": "ok", "trace_id": "t"}),
    )
    bus = MagicMock(spec=RedisStreamBus)
    bus.publish = AsyncMock()

    async with maker() as db:
        with (
            patch("personal_agent.orchestrator.Orchestrator", return_value=orchestrator),
            patch("personal_agent.service.app.run_gateway_pipeline", AsyncMock(return_value=None)),
            patch(
                "personal_agent.service.app._resolve_session_selection",
                AsyncMock(return_value=("primary-key", None)),
            ),
            patch("personal_agent.events.bus.get_event_bus", return_value=bus),
            patch(
                "personal_agent.events.session_write_waiter.await_previous_session_write",
                _write_during_the_wait,
            ),
        ):
            await chat(
                message="second question",
                session_id=str(session_id),
                request_user=RequestUser(user_id=user_id, email="fre1733@test.invalid"),
                db=db,
            )
    release_session_write_wait(str(session_id))

    stored = await _stored_messages(maker, session_id)
    assert [(m["role"], m["content"]) for m in stored] == [
        ("user", "first question"),
        ("assistant", "the prior turn's reply"),
        ("user", "second question"),
    ]
    history = session_manager.update_session.call_args.kwargs["messages"]
    assert [m["content"] for m in history] == ["first question", "the prior turn's reply"]


async def test_concurrent_appends_all_survive(
    maker: async_sessionmaker[AsyncSession], seeded: tuple[UUID, UUID]
) -> None:
    """AC-1: every append survives when the appends run at the same time."""
    _, session_id = seeded

    async def _append(number: int) -> None:
        async with maker() as db:
            message = {"role": "user", "content": f"message {number}"}
            await SessionRepository(db).append_message(session_id, message)

    await asyncio.gather(*(_append(number) for number in range(20)))

    stored = await _stored_messages(maker, session_id)
    assert stored[0] == _FIRST_USER
    assert {m["content"] for m in stored[1:]} == {f"message {number}" for number in range(20)}
    assert len(stored) == 21


async def test_an_append_from_a_stale_session_keeps_the_other_write(
    maker: async_sessionmaker[AsyncSession], seeded: tuple[UUID, UUID]
) -> None:
    """A session that loaded the row earlier does not write its old list back."""
    _, session_id = seeded
    async with maker() as stale_db:
        stale_repo = SessionRepository(stale_db)
        # Hold the object: the identity map is weak, and a dropped object loads again.
        held = await stale_repo.get(session_id)
        assert held is not None
        async with maker() as other:
            await SessionRepository(other).append_message(session_id, dict(_PRIOR_REPLY))

        second = {"role": "user", "content": "second question"}
        returned = await stale_repo.append_message(session_id, second)

    assert returned is not None
    assert returned.messages == [_FIRST_USER, _PRIOR_REPLY, second]
    assert await _stored_messages(maker, session_id) == [_FIRST_USER, _PRIOR_REPLY, second]


async def test_an_append_to_a_session_with_null_messages_starts_the_list(
    maker: async_sessionmaker[AsyncSession], seeded: tuple[UUID, UUID]
) -> None:
    """The column has a default but allows NULL, and NULL || a value is NULL in Postgres."""
    _, session_id = seeded
    async with maker() as db:
        await db.execute(
            text("UPDATE sessions SET messages = NULL WHERE session_id = :sid"), {"sid": session_id}
        )
        await db.commit()
    async with maker() as db:
        await SessionRepository(db).append_message(session_id, dict(_FIRST_USER))

    assert await _stored_messages(maker, session_id) == [_FIRST_USER]


async def test_an_append_to_an_unknown_session_returns_none(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The return contract stays: no session, no row, and ``None``."""
    async with maker() as db:
        assert await SessionRepository(db).append_message(uuid4(), dict(_FIRST_USER)) is None
