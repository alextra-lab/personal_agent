"""Tests for the read-only settings menu payload (FRE-1702, stage 1 of FRE-1698).

Proves AC-2 (a saved choice shows beside the default it overrides), AC-4 (no response
carries a secret or infrastructure value) and the backend half of AC-5 (only a saved
choice is clearable), plus the shape the PWA renders for AC-1.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest

from personal_agent.config import (
    load_governance_config,
    load_model_config,
    settings,
)
from personal_agent.config.seshat_effort import load_seshat_effort
from personal_agent.config.settings import AppConfig
from personal_agent.cost_gate import load_budget_config
from personal_agent.orchestrator.constraint_options import (
    CONSTRAINT_OPTIONS,
    artifact_builder_default_key,
    default_action_id,
)
from personal_agent.service import settings_menu
from personal_agent.service.settings_menu import (
    MenuInputs,
    SessionInputs,
    SettingRow,
    SettingsMenu,
    build_menu,
    scrub,
)
from personal_agent.service.settings_menu_groups import SETTING_GROUPS

_SENTINEL = "SENTINEL-9f3c1e-do-not-leak"

_GROUP_IDS = [
    "conversation",
    "saved_choices",
    "models_per_role",
    "effort_and_modes",
    "budgets",
    "turn_runs",
    "memory",
    "tools",
    "grounding",
]


def _inputs(**overrides: object) -> MenuInputs:
    base: dict[str, object] = {
        "app_settings": settings,
        "models": load_model_config(),
        "effort": load_seshat_effort(),
        "governance": load_governance_config(),
        "budget": load_budget_config("config/governance/budget.yaml.example"),
        "saved_choices": {},
        "session": None,
        "location_consent": None,
        "grounding": [],
        "provider_available": lambda _provider: True,
    }
    base.update(overrides)
    return MenuInputs(**base)  # type: ignore[arg-type]


def _rows(menu: SettingsMenu) -> Iterator[SettingRow]:
    for group in [*menu.groups, menu.system]:
        yield from group.rows


def _row(menu: SettingsMenu, group_id: str, name: str) -> SettingRow:
    group = next(g for g in [*menu.groups, menu.system] if g.id == group_id)
    return next(r for r in group.rows if r.name == name)


def _leaves(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _leaves(v)
    elif isinstance(value, list):
        for v in value:
            yield from _leaves(v)


# --- the group table -------------------------------------------------------------------


def test_every_setting_has_a_group() -> None:
    """A new AppConfig field fails here until it is placed in a menu group."""
    assert set(AppConfig.model_fields) == set(SETTING_GROUPS)


def test_default_factory_fields_are_known() -> None:
    """Only `environment` reads the process environment in its default factory.

    The `.env` source check relies on `model_fields_set`, which a default factory
    bypasses. A new default-factory field must be reviewed against that.
    """
    factory_fields = {n for n, f in AppConfig.model_fields.items() if f.default_factory}
    assert factory_fields == {
        "environment",
        "service_request_task_lifetime_seconds",
        "sub_agent_rounds_by_thoroughness",
        "mcp_gateway_command",
        "mcp_gateway_enabled_servers",
        "tool_result_digest_exclude_tools",
        "url_guard_allowlist",
        "freshness_tier_factors",
    }
    assert settings_menu.ENV_DRIVEN_FIELDS <= factory_fields


# --- shape (AC-1 backend half) ---------------------------------------------------------


def test_nine_groups_then_system_in_order() -> None:
    menu = build_menu(_inputs())
    assert [g.id for g in menu.groups] == _GROUP_IDS
    assert menu.system.id == "system"
    assert all(g.title for g in [*menu.groups, menu.system])


def test_every_row_has_a_name_a_value_and_a_source() -> None:
    menu = build_menu(_inputs())
    rows = list(_rows(menu))
    assert len(rows) > 300
    for row in rows:
        assert row.name
        assert row.source in {"code default", ".env", "config file", "database row", "session"}
        assert row.value is not None or row.name  # None is a real value for an unset option


def test_every_app_setting_appears_exactly_once() -> None:
    names = [r.name for r in _rows(build_menu(_inputs()))]
    for setting_name in AppConfig.model_fields:
        assert names.count(f"setting.{setting_name}") == 1, setting_name


def test_env_source_and_the_default_it_overrides() -> None:
    changed = settings.model_copy(update={"conversation_max_history_messages": 7})
    row = _row(
        build_menu(_inputs(app_settings=changed)),
        "turn_runs",
        "setting.conversation_max_history_messages",
    )
    assert row.value == 7
    assert row.source == ".env"
    assert row.overrides is not None and row.overrides.source == "code default"
    assert row.overrides.value != 7


def test_environment_is_labelled_env_driven() -> None:
    row = _row(build_menu(_inputs()), "system", "setting.environment")
    assert row.source == ".env"


# --- AC-2: a saved choice shows beside the default it overrides ------------------------


def test_saved_choice_shows_beside_default() -> None:
    models = load_model_config()
    default_key = artifact_builder_default_key(models)
    saved_key = next(
        k for k in models.models if k != default_key and models.models[k].kind.value == "llm"
    )
    menu = build_menu(
        _inputs(saved_choices={"artifact_builder": saved_key, "tool_iteration_limit": "finish_now"})
    )

    builder = _row(menu, "saved_choices", "saved_choice.artifact_builder")
    assert builder.value == saved_key
    assert builder.source == "database row"
    assert builder.clearable is True
    assert builder.overrides is not None
    assert builder.overrides.value == default_key
    assert builder.overrides.source == "config file"

    static = _row(menu, "saved_choices", "saved_choice.tool_iteration_limit")
    assert static.value == "finish_now"
    assert static.source == "database row"
    assert static.overrides is not None
    assert default_action_id("tool_iteration_limit") in str(static.overrides.value)


def test_unsaved_choice_is_not_set_and_not_clearable() -> None:
    menu = build_menu(_inputs())
    for name in [*CONSTRAINT_OPTIONS, "artifact_builder"]:
        row = _row(menu, "saved_choices", f"saved_choice.{name}")
        assert row.value == "not set"
        assert row.clearable is False


def test_unreadable_saved_choices_are_unavailable_not_not_set() -> None:
    """A failed read must not show a saved choice as absent, with its Clear button hidden."""
    menu = build_menu(_inputs(saved_choices=None))
    group = next(g for g in menu.groups if g.id == "saved_choices")
    assert group.error == "unavailable" and group.rows == []
    row = _row(menu, "models_per_role", "role.artifact_builder")
    assert row.note is not None and "saved choices could not be read" in row.note


def test_choices_that_the_turn_never_applies_say_so() -> None:
    """`attachment_cost` and `spend_threshold` pause with allow_preference=False."""
    menu = build_menu(
        _inputs(
            saved_choices={"attachment_cost": "proceed_cloud", "spend_threshold": "continue_turn"}
        )
    )
    for name in ("attachment_cost", "spend_threshold"):
        row = _row(menu, "saved_choices", f"saved_choice.{name}")
        assert row.note is not None and "always asks" in row.note
        assert row.clearable is True


# --- AC-5 (backend half): only a saved choice is clearable -----------------------------


def test_only_saved_choices_are_clearable() -> None:
    menu = build_menu(
        _inputs(saved_choices={"artifact_builder": "gpt-6.1-sol", "spend_threshold": "answer_now"})
    )
    clearable = {r.name for r in _rows(menu) if r.clearable}
    assert clearable == {"saved_choice.artifact_builder", "saved_choice.spend_threshold"}


# --- models per role ------------------------------------------------------------------


def test_artifact_builder_role_shows_the_saved_choice_over_the_yaml_binding() -> None:
    """The failure that started FRE-1698: Observe showed the YAML default, not the saved row."""
    models = load_model_config()
    default_key = artifact_builder_default_key(models)
    saved_key = next(
        k for k in models.models if k != default_key and models.models[k].kind.value == "llm"
    )
    row = _row(
        build_menu(_inputs(saved_choices={"artifact_builder": saved_key})),
        "models_per_role",
        "role.artifact_builder",
    )
    assert row.value == saved_key
    assert row.source == "database row"
    assert row.overrides is not None and row.overrides.value == default_key


def test_artifact_builder_role_uses_the_default_when_the_saved_provider_is_unavailable() -> None:
    """The group shows what the turn would use, not the raw row."""
    models = load_model_config()
    default_key = artifact_builder_default_key(models)
    saved_key = next(
        k for k in models.models if k != default_key and models.models[k].kind.value == "llm"
    )
    row = _row(
        build_menu(
            _inputs(
                saved_choices={"artifact_builder": saved_key}, provider_available=lambda _p: False
            )
        ),
        "models_per_role",
        "role.artifact_builder",
    )
    assert row.value == default_key
    assert row.source == "config file"
    assert row.note is not None and saved_key in row.note


def test_primary_model_follows_the_session_selection() -> None:
    models = load_model_config()
    primary_default = models.roles["primary"].deployment
    other = next(
        k for k in models.models if k != primary_default and models.models[k].kind.value == "llm"
    )
    menu = build_menu(
        _inputs(session=SessionInputs(selections={"primary": other}, effort_level="deep"))
    )
    primary = _row(menu, "conversation", "session.primary_model")
    assert primary.value == other
    assert primary.source == "session"
    assert primary.overrides is not None and primary.overrides.value == primary_default
    effort = _row(menu, "conversation", "session.effort_level")
    assert effort.value == "deep" and effort.source == "session"
    # A selection for a role the turn does not read is not applied (F9).
    sub = _row(
        build_menu(
            _inputs(session=SessionInputs(selections={"sub_agent": other}, effort_level=None))
        ),
        "models_per_role",
        "role.sub_agent",
    )
    assert sub.source == "config file"


def test_inherit_role_shows_the_sessions_primary() -> None:
    """FRE-1702 AC-6 — sub_agent inherits the session's primary, as the config API shows it."""
    models = load_model_config()
    primary_default = models.roles["primary"].deployment
    other = next(
        k for k in models.models if k != primary_default and models.models[k].kind.value == "llm"
    )
    menu = build_menu(
        _inputs(session=SessionInputs(selections={"primary": other}, effort_level=None))
    )
    sub = _row(menu, "models_per_role", "role.sub_agent")
    assert sub.value == other
    assert sub.source == "session"


def test_without_a_session_the_conversation_group_shows_defaults() -> None:
    menu = build_menu(_inputs())
    assert _row(menu, "conversation", "session.primary_model").source == "config file"
    assert _row(menu, "conversation", "session.effort_level").source == "config file"


# --- budgets read the running gate's config ---------------------------------------------


def test_budget_caps_come_from_the_gate_config() -> None:
    menu = build_menu(_inputs())
    cap = _row(menu, "budgets", "cap.weekly._total")
    assert isinstance(cap.value, float) and cap.value > 0
    assert cap.source == "config file"


def test_budget_group_is_unavailable_when_the_gate_is_not_started() -> None:
    menu = build_menu(_inputs(budget=None))
    budgets = next(g for g in menu.groups if g.id == "budgets")
    assert budgets.error == "unavailable"
    # The other groups still render.
    assert next(g for g in menu.groups if g.id == "memory").rows


# --- AC-4: no value of a secret, infrastructure or path-bearing setting ------------------


def test_secret_row_is_exactly_set() -> None:
    changed = settings.model_copy(update={"anthropic_api_key": _SENTINEL})
    menu = build_menu(_inputs(app_settings=changed))
    assert _row(menu, "system", "setting.anthropic_api_key").value == "set"
    assert _SENTINEL not in menu.model_dump_json()


def test_unset_secret_row_is_not_set() -> None:
    changed = settings.model_copy(update={"anthropic_api_key": None})
    assert (
        _row(build_menu(_inputs(app_settings=changed)), "system", "setting.anthropic_api_key").value
        == "not set"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("database_url", f"postgresql://u:{_SENTINEL}@h/db"),
        ("elasticsearch_url", f"http://{_SENTINEL}:9200"),
        ("slm_health_url", f"http://{_SENTINEL}/health"),
        ("url_guard_allowlist", [f"{_SENTINEL}.example"]),
        ("owner_name", _SENTINEL),
        ("user_display_names_json", f'{{"a": "{_SENTINEL}"}}'),
    ],
)
def test_no_sentinel_for_infrastructure_or_identity_settings(field: str, value: object) -> None:
    changed = settings.model_copy(update={field: value})
    menu = build_menu(_inputs(app_settings=changed))
    row = _row(menu, settings_menu.menu_group_of(field), f"setting.{field}")
    assert row.value == "set"
    assert _SENTINEL not in menu.model_dump_json()


def test_model_endpoint_is_never_returned() -> None:
    models = load_model_config()
    key = next(k for k, m in models.models.items() if m.endpoint)
    patched = models.model_copy(
        update={
            "models": {
                **models.models,
                key: models.models[key].model_copy(update={"endpoint": f"http://{_SENTINEL}"}),
            }
        }
    )
    menu = build_menu(_inputs(models=patched))
    row = _row(menu, "effort_and_modes", f"deployment.{key}")
    assert row.value["endpoint"] == "set"  # type: ignore[index]
    assert _SENTINEL not in menu.model_dump_json()


def test_governance_paths_are_counts_not_values() -> None:
    governance = load_governance_config()
    name, policy = next(
        (n, p) for n, p in governance.tools.items() if p.allowed_paths or p.forbidden_paths
    )
    patched_policy = policy.model_copy(
        update={"allowed_paths": [f"/opt/{_SENTINEL}/**"], "forbidden_paths": [_SENTINEL]}
    )
    patched = governance.model_copy(update={"tools": {**governance.tools, name: patched_policy}})
    menu = build_menu(_inputs(governance=patched))
    row = _row(menu, "tools", f"tool.{name}")
    assert row.value["allowed_paths"] == 1  # type: ignore[index]
    assert row.value["forbidden_paths"] == 1  # type: ignore[index]
    assert _SENTINEL not in menu.model_dump_json()


def test_no_tool_row_shows_a_deleted_policy_field() -> None:
    """FRE-1719: the five ToolPolicy fields that nothing read are gone from every tool row."""
    deleted = {
        "allowed_commands",
        "forbidden_commands",
        "requires_outbound_gateway",
        "rate_limit_per_hour",
        "category",
    }
    menu = build_menu(_inputs(governance=load_governance_config()))
    tool_rows = [
        r for g in menu.groups if g.id == "tools" for r in g.rows if r.name.startswith("tool.")
    ]
    assert tool_rows
    for row in tool_rows:
        assert deleted.isdisjoint(row.value), (row.name, deleted & set(row.value))  # type: ignore[arg-type]


def test_a_loader_error_text_never_reaches_the_response(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_inputs: MenuInputs) -> list[SettingRow]:
        raise ValueError(f"cannot read /opt/seshat/config: {_SENTINEL}")

    monkeypatch.setattr(settings_menu, "_group_models_per_role", _boom)
    menu = build_menu(_inputs())
    group = next(g for g in menu.groups if g.id == "models_per_role")
    assert group.error == "unavailable" and group.rows == []
    assert _SENTINEL not in menu.model_dump_json()
    assert next(g for g in menu.groups if g.id == "memory").rows


def test_sweep_no_url_or_path_leaf_in_the_live_menu() -> None:
    """Built from the live AppConfig and the committed YAML, no leaf is a URL or a path."""
    menu = build_menu(_inputs())
    bad = re.compile(r"://|/opt/|/app/|\.ssh|^~|^\$|\*\*")
    offenders = [
        (row.name, leaf)
        for row in _rows(menu)
        for leaf in [
            *_leaves(row.value),
            *(_leaves(row.overrides.value) if row.overrides else []),
            *([row.note] if row.note else []),
        ]
        if bad.search(leaf)
    ]
    assert offenders == []


# --- the scrubber ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "leaf",
    [
        "http://host/x",
        "postgresql://u:p@h/db",
        "/opt/seshat/x",
        "/app/.venv/bin",
        "~/.ssh/id",
        "$HOME/.ssh/**",
        "src/**/*.py",
    ],
)
def test_scrub_replaces_urls_and_paths(leaf: str) -> None:
    assert scrub({"a": [leaf, {"b": leaf}]}) == {"a": ["set", {"b": "set"}]}


@pytest.mark.parametrize(
    "leaf", ["/no_think", "claude_sonnet", "qwen3.8-flash-next", "p2", "a b c"]
)
def test_scrub_keeps_plain_values(leaf: str) -> None:
    assert scrub(leaf) == leaf
    assert scrub(7) == 7
    assert scrub(None) is None
