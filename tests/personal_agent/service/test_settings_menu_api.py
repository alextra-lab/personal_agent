"""Route tests for GET /api/v1/settings (FRE-1702)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from personal_agent.service.app import app
from personal_agent.service.auth import RequestUser, get_request_user
from personal_agent.service.database import get_db_session
from personal_agent.service.settings_menu import (
    MenuInputs,
    SessionNotFoundError,
    build_menu,
)

_URL = "/api/v1/settings"
_COLLECT = "personal_agent.service.settings_menu.collect_inputs"


def _client() -> TestClient:
    async def _override_user() -> RequestUser:
        return RequestUser(user_id=uuid4(), email="test@example.com")

    async def _override_db() -> object:
        return object()

    app.dependency_overrides[get_request_user] = _override_user
    app.dependency_overrides[get_db_session] = _override_db
    return TestClient(app)


def _teardown() -> None:
    app.dependency_overrides.pop(get_request_user, None)
    app.dependency_overrides.pop(get_db_session, None)


def _live_inputs() -> MenuInputs:
    from personal_agent.config import load_governance_config, load_model_config, settings
    from personal_agent.config.seshat_effort import load_seshat_effort
    from personal_agent.cost_gate import load_budget_config

    return MenuInputs(
        app_settings=settings,
        models=load_model_config(),
        effort=load_seshat_effort(),
        governance=load_governance_config(),
        budget=load_budget_config("config/governance/budget.yaml.example"),
        saved_choices={},
        session=None,
        location_consent=None,
        grounding=[],
        provider_available=lambda _p: True,
    )


def test_returns_nine_groups_and_system() -> None:
    client = _client()
    try:
        with patch(_COLLECT, AsyncMock(return_value=_live_inputs())):
            resp = client.get(_URL)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["groups"]) == 9
        assert body["system"]["id"] == "system"
        assert body["groups"][0]["id"] == "conversation"
        assert body == build_menu(_live_inputs()).model_dump(mode="json")
    finally:
        _teardown()


def test_session_id_reaches_the_collector() -> None:
    session_id = uuid4()
    collect = AsyncMock(return_value=_live_inputs())
    client = _client()
    try:
        with patch(_COLLECT, collect):
            assert client.get(_URL, params={"session_id": str(session_id)}).status_code == 200
        assert collect.await_args.kwargs["session_id"] == session_id
    finally:
        _teardown()


def test_invalid_session_id_is_422() -> None:
    client = _client()
    try:
        assert client.get(_URL, params={"session_id": "not-a-uuid"}).status_code == 422
    finally:
        _teardown()


def test_another_users_session_is_404() -> None:
    client = _client()
    try:
        with patch(_COLLECT, AsyncMock(side_effect=SessionNotFoundError("x"))):
            resp = client.get(_URL, params={"session_id": str(uuid4())})
        assert resp.status_code == 404
    finally:
        _teardown()


def test_the_route_uses_owner_authentication() -> None:
    """The route depends on get_request_user, as PUT /api/v1/preferences/constraint does."""
    route = next(r for r in app.routes if getattr(r, "path", None) == _URL)
    put = next(
        r
        for r in app.routes
        if getattr(r, "path", None) == "/api/v1/preferences/constraint"
        and "PUT" in getattr(r, "methods", set())
    )
    assert get_request_user in {d.call for d in route.dependant.dependencies}
    assert get_request_user in {d.call for d in put.dependant.dependencies}
