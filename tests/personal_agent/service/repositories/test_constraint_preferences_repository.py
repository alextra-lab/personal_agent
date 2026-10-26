"""ConstraintPreferencesRepository list/delete tests (FRE-1702).

Real-DB tests against the test-stack Postgres (:5433 — FRE-375 isolation),
mirroring ``test_session_model_selection_repository.py``. Skips cleanly when the
test stack is unreachable (``make test-infra-up``).

Proves the two reads the settings menu needs: ``list_for_user`` returns only the
caller's rows, and ``delete`` removes exactly one (user, constraint) row and says
whether a row existed.
"""

from __future__ import annotations

import socket
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from personal_agent.service.database import AsyncSessionLocal, engine
from personal_agent.service.repositories.constraint_preferences_repository import (
    ConstraintPreferencesRepository,
)


def _postgres_available() -> bool:
    try:
        with socket.create_connection(("localhost", 5433), timeout=2):
            return True
    except OSError:
        return False


@pytest_asyncio.fixture(autouse=True)
async def _dispose_engine_per_test():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_user(db):
    user_id = uuid4()
    await db.execute(
        text("INSERT INTO users (user_id, email) VALUES (:uid, :email)"),
        {"uid": user_id, "email": f"fre1702-{user_id}@test.invalid"},
    )
    await db.commit()
    return user_id


async def _cleanup(db, *user_ids) -> None:
    for user_id in user_ids:
        await db.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": user_id})
    await db.commit()


@pytest.mark.asyncio
async def test_list_for_user_returns_only_that_users_rows():
    """Another user's saved choices never appear in the caller's list."""
    if not _postgres_available():
        pytest.skip("test Postgres :5433 unreachable — run make test-infra-up")
    async with AsyncSessionLocal() as db:
        mine = await _seed_user(db)
        other = await _seed_user(db)
        try:
            repo = ConstraintPreferencesRepository(db)
            await repo.upsert(
                user_id=mine, constraint_name="artifact_builder", preferred_action="claude_sonnet"
            )
            await repo.upsert(
                user_id=mine, constraint_name="spend_threshold", preferred_action="always_pause"
            )
            await repo.upsert(
                user_id=other, constraint_name="artifact_builder", preferred_action="gpt-6.1-sol"
            )

            rows = await repo.list_for_user(mine)

            assert {(r.constraint_name, r.preferred_action) for r in rows} == {
                ("artifact_builder", "claude_sonnet"),
                ("spend_threshold", "always_pause"),
            }
        finally:
            await _cleanup(db, mine, other)


@pytest.mark.asyncio
async def test_list_for_user_empty_when_no_rows():
    """A user with no saved choice gets an empty list, not an error."""
    if not _postgres_available():
        pytest.skip("test Postgres :5433 unreachable — run make test-infra-up")
    async with AsyncSessionLocal() as db:
        user_id = await _seed_user(db)
        try:
            assert await ConstraintPreferencesRepository(db).list_for_user(user_id) == []
        finally:
            await _cleanup(db, user_id)


@pytest.mark.asyncio
async def test_delete_removes_one_row_and_reports_it():
    """Delete removes the named row only, returns True; a second delete returns False."""
    if not _postgres_available():
        pytest.skip("test Postgres :5433 unreachable — run make test-infra-up")
    async with AsyncSessionLocal() as db:
        user_id = await _seed_user(db)
        other = await _seed_user(db)
        try:
            repo = ConstraintPreferencesRepository(db)
            await repo.upsert(
                user_id=user_id,
                constraint_name="artifact_builder",
                preferred_action="claude_sonnet",
            )
            await repo.upsert(
                user_id=user_id, constraint_name="spend_threshold", preferred_action="answer_now"
            )
            await repo.upsert(
                user_id=other, constraint_name="artifact_builder", preferred_action="gpt-6.1-sol"
            )

            assert await repo.delete(user_id, "artifact_builder") is True
            assert await repo.get_preferred_action(user_id, "artifact_builder") is None
            # Untouched: the same user's other row and the other user's same-name row.
            assert await repo.get_preferred_action(user_id, "spend_threshold") == "answer_now"
            assert await repo.get_preferred_action(other, "artifact_builder") == "gpt-6.1-sol"

            assert await repo.delete(user_id, "artifact_builder") is False
        finally:
            await _cleanup(db, user_id, other)
