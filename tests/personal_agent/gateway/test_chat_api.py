"""Tests for the gateway chat API endpoint (FRE-235).

Uses FastAPI's TestClient with mocked SessionRepository and Anthropic client.
Background streaming tasks are patched out for unit tests — only the
synchronous contract of the endpoint is verified here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_agent.gateway.chat_api import router as chat_router

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session_model(session_id: str | None = None) -> Any:
    """Build a minimal mock SessionModel for a cloud chat session."""
    sid = session_id or str(uuid4())
    session = MagicMock()
    session.session_id = sid
    session.created_at = datetime(2026, 1, 1, 10, 0, 0)
    session.last_active_at = datetime(2026, 1, 1, 10, 5, 0)
    session.mode = "NORMAL"
    session.channel = "CHAT"
    session.messages = []
    return session


def _build_app() -> FastAPI:
    """Build a minimal test FastAPI app with the chat router mounted."""
    app = FastAPI()
    app.include_router(chat_router)
    return app


# ---------------------------------------------------------------------------
# POST /chat — response shape
# ---------------------------------------------------------------------------


def test_chat_starts_streaming() -> None:
    """POST /chat returns session_id, trace_id, and status=streaming immediately."""
    sid = str(uuid4())
    mock_session = _make_session_model(session_id=sid)

    with (
        patch(
            "personal_agent.gateway.chat_api.get_settings",
            return_value=MagicMock(anthropic_api_key="sk-test"),
        ),
        patch(
            "personal_agent.gateway.chat_api.AsyncSessionLocal",
        ) as mock_session_local,
        patch(
            "personal_agent.service.repositories.session_repository.SessionRepository.get",
            new_callable=AsyncMock,
            return_value=mock_session,
        ),
        patch("asyncio.create_task") as mock_create_task,
    ):
        # Make AsyncSessionLocal a context manager that yields an AsyncMock db session
        mock_db = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session_local.return_value = mock_ctx

        app = _build_app()
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.post(
                "/chat",
                data={"message": "Hello", "session_id": sid},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == sid
    assert data["status"] == "streaming"
    # trace_id must be a non-empty string that looks like a UUID (no dashes stripped)
    assert "trace_id" in data
    assert len(data["trace_id"]) == 36  # UUID canonical form
    assert data["trace_id"].count("-") == 4
    mock_create_task.assert_called_once()


def test_chat_invalid_uuid() -> None:
    """POST /chat with a non-UUID session_id returns 422."""
    with (
        patch(
            "personal_agent.gateway.chat_api.get_settings",
            return_value=MagicMock(anthropic_api_key="sk-test"),
        ),
    ):
        app = _build_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/chat",
                data={"message": "Hello", "session_id": "not-a-uuid"},
            )

    assert resp.status_code == 422


def test_chat_missing_api_key() -> None:
    """POST /chat returns 503 when no Anthropic API key is configured."""
    sid = str(uuid4())

    with (
        patch(
            "personal_agent.gateway.chat_api.get_settings",
            return_value=MagicMock(anthropic_api_key=None),
        ),
    ):
        app = _build_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/chat",
                data={"message": "Hello", "session_id": sid},
            )

    assert resp.status_code == 503
    assert "Anthropic API key" in resp.json()["detail"]
