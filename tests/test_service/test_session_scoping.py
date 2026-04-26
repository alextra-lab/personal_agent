"""Tests for session ownership scoping — FRE-268.

Verifies that SessionRepository filters by user_id so that one user
cannot read, list, update, or continue another user's sessions.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from personal_agent.service.models import SessionCreate, SessionModel, SessionUpdate
from personal_agent.service.repositories.session_repository import SessionRepository


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(user_id: UUID) -> SessionModel:
    """Return a SessionModel owned by the given user_id."""
    s = SessionModel()
    s.session_id = uuid4()
    s.user_id = user_id
    s.created_at = datetime.now(timezone.utc)
    s.last_active_at = datetime.now(timezone.utc)
    s.mode = "NORMAL"
    s.channel = "CHAT"
    s.metadata_ = {}
    s.messages = []
    return s


def _async_db() -> MagicMock:
    """Return a mock AsyncSession with awaitable execute/commit/refresh."""
    db = MagicMock()
    db.execute = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


# ---------------------------------------------------------------------------
# list_recent — must filter by user_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_recent_returns_only_caller_sessions() -> None:
    """list_recent(user_id=X) must not return sessions belonging to other users."""
    user_a = uuid4()
    user_b = uuid4()
    session_a = _make_session(user_a)

    db = _async_db()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [session_a]
    db.execute.return_value = mock_result

    repo = SessionRepository(db)
    sessions = await repo.list_recent(limit=50, user_id=user_a)

    assert len(sessions) == 1
    assert sessions[0].user_id == user_a

    # The WHERE clause must include user_id — verify the query was called with a filter
    # Postgres renders UUIDs without hyphens in compiled SQL
    called_query = db.execute.call_args[0][0]
    query_str = str(called_query.compile(compile_kwargs={"literal_binds": True}))
    assert str(user_a).replace("-", "") in query_str


@pytest.mark.asyncio
async def test_list_recent_empty_when_no_sessions_for_user() -> None:
    """list_recent returns empty list when user has no sessions."""
    db = _async_db()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    db.execute.return_value = mock_result

    repo = SessionRepository(db)
    sessions = await repo.list_recent(limit=50, user_id=uuid4())

    assert sessions == []


# ---------------------------------------------------------------------------
# get — 404 on ownership mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_session_when_user_matches() -> None:
    """get(session_id, user_id=owner) returns the session."""
    owner = uuid4()
    session = _make_session(owner)

    db = _async_db()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = session
    db.execute.return_value = mock_result

    repo = SessionRepository(db)
    result = await repo.get(session.session_id, user_id=owner)

    assert result is session


@pytest.mark.asyncio
async def test_get_returns_none_when_user_does_not_match() -> None:
    """get(session_id, user_id=stranger) returns None — caller sees 404."""
    owner = uuid4()
    stranger = uuid4()
    session = _make_session(owner)

    db = _async_db()
    # DB query filtered by both session_id AND user_id — no row returned
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    db.execute.return_value = mock_result

    repo = SessionRepository(db)
    result = await repo.get(session.session_id, user_id=stranger)

    assert result is None


@pytest.mark.asyncio
async def test_get_without_user_id_returns_session_unscoped() -> None:
    """get(session_id) without user_id skips ownership filter (internal callers)."""
    owner = uuid4()
    session = _make_session(owner)

    db = _async_db()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = session
    db.execute.return_value = mock_result

    repo = SessionRepository(db)
    result = await repo.get(session.session_id)  # no user_id

    assert result is session


# ---------------------------------------------------------------------------
# create — assigns user_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_stores_user_id_on_session() -> None:
    """create(data, user_id=X) must set session.user_id = X."""
    uid = uuid4()
    db = _async_db()

    created_session: list[SessionModel] = []

    def capture_add(obj: object) -> None:
        if isinstance(obj, SessionModel):
            created_session.append(obj)

    db.add.side_effect = capture_add

    repo = SessionRepository(db)
    await repo.create(SessionCreate(channel="CHAT", mode="NORMAL"), user_id=uid)

    assert len(created_session) == 1
    assert created_session[0].user_id == uid


# ---------------------------------------------------------------------------
# update — returns None on ownership mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_returns_none_when_user_does_not_match() -> None:
    """update scoped by user_id returns None when session belongs to someone else."""
    owner = uuid4()
    stranger = uuid4()
    session = _make_session(owner)

    db = _async_db()
    execute_result = MagicMock()
    execute_result.rowcount = 0  # UPDATE touched 0 rows (wrong user_id)
    db.execute.return_value = execute_result

    # Subsequent get also returns None (no row for this user)
    get_result = MagicMock()
    get_result.scalar_one_or_none.return_value = None
    db.execute.side_effect = [execute_result, get_result]

    repo = SessionRepository(db)
    result = await repo.update(
        session.session_id,
        SessionUpdate(mode="ALERT"),
        user_id=stranger,
    )

    assert result is None
