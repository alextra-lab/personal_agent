"""SessionModelSelectionRepository tests (ADR-0121 §4 / FRE-917).

Real-DB tests against the test-stack Postgres (:5433 — FRE-375 isolation),
mirroring ``test_session_repository_retention.py``. Skips cleanly when the
test-stack is unreachable (``make test-infra-up``).

Proves the selection store's CRUD contract: a missing row reads ``None`` (caller
applies the default), an insert then a conflicting insert upserts in place (no
duplicate PK), and ``get_all`` returns the session's whole role → key map.
"""

from __future__ import annotations

import socket
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from personal_agent.service.database import AsyncSessionLocal, engine
from personal_agent.service.repositories.session_model_selection_repository import (
    SessionModelSelectionRepository,
)


def _postgres_available() -> bool:
    try:
        with socket.create_connection(("localhost", 5433), timeout=2):
            return True
    except OSError:
        return False


def test_model_timestamps_have_server_default() -> None:
    """created_at/updated_at carry a DDL server_default (no DB needed).

    Guards the deploy-ordering bug: if ``Base.metadata.create_all`` builds this
    table before migration 0020 runs, the columns must still get ``DEFAULT NOW()``
    at the DDL level, or the migration's backfill INSERT (which omits these
    columns) would raise a NOT NULL violation.
    """
    from personal_agent.service.models import SessionModelSelectionModel

    table = SessionModelSelectionModel.__table__
    assert table.c.created_at.server_default is not None
    assert table.c.updated_at.server_default is not None


@pytest_asyncio.fixture(autouse=True)
async def _dispose_engine_per_test():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_session(db) -> object:
    user_id = uuid4()
    session_id = uuid4()
    await db.execute(
        text("INSERT INTO users (user_id, email) VALUES (:uid, :email)"),
        {"uid": user_id, "email": f"fre917-{user_id}@test.invalid"},
    )
    await db.execute(
        text(
            "INSERT INTO sessions (session_id, user_id, created_at, last_active_at)"
            " VALUES (:sid, :uid, :now, :now)"
        ),
        {"sid": session_id, "uid": user_id, "now": datetime.now(timezone.utc)},
    )
    await db.commit()
    return user_id, session_id


async def _cleanup(db, user_id, session_id) -> None:
    await db.execute(text("DELETE FROM sessions WHERE session_id = :sid"), {"sid": session_id})
    await db.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": user_id})
    await db.commit()


@pytest.mark.asyncio
async def test_get_missing_returns_none():
    """A role with no stored selection reads None (caller applies the default)."""
    if not _postgres_available():
        pytest.skip("test Postgres :5433 unreachable — run make test-infra-up")
    async with AsyncSessionLocal() as db:
        user_id, session_id = await _seed_session(db)
        try:
            repo = SessionModelSelectionRepository(db)
            assert await repo.get(session_id, "primary") is None
        finally:
            await _cleanup(db, user_id, session_id)


@pytest.mark.asyncio
async def test_upsert_inserts_then_updates_in_place():
    """A second upsert for the same (session, role) updates rather than duplicates."""
    if not _postgres_available():
        pytest.skip("test Postgres :5433 unreachable — run make test-infra-up")
    async with AsyncSessionLocal() as db:
        user_id, session_id = await _seed_session(db)
        try:
            repo = SessionModelSelectionRepository(db)
            await repo.upsert(
                session_id=session_id, role="primary", deployment_key="qwen3.6-35b-thinking"
            )
            assert await repo.get(session_id, "primary") == "qwen3.6-35b-thinking"

            await repo.upsert(session_id=session_id, role="primary", deployment_key="claude_sonnet")
            assert await repo.get(session_id, "primary") == "claude_sonnet"

            count = (
                await db.execute(
                    text(
                        "SELECT COUNT(*) FROM session_model_selections "
                        "WHERE session_id = :sid AND role = 'primary'"
                    ),
                    {"sid": session_id},
                )
            ).scalar()
            assert count == 1
        finally:
            await _cleanup(db, user_id, session_id)


@pytest.mark.asyncio
async def test_get_all_returns_role_map():
    """get_all returns every stored role → deployment key for the session."""
    if not _postgres_available():
        pytest.skip("test Postgres :5433 unreachable — run make test-infra-up")
    async with AsyncSessionLocal() as db:
        user_id, session_id = await _seed_session(db)
        try:
            repo = SessionModelSelectionRepository(db)
            await repo.upsert(session_id=session_id, role="primary", deployment_key="claude_sonnet")
            await repo.upsert(
                session_id=session_id, role="artifact_builder", deployment_key="claude_haiku"
            )
            assert await repo.get_all(session_id) == {
                "primary": "claude_sonnet",
                "artifact_builder": "claude_haiku",
            }
        finally:
            await _cleanup(db, user_id, session_id)
