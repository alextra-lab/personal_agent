"""FRE-436: /chat endpoint must persist and use the correct execution_profile.

Two bugs:
1. New-session creation: SessionCreate() was called without passing the request
   profile — so the DB row always stored 'local' regardless of which profile the
   caller requested.
2. Profile activation for existing sessions: the raw request-param 'profile'
   (default 'local') was used instead of session.execution_profile, so the LLM
   factory could be pointed at the wrong backend even when the stored profile
   was 'cloud'.

Both bugs caused cloud sessions to appear as 'local' in the session history view.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from personal_agent.service.models import SessionCreate


# ── 1. SessionCreate profile field ───────────────────────────────────────────


def test_session_create_defaults_to_none_not_local() -> None:
    """SessionCreate.execution_profile must NOT default to 'local'.

    The repository's create() adds the 'local' fallback — SessionCreate itself
    should be neutral so callers can pass any profile without fighting a baked-in
    default.
    """
    sc = SessionCreate()
    assert sc.execution_profile is None, (
        "SessionCreate.execution_profile should be None by default so the "
        "repo.create() fallback ('local') is the single source of truth"
    )


def test_session_create_accepts_cloud_profile() -> None:
    """SessionCreate can carry a cloud profile through to the repository."""
    sc = SessionCreate(execution_profile="cloud")
    assert sc.execution_profile == "cloud"


# ── 2. _resolve_session_profile logic replicated for /chat ───────────────────


def _make_session(profile: str) -> MagicMock:
    """Return a minimal mock SessionModel with a given execution_profile."""
    s = MagicMock()
    s.execution_profile = profile
    s.session_id = uuid4()
    return s


@pytest.mark.asyncio
async def test_new_session_uses_supplied_profile() -> None:
    """When no session row exists yet, the supplied profile is adopted."""
    from personal_agent.service.app import _resolve_session_profile

    with patch("personal_agent.service.app.AsyncSessionLocal") as mock_ctx:
        db_mock = AsyncMock()
        repo_mock = MagicMock()
        repo_mock.get = AsyncMock(return_value=None)  # session doesn't exist yet

        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=db_mock)
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch("personal_agent.service.app.SessionRepository", return_value=repo_mock):
            result = await _resolve_session_profile(
                str(uuid4()), "cloud", uuid4(), trace_id="t1"
            )

    assert result == "cloud", "New session must adopt the caller-supplied profile"


@pytest.mark.asyncio
async def test_existing_session_ignores_request_profile() -> None:
    """Existing session: stored execution_profile is authoritative; request param ignored."""
    from personal_agent.service.app import _resolve_session_profile

    stored_session = _make_session("cloud")

    with patch("personal_agent.service.app.AsyncSessionLocal") as mock_ctx:
        db_mock = AsyncMock()
        repo_mock = MagicMock()
        repo_mock.get = AsyncMock(return_value=stored_session)

        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=db_mock)
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch("personal_agent.service.app.SessionRepository", return_value=repo_mock):
            # Caller sends profile="local" but the stored session is "cloud"
            result = await _resolve_session_profile(
                str(stored_session.session_id), "local", uuid4(), trace_id="t2"
            )

    assert result == "cloud", (
        "Stored execution_profile must win over the request-param profile for existing sessions"
    )


@pytest.mark.asyncio
async def test_new_session_fallback_to_local_when_no_profile() -> None:
    """New session with no profile supplied defaults to 'local'."""
    from personal_agent.service.app import _resolve_session_profile

    with patch("personal_agent.service.app.AsyncSessionLocal") as mock_ctx:
        db_mock = AsyncMock()
        repo_mock = MagicMock()
        repo_mock.get = AsyncMock(return_value=None)

        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=db_mock)
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch("personal_agent.service.app.SessionRepository", return_value=repo_mock):
            result = await _resolve_session_profile(
                str(uuid4()), None, uuid4(), trace_id="t3"
            )

    assert result == "local", "New session with no profile must fall back to 'local'"


# ── 3. Session repository: execution_profile persisted correctly ──────────────


@pytest.mark.asyncio
async def test_session_repo_create_persists_cloud_profile() -> None:
    """repo.create(SessionCreate(execution_profile='cloud')) stores 'cloud'."""
    from personal_agent.service.models import SessionModel
    from personal_agent.service.repositories.session_repository import SessionRepository

    db_mock = AsyncMock()
    db_mock.add = MagicMock()
    db_mock.commit = AsyncMock()

    # Simulate db.refresh by setting attributes on the added object
    captured: list[SessionModel] = []

    def _add(obj: SessionModel) -> None:
        captured.append(obj)

    db_mock.add.side_effect = _add
    db_mock.refresh = AsyncMock(side_effect=lambda obj: None)

    repo = SessionRepository(db_mock)
    user_id = uuid4()

    await repo.create(
        SessionCreate(execution_profile="cloud"),
        user_id=user_id,
    )

    assert len(captured) == 1
    assert captured[0].execution_profile == "cloud", (
        "repo.create must persist the requested execution_profile, not default to 'local'"
    )


@pytest.mark.asyncio
async def test_session_repo_create_defaults_none_to_local() -> None:
    """repo.create(SessionCreate()) with no profile defaults to 'local'."""
    from personal_agent.service.models import SessionModel
    from personal_agent.service.repositories.session_repository import SessionRepository

    db_mock = AsyncMock()
    db_mock.add = MagicMock()
    db_mock.commit = AsyncMock()

    captured: list[SessionModel] = []
    db_mock.add.side_effect = captured.append
    db_mock.refresh = AsyncMock(side_effect=lambda obj: None)

    repo = SessionRepository(db_mock)
    await repo.create(SessionCreate(), user_id=uuid4())

    assert len(captured) == 1
    assert captured[0].execution_profile == "local", (
        "When no profile is supplied, 'local' is the correct fallback"
    )
