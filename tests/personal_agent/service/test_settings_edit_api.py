"""Edit a setting through the API: owner check, validation, audit, Clear (FRE-1722).

The store is an in-memory fake here, so these tests run in ``make test``. The same behaviour on
real rows is in ``test_settings_edit_db.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue

from personal_agent.config.model_loader import (
    load_model_config,
    resolve_role_model_key,
    resolve_role_target,
)
from personal_agent.config.setting_overrides import current_overrides, replace_overrides
from personal_agent.service import settings_edit
from personal_agent.service.app import app
from personal_agent.service.auth import RequestUser, get_request_user
from personal_agent.service.database import get_db_session

_URL = "/api/v1/settings/overrides"
_OWNER = "owner@example.com"
_ROLE = "compressor"
_PARAMETER = f"role.{_ROLE}"
_ROOT = Path(__file__).resolve().parents[3]


@dataclass
class _Store:
    """The rows that the fake repository holds."""

    overrides: dict[str, JsonValue] = field(default_factory=dict)
    audit: list[dict[str, object]] = field(default_factory=list)


class _FakeRepository:
    """In-memory stand-in for ``SettingsOverridesRepository``."""

    store = _Store()

    def __init__(self, db: object) -> None:
        self.db = db

    async def list_all(self) -> dict[str, JsonValue]:
        return dict(self.store.overrides)

    async def get(self, parameter: str) -> JsonValue:
        return self.store.overrides.get(parameter)

    async def set(
        self, *, parameter: str, value: JsonValue, changed_by: UUID, old_value: JsonValue
    ) -> None:
        self.store.overrides[parameter] = value
        self.store.audit.append(
            {
                "parameter": parameter,
                "action": "set",
                "changed_by": changed_by,
                "old_value": old_value,
                "new_value": value,
            }
        )

    async def clear(self, *, parameter: str, changed_by: UUID, new_value: JsonValue) -> bool:
        if parameter not in self.store.overrides:
            return False
        old_value = self.store.overrides.pop(parameter)
        self.store.audit.append(
            {
                "parameter": parameter,
                "action": "clear",
                "changed_by": changed_by,
                "old_value": old_value,
                "new_value": new_value,
            }
        )
        return True


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Store]:
    fresh = _Store()
    monkeypatch.setattr(_FakeRepository, "store", fresh)
    monkeypatch.setattr(settings_edit, "SettingsOverridesRepository", _FakeRepository)
    monkeypatch.setattr(
        settings_edit,
        "settings",
        SimpleNamespace(gateway_auth_enabled=True, agent_owner_email=_OWNER),
    )
    monkeypatch.setattr(
        settings_edit, "build_provider_availability", lambda *_args: lambda _provider: True
    )
    replace_overrides({})
    yield fresh
    replace_overrides({})


def _client(email: str = _OWNER) -> TestClient:
    async def _override_user() -> RequestUser:
        return RequestUser(user_id=uuid4(), email=email)

    async def _override_db() -> object:
        return object()

    app.dependency_overrides[get_request_user] = _override_user
    app.dependency_overrides[get_db_session] = _override_db
    return TestClient(app)


@pytest.fixture(autouse=True)
def _teardown() -> Iterator[None]:
    yield
    app.dependency_overrides.pop(get_request_user, None)
    app.dependency_overrides.pop(get_db_session, None)


def _other_llm() -> str:
    config = load_model_config()
    bound = config.roles[_ROLE].deployment
    return next(
        key for key, model in config.models.items() if model.kind.value == "llm" and key != bound
    )


def test_ac1_edit_then_resolve_returns_the_new_model_and_clear_restores(store: _Store) -> None:
    file_key = resolve_role_target(_ROLE)[0]
    target = _other_llm()
    client = _client()

    resp = client.put(_URL, json={"parameter": _PARAMETER, "value": target})
    assert resp.status_code == 200
    assert resolve_role_target(_ROLE)[0] == target
    assert resolve_role_model_key(_ROLE) == target

    resp = client.delete(_URL, params={"parameter": _PARAMETER})
    assert resp.status_code == 200
    assert resp.json() == {"parameter": _PARAMETER, "cleared": True}
    assert resolve_role_target(_ROLE)[0] == file_key
    assert resolve_role_model_key(_ROLE) == file_key


@pytest.mark.parametrize(
    "value",
    ["no_such_deployment", "embedding", 5, None, {"deployment": "claude_sonnet"}],
)
def test_ac2_an_invalid_value_is_refused_with_a_reason_and_writes_nothing(
    store: _Store, value: JsonValue
) -> None:
    resp = _client().put(_URL, json={"parameter": _PARAMETER, "value": value})
    assert resp.status_code == 422
    assert resp.json()["detail"]
    assert store.overrides == {}
    assert store.audit == []


@pytest.mark.parametrize(
    "parameter", ["role.no_such_role", "setting.llm_timeout_seconds", "saved_choice.x", ""]
)
def test_ac2_a_parameter_that_cannot_be_edited_writes_nothing(
    store: _Store, parameter: str
) -> None:
    resp = _client().put(_URL, json={"parameter": parameter, "value": "claude_sonnet"})
    assert resp.status_code == 422
    assert store.overrides == {}
    assert store.audit == []


def test_ac2_a_non_owner_request_is_refused_and_writes_nothing(store: _Store) -> None:
    store.overrides[_PARAMETER] = "claude_sonnet"
    client = _client(email="someone.else@example.com")

    assert client.put(_URL, json={"parameter": _PARAMETER, "value": _other_llm()}).status_code == 403
    assert client.delete(_URL, params={"parameter": _PARAMETER}).status_code == 403
    assert store.overrides == {_PARAMETER: "claude_sonnet"}
    assert store.audit == []


def test_the_owner_check_runs_before_validation(store: _Store) -> None:
    resp = _client(email="x@example.com").put(_URL, json={"parameter": "nope", "value": 1})
    assert resp.status_code == 403


def test_a_deployment_with_no_credential_is_refused(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings_edit, "build_provider_availability", lambda *_args: lambda _provider: False
    )
    resp = _client().put(_URL, json={"parameter": _PARAMETER, "value": _other_llm()})
    assert resp.status_code == 422
    assert "credential" in resp.json()["detail"]
    assert store.overrides == {}


def test_inherit_needs_no_provider_check(store: _Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings_edit, "build_provider_availability", lambda *_args: lambda _provider: False
    )
    resp = _client().put(_URL, json={"parameter": _PARAMETER, "value": "inherit"})
    assert resp.status_code == 200


def test_ac3_each_edit_and_each_clear_writes_one_audit_row(store: _Store) -> None:
    file_key = resolve_role_target(_ROLE)[0]
    first = _other_llm()
    client = _client()

    client.put(_URL, json={"parameter": _PARAMETER, "value": first})
    client.put(_URL, json={"parameter": _PARAMETER, "value": "inherit"})
    client.delete(_URL, params={"parameter": _PARAMETER})

    assert [(r["action"], r["old_value"], r["new_value"]) for r in store.audit] == [
        ("set", file_key, first),
        ("set", first, "inherit"),
        ("clear", "inherit", file_key),
    ]
    assert {r["parameter"] for r in store.audit} == {_PARAMETER}
    assert all(isinstance(r["changed_by"], UUID) for r in store.audit)


def test_the_process_copy_follows_the_committed_value_with_no_re_read(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed read after the commit must not leave turns on the old model."""

    async def _fail(_self: object) -> None:
        raise AssertionError("no re-read after a commit")

    monkeypatch.setattr(_FakeRepository, "list_all", _fail)
    target = _other_llm()
    client = _client()

    assert client.put(_URL, json={"parameter": _PARAMETER, "value": target}).status_code == 200
    assert resolve_role_target(_ROLE)[0] == target
    assert client.delete(_URL, params={"parameter": _PARAMETER}).status_code == 200
    assert _PARAMETER not in current_overrides()


def test_ac3_clear_with_nothing_stored_writes_no_audit_row(store: _Store) -> None:
    resp = _client().delete(_URL, params={"parameter": _PARAMETER})
    assert resp.status_code == 200
    assert resp.json()["cleared"] is False
    assert store.audit == []


@pytest.mark.parametrize(
    ("auth_enabled", "owner_email", "email", "expected"),
    [
        (True, _OWNER, _OWNER, True),
        (True, _OWNER, _OWNER.upper(), True),
        (True, _OWNER, "other@example.com", False),
        (True, None, _OWNER, False),
        (True, "", "", False),
        (False, _OWNER, _OWNER, False),
    ],
)
def test_is_owner_needs_a_verified_identity_and_a_configured_owner(
    monkeypatch: pytest.MonkeyPatch,
    auth_enabled: bool,
    owner_email: str | None,
    email: str,
    expected: bool,
) -> None:
    monkeypatch.setattr(
        settings_edit,
        "settings",
        SimpleNamespace(gateway_auth_enabled=auth_enabled, agent_owner_email=owner_email),
    )
    assert settings_edit.is_owner(RequestUser(user_id=uuid4(), email=email)) is expected


def test_the_gateway_runs_one_process() -> None:
    """The override copy is per process, so a second worker would miss an edit until restart."""
    for name in ("docker-compose.cloud.yml", "Dockerfile.gateway", "Makefile"):
        assert "--workers" not in (_ROOT / name).read_text(), name
