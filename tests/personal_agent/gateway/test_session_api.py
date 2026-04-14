"""Tests for the gateway session API endpoints (FRE-206).

Uses FastAPI's TestClient with mocked SessionRepository.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_agent.gateway.app import create_gateway_router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session_model(
    session_id: str | None = None,
    mode: str = "NORMAL",
    channel: str = "CHAT",
    message_count: int = 2,
) -> Any:
    """Build a minimal mock SessionModel."""
    sid = session_id or str(uuid4())
    session = MagicMock()
    session.session_id = sid
    session.created_at = datetime(2026, 1, 1, 10, 0, 0)
    session.last_active_at = datetime(2026, 1, 1, 10, 5, 0)
    session.mode = mode
    session.channel = channel
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

    with patch(
        "personal_agent.service.repositories.session_repository.SessionRepository.list_recent",
        new_callable=AsyncMock,
        return_value=[s1, s2],
    ):
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/sessions?limit=10")

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2
    assert "session_id" in data[0]
    assert "message_count" in data[0]


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
    """GET /sessions/{id} returns 200 with session dict."""
    db_session = AsyncMock()
    sid = str(uuid4())
    session_model = _make_session_model(session_id=sid)

    with patch(
        "personal_agent.service.repositories.session_repository.SessionRepository.get",
        new_callable=AsyncMock,
        return_value=session_model,
    ):
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/sessions/{sid}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == sid
    assert data["mode"] == "NORMAL"


def test_get_session_not_found() -> None:
    """GET /sessions/{id} returns 404 when session does not exist."""
    db_session = AsyncMock()

    with patch(
        "personal_agent.service.repositories.session_repository.SessionRepository.get",
        new_callable=AsyncMock,
        return_value=None,
    ):
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get(f"/api/v1/sessions/{uuid4()}")

    assert resp.status_code == 404


def test_get_session_invalid_uuid() -> None:
    """GET /sessions/{id} returns 422 for non-UUID session_id."""
    db_session = AsyncMock()
    app = _build_app_with_db_factory(db_session)

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/sessions/not-a-valid-uuid")

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/sessions/{session_id}/messages
# ---------------------------------------------------------------------------


def test_get_session_messages_returns_messages() -> None:
    """GET /sessions/{id}/messages returns list of messages."""
    db_session = AsyncMock()
    sid = str(uuid4())
    session_model = _make_session_model(session_id=sid, message_count=3)

    with patch(
        "personal_agent.service.repositories.session_repository.SessionRepository.get",
        new_callable=AsyncMock,
        return_value=session_model,
    ):
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/sessions/{sid}/messages?limit=50")

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 3
    assert data[0]["role"] == "user"


def test_get_session_messages_limit_applied() -> None:
    """GET /sessions/{id}/messages respects the limit query param."""
    db_session = AsyncMock()
    sid = str(uuid4())
    session_model = _make_session_model(session_id=sid, message_count=10)

    with patch(
        "personal_agent.service.repositories.session_repository.SessionRepository.get",
        new_callable=AsyncMock,
        return_value=session_model,
    ):
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/sessions/{sid}/messages?limit=3")

    assert resp.status_code == 200
    assert len(resp.json()) == 3
