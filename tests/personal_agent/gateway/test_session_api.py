"""Tests for the gateway session API endpoints (FRE-206 + cross-user leak hotfix).

Uses FastAPI's TestClient with mocked SessionRepository. Each test attaches
the ``Cf-Access-Authenticated-User-Email`` header and patches
``_get_user_with_display_name`` so the endpoint's user-scoping helper
returns a stable mock user_id — closes the data leak in
``gateway/session_api.py`` where session data was unscoped.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_agent.gateway.app import create_gateway_router

_TEST_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
_AUTH_HEADERS = {"Cf-Access-Authenticated-User-Email": "tester@example.com"}


@contextmanager
def _patched_user_resolver(user_id: UUID = _TEST_USER_ID) -> Any:
    """Patch the CF Access → user_id resolver to return a stable UUID."""
    with patch(
        "personal_agent.gateway.session_api._get_user_with_display_name",
        new_callable=AsyncMock,
        return_value=(user_id, None),
    ) as mock_resolver:
        yield mock_resolver


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session_model(
    session_id: str | None = None,
    mode: str = "NORMAL",
    channel: str = "CHAT",
    message_count: int = 2,
    messages: list[dict] | None = None,
    execution_profile: str = "local",
) -> Any:
    """Build a minimal mock SessionModel.

    Args:
        session_id: Optional UUID string; generated if not provided.
        mode: Session mode string.
        channel: Session channel string.
        message_count: Number of synthetic messages to generate when
            ``messages`` is not provided.
        messages: Explicit list of message dicts; overrides ``message_count``.
        execution_profile: Server-owned execution profile (ADR-0079).
    """
    sid = session_id or str(uuid4())
    session = MagicMock()
    session.session_id = sid
    session.created_at = datetime(2026, 1, 1, 10, 0, 0)
    session.last_active_at = datetime(2026, 1, 1, 10, 5, 0)
    session.mode = mode
    session.channel = channel
    session.execution_profile = execution_profile
    if messages is not None:
        session.messages = messages
    else:
        session.messages = [
            {"role": "user", "content": f"message {i}"} for i in range(message_count)
        ]
    return session


def _build_app_with_db_factory(mock_session: Any) -> FastAPI:
    """Build test app with a mock DB factory yielding mock_session."""
    app = FastAPI()
    app.include_router(create_gateway_router())
    app.state.knowledge_graph = None
    app.state.es_client = None

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _factory():
        yield mock_session

    app.state.db_session_factory = _factory
    return app


# ---------------------------------------------------------------------------
# GET /api/v1/sessions
# ---------------------------------------------------------------------------


def test_list_sessions_returns_list() -> None:
    """GET /sessions returns a list of session summaries."""
    db_session = AsyncMock()
    s1 = _make_session_model()
    s2 = _make_session_model()

    list_recent_mock = AsyncMock(return_value=[s1, s2])
    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(
            patch(
                "personal_agent.service.repositories.session_repository.SessionRepository.list_recent",
                list_recent_mock,
            )
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/sessions?limit=10", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2
    assert "session_id" in data[0]
    assert "message_count" in data[0]
    # Hotfix invariant: user_id MUST be threaded to the repository.
    list_recent_mock.assert_awaited_once()
    assert list_recent_mock.await_args.kwargs.get("user_id") == _TEST_USER_ID


def test_list_sessions_401_without_cf_access_header() -> None:
    """GET /sessions returns 401 when the CF Access header is absent."""
    db_session = AsyncMock()
    app = _build_app_with_db_factory(db_session)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/sessions")
    assert resp.status_code == 401


def test_list_sessions_503_when_no_factory() -> None:
    """GET /sessions returns 503 when db_session_factory is not attached."""
    app = FastAPI()
    app.include_router(create_gateway_router())
    app.state.knowledge_graph = None
    app.state.es_client = None
    app.state.db_session_factory = None

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/sessions")

    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# GET /api/v1/sessions/{session_id}
# ---------------------------------------------------------------------------


def test_get_session_found() -> None:
    """GET /sessions/{id} returns 200 with session dict, scoped by user_id."""
    db_session = AsyncMock()
    sid = str(uuid4())
    session_model = _make_session_model(session_id=sid)

    get_mock = AsyncMock(return_value=session_model)
    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(
            patch(
                "personal_agent.service.repositories.session_repository.SessionRepository.get",
                get_mock,
            )
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/sessions/{sid}", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == sid
    assert data["mode"] == "NORMAL"
    get_mock.assert_awaited_once()
    assert get_mock.await_args.kwargs.get("user_id") == _TEST_USER_ID


def test_get_session_404_when_other_user_owns_it() -> None:
    """GET /sessions/{id} returns 404 (not 403) when another user owns the session.

    repo.get(uuid, user_id=X) returns None on ownership mismatch — endpoint
    must not confirm existence of other users' sessions.
    """
    db_session = AsyncMock()
    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(
            patch(
                "personal_agent.service.repositories.session_repository.SessionRepository.get",
                new_callable=AsyncMock,
                return_value=None,
            )
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get(f"/api/v1/sessions/{uuid4()}", headers=_AUTH_HEADERS)
    assert resp.status_code == 404


def test_get_session_not_found() -> None:
    """GET /sessions/{id} returns 404 when session does not exist."""
    db_session = AsyncMock()

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(
            patch(
                "personal_agent.service.repositories.session_repository.SessionRepository.get",
                new_callable=AsyncMock,
                return_value=None,
            )
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get(f"/api/v1/sessions/{uuid4()}", headers=_AUTH_HEADERS)

    assert resp.status_code == 404


def test_get_session_invalid_uuid() -> None:
    """GET /sessions/{id} returns 422 for non-UUID session_id."""
    db_session = AsyncMock()
    app = _build_app_with_db_factory(db_session)

    with _patched_user_resolver():
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/sessions/not-a-valid-uuid", headers=_AUTH_HEADERS)

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/sessions/{session_id}/messages
# ---------------------------------------------------------------------------


def test_get_session_messages_returns_messages() -> None:
    """GET /sessions/{id}/messages returns list of messages, scoped by user_id."""
    db_session = AsyncMock()
    sid = str(uuid4())
    session_model = _make_session_model(session_id=sid, message_count=3)

    get_mock = AsyncMock(return_value=session_model)
    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(
            patch(
                "personal_agent.service.repositories.session_repository.SessionRepository.get",
                get_mock,
            )
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/sessions/{sid}/messages?limit=50", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 3
    assert data[0]["role"] == "user"
    get_mock.assert_awaited_once()
    assert get_mock.await_args.kwargs.get("user_id") == _TEST_USER_ID


def test_get_session_messages_limit_applied() -> None:
    """GET /sessions/{id}/messages respects the limit query param."""
    db_session = AsyncMock()
    sid = str(uuid4())
    session_model = _make_session_model(session_id=sid, message_count=10)

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(
            patch(
                "personal_agent.service.repositories.session_repository.SessionRepository.get",
                new_callable=AsyncMock,
                return_value=session_model,
            )
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/sessions/{sid}/messages?limit=3", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    assert len(resp.json()) == 3


def test_get_session_messages_401_without_cf_access_header() -> None:
    """GET /sessions/{id}/messages returns 401 when CF Access header missing."""
    db_session = AsyncMock()
    app = _build_app_with_db_factory(db_session)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(f"/api/v1/sessions/{uuid4()}/messages")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Session title derivation
# ---------------------------------------------------------------------------


def test_list_sessions_includes_title() -> None:
    """GET /sessions includes a ``title`` derived from the first user message."""
    db_session = AsyncMock()
    session_model = _make_session_model(
        messages=[{"role": "user", "content": "Hello world this is a test message"}]
    )

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(
            patch(
                "personal_agent.service.repositories.session_repository.SessionRepository.list_recent",
                new_callable=AsyncMock,
                return_value=[session_model],
            )
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/sessions", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["title"] == "Hello world this is a test message"


def test_list_sessions_truncates_title() -> None:
    """GET /sessions truncates titles longer than 60 chars with an ellipsis."""
    db_session = AsyncMock()
    long_content = "A" * 80  # 80 characters
    session_model = _make_session_model(messages=[{"role": "user", "content": long_content}])

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(
            patch(
                "personal_agent.service.repositories.session_repository.SessionRepository.list_recent",
                new_callable=AsyncMock,
                return_value=[session_model],
            )
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/sessions", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    # 60 chars + the single '…' character = 61 characters total
    assert len(data[0]["title"]) == 61
    assert data[0]["title"].endswith("…")


# ---------------------------------------------------------------------------
# Execution profile — GET field + PATCH (ADR-0079 / FRE-416)
# ---------------------------------------------------------------------------

_SESSION_GET = "personal_agent.service.repositories.session_repository.SessionRepository.get"
_SESSION_UPDATE = "personal_agent.service.repositories.session_repository.SessionRepository.update"
_EMIT_PROFILE = "personal_agent.transport.agui.transport.emit_session_profile"


def test_get_session_includes_execution_profile() -> None:
    """GET /sessions/{id} surfaces the server-owned execution profile."""
    db_session = AsyncMock()
    sid = str(uuid4())
    session_model = _make_session_model(session_id=sid, execution_profile="cloud")

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(patch(_SESSION_GET, new_callable=AsyncMock, return_value=session_model))
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/sessions/{sid}", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    assert resp.json()["execution_profile"] == "cloud"


def test_patch_session_profile_updates_and_emits() -> None:
    """PATCH /sessions/{id} persists the profile (scoped) and emits a STATE_DELTA."""
    db_session = AsyncMock()
    sid = str(uuid4())
    updated = _make_session_model(session_id=sid, execution_profile="cloud")
    update_mock = AsyncMock(return_value=updated)

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(patch(_SESSION_UPDATE, update_mock))
        emit_mock = stack.enter_context(patch(_EMIT_PROFILE, new_callable=AsyncMock))
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.patch(
                f"/api/v1/sessions/{sid}", json={"profile": "cloud"}, headers=_AUTH_HEADERS
            )

    assert resp.status_code == 200
    assert resp.json()["execution_profile"] == "cloud"
    update_mock.assert_awaited_once()
    # Ownership invariant: write MUST be scoped to the resolved user_id.
    assert update_mock.await_args.kwargs.get("user_id") == _TEST_USER_ID
    emit_mock.assert_awaited_once()


def test_patch_session_profile_invalid_name_422() -> None:
    """PATCH /sessions/{id} rejects unknown profile names with 422."""
    db_session = AsyncMock()
    sid = str(uuid4())
    app = _build_app_with_db_factory(db_session)

    with _patched_user_resolver():
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.patch(
                f"/api/v1/sessions/{sid}", json={"profile": "bogus"}, headers=_AUTH_HEADERS
            )

    assert resp.status_code == 422


def test_patch_session_profile_404_when_other_user_owns_it() -> None:
    """PATCH /sessions/{id} returns 404 when the scoped update touches no row."""
    db_session = AsyncMock()
    sid = str(uuid4())

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(patch(_SESSION_UPDATE, new_callable=AsyncMock, return_value=None))
        stack.enter_context(patch(_EMIT_PROFILE, new_callable=AsyncMock))
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.patch(
                f"/api/v1/sessions/{sid}", json={"profile": "cloud"}, headers=_AUTH_HEADERS
            )

    assert resp.status_code == 404


def test_patch_session_profile_invalid_uuid_422() -> None:
    """PATCH /sessions/{id} returns 422 for a non-UUID session_id."""
    db_session = AsyncMock()
    app = _build_app_with_db_factory(db_session)

    with _patched_user_resolver():
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.patch(
                "/api/v1/sessions/not-a-uuid", json={"profile": "cloud"}, headers=_AUTH_HEADERS
            )

    assert resp.status_code == 422
