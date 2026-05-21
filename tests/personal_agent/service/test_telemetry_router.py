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
from personal_agent.service.auth import RequestUser, get_request_user
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


def _build_app(user_id: UUID | None = None) -> FastAPI:
    """Build a test app. Overrides get_request_user when user_id is given."""
    app = FastAPI()
    app.include_router(router)
    if user_id is not None:
        async def _override_user() -> RequestUser:
            return RequestUser(user_id=user_id, email="test@example.com")
        app.dependency_overrides[get_request_user] = _override_user
    return app


# ---------------------------------------------------------------------------
# 401 — missing / invalid JWT
# ---------------------------------------------------------------------------


def test_card_click_no_auth_is_401() -> None:
    """POST without a resolved user → 401.

    get_request_user not overridden — reads no CF email header → 401.
    """
    app = _build_app()  # no user_id override → get_request_user runs real logic

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/v1/telemetry/card_click",
            json={"artifact_id": str(uuid4()), "kind": "card_click", "surface": "inline"},
        )

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 422 — malformed body
# ---------------------------------------------------------------------------


def test_card_click_bad_uuid_is_422() -> None:
    """Non-UUID artifact_id → 422 validation error."""
    app = _build_app(user_id=uuid4())

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/telemetry/card_click",
            json={"artifact_id": "not-a-uuid", "kind": "card_click", "surface": "inline"},
        )

    assert resp.status_code == 422


def test_card_click_bad_surface_is_422() -> None:
    """surface must be 'inline' | 'drawer' | 'standalone'."""
    app = _build_app(user_id=uuid4())

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/telemetry/card_click",
            json={"artifact_id": str(uuid4()), "kind": "card_click", "surface": "modal"},
        )

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 204 — happy path
# ---------------------------------------------------------------------------


def test_card_click_happy_path_returns_204() -> None:
    """Resolved user + valid body → 204 No Content."""
    app = _build_app(user_id=uuid4())

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/telemetry/card_click",
            json={
                "artifact_id": str(uuid4()),
                "session_id": str(uuid4()),
                "kind": "card_click",
                "surface": "drawer",
            },
        )

    assert resp.status_code == 204


def test_card_click_emits_structlog_event() -> None:
    """Resolved request emits 'artifact_card_click' structlog event."""
    import structlog.testing

    art_id = uuid4()
    app = _build_app(user_id=uuid4())

    with structlog.testing.capture_logs() as logs:
        with TestClient(app) as client:
            client.post(
                "/api/v1/telemetry/card_click",
                json={
                    "artifact_id": str(art_id),
                    "kind": "card_click",
                    "surface": "inline",
                },
            )

    event_names = [log.get("event") for log in logs]
    assert "artifact_card_click" in event_names
    click_log = next(l for l in logs if l.get("event") == "artifact_card_click")
    assert click_log["artifact_id"] == str(art_id)
    assert click_log["surface"] == "inline"


def test_card_click_without_session_id_is_valid() -> None:
    """session_id is optional — omitting it is not a validation error."""
    app = _build_app(user_id=uuid4())

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/telemetry/card_click",
            json={
                "artifact_id": str(uuid4()),
                "kind": "card_click",
                "surface": "standalone",
            },
        )

    assert resp.status_code == 204
