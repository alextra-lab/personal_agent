"""Edit a setting through the API: owner check, validation, audit, Clear (FRE-1722, FRE-1723).

The store is an in-memory fake here, so these tests run in ``make test``. The same behaviour on
real rows is in ``test_settings_edit_db.py``.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import litellm
import pytest
import structlog
from fastapi.testclient import TestClient
from pydantic import BaseModel, JsonValue, ValidationError, model_validator

from personal_agent.brainstem.consumers.mode_controller import ModeControllerConsumer
from personal_agent.config import settings
from personal_agent.config.governance_loader import file_governance_config, load_governance_config
from personal_agent.config.model_loader import (
    file_deployment_fields,
    file_model_config,
    load_model_config,
    resolve_role_model_key,
    resolve_role_target,
)
from personal_agent.config.seshat_effort import (
    effort_spec,
    file_seshat_effort,
    get_seshat_effort,
    preset_values,
    reset_current_effort,
    rounds_for_turn,
    set_current_effort,
)
from personal_agent.config.setting_classes import READ_ONLY_SETTINGS
from personal_agent.config.setting_overrides import (
    apply_setting_overrides,
    current_overrides,
    replace_overrides,
)
from personal_agent.config.settings import AppConfig
from personal_agent.cost_gate import BudgetConfig, BudgetConfigError, CapEntry, CostGate
from personal_agent.llm_client.pricing import register_model_pricing
from personal_agent.orchestrator.executor import _resolve_max_iterations
from personal_agent.service import settings_edit
from personal_agent.service.app import app
from personal_agent.service.auth import RequestUser, get_request_user
from personal_agent.service.database import get_db_session
from personal_agent.service.settings_menu import MenuInputs, build_menu

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
    audit_reads: int = 0


class _FakeRepository:
    """In-memory stand-in for ``SettingsOverridesRepository``."""

    store = _Store()

    def __init__(self, db: object) -> None:
        self.db = db

    async def list_all(self) -> dict[str, JsonValue]:
        return dict(self.store.overrides)

    async def find(self, parameter: str) -> tuple[JsonValue] | None:
        if parameter not in self.store.overrides:
            return None
        return (self.store.overrides[parameter],)

    async def unaudited_parameters(self, parameters: Collection[str]) -> list[str]:
        self.store.audit_reads += 1
        latest = {str(row["parameter"]): row["action"] for row in self.store.audit}
        return sorted(name for name in parameters if latest.get(name) != "set")

    async def set(
        self,
        *,
        parameter: str,
        value: JsonValue,
        changed_by: UUID,
        old_value: JsonValue,
        hidden: bool = False,
    ) -> None:
        self.store.overrides[parameter] = value
        self.store.audit.append(
            {
                "parameter": parameter,
                "action": "set",
                "changed_by": changed_by,
                "old_value": old_value,
                "new_value": "set" if hidden else value,
            }
        )

    async def clear(
        self, *, parameter: str, changed_by: UUID, new_value: JsonValue, hidden: bool = False
    ) -> bool:
        if parameter not in self.store.overrides:
            return False
        old_value = self.store.overrides.pop(parameter)
        self.store.audit.append(
            {
                "parameter": parameter,
                "action": "clear",
                "changed_by": changed_by,
                "old_value": "set" if hidden else old_value,
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
    "parameter", ["role.no_such_role", "setting.no_such_setting", "saved_choice.x", ""]
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

    assert (
        client.put(_URL, json={"parameter": _PARAMETER, "value": _other_llm()}).status_code == 403
    )
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


# ---------------------------------------------------------------------------
# FRE-1723: effort presets and budget caps
# ---------------------------------------------------------------------------

_DEEP = "effort.level.deep"
_CAP = "cap.daily.main_inference"


def _budget() -> BudgetConfig:
    return BudgetConfig(
        roles={},
        caps=[
            CapEntry(time_window="daily", role="main_inference", cap_usd=Decimal("10.00")),
            CapEntry(time_window="weekly", role="_total", cap_usd=Decimal("100.00")),
        ],
    )


def _preset(rounds: int = 3) -> dict[str, JsonValue]:
    return {
        "expansion": True,
        "sub_agent_rounds": rounds,
        "planner_policy": "baseline",
        "primary_mode": None,
    }


@pytest.fixture
def budget_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Budget file values that no test reads from the git-ignored file."""
    monkeypatch.setattr(settings_edit, "load_budget_config", _budget)


def test_fre1723_ac1_edit_a_preset_then_the_next_turn_uses_it_and_clear_restores(
    store: _Store,
) -> None:
    file_rounds = effort_spec("deep").sub_agent_rounds
    client = _client()

    resp = client.put(_URL, json={"parameter": _DEEP, "value": _preset()})
    assert resp.status_code == 200
    token = set_current_effort(effort_spec("deep"))
    try:
        assert rounds_for_turn("thorough") == 3
    finally:
        reset_current_effort(token)

    resp = client.delete(_URL, params={"parameter": _DEEP})
    assert resp.json()["cleared"] is True
    assert effort_spec("deep").sub_agent_rounds == file_rounds


def test_fre1723_ac1_edit_the_default_level_and_clear_restores(store: _Store) -> None:
    file_level = effort_spec(None).level
    other = "deep" if file_level != "deep" else "fast"
    client = _client()

    assert (
        client.put(_URL, json={"parameter": "effort.default_level", "value": other}).status_code
        == 200
    )
    assert effort_spec(None).level == other

    client.delete(_URL, params={"parameter": "effort.default_level"})
    assert effort_spec(None).level == file_level


def test_fre1723_ac2_a_lowered_cap_reaches_the_gate_and_clear_restores(
    store: _Store, budget_file: None
) -> None:
    gate = CostGate(config=_budget(), db_url="postgresql://unused/unused")
    client = _client()

    resp = client.put(_URL, json={"parameter": _CAP, "value": 2.5})
    assert resp.status_code == 200
    assert resp.json() == {"parameter": _CAP, "value": 2.5}
    assert client.put(_URL, json={"parameter": _CAP, "value": 5}).json()["value"] == 5.0
    assert client.put(_URL, json={"parameter": _CAP, "value": 2.5}).status_code == 200
    assert gate.config.caps_for("main_inference")[0].cap_usd == Decimal("2.5")

    assert client.delete(_URL, params={"parameter": _CAP}).json()["cleared"] is True
    assert gate.config.caps_for("main_inference")[0].cap_usd == Decimal("10.00")


@pytest.mark.parametrize(
    ("parameter", "value", "reason"),
    [
        (_CAP, -1, "greater than or equal to 0"),
        (_CAP, "5", "number"),
        (_CAP, True, "number"),
        (_CAP, None, "number"),
        (_CAP, 10000, "digits"),
        ("cap.daily.no_such_role", 5, "not a cap"),
        (_DEEP, {"expansion": "yes"}, "expansion"),
        (_DEEP, {"expansion": True}, "needs sub_agent_rounds"),
        (_DEEP, "fast", "takes the values of a preset"),
        ("effort.level.no_such", {"expansion": False}, "no_such"),
        ("effort.default_level", "no_such", "default_level"),
    ],
)
def test_fre1723_ac3_an_invalid_value_is_refused_with_a_reason_and_writes_nothing(
    store: _Store, budget_file: None, parameter: str, value: JsonValue, reason: str
) -> None:
    resp = _client().put(_URL, json={"parameter": parameter, "value": value})
    assert resp.status_code == 422
    assert reason in resp.json()["detail"]
    assert store.overrides == {}
    assert store.audit == []
    assert current_overrides() == {}


def test_fre1723_ac3_an_unreadable_budget_file_refuses_the_edit(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail() -> BudgetConfig:
        raise BudgetConfigError("Budget config not found")

    monkeypatch.setattr(settings_edit, "load_budget_config", _fail)
    resp = _client().put(_URL, json={"parameter": _CAP, "value": 5})
    assert resp.status_code == 422
    assert store.overrides == {}


def test_fre1723_ac3_each_edit_writes_one_audit_row_with_the_stored_form(
    store: _Store, budget_file: None
) -> None:
    client = _client()
    file_preset = preset_values(file_seshat_effort(), "deep")

    client.put(_URL, json={"parameter": _CAP, "value": 5})
    client.put(_URL, json={"parameter": _CAP, "value": 2.5})
    client.delete(_URL, params={"parameter": _CAP})
    client.put(_URL, json={"parameter": _DEEP, "value": _preset()})
    client.delete(_URL, params={"parameter": _DEEP})

    assert [(r["parameter"], r["action"], r["old_value"], r["new_value"]) for r in store.audit] == [
        (_CAP, "set", 10.0, 5.0),
        (_CAP, "set", 5.0, 2.5),
        (_CAP, "clear", 2.5, 10.0),
        (_DEEP, "set", file_preset, _preset()),
        (_DEEP, "clear", _preset(), file_preset),
    ]


def test_fre1723_a_non_owner_cannot_edit_a_preset_or_a_cap(
    store: _Store, budget_file: None
) -> None:
    client = _client(email="someone.else@example.com")
    assert client.put(_URL, json={"parameter": _CAP, "value": 1}).status_code == 403
    assert client.put(_URL, json={"parameter": _DEEP, "value": _preset()}).status_code == 403
    assert store.overrides == {}
    assert store.audit == []


def test_fre1723_a_cap_edit_mirrors_the_enforced_caps_and_a_mirror_failure_keeps_the_edit(
    store: _Store, budget_file: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = CostGate(config=_budget(), db_url="postgresql://unused/unused")
    gate.pool = object()
    mirrored: list[Decimal] = []

    async def _sync(config: BudgetConfig, _pool: object) -> None:
        mirrored.append(config.caps[0].cap_usd)

    monkeypatch.setattr(settings_edit, "get_default_gate_or_none", lambda: gate)
    monkeypatch.setattr(settings_edit, "sync_budget_policies_to_db", _sync)
    client = _client()

    client.put(_URL, json={"parameter": _CAP, "value": 2.5})
    client.delete(_URL, params={"parameter": _CAP})
    client.delete(_URL, params={"parameter": _CAP})
    client.put(_URL, json={"parameter": _DEEP, "value": _preset()})
    assert mirrored == [Decimal("2.5"), Decimal("10.00")]

    async def _fail(_config: BudgetConfig, _pool: object) -> None:
        raise OSError("mirror down")

    monkeypatch.setattr(settings_edit, "sync_budget_policies_to_db", _fail)
    assert client.put(_URL, json={"parameter": _CAP, "value": 1}).status_code == 200
    assert gate.config.caps[0].cap_usd == Decimal("1.0")


# ---------------------------------------------------------------------------
# FRE-1724: AppConfig settings, governance, deployments and role estimates
# ---------------------------------------------------------------------------

_SENTINEL = "sk-sentinel-secret-value-1724"


@pytest.fixture
def clean_settings() -> Iterator[None]:
    yield
    replace_overrides({})
    apply_setting_overrides(at_start=True)


def _execution_context() -> SimpleNamespace:
    return SimpleNamespace(tool_iteration_bonus=0, grounding_retrieval_grant=0)


def test_fre1724_ac3_next_turn_an_edit_changes_what_the_reader_gets_and_clear_restores(
    store: _Store, clean_settings: None
) -> None:
    before = _resolve_max_iterations(_execution_context())  # type: ignore[arg-type]
    edited = settings.orchestrator_spend_threshold + 7

    resp = _client().put(
        _URL, json={"parameter": "setting.orchestrator_max_tool_iterations", "value": edited}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == edited
    assert _resolve_max_iterations(_execution_context()) == edited  # type: ignore[arg-type]

    cleared = _client().delete(
        _URL, params={"parameter": "setting.orchestrator_max_tool_iterations"}
    )

    assert cleared.status_code == 200
    assert _resolve_max_iterations(_execution_context()) == before  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fre1724_ac3_next_restart_an_edit_changes_the_reader_at_the_next_start_only(
    store: _Store, clean_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    parameter = "setting.mode_evaluation_interval_seconds"
    before = settings.mode_evaluation_interval_seconds
    edited = before + 11

    resp = _client().put(_URL, json={"parameter": parameter, "value": edited})

    assert resp.status_code == 200, resp.text
    assert settings.mode_evaluation_interval_seconds == before
    assert _mode_controller()._eval_interval == before

    # A new process: an empty copy, then the startup load from the stored rows.
    replace_overrides({})
    monkeypatch.setattr(settings_edit, "AsyncSessionLocal", _FakeSessionFactory)
    await settings_edit.load_overrides_at_startup()

    assert settings.mode_evaluation_interval_seconds == edited
    assert _mode_controller()._eval_interval == edited


class _FakeSessionFactory:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_args: object) -> None:
        return None


def _mode_controller() -> ModeControllerConsumer:
    return ModeControllerConsumer(mode_manager=SimpleNamespace())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    ["many", True, None, [1], -1],
)
def test_fre1724_an_invalid_setting_is_refused_and_writes_nothing(
    store: _Store, clean_settings: None, value: JsonValue
) -> None:
    resp = _client().put(
        _URL, json={"parameter": "setting.orchestrator_max_tool_iterations", "value": value}
    )

    assert resp.status_code == 422
    assert resp.json()["detail"]
    assert store.overrides == {}
    assert store.audit == []


def test_fre1724_a_read_only_setting_is_refused_with_its_reason(
    store: _Store, clean_settings: None
) -> None:
    resp = _client().put(_URL, json={"parameter": "setting.database_echo", "value": True})

    assert resp.status_code == 422
    assert "imports" in resp.json()["detail"]
    assert store.overrides == {}


def test_fre1724_a_cross_field_refusal_keeps_the_settings_check_reason(
    store: _Store, clean_settings: None
) -> None:
    resp = _client().put(
        _URL,
        json={
            "parameter": "setting.orchestrator_spend_threshold",
            "value": settings.orchestrator_max_tool_iterations + 1,
        },
    )

    assert resp.status_code == 422
    assert "orchestrator" in resp.json()["detail"]


def test_fre1724_a_ceiling_below_a_preset_is_refused(store: _Store, clean_settings: None) -> None:
    resp = _client().put(
        _URL, json={"parameter": "setting.sub_agent_max_tool_iterations", "value": 1}
    )

    assert resp.status_code == 422
    assert "exceed" in resp.json()["detail"]
    assert store.overrides == {}


def test_fre1724_a_non_owner_cannot_edit_a_setting(store: _Store, clean_settings: None) -> None:
    resp = _client(email="other@example.com").put(
        _URL, json={"parameter": "setting.orchestrator_max_tool_iterations", "value": 3}
    )

    assert resp.status_code == 403
    assert store.overrides == {}


def test_fre1724_the_stored_form_passes_the_same_check_on_reload(
    store: _Store, clean_settings: None
) -> None:
    resp = _client().put(_URL, json={"parameter": "setting.llm_timeout_seconds", "value": 77})

    assert resp.status_code == 200, resp.text
    settings_edit.validate_override(
        "setting.llm_timeout_seconds", store.overrides["setting.llm_timeout_seconds"]
    )


def test_fre1724_a_tool_edit_reaches_the_reader_and_writes_one_audit_row(
    store: _Store,
) -> None:
    tool = next(iter(file_governance_config().tools))
    parameter = f"tool.{tool}"
    file_value = file_governance_config().tools[tool].loop_max_consecutive

    resp = _client().put(
        _URL, json={"parameter": parameter, "value": {"loop_max_consecutive": file_value + 2}}
    )

    assert resp.status_code == 200, resp.text
    assert load_governance_config().tools[tool].loop_max_consecutive == file_value + 2
    assert len(store.audit) == 1
    assert store.audit[0]["old_value"]["loop_max_consecutive"] == file_value  # type: ignore[index]

    _client().delete(_URL, params={"parameter": parameter})

    assert load_governance_config().tools[tool].loop_max_consecutive == file_value


def test_fre1724_a_deployment_edit_reaches_the_catalog(store: _Store) -> None:
    key = next(iter(load_model_config().models))
    before = load_model_config().models[key].default_timeout

    resp = _client().put(
        _URL, json={"parameter": f"deployment.{key}", "value": {"default_timeout": before + 3}}
    )

    assert resp.status_code == 200, resp.text
    assert load_model_config().models[key].default_timeout == before + 3


# ---------------------------------------------------------------------------
# FRE-1733 AC-4: a price edit reaches the litellm cost table, and a row holds the changed fields
# ---------------------------------------------------------------------------


@pytest.fixture
def cost_table() -> Iterator[None]:
    saved = copy.deepcopy(litellm.model_cost)
    yield
    litellm.model_cost.clear()
    litellm.model_cost.update(saved)


def _priced_deployment() -> str:
    return next(
        key
        for key, model in file_model_config().models.items()
        if model.kind.value == "llm" and model.input_cost_per_token and model.provider
    )


def _table_price(key: str) -> float:
    """Read the cost table with the lookup of ``estimate_reservation_for_call``."""
    model = file_model_config().models[key]
    litellm_model = f"{model.provider}/{model.id}"
    entry = litellm.model_cost.get(litellm_model) or litellm.model_cost.get(model.id)
    assert entry is not None
    price: float = entry["input_cost_per_token"]
    return price


def test_fre1733_a_price_edit_reaches_the_cost_table_and_clear_restores(
    store: _Store, cost_table: None
) -> None:
    key = _priced_deployment()
    file_price = file_model_config().models[key].input_cost_per_token
    assert file_price is not None
    register_model_pricing(file_model_config())
    new_price = file_price * 3
    assert _table_price(key) == file_price

    resp = _client().put(
        _URL, json={"parameter": f"deployment.{key}", "value": {"input_cost_per_token": new_price}}
    )

    assert resp.status_code == 200, resp.text
    assert _table_price(key) == new_price

    _client().delete(_URL, params={"parameter": f"deployment.{key}"})

    assert _table_price(key) == file_price


def test_fre1733_a_failed_price_registration_does_not_undo_the_edit(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _priced_deployment()
    file_price = file_model_config().models[key].input_cost_per_token
    assert file_price is not None

    def _fail(_config: object) -> int:
        raise RuntimeError("registry refused")

    monkeypatch.setattr(settings_edit, "register_model_pricing", _fail)

    with structlog.testing.capture_logs() as logs:
        resp = _client().put(
            _URL,
            json={
                "parameter": f"deployment.{key}",
                "value": {"input_cost_per_token": file_price * 3},
            },
        )

    assert resp.status_code == 200, resp.text
    assert load_model_config().models[key].input_cost_per_token == file_price * 3
    failures = [log for log in logs if log["event"] == "model_pricing_registration_failed"]
    assert len(failures) == 1
    assert failures[0]["trace_id"]


def test_fre1733_a_whole_row_with_one_changed_field_stores_one_field(store: _Store) -> None:
    key = _priced_deployment()
    fields = file_deployment_fields(key)
    assert isinstance(fields, dict)
    timeout = fields["default_timeout"]
    assert isinstance(timeout, int)
    parameter = f"deployment.{key}"

    resp = _client().put(
        _URL, json={"parameter": parameter, "value": {**fields, "default_timeout": timeout + 3}}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == {"default_timeout": timeout + 3}
    assert store.overrides[parameter] == {"default_timeout": timeout + 3}


def test_fre1733_a_row_that_returns_to_the_file_is_removed(store: _Store) -> None:
    key = _priced_deployment()
    fields = file_deployment_fields(key)
    assert isinstance(fields, dict)
    timeout = fields["default_timeout"]
    assert isinstance(timeout, int)
    parameter = f"deployment.{key}"
    _client().put(_URL, json={"parameter": parameter, "value": {"default_timeout": timeout + 3}})
    assert parameter in store.overrides

    resp = _client().put(_URL, json={"parameter": parameter, "value": fields})

    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == {}
    assert parameter not in store.overrides
    assert parameter not in current_overrides()
    assert load_model_config().models[key].default_timeout == timeout
    assert [row["action"] for row in store.audit] == ["set", "clear"]


def test_fre1733_an_unchanged_row_with_no_override_writes_nothing(store: _Store) -> None:
    key = _priced_deployment()

    resp = _client().put(
        _URL, json={"parameter": f"deployment.{key}", "value": file_deployment_fields(key)}
    )

    assert resp.status_code == 200, resp.text
    assert store.overrides == {}
    assert store.audit == []


@pytest.mark.parametrize(
    "parameter",
    [f"tool.{next(iter(file_governance_config().tools))}", "role_estimate.main_inference"],
)
def test_fre1733_an_empty_put_keeps_a_stored_row(store: _Store, parameter: str) -> None:
    """A client that sends ``{}`` gets a refusal, the same as on a deployment row."""
    store.overrides[parameter] = {"a_stored_field": 1}

    resp = _client().put(_URL, json={"parameter": parameter, "value": {}})

    assert resp.status_code == 422
    assert parameter in store.overrides
    assert store.audit == []


def test_fre1733_a_setting_that_stores_an_empty_object_keeps_its_row(store: _Store) -> None:
    """The row is removed for the three field families only. A setting can hold ``{}``."""
    parameter = "setting.sub_agent_rounds_by_thoroughness"

    resp = _client().put(_URL, json={"parameter": parameter, "value": {}})

    assert resp.status_code == 200, resp.text
    assert store.overrides[parameter] == {}


def test_fre1724_uncapped_roles_and_grounding_state_cannot_be_edited(store: _Store) -> None:
    for parameter in ("uncapped_roles", "grounding_enforcement.a_model"):
        resp = _client().put(_URL, json={"parameter": parameter, "value": []})
        assert resp.status_code == 422
    assert store.overrides == {}


# ---------------------------------------------------------------------------
# FRE-1724 AC-4: a secret value never reaches a response, an audit row or a log line
# ---------------------------------------------------------------------------


SECRET_PARAMETERS = sorted(
    f"setting.{field}" for field, reason in READ_ONLY_SETTINGS.items() if "secret" in reason
)


def test_fre1724_ac4_the_secret_list_is_not_empty() -> None:
    assert "setting.openai_api_key" in SECRET_PARAMETERS
    assert len(SECRET_PARAMETERS) >= 16


@pytest.mark.parametrize("parameter", SECRET_PARAMETERS)
def test_fre1724_ac4_a_secret_is_refused_and_appears_nowhere(
    store: _Store, clean_settings: None, parameter: str, caplog: pytest.LogCaptureFixture
) -> None:
    with structlog.testing.capture_logs() as entries:
        resp = _client().put(_URL, json={"parameter": parameter, "value": _SENTINEL})

    assert resp.status_code == 422
    assert _SENTINEL not in resp.text
    assert store.overrides == {}
    assert store.audit == []
    assert _SENTINEL not in str(entries)
    assert _SENTINEL not in caplog.text


@pytest.mark.parametrize(
    "body",
    [
        f'"{_SENTINEL}"',
        f'["{_SENTINEL}"]',
        f'{{"value": "{_SENTINEL}"}}',
        f'{{"parameter": 1, "value": "{_SENTINEL}"}}',
        f'{{"parameter": "setting.openai_api_key", "value": "{_SENTINEL}"',
        f"{_SENTINEL}",
    ],
)
def test_fre1724_ac4_a_malformed_body_does_not_echo_the_value(
    store: _Store, clean_settings: None, body: str, caplog: pytest.LogCaptureFixture
) -> None:
    with structlog.testing.capture_logs() as entries:
        resp = _client().put(_URL, content=body, headers={"content-type": "application/json"})

    assert resp.status_code == 422
    assert _SENTINEL not in resp.text
    assert _SENTINEL not in str(entries)
    assert _SENTINEL not in caplog.text
    assert store.audit == []


def test_fre1724_ac4_a_password_in_a_url_is_refused_on_every_setting(
    store: _Store, clean_settings: None, caplog: pytest.LogCaptureFixture
) -> None:
    value = f"http://user:{_SENTINEL}@example.test"

    with structlog.testing.capture_logs() as entries:
        resp = _client().put(_URL, json={"parameter": "setting.searxng_base_url", "value": value})

    assert resp.status_code == 422
    assert _SENTINEL not in resp.text
    assert _SENTINEL not in str(entries)
    assert _SENTINEL not in caplog.text
    assert store.overrides == {}


def test_fre1724_ac4_a_write_only_row_holds_set_in_the_response_the_audit_and_the_log(
    store: _Store, clean_settings: None, caplog: pytest.LogCaptureFixture
) -> None:
    value = f"http://{_SENTINEL}.example.test"
    parameter = "setting.searxng_base_url"

    with structlog.testing.capture_logs() as entries:
        put = _client().put(_URL, json={"parameter": parameter, "value": value})
        second = _client().put(_URL, json={"parameter": parameter, "value": f"{value}/second"})
        cleared = _client().delete(_URL, params={"parameter": parameter})

    assert put.status_code == 200, put.text
    assert second.status_code == 200, second.text
    assert cleared.status_code == 200
    assert put.json()["value"] == "set"
    assert _SENTINEL not in put.text + second.text + cleared.text
    assert len(store.audit) == 3
    assert _SENTINEL not in str(store.audit)
    # The capture must hold the write lines, or the next check proves nothing.
    assert {"setting_override_stored", "setting_override_cleared"} <= {
        str(entry["event"]) for entry in entries
    }
    assert _SENTINEL not in str(entries)
    assert _SENTINEL not in caplog.text


def test_fre1724_ac4_a_refused_hidden_value_keeps_the_error_type_only(
    store: _Store, clean_settings: None, caplog: pytest.LogCaptureFixture
) -> None:
    with structlog.testing.capture_logs() as entries:
        resp = _client().put(
            _URL, json={"parameter": "setting.ws_max_message_size", "value": _SENTINEL}
        )

    assert resp.status_code == 422
    assert _SENTINEL not in resp.text
    assert _SENTINEL not in str(entries)
    assert _SENTINEL not in caplog.text


def test_fre1724_ac4_a_cross_field_message_with_a_url_does_not_reach_a_reason() -> None:
    """Codex plan review: a validator message can quote a database URL with its password."""

    class _Model(BaseModel):
        value: int

        @model_validator(mode="after")
        def _check(self) -> _Model:
            raise ValueError(f"database_url='postgresql://agent:{_SENTINEL}@db/x' is refused")

    with pytest.raises(ValidationError) as excinfo:
        _Model(value=1)

    for hidden in (True, False):
        reason = settings_edit._refusal_reason("setting.x", excinfo.value, hidden=hidden)
        assert _SENTINEL not in reason
        assert "postgresql" not in reason


def test_fre1724_ac4_the_log_capture_sees_a_seeded_secret() -> None:
    """A seeded negative: the checks above are only as good as this capture."""
    with structlog.testing.capture_logs() as entries:
        settings_edit.log.warning("seeded", value=_SENTINEL)

    assert _SENTINEL in str(entries)


@pytest.mark.asyncio
async def test_fre1724_a_stored_ceiling_below_a_preset_logs_an_error_and_starts(
    store: _Store, clean_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex plan review: the fatal effort check runs before the overrides load."""
    store.overrides["setting.sub_agent_max_tool_iterations"] = 1
    monkeypatch.setattr(settings_edit, "AsyncSessionLocal", _FakeSessionFactory)

    with structlog.testing.capture_logs() as entries:
        await settings_edit.load_overrides_at_startup()

    assert "effort_round_ceiling_conflict" in {str(entry["event"]) for entry in entries}


def test_fre1724_every_control_value_passes_its_own_validator(
    budget_file: None, clean_settings: None
) -> None:
    """A control starts from ``edit.current``, so an unchanged value must save. Real files."""
    models = load_model_config()
    menu = build_menu(
        MenuInputs(
            app_settings=settings,
            models=models,
            effort=get_seshat_effort(),
            governance=load_governance_config(),
            budget=settings_edit.load_budget_config(),
            saved_choices={},
            session=None,
            location_consent=False,
            grounding=[],
            provider_available=lambda _provider: True,
            is_owner=True,
        )
    )
    groups = [*menu.groups, menu.system]

    assert [group.id for group in groups if group.error] == []
    checked = 0
    for row in (row for group in groups for row in group.rows):
        edit = row.edit
        if edit is None or edit.kind != "setting" or edit.current is None:
            continue
        if edit.input in ("json", "number"):
            value: JsonValue = json.loads(edit.current)
        elif edit.input == "boolean":
            value = edit.current == "true"
        else:
            value = edit.current
        settings_edit.validate_override(row.name, value)
        checked += 1
    assert checked > 300


def test_fre1724_a_clear_that_leaves_a_preset_above_the_ceiling_is_refused(
    store: _Store, clean_settings: None
) -> None:
    """Code review: a Clear changes the live ceiling as an edit does."""
    ceiling = "setting.sub_agent_max_tool_iterations"
    file_ceiling = settings.sub_agent_max_tool_iterations
    high = file_ceiling + 10
    assert _client().put(_URL, json={"parameter": ceiling, "value": high}).status_code == 200
    preset = _preset(rounds=file_ceiling + 5)
    assert (
        _client().put(_URL, json={"parameter": "effort.level.deep", "value": preset}).status_code
        == 200
    )

    resp = _client().delete(_URL, params={"parameter": ceiling})

    assert resp.status_code == 422
    assert "exceed" in resp.json()["detail"]
    assert ceiling in store.overrides
    assert settings.sub_agent_max_tool_iterations == high

    assert _client().delete(_URL, params={"parameter": "effort.level.deep"}).status_code == 200
    assert _client().delete(_URL, params={"parameter": ceiling}).status_code == 200
    assert settings.sub_agent_max_tool_iterations == file_ceiling


def test_fre1724_raising_a_ceiling_reaches_a_preset_that_the_old_ceiling_skipped(
    store: _Store, clean_settings: None
) -> None:
    """The cached presets depend on the ceiling, so an edit of it must drop the cache."""
    file_ceiling = settings.sub_agent_max_tool_iterations
    replace_overrides({"effort.level.deep": _preset(rounds=file_ceiling + 5)})
    assert preset_values(get_seshat_effort(), "deep") != _preset(rounds=file_ceiling + 5)

    resp = _client().put(
        _URL,
        json={"parameter": "setting.sub_agent_max_tool_iterations", "value": file_ceiling + 10},
    )

    assert resp.status_code == 200, resp.text
    assert preset_values(get_seshat_effort(), "deep") == _preset(rounds=file_ceiling + 5)


def test_fre1724_the_audit_tells_a_stored_null_from_no_row(
    store: _Store, clean_settings: None
) -> None:
    """Code review: ``repo.get`` returned None for both, so the audit named the file value."""
    field_info = AppConfig.model_fields["linear_personal_agent_label_id"]
    assert field_info.annotation == str | None
    parameter = "setting.linear_personal_agent_label_id"
    assert _client().put(_URL, json={"parameter": parameter, "value": None}).status_code == 200
    assert _client().put(_URL, json={"parameter": parameter, "value": "x"}).status_code == 200

    assert store.audit[1]["old_value"] == "not set"


# ---------------------------------------------------------------------------
# FRE-1753: a stored override with no audit row warns at startup
# ---------------------------------------------------------------------------

_AUDITED = "setting.fre1753_audited"
_UNAUDITED = "setting.fre1753_unaudited"
_CLEARED = "setting.fre1753_cleared"
_NO_AUDIT_EVENT = "setting_override_without_audit"
_AUDIT_FAILED_EVENT = "setting_override_audit_check_failed"


def _audit_row(parameter: str, action: str) -> dict[str, object]:
    return {
        "parameter": parameter,
        "action": action,
        "changed_by": uuid4(),
        "old_value": None,
        "new_value": None,
    }


def _warnings_named(entries: list[dict[str, object]], event: str) -> list[dict[str, object]]:
    return [entry for entry in entries if entry["event"] == event]


def _seed_three_overrides(store: _Store) -> None:
    """One audited row, one with no audit row, one whose latest audit row is a ``clear``."""
    store.overrides.update({_AUDITED: _SENTINEL, _UNAUDITED: _SENTINEL, _CLEARED: _SENTINEL})
    store.audit.extend(
        [
            _audit_row(_AUDITED, "set"),
            _audit_row(_CLEARED, "set"),
            _audit_row(_CLEARED, "clear"),
        ]
    )


@pytest.mark.asyncio
async def test_fre1753_ac1_each_unaudited_override_warns_once_and_an_audited_one_does_not(
    store: _Store, clean_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_three_overrides(store)
    monkeypatch.setattr(settings_edit, "AsyncSessionLocal", _FakeSessionFactory)

    with structlog.testing.capture_logs() as entries:
        await settings_edit.load_overrides_at_startup()

    warned = _warnings_named(entries, _NO_AUDIT_EVENT)
    assert sorted(str(entry["parameter"]) for entry in warned) == [_CLEARED, _UNAUDITED]
    assert all(entry["log_level"] == "warning" for entry in warned)
    assert all(entry["trace_id"] for entry in warned)


@pytest.mark.asyncio
async def test_fre1753_an_empty_override_set_reads_no_audit_row(
    store: _Store, clean_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the empty-set shortcut (no session, no query): it does not prove the warning."""
    monkeypatch.setattr(settings_edit, "AsyncSessionLocal", _FakeSessionFactory)

    with structlog.testing.capture_logs() as entries:
        await settings_edit.load_overrides_at_startup()

    assert store.audit_reads == 0
    assert _warnings_named(entries, _NO_AUDIT_EVENT) == []


@pytest.mark.asyncio
async def test_fre1753_ac2_the_warning_names_the_parameter_and_not_the_value(
    store: _Store, clean_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_three_overrides(store)
    monkeypatch.setattr(settings_edit, "AsyncSessionLocal", _FakeSessionFactory)

    with structlog.testing.capture_logs() as entries:
        await settings_edit.load_overrides_at_startup()

    assert _warnings_named(entries, _NO_AUDIT_EVENT), "the capture holds the warnings first"
    assert _SENTINEL not in str(entries)


@pytest.mark.asyncio
async def test_fre1753_ac3_a_failed_audit_read_logs_and_the_start_continues(
    store: _Store, clean_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_three_overrides(store)
    monkeypatch.setattr(settings_edit, "AsyncSessionLocal", _FakeSessionFactory)

    async def _fail(_self: object, _parameters: Collection[str]) -> list[str]:
        raise RuntimeError(f"audit table unreadable near {_SENTINEL}")

    monkeypatch.setattr(_FakeRepository, "unaudited_parameters", _fail)

    with structlog.testing.capture_logs() as entries:
        await settings_edit.load_overrides_at_startup()

    failed = _warnings_named(entries, _AUDIT_FAILED_EVENT)
    assert len(failed) == 1
    assert failed[0]["error_type"] == "RuntimeError"
    assert _SENTINEL not in str(entries)
    assert _warnings_named(entries, _NO_AUDIT_EVENT) == []
    assert set(current_overrides()) == {_AUDITED, _UNAUDITED, _CLEARED}


class _SecondUseFails:
    """A session factory: the first use (the load) works, and the second (the audit read) fails."""

    def __init__(self, where: str) -> None:
        self.where = where
        self.uses = 0

    def __call__(self) -> _SecondUseFails:
        self.uses += 1
        return self

    async def __aenter__(self) -> object:
        if self.where == "enter" and self.uses == 2:
            raise ConnectionError("cannot open a session")
        return object()

    async def __aexit__(self, *_args: object) -> None:
        if self.where == "exit" and self.uses == 2:
            raise ConnectionError("cannot close a session")


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["enter", "exit"])
async def test_fre1753_ac3_a_session_that_fails_to_open_or_close_does_not_stop_the_start(
    store: _Store, clean_settings: None, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    _seed_three_overrides(store)
    factory: Callable[[], object] = _SecondUseFails(where)
    monkeypatch.setattr(settings_edit, "AsyncSessionLocal", factory)

    with structlog.testing.capture_logs() as entries:
        await settings_edit.load_overrides_at_startup()

    assert len(_warnings_named(entries, _AUDIT_FAILED_EVENT)) == 1
    assert set(current_overrides()) == {_AUDITED, _UNAUDITED, _CLEARED}
