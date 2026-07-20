"""Tests for PUT /api/v1/preferences/constraint validation (ADR-0122 §3 / FRE-881).

Proves the AC-6 API clause: settings validation consults the ADR-0121 catalog for
``artifact_builder`` and rejects a non-catalog ``preferred_action`` with 422, while a
real catalog llm key is accepted. Static constraints and unknown constraints keep their
existing behaviour.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from personal_agent.service.app import app
from personal_agent.service.auth import RequestUser, get_request_user
from personal_agent.service.database import get_db_session

_URL = "/api/v1/preferences/constraint"


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


def test_artifact_builder_non_catalog_action_rejected_422() -> None:
    """A preferred_action naming a key absent from the catalog is rejected (AC-6)."""
    client = _client()
    try:
        resp = client.put(
            _URL,
            json={"constraint_name": "artifact_builder", "preferred_action": "not_a_model"},
        )
        assert resp.status_code == 422
        assert "artifact_builder" in resp.json()["detail"]
    finally:
        _teardown()


def test_artifact_builder_catalog_key_accepted() -> None:
    """A real catalog llm key is accepted and reaches storage."""
    client = _client()
    upsert = AsyncMock(return_value=None)
    try:
        with patch(
            "personal_agent.service.repositories.constraint_preferences_repository."
            "ConstraintPreferencesRepository.upsert",
            upsert,
        ):
            resp = client.put(
                _URL,
                json={
                    "constraint_name": "artifact_builder",
                    "preferred_action": "qwen3.6-35b-instruct",
                },
            )
        assert resp.status_code == 200
        assert resp.json()["preferred_action"] == "qwen3.6-35b-instruct"
        upsert.assert_awaited_once()
    finally:
        _teardown()


def test_artifact_builder_always_pause_accepted() -> None:
    """The reserved always_pause value is valid for the computed constraint too."""
    client = _client()
    upsert = AsyncMock(return_value=None)
    try:
        with patch(
            "personal_agent.service.repositories.constraint_preferences_repository."
            "ConstraintPreferencesRepository.upsert",
            upsert,
        ):
            resp = client.put(
                _URL,
                json={"constraint_name": "artifact_builder", "preferred_action": "always_pause"},
            )
        assert resp.status_code == 200
    finally:
        _teardown()


def test_catalog_unavailable_is_503_not_500() -> None:
    """A catalog load failure while validating the computed constraint → clean 503."""
    from personal_agent.config.model_loader import ModelConfigError

    client = _client()

    def _boom(_constraint: str) -> set[str]:
        raise ModelConfigError("catalog unparseable")

    try:
        with patch(
            "personal_agent.orchestrator.constraint_options.valid_preference_actions",
            _boom,
        ):
            resp = client.put(
                _URL,
                json={
                    "constraint_name": "artifact_builder",
                    "preferred_action": "qwen3.6-35b-instruct",
                },
            )
        assert resp.status_code == 503
    finally:
        _teardown()


def test_unknown_constraint_rejected_422() -> None:
    client = _client()
    try:
        resp = client.put(
            _URL,
            json={"constraint_name": "not_a_constraint", "preferred_action": "always_pause"},
        )
        assert resp.status_code == 422
        assert "unknown constraint" in resp.json()["detail"]
    finally:
        _teardown()


def test_static_constraint_still_validated() -> None:
    """A static constraint keeps registry-based validation (regression guard)."""
    client = _client()
    upsert = AsyncMock(return_value=None)
    try:
        with patch(
            "personal_agent.service.repositories.constraint_preferences_repository."
            "ConstraintPreferencesRepository.upsert",
            upsert,
        ):
            ok = client.put(
                _URL,
                json={"constraint_name": "tool_iteration_limit", "preferred_action": "finish_now"},
            )
            bad = client.put(
                _URL,
                json={"constraint_name": "tool_iteration_limit", "preferred_action": "bogus"},
            )
        assert ok.status_code == 200
        assert bad.status_code == 422
    finally:
        _teardown()
