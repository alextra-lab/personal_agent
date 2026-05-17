"""Unit tests for /internal/artifacts/{id} resolve endpoint (FRE-227).

These tests build a minimal FastAPI app, override the DB dependency with
a stub session, and patch the CF Access JWT verifier. No real Postgres,
aiobotocore, JWKS endpoint, or Cloudflare Worker is involved.
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
from personal_agent.service.cf_access_jwt import (
    CFAccessClaims,
    CFAccessVerifierError,
)
from personal_agent.service.database import get_db_session

_TOKEN = "test-token-deadbeef"
_JWT = "header.body.signature"  # opaque — the verifier is mocked


class _StubSession:
    """Async-session stub returning one canned ``execute`` result."""

    def __init__(self, *, found: SimpleNamespace | None) -> None:
        self._found = found
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        self.queries.append((str(statement), dict(params or {})))
        return SimpleNamespace(one_or_none=lambda: self._found)


def _build_app(session: _StubSession) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    async def _override_db() -> Any:
        yield session

    app.dependency_overrides[get_db_session] = _override_db
    return app


def _stub_verifier(claims: CFAccessClaims) -> Any:
    """Build an object with an async ``verify`` returning the given claims."""
    v = SimpleNamespace()
    v.verify = AsyncMock(return_value=claims)
    return v


def _stub_verifier_rejecting() -> Any:
    """Build a verifier whose ``verify`` raises ``CFAccessVerifierError``."""
    v = SimpleNamespace()
    v.verify = AsyncMock(side_effect=CFAccessVerifierError("bad jwt"))
    return v


@pytest.fixture(autouse=True)
def _patch_token(monkeypatch: pytest.MonkeyPatch) -> None:
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
    app = _build_app(session)

    claims = CFAccessClaims(email="alex@example.com", sub="u", aud="a", iss="i")
    monkeypatch.setattr(router_module, "get_verifier", lambda: _stub_verifier(claims))
    monkeypatch.setattr(
        router_module, "get_or_create_user_by_email", AsyncMock(return_value=user_id)
    )

    with TestClient(app) as client:
        resp = client.get(
            f"/internal/artifacts/{art_id}",
            headers={
                "x-internal-token": _TOKEN,
                "x-cf-access-jwt-assertion": _JWT,
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["artifact_id"] == str(art_id)
    assert body["r2_key"].startswith(f"note/{user_id}/")


# ---------------------------------------------------------------------------
# 401 — internal token
# ---------------------------------------------------------------------------


def test_missing_internal_token_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_app(_StubSession(found=None))
    monkeypatch.setattr(router_module, "get_verifier", lambda: _stub_verifier(
        CFAccessClaims(email="x@y.z", sub="s", aud="a", iss="i")
    ))

    with TestClient(app) as client:
        resp = client.get(
            f"/internal/artifacts/{uuid4()}",
            headers={"x-cf-access-jwt-assertion": _JWT},
        )

    assert resp.status_code == 401


def test_wrong_internal_token_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_app(_StubSession(found=None))
    monkeypatch.setattr(router_module, "get_verifier", lambda: _stub_verifier(
        CFAccessClaims(email="x@y.z", sub="s", aud="a", iss="i")
    ))

    with TestClient(app) as client:
        resp = client.get(
            f"/internal/artifacts/{uuid4()}",
            headers={
                "x-internal-token": "nope",
                "x-cf-access-jwt-assertion": _JWT,
            },
        )

    assert resp.status_code == 401


def test_unset_token_on_server_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        router_module.settings, "artifact_resolve_internal_token", None, raising=False
    )
    app = _build_app(_StubSession(found=None))

    with TestClient(app) as client:
        resp = client.get(
            f"/internal/artifacts/{uuid4()}",
            headers={
                "x-internal-token": _TOKEN,
                "x-cf-access-jwt-assertion": _JWT,
            },
        )

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 503 — verifier misconfigured
# ---------------------------------------------------------------------------


def test_missing_verifier_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """If cf_access_team_domain / cf_access_aud aren't set, fail closed."""
    app = _build_app(_StubSession(found=None))
    monkeypatch.setattr(router_module, "get_verifier", lambda: None)

    with TestClient(app) as client:
        resp = client.get(
            f"/internal/artifacts/{uuid4()}",
            headers={
                "x-internal-token": _TOKEN,
                "x-cf-access-jwt-assertion": _JWT,
            },
        )

    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# 401 — JWT verification
# ---------------------------------------------------------------------------


def test_missing_jwt_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """No JWT in the request → 401. Token alone is not sufficient."""
    app = _build_app(_StubSession(found=None))
    monkeypatch.setattr(router_module, "get_verifier", lambda: _stub_verifier(
        CFAccessClaims(email="x@y.z", sub="s", aud="a", iss="i")
    ))

    with TestClient(app) as client:
        resp = client.get(
            f"/internal/artifacts/{uuid4()}",
            headers={"x-internal-token": _TOKEN},
        )

    assert resp.status_code == 401


def test_invalid_jwt_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """JWT verifier rejects → 401, never reaches the DB."""
    session = _StubSession(found=None)
    app = _build_app(session)
    monkeypatch.setattr(router_module, "get_verifier", lambda: _stub_verifier_rejecting())

    with TestClient(app) as client:
        resp = client.get(
            f"/internal/artifacts/{uuid4()}",
            headers={
                "x-internal-token": _TOKEN,
                "x-cf-access-jwt-assertion": _JWT,
            },
        )

    assert resp.status_code == 401
    # DB must not be touched when JWT verification fails.
    assert session.queries == []


def test_email_header_alone_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forwarded email without JWT must NOT authenticate.

    Regression guard against the pre-2026-05-17 behavior where
    ``X-Authenticated-User-Email`` alone was trusted.
    """
    session = _StubSession(found=None)
    app = _build_app(session)
    monkeypatch.setattr(router_module, "get_verifier", lambda: _stub_verifier(
        CFAccessClaims(email="attacker@example.com", sub="s", aud="a", iss="i")
    ))
    # The forwarded email header is now meaningless without a JWT.

    with TestClient(app) as client:
        resp = client.get(
            f"/internal/artifacts/{uuid4()}",
            headers={
                "x-internal-token": _TOKEN,
                "x-authenticated-user-email": "attacker@example.com",
            },
        )

    assert resp.status_code == 401
    assert session.queries == []  # no user row created, no artifact lookup


# ---------------------------------------------------------------------------
# 404 — unknown / cross-user
# ---------------------------------------------------------------------------


def test_unknown_artifact_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_app(_StubSession(found=None))
    claims = CFAccessClaims(email="alex@example.com", sub="s", aud="a", iss="i")
    monkeypatch.setattr(router_module, "get_verifier", lambda: _stub_verifier(claims))
    monkeypatch.setattr(
        router_module, "get_or_create_user_by_email", AsyncMock(return_value=uuid4())
    )

    with TestClient(app) as client:
        resp = client.get(
            f"/internal/artifacts/{uuid4()}",
            headers={
                "x-internal-token": _TOKEN,
                "x-cf-access-jwt-assertion": _JWT,
            },
        )

    assert resp.status_code == 404


def test_cross_user_yields_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cross-user access masked as 404 (no 403, no metadata leak)."""
    app = _build_app(_StubSession(found=None))
    claims = CFAccessClaims(email="intruder@example.com", sub="s", aud="a", iss="i")
    monkeypatch.setattr(router_module, "get_verifier", lambda: _stub_verifier(claims))
    monkeypatch.setattr(
        router_module, "get_or_create_user_by_email", AsyncMock(return_value=uuid4())
    )

    with TestClient(app) as client:
        resp = client.get(
            f"/internal/artifacts/{uuid4()}",
            headers={
                "x-internal-token": _TOKEN,
                "x-cf-access-jwt-assertion": _JWT,
            },
        )

    assert resp.status_code == 404


def test_accepts_lowercase_jwt_header_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """``cf-access-jwt-assertion`` (no x- prefix) also works.

    Cloudflare sends the header as ``Cf-Access-Jwt-Assertion``; whether
    the Worker forwards it with ``X-`` prefixed or bare, the gateway
    accepts both.
    """
    art_id = uuid4()
    user_id = uuid4()
    session = _StubSession(found=_row(art_id, user_id))
    app = _build_app(session)
    claims = CFAccessClaims(email="alex@example.com", sub="s", aud="a", iss="i")
    monkeypatch.setattr(router_module, "get_verifier", lambda: _stub_verifier(claims))
    monkeypatch.setattr(
        router_module, "get_or_create_user_by_email", AsyncMock(return_value=user_id)
    )

    with TestClient(app) as client:
        resp = client.get(
            f"/internal/artifacts/{art_id}",
            headers={
                "x-internal-token": _TOKEN,
                "cf-access-jwt-assertion": _JWT,
            },
        )

    assert resp.status_code == 200
