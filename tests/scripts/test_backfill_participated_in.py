"""Tests for the FRE-343 backfill script.

Uses a real Neo4j + Postgres (via ``make up``). Marked integration.

The sessions table has a NOT NULL FK to users, so every test that inserts
a sessions row must first upsert the user_id into users.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from personal_agent.config.settings import get_settings
from personal_agent.memory.models import TurnNode
from personal_agent.memory.service import MemoryService
from scripts.backfill_participated_in import run_backfill

settings = get_settings()

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def memory_service():
    """Create and connect to MemoryService against live Neo4j."""
    service = MemoryService()
    connected = await service.connect()
    if not connected:
        pytest.skip("Neo4j not available (make up)")
    yield service
    await service.disconnect()


@pytest_asyncio.fixture
async def clean_test_data(memory_service: MemoryService):
    """Yield; each test uses unique IDs so no teardown is required."""
    yield


@pytest_asyncio.fixture
async def pg_engine():
    """Async SQLAlchemy engine against the live Postgres instance."""
    engine = create_async_engine(settings.database_url)
    yield engine
    await engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _upsert_user(engine, user_id: UUID, email: str) -> None:
    """Upsert a users row so sessions FK constraint is satisfied."""
    async with AsyncSession(engine) as db:
        await db.execute(
            text(
                "INSERT INTO users (user_id, email, created_at) "
                "VALUES (:uid, :email, :ts) "
                "ON CONFLICT (email) DO NOTHING"
            ),
            {"uid": user_id, "email": email, "ts": datetime.now(timezone.utc)},
        )
        await db.commit()


async def _insert_session(engine, session_id: str, user_id: UUID) -> None:
    """Insert a sessions row. user_id must already exist in users."""
    from uuid import UUID as _UUID
    async with AsyncSession(engine) as db:
        await db.execute(
            text(
                "INSERT INTO sessions (session_id, user_id, created_at, last_active_at) "
                "VALUES (:sid, :uid, :ts, :ts) ON CONFLICT DO NOTHING"
            ),
            {
                "sid": _UUID(session_id),
                "uid": user_id,
                "ts": datetime.now(timezone.utc),
            },
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_uses_session_user_id_when_set(
    memory_service: MemoryService, clean_test_data: None, pg_engine
) -> None:
    """A Session with user_id set -> edge is MERGEd to that :Person."""
    uid = uuid4()
    email = f"test-{uid}@example.com"

    # Provision the :Person in Neo4j and the user row in Postgres.
    await memory_service.get_or_provision_user_person(
        user_id=uid, email=email, display_name="Test User"
    )
    await _upsert_user(pg_engine, uid, email)

    session_id = str(uuid4())
    await _insert_session(pg_engine, session_id, uid)

    turn = TurnNode(
        turn_id=f"turn-{uuid4()}",
        timestamp=datetime.now(timezone.utc),
        user_message="hello",
    )
    turn.session_id = session_id
    await memory_service.create_conversation(turn, user_id=uid, visibility="group")

    # Drop the live edge to simulate pre-backfill state.
    async with memory_service.driver.session() as s:
        await s.run(
            "MATCH (:Person {user_id: $uid})-[r:PARTICIPATED_IN]->(:Turn {turn_id: $tid}) "
            "DELETE r",
            uid=str(uid),
            tid=turn.turn_id,
        )

    await run_backfill()

    async with memory_service.driver.session() as s:
        result = await s.run(
            "MATCH (:Person {user_id: $uid})-[r:PARTICIPATED_IN]->(:Turn {turn_id: $tid}) "
            "RETURN r.backfilled AS bf",
            uid=str(uid),
            tid=turn.turn_id,
        )
        rec = await result.single()
    assert rec is not None, "PARTICIPATED_IN edge was not created by backfill"
    assert rec["bf"] is True, "Expected r.backfilled=true on a newly created edge"


@pytest.mark.asyncio
async def test_backfill_uses_owner_fallback_for_null_session_user(
    memory_service: MemoryService, clean_test_data: None, pg_engine
) -> None:
    """A Session whose user_id maps to the owner -> edge is MERGEd to owner :Person."""
    # In production all sessions.user_id is NOT NULL (migration backfilled to owner).
    # We test that backfill correctly resolves the owner when it reads owner uuid.
    from personal_agent.service.auth import get_or_create_user_by_email

    async with AsyncSession(pg_engine) as db:
        owner_uid = await get_or_create_user_by_email(db, settings.agent_owner_email)
        await db.commit()

    # Ensure owner :Person exists in Neo4j (idempotent).
    await memory_service.get_or_provision_user_person(
        user_id=owner_uid,
        email=settings.agent_owner_email,
        display_name="Owner",
    )

    # Insert a session explicitly owned by the owner (simulates the fallback path).
    session_id = str(uuid4())
    await _insert_session(pg_engine, session_id, owner_uid)

    turn = TurnNode(
        turn_id=f"turn-{uuid4()}",
        timestamp=datetime.now(timezone.utc),
        user_message="owner orphan",
    )
    turn.session_id = session_id
    await memory_service.create_conversation(
        turn, user_id=owner_uid, visibility="group"
    )

    # Remove the edge so backfill has to recreate it.
    async with memory_service.driver.session() as s:
        await s.run(
            "MATCH ()-[r:PARTICIPATED_IN]->(:Turn {turn_id: $tid}) DELETE r",
            tid=turn.turn_id,
        )

    await run_backfill()

    async with memory_service.driver.session() as s:
        result = await s.run(
            "MATCH (p:Person)-[r:PARTICIPATED_IN]->(:Turn {turn_id: $tid}) "
            "RETURN p.user_id AS uid",
            tid=turn.turn_id,
        )
        rec = await result.single()
    assert rec is not None, "No PARTICIPATED_IN edge found after owner-fallback backfill"
    assert rec["uid"] == str(owner_uid), (
        f"Expected owner uid {owner_uid}, got {rec['uid']}"
    )


@pytest.mark.asyncio
async def test_backfill_is_idempotent(
    memory_service: MemoryService, clean_test_data: None, pg_engine
) -> None:
    """Running backfill twice produces exactly one edge per (user, turn)."""
    uid = uuid4()
    email = f"test-{uid}@example.com"

    await memory_service.get_or_provision_user_person(
        user_id=uid, email=email, display_name="Test User"
    )
    await _upsert_user(pg_engine, uid, email)

    session_id = str(uuid4())
    await _insert_session(pg_engine, session_id, uid)

    turn = TurnNode(
        turn_id=f"turn-{uuid4()}",
        timestamp=datetime.now(timezone.utc),
        user_message="idempotent check",
    )
    turn.session_id = session_id
    await memory_service.create_conversation(turn, user_id=uid, visibility="group")

    # Drop edge so both runs actually do work.
    async with memory_service.driver.session() as s:
        await s.run(
            "MATCH (:Person {user_id: $uid})-[r:PARTICIPATED_IN]->(:Turn {turn_id: $tid}) "
            "DELETE r",
            uid=str(uid),
            tid=turn.turn_id,
        )

    await run_backfill()
    await run_backfill()

    async with memory_service.driver.session() as s:
        result = await s.run(
            "MATCH (:Person {user_id: $uid})-[r:PARTICIPATED_IN]->(:Turn {turn_id: $tid}) "
            "RETURN count(r) AS cnt",
            uid=str(uid),
            tid=turn.turn_id,
        )
        rec = await result.single()
    assert rec is not None
    assert rec["cnt"] == 1, (
        f"Expected exactly 1 edge after two backfill runs, got {rec['cnt']}"
    )
