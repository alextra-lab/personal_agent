"""FRE-860 (ADR-0098 D4/D6) — SessionRepository retention (soft-prune) tests.

Real-DB tests against the test-stack Postgres (:5433 — see FRE-375 isolation),
mirroring ``tests/integration/test_notes_search_db.py``. Skips cleanly if the
test-stack is unreachable (``make test-infra-up``).

Proves the ticket's acceptance criteria directly: a session older than the
retention window is pruned (messages cleared, purged_at stamped); a session
inside the window is retained (untouched); and resuming a pruned session
(append_message) clears the tombstone rather than leaving purged_at/messages
in an inconsistent state (the reactivation-semantics fix from codex review).
"""

from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from personal_agent.service.database import AsyncSessionLocal, engine
from personal_agent.service.models import SessionUpdate
from personal_agent.service.repositories.session_repository import SessionRepository

RETENTION_DAYS = 180


def _postgres_available() -> bool:
    """Return True when the test Postgres substrate (port 5433) is reachable."""
    try:
        with socket.create_connection(("localhost", 5433), timeout=2):
            return True
    except OSError:
        return False


@pytest_asyncio.fixture(autouse=True)
async def _dispose_engine_per_test():
    """Dispose the shared async engine's pool before and after each test.

    ``AsyncSessionLocal``'s engine is a module-level singleton (created once at
    import time and reused by every test file in the suite); asyncpg
    connections in its pool bind to whichever event loop first used them.
    pytest-asyncio's default function-scoped loop means each test function
    (in this file, and in any other file that ran before it in the same
    suite run) gets a fresh loop, so a pool left over from a prior test
    would raise "attached to a different loop". Disposing both before (in
    case a preceding test file left a stale pool) and after (so the next
    test file starts clean) forces fresh connections under each test's loop.
    """
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_user(db) -> object:
    user_id = uuid4()
    await db.execute(
        text("INSERT INTO users (user_id, email) VALUES (:uid, :email)"),
        {"uid": user_id, "email": f"fre860-{user_id}@test.invalid"},
    )
    return user_id


async def _seed_session(db, user_id, *, last_active_at: datetime) -> object:
    session_id = uuid4()
    await db.execute(
        text(
            "INSERT INTO sessions (session_id, user_id, created_at, last_active_at, messages)"
            " VALUES (:sid, :uid, :now, :last_active, CAST(:messages AS jsonb))"
        ),
        {
            "sid": session_id,
            "uid": user_id,
            "now": datetime.now(timezone.utc),
            "last_active": last_active_at,
            "messages": '[{"role": "user", "content": "hi"}]',
        },
    )
    await db.commit()
    return session_id


async def _cleanup(db, user_id, session_ids: list) -> None:
    """Delete seeded rows so the shared test-stack ``sessions`` table stays clean.

    ``sessions`` is a persistent table shared across the whole test run (not
    per-test-isolated) — leftover rows from a prior run would otherwise be
    caught by a later run's retention sweep and inflate its pruned-count.
    """
    for session_id in session_ids:
        await db.execute(text("DELETE FROM sessions WHERE session_id = :sid"), {"sid": session_id})
    await db.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": user_id})
    await db.commit()


@pytest.mark.asyncio
async def test_prune_expired_prunes_old_retains_recent() -> None:
    """Session older than the window is pruned; a session inside it is retained."""
    if not _postgres_available():
        pytest.skip("Test Postgres (port 5433) not reachable — run make test-infra-up")

    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        user_id = await _seed_user(db)
        old_session_id = await _seed_session(db, user_id, last_active_at=now - timedelta(days=200))
        recent_session_id = await _seed_session(
            db, user_id, last_active_at=now - timedelta(days=10)
        )
        try:
            repo = SessionRepository(db)
            # >=1 rather than ==1: `sessions` is a shared table across the full
            # test-suite run, not per-test-isolated, so other dormant rows may
            # also match — the two per-row assertions below are the real proof.
            assert await repo.prune_expired(retention_days=RETENTION_DAYS) >= 1

            old_session = await repo.get(old_session_id)
            assert old_session is not None
            assert old_session.purged_at is not None
            assert old_session.messages == []

            recent_session = await repo.get(recent_session_id)
            assert recent_session is not None
            assert recent_session.purged_at is None
            assert recent_session.messages == [{"role": "user", "content": "hi"}]

            # Idempotent — a second sweep touches nothing further for our rows
            # (already-purged rows are excluded via the purged_at IS NULL guard).
            assert await repo.prune_expired(retention_days=RETENTION_DAYS) == 0
        finally:
            await _cleanup(db, user_id, [old_session_id, recent_session_id])


@pytest.mark.asyncio
async def test_append_message_to_pruned_session_clears_tombstone() -> None:
    """Resuming a pruned session (append_message) clears purged_at — no inconsistent state."""
    if not _postgres_available():
        pytest.skip("Test Postgres (port 5433) not reachable — run make test-infra-up")

    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        user_id = await _seed_user(db)
        session_id = await _seed_session(db, user_id, last_active_at=now - timedelta(days=200))
        try:
            repo = SessionRepository(db)
            assert await repo.prune_expired(retention_days=RETENTION_DAYS) >= 1

            pruned = await repo.get(session_id)
            assert pruned is not None
            assert pruned.purged_at is not None
            assert pruned.messages == []

            new_message = {
                "role": "assistant",
                "content": "welcome back",
                "model": "anthropic/claude-sonnet-5",
                "model_role": "primary",
                "model_config_path": "/opt/seshat/config/models.cloud.yaml",
            }
            updated = await repo.append_message(session_id, new_message)

            assert updated is not None
            assert updated.purged_at is None
            assert updated.messages == [new_message]
        finally:
            await _cleanup(db, user_id, [session_id])


@pytest.mark.asyncio
async def test_update_non_messages_field_on_pruned_session_clears_tombstone() -> None:
    """A non-messages update() (e.g. mode) on a purged session also clears purged_at.

    Regression guard: last_active_at is bumped unconditionally by update(), so
    if purged_at were only cleared for a messages write, a mode/channel/
    execution_profile update would make a purged session look freshly active
    (last_active_at = now) while staying permanently excluded from future
    retention re-evaluation (purged_at IS NULL is the scan/prune guard) with
    messages stuck at '[]' forever.
    """
    if not _postgres_available():
        pytest.skip("Test Postgres (port 5433) not reachable — run make test-infra-up")

    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        user_id = await _seed_user(db)
        session_id = await _seed_session(db, user_id, last_active_at=now - timedelta(days=200))
        try:
            repo = SessionRepository(db)
            assert await repo.prune_expired(retention_days=RETENTION_DAYS) >= 1

            pruned = await repo.get(session_id)
            assert pruned is not None
            assert pruned.purged_at is not None

            updated = await repo.update(session_id, SessionUpdate(mode="FOCUS"))

            assert updated is not None
            assert updated.mode == "FOCUS"
            assert updated.purged_at is None
        finally:
            await _cleanup(db, user_id, [session_id])
