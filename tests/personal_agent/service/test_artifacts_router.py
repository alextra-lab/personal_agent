"""Unit tests for /internal/artifacts/{id} resolve endpoint (FRE-227).

These tests build a minimal FastAPI app, override the DB dependency with a
stub session, and patch the email->user_id resolver. No real Postgres,
aiobotocore, or Cloudflare Worker required.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_agent.service import artifacts_router as router_module
from personal_agent.service.artifacts_router import router
from personal_agent.service.database import get_db_session

_TOKEN = "test-token-deadbeef"


class _StubSession:
    """Async-session stub returning one canned ``execute`` result."""

    def __init__(self, *, found: SimpleNamespace | None) -> None:
        self._found = found
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        self.queries.append((str(statement), dict(params or {})))
        return SimpleNamespace(one_or_none=lambda: self._found)


def _build_app(session: _StubSession, *, email_resolves_to: UUID | None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    async def _override_db() -> Any:
        yield session

    app.dependency_overrides[get_db_session] = _override_db
    return app


@pytest.fixture(autouse=True)
def _patch_token_and_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        router_module.settings,
        "artifact_resolve_internal_token",
        _TOKEN,
        raising=False,
    )


def _row(art_id: UUID, user_id: UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=art_id,
        user_id=user_id,
        r2_key=f"note/{user_id}/GLOBAL/{art_id}.md",
        content_type="text/markdown; charset=utf-8",
        size_bytes=42,
        created_at=datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    art_id = uuid4()
    user_id = uuid4()
    session = _StubSession(found=_row(art_id, user_id))
    app = _build_app(session, email_resolves_to=user_id)

    monkeypatch.setattr(
        router_module,
        "get_or_create_user_by_email",
        AsyncMock(return_value=user_id),
    )

    with TestClient(app) as client:
        resp = client.get(
            f"/internal/artifacts/{art_id}",
            headers={
                "x-internal-token": _TOKEN,
                "x-authenticated-user-email": "alex@example.com",
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["artifact_id"] == str(art_id)
    assert body["r2_key"].startswith(f"note/{user_id}/")
    assert body["content_type"] == "text/markdown; charset=utf-8"
    assert body["size_bytes"] == 42


# ---------------------------------------------------------------------------
# 401 — bad / missing token
# ---------------------------------------------------------------------------


def test_missing_token_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _StubSession(found=None)
    app = _build_app(session, email_resolves_to=uuid4())

    with TestClient(app) as client:
        resp = client.get(
            f"/internal/artifacts/{uuid4()}",
            headers={"x-authenticated-user-email": "x@example.com"},
        )

    assert resp.status_code == 401


def test_wrong_token_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _StubSession(found=None)
    app = _build_app(session, email_resolves_to=uuid4())

    with TestClient(app) as client:
        resp = client.get(
            f"/internal/artifacts/{uuid4()}",
            headers={
                "x-internal-token": "nope",
                "x-authenticated-user-email": "x@example.com",
            },
        )

    assert resp.status_code == 401


def test_unset_token_on_server_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the gateway hasn't been configured with a token, every call is 401."""
    monkeypatch.setattr(
        router_module.settings,
        "artifact_resolve_internal_token",
        None,
        raising=False,
    )
    session = _StubSession(found=None)
    app = _build_app(session, email_resolves_to=uuid4())

    with TestClient(app) as client:
        resp = client.get(
            f"/internal/artifacts/{uuid4()}",
            headers={
                "x-internal-token": _TOKEN,
                "x-authenticated-user-email": "x@example.com",
            },
        )

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 404 — auth-shape mismatch / unknown id / cross-user
# ---------------------------------------------------------------------------


def test_missing_email_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _StubSession(found=None)
    app = _build_app(session, email_resolves_to=uuid4())

    with TestClient(app) as client:
        resp = client.get(
            f"/internal/artifacts/{uuid4()}",
            headers={"x-internal-token": _TOKEN},
        )

    assert resp.status_code == 404


def test_unknown_artifact_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """``scalar_one_or_none()`` returning None must surface as 404."""
    session = _StubSession(found=None)
    app = _build_app(session, email_resolves_to=uuid4())

    monkeypatch.setattr(
        router_module,
        "get_or_create_user_by_email",
        AsyncMock(return_value=uuid4()),
    )

    with TestClient(app) as client:
        resp = client.get(
            f"/internal/artifacts/{uuid4()}",
            headers={
                "x-internal-token": _TOKEN,
                "x-authenticated-user-email": "x@example.com",
            },
        )

    assert resp.status_code == 404


def test_cross_user_yields_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """Querying for an artifact owned by another user must 404, never 200.

    The WHERE clause filters by user_id so the stub returns None — this
    test asserts that the endpoint does not leak a 403 vs 404 split.
    """
    session = _StubSession(found=None)  # the WHERE filter would yield no rows
    app = _build_app(session, email_resolves_to=uuid4())

    monkeypatch.setattr(
        router_module,
        "get_or_create_user_by_email",
        AsyncMock(return_value=uuid4()),
    )

    with TestClient(app) as client:
        resp = client.get(
            f"/internal/artifacts/{uuid4()}",
            headers={
                "x-internal-token": _TOKEN,
                "x-authenticated-user-email": "intruder@example.com",
            },
        )

    assert resp.status_code == 404
