"""Tests for the settings menu payload (FRE-1702, FRE-1722; FRE-1698).

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
from personal_agent.config.model_loader import file_role_deployments
from personal_agent.config.seshat_effort import load_seshat_effort
from personal_agent.config.settings import AppConfig
from personal_agent.cost_gate import load_budget_config
from personal_agent.cost_gate.policy import apply_cap_overrides
from personal_agent.orchestrator.constraint_options import (
    CONSTRAINT_OPTIONS,
    PREFERENCE_NEVER_APPLIED,
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


# ---------------------------------------------------------------------------
# FRE-1722: edit fields on role binding and saved choice rows
# ---------------------------------------------------------------------------


def _overridden_inputs(role: str, deployment: str, **overrides: object) -> MenuInputs:
    """Inputs with one stored override applied, as the gateway holds them after an edit."""
    from personal_agent.config.setting_overrides import replace_overrides

    file_bindings = file_role_deployments()
    replace_overrides({f"role.{role}": deployment})
    try:
        models = load_model_config()
    finally:
        replace_overrides({})
    return _inputs(
        models=models,
        file_bindings=file_bindings,
        overrides={f"role.{role}": deployment},
        **overrides,
    )


def _other_llm(role: str) -> str:
    config = load_model_config()
    bound = config.roles[role].deployment
    return next(
        key for key, model in config.models.items() if model.kind.value == "llm" and key != bound
    )


def test_owner_sees_a_control_and_the_file_default_on_a_role_row() -> None:
    bound = load_model_config().roles["compressor"].deployment
    row = _row(build_menu(_inputs(is_owner=True)), "models_per_role", "role.compressor")
    assert row.source == "config file"
    assert row.file_default is not None
    assert row.file_default.value == bound
    assert row.file_default.source == "config file"
    assert row.edit is not None
    assert row.edit.kind == "role_binding"
    assert row.edit.current == bound
    assert bound in row.edit.options
    assert "embedding" not in row.edit.options
    assert row.clearable is False


def test_a_non_owner_sees_the_file_default_but_no_role_control() -> None:
    row = _row(build_menu(_inputs(is_owner=False)), "models_per_role", "role.compressor")
    assert row.file_default is not None
    assert row.edit is None
    assert row.clearable is False


def test_an_applied_override_shows_as_the_winning_source_with_clear() -> None:
    target = _other_llm("compressor")
    file_key = load_model_config().roles["compressor"].deployment
    menu = build_menu(_overridden_inputs("compressor", target, is_owner=True))
    row = _row(menu, "models_per_role", "role.compressor")
    assert row.value == target
    assert row.source == "override"
    assert row.file_default is not None
    assert row.file_default.value == file_key
    assert row.clearable is True
    assert row.edit is not None
    assert row.edit.current == target


def test_a_non_owner_gets_no_clear_on_an_override() -> None:
    target = _other_llm("compressor")
    menu = build_menu(_overridden_inputs("compressor", target, is_owner=False))
    row = _row(menu, "models_per_role", "role.compressor")
    assert row.source == "override"
    assert row.clearable is False
    assert row.edit is None


def test_a_stale_override_says_so_and_can_be_cleared() -> None:
    stale = "no_such_deployment"
    menu = build_menu(
        _inputs(
            is_owner=True,
            overrides={"role.compressor": stale},
            file_bindings=file_role_deployments(),
        )
    )
    row = _row(menu, "models_per_role", "role.compressor")
    assert row.source == "config file"
    assert row.note is not None
    assert stale in row.note
    assert row.clearable is True


def test_a_session_selection_still_beats_an_edited_primary_and_says_so() -> None:
    """An open conversation stores its own primary. The edit reaches new conversations."""
    target = _other_llm("primary")
    session_key = next(
        key
        for key, model in load_model_config().models.items()
        if model.kind.value == "llm"
        and key not in (target, load_model_config().roles["primary"].deployment)
    )
    menu = build_menu(
        _overridden_inputs(
            "primary",
            target,
            is_owner=True,
            session=SessionInputs(selections={"primary": session_key}, effort_level=None),
        )
    )
    row = _row(menu, "models_per_role", "role.primary")
    assert row.value == session_key
    assert row.source == "session"
    assert row.overrides is not None
    assert row.overrides.value == target
    assert row.overrides.source == "override"
    assert row.note is not None
    assert "new conversations" in row.note
    session_row = _row(menu, "conversation", "session.primary_model")
    assert session_row.overrides is not None
    assert session_row.overrides.source == "override"


def test_a_saved_choice_still_shows_beside_an_edited_artifact_builder_binding() -> None:
    """A saved choice beats the binding. The row must show both (FRE-1698)."""
    target = _other_llm("artifact_builder")
    saved = next(
        key
        for key, model in load_model_config().models.items()
        if model.kind.value == "llm"
        and key not in (target, load_model_config().roles["artifact_builder"].deployment)
    )
    menu = build_menu(
        _overridden_inputs(
            "artifact_builder",
            target,
            is_owner=True,
            saved_choices={"artifact_builder": saved},
        )
    )
    row = _row(menu, "models_per_role", "role.artifact_builder")
    assert row.value == saved
    assert row.source == "database row"
    assert row.overrides is not None
    assert row.overrides.value == target
    assert row.overrides.source == "override"


def test_inherit_role_can_be_edited_and_shows_its_literal_binding() -> None:
    row = _row(build_menu(_inputs(is_owner=True)), "models_per_role", "role.sub_agent")
    assert row.edit is not None
    assert row.edit.current == "inherit"
    assert row.file_default is not None
    assert row.file_default.value == "inherit"


def test_an_inherit_row_reads_its_own_override_not_the_primary_override() -> None:
    own = build_menu(_overridden_inputs("compressor", "inherit", is_owner=True))
    row = _row(own, "models_per_role", "role.compressor")
    assert row.source == "override"
    assert row.clearable is True

    primary = build_menu(_overridden_inputs("primary", _other_llm("primary"), is_owner=True))
    row = _row(primary, "models_per_role", "role.sub_agent")
    assert row.source == "config file"
    assert row.clearable is False


def test_a_saved_choice_row_shows_a_control_and_its_default_to_every_user() -> None:
    row = _row(
        build_menu(_inputs(is_owner=False)), "saved_choices", "saved_choice.tool_iteration_limit"
    )
    assert row.value == "not set"
    assert row.file_default is not None
    assert "finish_now" in str(row.file_default.value)
    assert row.edit is not None
    assert row.edit.kind == "saved_choice"
    assert row.edit.options == ["continue_10", "finish_now", "always_pause"]
    assert row.edit.current is None


def test_a_saved_artifact_builder_row_shows_the_file_binding_as_its_default() -> None:
    target = _other_llm("artifact_builder")
    file_key = load_model_config().roles["artifact_builder"].deployment
    menu = build_menu(
        _overridden_inputs("artifact_builder", target, saved_choices={"artifact_builder": target})
    )
    row = _row(menu, "saved_choices", "saved_choice.artifact_builder")
    assert row.file_default is not None
    assert row.file_default.value == file_key
    assert row.edit is not None
    assert row.edit.current == target
    assert "always_pause" in row.edit.options
    assert row.overrides is not None
    assert row.overrides.source == "override"


@pytest.mark.parametrize("constraint", sorted(PREFERENCE_NEVER_APPLIED))
def test_a_choice_that_the_turn_never_applies_has_no_control(constraint: str) -> None:
    row = _row(build_menu(_inputs()), "saved_choices", f"saved_choice.{constraint}")
    assert row.edit is None
    assert row.file_default is not None


def test_a_group_builder_keeps_working_with_no_catalog_for_saved_choices() -> None:
    menu = build_menu(_inputs(models=None))
    group = next(g for g in menu.groups if g.id == "saved_choices")
    assert group.error is None
    assert _row(menu, "saved_choices", "saved_choice.artifact_builder").edit is None


# --- FRE-1723: effort presets and budget caps ------------------------------------------------


_DEEP = "effort.level.deep"
_PRESET = {
    "expansion": True,
    "sub_agent_rounds": 3,
    "planner_policy": "baseline",
    "primary_mode": None,
}


def _stored_effort(overrides: dict[str, object]) -> dict[str, object]:
    """Return inputs as the gateway builds them while these overrides are stored."""
    file_effort = load_seshat_effort()
    levels = dict(file_effort.levels)
    default_level = file_effort.default_level
    if _DEEP in overrides:
        levels["deep"] = levels["deep"].model_copy(update=dict(_PRESET))
    if "effort.default_level" in overrides:
        default_level = overrides["effort.default_level"]  # type: ignore[assignment]
    return {
        "effort": file_effort.model_copy(update={"levels": levels, "default_level": default_level}),
        "file_effort": file_effort,
        "overrides": overrides,
    }


def test_owner_sees_a_control_and_the_file_default_on_a_preset_row() -> None:
    menu = build_menu(_inputs(file_effort=load_seshat_effort(), is_owner=True))
    row = _row(menu, "effort_and_modes", _DEEP)
    assert row.source == "config file"
    assert row.edit is not None and row.edit.kind == "effort_preset"
    assert row.edit.preset_choices == {
        "planner_policy": ["baseline", "p2"],
        "primary_mode": ["fast", "deep"],
    }
    assert row.file_default is not None and row.file_default.value == row.value
    assert row.clearable is False


def test_a_non_owner_sees_no_control_on_a_preset_a_default_level_or_a_cap() -> None:
    menu = build_menu(_inputs(**_stored_effort({_DEEP: _PRESET})))
    for group, name in (
        ("effort_and_modes", _DEEP),
        ("effort_and_modes", "effort.default_level"),
        ("budgets", "cap.weekly._total"),
    ):
        row = _row(menu, group, name)
        assert row.edit is None
        assert row.clearable is False
    assert _row(menu, "effort_and_modes", _DEEP).source == "override"


def test_a_stored_preset_shows_the_live_value_the_file_default_and_clear() -> None:
    menu = build_menu(_inputs(**_stored_effort({_DEEP: _PRESET}), is_owner=True))
    row = _row(menu, "effort_and_modes", _DEEP)
    assert row.value == _PRESET
    assert row.source == "override"
    assert row.clearable is True
    assert row.file_default is not None
    assert row.file_default.value == load_seshat_effort().levels["deep"].model_dump(
        mode="json", exclude={"level"}
    )


def test_a_stale_preset_says_so_and_can_be_cleared() -> None:
    stale = {**_PRESET, "sub_agent_rounds": 100000}
    menu = build_menu(
        _inputs(file_effort=load_seshat_effort(), overrides={_DEEP: stale}, is_owner=True)
    )
    row = _row(menu, "effort_and_modes", _DEEP)
    assert row.source == "config file"
    assert row.clearable is True
    assert row.note is not None and "not usable" in row.note


def test_the_default_level_rows_name_the_d7_routing_change_and_other_levels_do_not() -> None:
    menu = build_menu(_inputs(file_effort=load_seshat_effort(), is_owner=True))
    default_level = load_seshat_effort().default_level
    other = next(level for level in load_seshat_effort().levels if level != default_level)
    assert "D7" in (_row(menu, "effort_and_modes", f"effort.level.{default_level}").note or "")
    assert _row(menu, "effort_and_modes", f"effort.level.{other}").note is None
    row = _row(menu, "effort_and_modes", "effort.default_level")
    assert "D7" in (row.note or "")
    assert "new conversations" in (row.note or "")
    assert row.edit is not None and row.edit.kind == "effort_default_level"
    assert row.edit.options == ["fast", "standard", "deep"]
    assert row.edit.current == default_level


def test_a_stored_default_level_shows_its_source_and_the_conversation_row_names_it() -> None:
    menu = build_menu(
        _inputs(
            **_stored_effort({"effort.default_level": "deep"}),
            session=SessionInputs(selections={}, effort_level="fast"),
            is_owner=True,
        )
    )
    row = _row(menu, "effort_and_modes", "effort.default_level")
    assert (row.value, row.source, row.clearable) == ("deep", "override", True)
    conversation = _row(menu, "conversation", "session.effort_level")
    assert conversation.overrides is not None
    assert conversation.overrides.value == "deep"
    assert conversation.overrides.source == "override"


def _budgets() -> dict[str, object]:
    file_budget = load_budget_config("config/governance/budget.yaml.example")
    return {"budget": file_budget, "file_budget": file_budget}


def test_owner_sees_a_number_control_on_a_cap_row() -> None:
    menu = build_menu(_inputs(**_budgets(), is_owner=True))
    row = _row(menu, "budgets", "cap.weekly._total")
    assert row.edit is not None and row.edit.kind == "budget_cap"
    assert row.edit.current == str(row.value)
    assert row.file_default is not None and row.file_default.value == row.value
    assert row.clearable is False


def test_a_stored_cap_shows_the_enforced_value_the_file_default_and_clear() -> None:
    file_budget = load_budget_config("config/governance/budget.yaml.example")
    parameter = "cap.weekly._total"
    file_cap = _row(build_menu(_inputs(budget=file_budget)), "budgets", parameter).value
    overrides = {parameter: 0.5}
    menu = build_menu(
        _inputs(
            budget=apply_cap_overrides(file_budget, overrides),
            file_budget=file_budget,
            overrides=overrides,
            is_owner=True,
        )
    )
    row = _row(menu, "budgets", parameter)
    assert (row.value, row.source, row.clearable) == (0.5, "override", True)
    assert row.file_default is not None and row.file_default.value == file_cap


def test_a_stale_cap_says_so_and_can_be_cleared() -> None:
    parameter = "cap.weekly._total"
    menu = build_menu(_inputs(**_budgets(), overrides={parameter: "bad"}, is_owner=True))
    row = _row(menu, "budgets", parameter)
    assert row.source == "config file"
    assert row.clearable is True
    assert row.note is not None and "not usable" in row.note


def test_uncapped_roles_stays_read_only_with_its_reason_for_the_owner() -> None:
    menu = build_menu(_inputs(**_budgets(), is_owner=True))
    row = _row(menu, "budgets", "uncapped_roles")
    assert row.edit is None
    assert row.applies == "read_only"
    assert row.read_only_reason


def test_fre1724_a_role_estimate_has_a_json_control_for_the_owner_only() -> None:
    owner = build_menu(_inputs(**_budgets(), is_owner=True))
    other = build_menu(_inputs(**_budgets()))
    role_row = next(r for r in _rows(owner) if r.name.startswith("role_estimate."))

    assert role_row.edit is not None and role_row.edit.input == "json"
    assert role_row.applies == "next_turn"
    assert next(r for r in _rows(other) if r.name == role_row.name).edit is None


# ---------------------------------------------------------------------------
# FRE-1724: every row has a class, and every editable setting has a control
# ---------------------------------------------------------------------------


def test_fre1724_ac2_every_menu_row_has_a_class() -> None:
    """``_with_class`` raises on a row with no entry, which makes the group unavailable."""
    from personal_agent.config import load_governance_config

    menu = build_menu(_inputs(**_budgets(), governance=load_governance_config(), is_owner=True))

    assert [g.id for g in menu.groups if g.error] == []
    assert menu.system.error is None
    for row in _rows(menu):
        assert row.applies in {"next_turn", "next_restart", "read_only"}, row.name
        if row.applies == "read_only":
            assert row.read_only_reason, row.name
            assert row.edit is None, row.name


def test_fre1724_ac2_a_row_family_with_no_entry_is_unavailable_not_silently_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from personal_agent.service import settings_menu

    monkeypatch.setattr(
        settings_menu,
        "_group_system",
        lambda _inp: [
            settings_menu.SettingRow(name="a_new_family.x", value=1, source="config file")
        ],
    )

    assert build_menu(_inputs()).system.error == "unavailable"


def test_fre1724_a_read_only_setting_shows_its_reason_and_no_control() -> None:
    menu = build_menu(_inputs(is_owner=True))
    row = next(r for r in _rows(menu) if r.name == "setting.openai_api_key")

    assert row.applies == "read_only"
    assert row.read_only_reason
    assert row.edit is None
    assert row.value in ("set", "not set")
    assert row.file_default is None


def test_fre1724_a_next_turn_setting_has_a_control_and_a_default_for_the_owner_only() -> None:
    owner = next(
        r
        for r in _rows(build_menu(_inputs(is_owner=True)))
        if r.name == "setting.orchestrator_max_tool_iterations"
    )
    other = next(
        r
        for r in _rows(build_menu(_inputs()))
        if r.name == "setting.orchestrator_max_tool_iterations"
    )

    assert owner.edit is not None
    assert owner.edit.kind == "setting"
    assert owner.edit.input == "number"
    assert owner.file_default is not None
    assert owner.applies == "next_turn"
    assert other.edit is None
    assert other.file_default is None


def test_fre1724_a_next_restart_setting_says_so() -> None:
    row = next(
        r
        for r in _rows(build_menu(_inputs(is_owner=True)))
        if r.name == "setting.mode_evaluation_interval_seconds"
    )

    assert row.applies == "next_restart"
    assert row.note == "applies at the next gateway restart"


def test_fre1724_a_pending_restart_edit_shows_the_live_value_and_the_stored_value() -> None:
    from personal_agent.config import settings as live_settings

    live = live_settings.mode_evaluation_interval_seconds
    menu = build_menu(
        _inputs(is_owner=True, overrides={"setting.mode_evaluation_interval_seconds": live + 5})
    )
    row = next(r for r in _rows(menu) if r.name == "setting.mode_evaluation_interval_seconds")

    assert row.value == live
    assert row.source != "override"
    assert row.clearable
    assert str(live + 5) in (row.note or "")


def test_fre1724_an_applied_setting_shows_override_and_the_value_it_overrides() -> None:
    from personal_agent.config import settings as live_settings

    live = live_settings.orchestrator_max_tool_iterations
    menu = build_menu(
        _inputs(
            is_owner=True,
            overrides={"setting.orchestrator_max_tool_iterations": live},
            applied_settings={"orchestrator_max_tool_iterations": live + 1},
        )
    )
    row = next(r for r in _rows(menu) if r.name == "setting.orchestrator_max_tool_iterations")

    assert row.source == "override"
    assert row.clearable
    assert row.file_default is not None and row.file_default.value == live + 1


def test_fre1724_a_hidden_editable_setting_is_write_only() -> None:
    from personal_agent.config import settings as live_settings

    menu = build_menu(
        _inputs(
            is_owner=True,
            overrides={"setting.searxng_base_url": "http://sentinel-host.test"},
        )
    )
    row = next(r for r in _rows(menu) if r.name == "setting.searxng_base_url")

    assert row.applies == "next_turn"
    assert row.value == ("set" if live_settings.searxng_base_url else "not set")
    assert row.edit is not None and row.edit.current is None
    assert "sentinel-host" not in row.model_dump_json()


@pytest.mark.parametrize(
    ("field", "control"),
    [
        ("expansion_enabled", "boolean"),
        ("llm_timeout_seconds", "number"),
        ("searxng_default_categories", "text"),
        ("grounding_verification_mode", "select"),
        ("tool_result_digest_exclude_tools", "json"),
    ],
)
def test_fre1724_the_control_follows_the_annotation(field: str, control: str) -> None:
    row = next(r for r in _rows(build_menu(_inputs(is_owner=True))) if r.name == f"setting.{field}")

    assert row.edit is not None
    assert row.edit.input == control


def test_fre1724_a_role_with_one_option_is_read_only() -> None:
    """Master's note on the ticket: a control with one choice does nothing."""
    menu = build_menu(_inputs(is_owner=True))
    role = next(r for r in _rows(menu) if r.name == "role.embedding")

    assert role.applies == "read_only"
    assert role.read_only_reason
    assert role.edit is None


def test_fre1724_governance_and_deployment_rows_have_a_json_control() -> None:
    from personal_agent.config import load_governance_config

    menu = build_menu(_inputs(governance=load_governance_config(), is_owner=True))
    names = {r.name: r for r in _rows(menu)}

    for name in ("sub_agent_tools", "mode_transition_rules"):
        assert names[name].edit is not None, name
    tool_row = next(r for n, r in names.items() if n.startswith("tool."))
    deployment_row = next(r for n, r in names.items() if n.startswith("deployment."))

    assert tool_row.edit is not None and "allowed_paths" not in (tool_row.edit.current or "")
    assert isinstance(tool_row.value, dict) and isinstance(tool_row.value["allowed_paths"], int)
    assert deployment_row.edit is not None
    assert "endpoint" not in (deployment_row.edit.current or "")
    assert "max_concurrency" in (deployment_row.note or "")


def test_fre1724_a_json_control_with_a_path_in_it_is_dropped_and_not_masked() -> None:
    from personal_agent.service import settings_menu

    row = settings_menu.SettingRow(
        name="sub_agent_tools",
        value=1,
        source="config file",
        edit=settings_menu.SettingEdit(
            kind="setting", options=[], current='{"a": "http://host"}', input="json"
        ),
    )

    scrubbed = settings_menu._scrub_row(row)

    assert scrubbed.edit is None
    assert "stays in the file" in (scrubbed.note or "")


def test_fre1724_session_rows_write_through_the_session_routes_when_a_conversation_is_open() -> (
    None
):
    session = SessionInputs(selections={}, effort_level=None)
    open_menu = build_menu(_inputs(session=session))
    closed_menu = build_menu(_inputs())

    assert _row(open_menu, "conversation", "session.effort_level").edit.kind == "session_effort"  # type: ignore[union-attr]
    assert _row(open_menu, "conversation", "session.primary_model").edit.kind == "session_primary"  # type: ignore[union-attr]
    for name in ("session.effort_level", "session.primary_model"):
        row = _row(closed_menu, "conversation", name)
        assert row.edit is None
        assert row.applies == "read_only"


def test_fre1724_location_consent_has_a_control_only_when_the_feature_is_on() -> None:
    from personal_agent.config import settings as live_settings

    on = live_settings.model_copy(update={"location_enabled": True})
    off = live_settings.model_copy(update={"location_enabled": False})

    row = _row(
        build_menu(_inputs(app_settings=on, location_consent=False)),
        "saved_choices",
        "location.consent",
    )
    assert row.edit is not None and row.edit.kind == "location_consent"
    assert row.edit.current == "false"

    row = _row(build_menu(_inputs(app_settings=off)), "saved_choices", "location.consent")
    assert row.edit is None
    assert row.applies == "read_only"
