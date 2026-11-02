"""A stored override changes what a role resolves to, and never breaks a load (FRE-1722)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from personal_agent.config.model_loader import (
    ModelConfigError,
    file_role_deployments,
    load_model_config,
    resolve_role_model_key,
    resolve_role_target,
    resolve_selected_deployment,
    role_binding_options,
    validate_role_override,
)
from personal_agent.config.setting_overrides import (
    current_overrides,
    replace_overrides,
    role_overrides,
)

_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _no_overrides() -> Iterator[None]:
    replace_overrides({})
    yield
    replace_overrides({})


def _other_llm(role: str) -> str:
    """Return an llm deployment that the role does not bind in the file."""
    config = load_model_config()
    bound = config.roles[role].deployment
    return next(
        key for key, model in config.models.items() if model.kind.value == "llm" and key != bound
    )


def test_no_override_returns_the_cached_file_object() -> None:
    assert load_model_config().roles is load_model_config().roles
    assert role_overrides() == ()


def test_an_override_changes_every_role_reader() -> None:
    role = "compressor"
    target = _other_llm(role)
    file_key = resolve_role_target(role)[0]

    replace_overrides({f"role.{role}": target})

    assert load_model_config().roles[role].deployment == target
    assert resolve_role_target(role)[0] == target
    assert resolve_role_model_key(role) == target
    assert resolve_selected_deployment(role, None, load_model_config()) == target

    replace_overrides({})

    assert resolve_role_target(role)[0] == file_key
    assert resolve_role_model_key(role) == file_key


def test_an_explicit_real_root_sees_the_override() -> None:
    """Substrate resolution passes the repo root. It must agree with every other reader."""
    target = _other_llm("compressor")
    replace_overrides({"role.compressor": target})
    assert resolve_role_model_key("compressor", root=_ROOT) == target


def test_an_override_keeps_the_binding_budget_and_mode() -> None:
    role = "compressor"
    file_binding = load_model_config().roles[role]
    replace_overrides({f"role.{role}": _other_llm(role)})
    binding = load_model_config().roles[role]
    assert binding.max_tokens == file_binding.max_tokens
    assert binding.mode == file_binding.mode
    assert binding.open == file_binding.open


def test_inherit_resolves_to_the_primary() -> None:
    replace_overrides({"role.compressor": "inherit"})
    assert resolve_role_model_key("compressor") == load_model_config().roles["primary"].deployment


@pytest.mark.parametrize(
    "stored",
    [
        {"role.compressor": "no_such_deployment"},
        {"role.compressor": "embedding"},
        {"role.primary": "inherit"},
        {"role.no_such_role": "claude_sonnet"},
    ],
)
def test_a_stale_override_never_breaks_a_load_or_a_role_lookup(stored: dict[str, str]) -> None:
    """A deploy can remove a deployment. Every turn loads the catalog, so this must not raise."""
    file_key = resolve_role_model_key("compressor")
    replace_overrides(stored)
    config = load_model_config()
    assert config.roles["primary"].deployment != "inherit"
    assert resolve_role_model_key("compressor") == file_key


def test_a_stale_override_does_not_drop_a_usable_one() -> None:
    target = _other_llm("insights")
    replace_overrides({"role.compressor": "no_such_deployment", "role.insights": target})
    assert resolve_role_model_key("insights") == target


def test_a_fixture_catalog_never_sees_an_override() -> None:
    fixture = _ROOT / "tests/personal_agent/config/fixtures/role_model_key_inherit/config/models.yaml"
    baseline = load_model_config(fixture)
    replace_overrides({"role.compressor": "inherit"})
    assert load_model_config(fixture).roles == baseline.roles


def test_a_non_role_parameter_does_not_touch_the_catalog() -> None:
    replace_overrides({"effort.level.deep": {"sub_agent_rounds": 5}, "role.compressor": 5})
    assert role_overrides() == ()
    assert "effort.level.deep" in current_overrides()


def test_file_role_deployments_ignore_the_override() -> None:
    file_key = load_model_config().roles["compressor"].deployment
    replace_overrides({"role.compressor": _other_llm("compressor")})
    assert file_role_deployments()["compressor"] == file_key


@pytest.mark.parametrize(
    ("role", "deployment", "reason"),
    [
        ("compressor", "no_such_deployment", "not defined"),
        ("compressor", "embedding", "requires"),
        ("primary", "inherit", "inherit"),
        ("no_such_role", "claude_sonnet", "no_such_role"),
    ],
)
def test_validation_uses_the_loading_model_and_states_a_reason(
    role: str, deployment: str, reason: str
) -> None:
    with pytest.raises(ModelConfigError, match=reason):
        validate_role_override(role, deployment)


def test_validation_accepts_a_compatible_deployment() -> None:
    validate_role_override("compressor", _other_llm("compressor"))


def test_options_hold_only_compatible_available_deployments() -> None:
    config = load_model_config()
    options = role_binding_options("compressor", config, is_provider_available=lambda _p: True)
    assert "inherit" in options
    assert "embedding" not in options
    assert "inherit" not in role_binding_options(
        "primary", config, is_provider_available=lambda _p: True
    )
    assert role_binding_options("embedding", config, is_provider_available=lambda _p: True) == [
        "embedding"
    ]
    assert role_binding_options("compressor", config, is_provider_available=lambda _p: False) == [
        "inherit"
    ]
