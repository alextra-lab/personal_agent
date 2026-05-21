"""Unit tests for the card-click telemetry endpoint (FRE-368, ADR-0070 D8).

The endpoint POSTs card-click events to the gateway which emits them via
structlog to Elasticsearch. These tests verify auth enforcement and body
validation — no real ES or Postgres required.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_agent.service import telemetry_router as telemetry_module
from personal_agent.service.cf_access_jwt import CFAccessClaims, CFAccessVerifierError
from personal_agent.service.database import get_db_session
from personal_agent.service.telemetry_router import router

_JWT = "header.body.signature"


def _stub_verifier(claims: CFAccessClaims) -> Any:
    v = type("V", (), {})()
    v.verify = AsyncMock(return_value=claims)
    return v


def _stub_verifier_rejecting() -> Any:
    v = type("V", (), {})()
    v.verify = AsyncMock(side_effect=CFAccessVerifierError("bad jwt"))
    return v


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


# ---------------------------------------------------------------------------
# 401 — missing / invalid JWT
# ---------------------------------------------------------------------------


def test_card_click_no_jwt_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST without a CF Access JWT → 401."""
    app = _build_app()
    monkeypatch.setattr(telemetry_module, "get_verifier", lambda: _stub_verifier(
        CFAccessClaims(email="x@y.z", sub="s", aud="a", iss="i")
    ))

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/telemetry/card_click",
            json={"artifact_id": str(uuid4()), "kind": "card_click", "surface": "inline"},
        )

    assert resp.status_code == 401


def test_card_click_invalid_jwt_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """JWT verification failure → 401."""
    app = _build_app()
    monkeypatch.setattr(telemetry_module, "get_verifier", lambda: _stub_verifier_rejecting())

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/telemetry/card_click",
            json={"artifact_id": str(uuid4()), "kind": "card_click", "surface": "inline"},
            headers={"cf-access-jwt-assertion": _JWT},
        )

    assert resp.status_code == 401


def test_card_click_verifier_unconfigured_is_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifier not configured → 503 (fail-closed)."""
    app = _build_app()
    monkeypatch.setattr(telemetry_module, "get_verifier", lambda: None)

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/telemetry/card_click",
            json={"artifact_id": str(uuid4()), "kind": "card_click", "surface": "inline"},
            headers={"cf-access-jwt-assertion": _JWT},
        )

    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# 422 — malformed body
# ---------------------------------------------------------------------------


def test_card_click_bad_uuid_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-UUID artifact_id → 422 validation error."""
    app = _build_app()
    claims = CFAccessClaims(email="alex@x.com", sub="u", aud="a", iss="i")
    monkeypatch.setattr(telemetry_module, "get_verifier", lambda: _stub_verifier(claims))
    monkeypatch.setattr(
        telemetry_module, "get_or_create_user_by_email", AsyncMock(return_value=uuid4())
    )

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/telemetry/card_click",
            json={"artifact_id": "not-a-uuid", "kind": "card_click", "surface": "inline"},
            headers={"cf-access-jwt-assertion": _JWT},
        )

    assert resp.status_code == 422


def test_card_click_bad_surface_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    """surface must be 'inline' | 'drawer' | 'standalone'."""
    app = _build_app()
    claims = CFAccessClaims(email="alex@x.com", sub="u", aud="a", iss="i")
    monkeypatch.setattr(telemetry_module, "get_verifier", lambda: _stub_verifier(claims))
    monkeypatch.setattr(
        telemetry_module, "get_or_create_user_by_email", AsyncMock(return_value=uuid4())
    )

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/telemetry/card_click",
            json={"artifact_id": str(uuid4()), "kind": "card_click", "surface": "modal"},
            headers={"cf-access-jwt-assertion": _JWT},
        )

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 204 — happy path
# ---------------------------------------------------------------------------


def test_card_click_happy_path_returns_204(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid JWT + valid body → 204 No Content."""
    user_id = uuid4()
    app = _build_app()
    claims = CFAccessClaims(email="alex@x.com", sub="u", aud="a", iss="i")
    monkeypatch.setattr(telemetry_module, "get_verifier", lambda: _stub_verifier(claims))
    monkeypatch.setattr(
        telemetry_module, "get_or_create_user_by_email", AsyncMock(return_value=user_id)
    )

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/telemetry/card_click",
            json={
                "artifact_id": str(uuid4()),
                "session_id": str(uuid4()),
                "kind": "card_click",
                "surface": "drawer",
            },
            headers={"cf-access-jwt-assertion": _JWT},
        )

    assert resp.status_code == 204


def test_card_click_emits_structlog_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verified request emits 'artifact_card_click' structlog event."""
    import structlog.testing

    user_id = uuid4()
    art_id = uuid4()
    app = _build_app()
    claims = CFAccessClaims(email="alex@x.com", sub="u", aud="a", iss="i")
    monkeypatch.setattr(telemetry_module, "get_verifier", lambda: _stub_verifier(claims))
    monkeypatch.setattr(
        telemetry_module, "get_or_create_user_by_email", AsyncMock(return_value=user_id)
    )

    with structlog.testing.capture_logs() as logs:
        with TestClient(app) as client:
            client.post(
                "/api/v1/telemetry/card_click",
                json={
                    "artifact_id": str(art_id),
                    "kind": "card_click",
                    "surface": "inline",
                },
                headers={"cf-access-jwt-assertion": _JWT},
            )

    event_names = [log.get("event") for log in logs]
    assert "artifact_card_click" in event_names
    click_log = next(l for l in logs if l.get("event") == "artifact_card_click")
    assert click_log["artifact_id"] == str(art_id)
    assert click_log["surface"] == "inline"


def test_card_click_without_session_id_is_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """session_id is optional — omitting it is not a validation error."""
    user_id = uuid4()
    app = _build_app()
    claims = CFAccessClaims(email="alex@x.com", sub="u", aud="a", iss="i")
    monkeypatch.setattr(telemetry_module, "get_verifier", lambda: _stub_verifier(claims))
    monkeypatch.setattr(
        telemetry_module, "get_or_create_user_by_email", AsyncMock(return_value=user_id)
    )

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/telemetry/card_click",
            json={
                "artifact_id": str(uuid4()),
                "kind": "card_click",
                "surface": "standalone",
            },
            headers={"cf-access-jwt-assertion": _JWT},
        )

    assert resp.status_code == 204
