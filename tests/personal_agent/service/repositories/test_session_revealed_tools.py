"""ADR-0156 A5.4 (FRE-1595 AC-1) — the ``sessions.revealed_tools`` record.

Real-DB tests against the test-stack Postgres (:5433, FRE-375 isolation). They skip cleanly if
the test stack is unreachable (``make test-infra-up``).

- ``add_revealed_tools`` is a set union in one ``UPDATE``, so two concurrent reveals both persist.
- Both methods filter on ``session_id`` and ``user_id``: a wrong user reads and changes nothing.
- The PATCH endpoint cannot set or clear the record, and a ``metadata`` PATCH leaves it.
- Pruning a session clears the record with its messages.
"""

from __future__ import annotations

import asyncio
import socket
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from personal_agent.service.database import AsyncSessionLocal, engine
from personal_agent.service.models import SessionUpdate
from personal_agent.service.repositories.session_repository import SessionRepository

pytestmark = pytest.mark.asyncio


def _postgres_available() -> bool:
    try:
        with socket.create_connection(("localhost", 5433), timeout=2):
            return True
    except OSError:
        return False


@pytest_asyncio.fixture(autouse=True)
async def _dispose_engine_per_test():
    """Dispose the shared engine's pool around each test (asyncpg binds to one event loop)."""
    await engine.dispose()
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def owned_session():
    """Yield ``(session_id, user_id)`` for a fresh session; delete both rows at teardown."""
    if not _postgres_available():
        pytest.skip("Test Postgres (port 5433) not reachable — run make test-infra-up")
    user_id, session_id = uuid4(), uuid4()
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("INSERT INTO users (user_id, email) VALUES (:uid, :email)"),
            {"uid": user_id, "email": f"fre1595-{user_id}@test.invalid"},
        )
        await db.execute(
            text(
                "INSERT INTO sessions (session_id, user_id, created_at, last_active_at, messages)"
                " VALUES (:sid, :uid, :now, :now, CAST(:messages AS jsonb))"
            ),
            {"sid": session_id, "uid": user_id, "now": now, "messages": '[{"role": "user"}]'},
        )
        await db.commit()
    try:
        yield session_id, user_id
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(
                text("DELETE FROM sessions WHERE session_id = :sid"), {"sid": session_id}
            )
            await db.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": user_id})
            await db.commit()


async def _add(session_id: UUID, user_id: UUID, names: list[str]) -> int:
    async with AsyncSessionLocal() as db:
        return await SessionRepository(db).add_revealed_tools(session_id, user_id, names)


async def _load(session_id: UUID, user_id: UUID) -> frozenset[str]:
    async with AsyncSessionLocal() as db:
        return await SessionRepository(db).load_revealed_tools(session_id, user_id)


async def test_a_new_session_has_no_reveal(owned_session) -> None:
    session_id, user_id = owned_session
    assert await _load(session_id, user_id) == frozenset()


async def test_an_add_is_a_set_union(owned_session) -> None:
    session_id, user_id = owned_session
    assert await _add(session_id, user_id, ["search_logs", "measure_latency"]) == 1
    assert await _add(session_id, user_id, ["search_logs", "other_tool"]) == 1
    assert await _load(session_id, user_id) == {"search_logs", "measure_latency", "other_tool"}


async def test_concurrent_adds_all_persist(owned_session) -> None:
    """Two adds that wait on the same row lock both persist (no lost update)."""
    session_id, user_id = owned_session
    async with AsyncSessionLocal() as holder:
        assert (await holder.execute(text("SHOW transaction_isolation"))).scalar() == (
            "read committed"
        )
        # Hold the row lock, so both adds must queue behind it and run against a changed row.
        await holder.execute(
            text("SELECT 1 FROM sessions WHERE session_id = :sid FOR UPDATE"), {"sid": session_id}
        )
        first = asyncio.create_task(_add(session_id, user_id, ["tool_a"]))
        second = asyncio.create_task(_add(session_id, user_id, ["tool_b"]))
        async with AsyncSessionLocal() as probe:
            for _ in range(600):
                # pg_stat_activity is a snapshot that Postgres caches for the whole transaction.
                # Clear it before each read, or the count stays at its first value.
                await probe.execute(text("SELECT pg_stat_clear_snapshot()"))
                waiting = (
                    await probe.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE wait_event_type = 'Lock' AND query LIKE 'UPDATE sessions%'"
                        )
                    )
                ).scalar()
                if waiting == 2:
                    break
                await asyncio.sleep(0.05)
        assert waiting == 2, "the two adds never waited on the row lock together"
        await holder.commit()
    assert await asyncio.gather(first, second) == [1, 1]
    assert await _load(session_id, user_id) == {"tool_a", "tool_b"}


async def test_a_wrong_user_reads_and_changes_nothing(owned_session) -> None:
    session_id, user_id = owned_session
    await _add(session_id, user_id, ["search_logs"])
    stranger = uuid4()
    assert await _add(session_id, stranger, ["evil_tool"]) == 0
    assert await _load(session_id, stranger) == frozenset()
    assert await _load(session_id, user_id) == {"search_logs"}


async def test_an_add_to_no_row_returns_zero(owned_session) -> None:
    _session_id, user_id = owned_session
    assert await _add(uuid4(), user_id, ["search_logs"]) == 0


async def test_a_patch_cannot_set_or_clear_the_record(owned_session) -> None:
    """The PATCH endpoint ignores ``revealed_tools`` and a ``metadata`` change keeps it."""
    from personal_agent.service.app import update_session
    from personal_agent.service.auth import RequestUser

    session_id, user_id = owned_session
    await _add(session_id, user_id, ["search_logs"])
    assert "revealed_tools" not in SessionUpdate.model_fields
    user = RequestUser(user_id=user_id, email="fre1595@test.invalid", display_name=None)

    for body in (
        {"revealed_tools": []},
        {"revealed_tools": ["evil_tool"], "metadata": {"theme": "dark"}},
        {"metadata": {}},
    ):
        async with AsyncSessionLocal() as db:
            await update_session(
                str(session_id), SessionUpdate.model_validate(body), request_user=user, db=db
            )
        assert await _load(session_id, user_id) == {"search_logs"}, body


async def test_pruning_clears_the_record(owned_session) -> None:
    session_id, user_id = owned_session
    await _add(session_id, user_id, ["search_logs"])
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("UPDATE sessions SET last_active_at = :old WHERE session_id = :sid"),
            {"old": datetime.now(timezone.utc) - timedelta(days=400), "sid": session_id},
        )
        await db.commit()
        assert await SessionRepository(db).prune_expired(retention_days=180) >= 1
    assert await _load(session_id, user_id) == frozenset()
