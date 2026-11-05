"""A stored deployment edit wins over the catalog file, and never raises (FRE-1724)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from personal_agent.config import model_loader
from personal_agent.config.model_loader import (
    ModelConfigError,
    file_deployment_fields,
    load_model_config,
    validate_deployment_override,
)
from personal_agent.config.setting_overrides import replace_overrides


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    replace_overrides({})
    yield
    replace_overrides({})


@pytest.fixture
def key() -> str:
    return next(iter(load_model_config().models))


def test_an_edit_wins_over_the_file_and_clear_restores(key: str) -> None:
    before = load_model_config().models[key].default_timeout

    replace_overrides({f"deployment.{key}": {"default_timeout": before + 11}})
    assert load_model_config().models[key].default_timeout == before + 11

    replace_overrides({})
    assert load_model_config().models[key].default_timeout == before


def test_a_mode_edit_reaches_the_catalog() -> None:
    config = load_model_config()
    key, definition = next((name, model) for name, model in config.models.items() if model.modes)
    mode = next(iter(definition.modes))
    modes = {
        name: spec.model_dump(mode="json", exclude_none=True)
        for name, spec in definition.modes.items()
    }
    modes[mode]["temperature"] = 0.25

    stored = validate_deployment_override(key, {"modes": modes})
    replace_overrides({f"deployment.{key}": stored})

    assert load_model_config().models[key].modes[mode].temperature == 0.25


def test_every_deployment_passes_its_own_file_fields() -> None:
    """The edit control starts from these fields, so an unchanged Save must pass."""
    for key in load_model_config().models:
        fields = file_deployment_fields(key)
        assert isinstance(fields, dict)
        assert "endpoint" not in fields
        # FRE-1733: an unchanged Save differs from the file in no field.
        assert validate_deployment_override(key, fields) == {}


def test_one_changed_field_of_the_whole_row_stores_one_field(key: str) -> None:
    """The PWA sends the whole row (FRE-1733 AC-4)."""
    fields = file_deployment_fields(key)
    assert isinstance(fields, dict)
    timeout = fields["default_timeout"]
    assert isinstance(timeout, int)

    stored = validate_deployment_override(key, {**fields, "default_timeout": timeout + 11})

    assert stored == {"default_timeout": timeout + 11}


def test_a_later_file_change_to_another_field_takes_effect(key: str) -> None:
    """A stored row holds the edited field only, so the file still owns the rest (FRE-1733 AC-4)."""
    fields = file_deployment_fields(key)
    assert isinstance(fields, dict)
    timeout, max_tokens = fields["default_timeout"], fields["max_tokens"]
    assert isinstance(timeout, int) and isinstance(max_tokens, int)
    stored = validate_deployment_override(key, {**fields, "default_timeout": timeout + 11})
    read = model_loader._read_catalog_content

    def _changed_file(path: Path) -> dict[str, object]:
        content = read(path)
        models = content["models"]
        assert isinstance(models, dict)
        return {**content, "models": {**models, key: {**models[key], "max_tokens": max_tokens + 1}}}

    def _forget_catalogs() -> None:
        model_loader._load_model_config_at_path.cache_clear()
        model_loader._load_model_config_with_overrides.cache_clear()

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(model_loader, "_read_catalog_content", _changed_file)
        _forget_catalogs()
        try:
            replace_overrides({f"deployment.{key}": stored})
            definition = load_model_config().models[key]
        finally:
            _forget_catalogs()

    assert definition.default_timeout == timeout + 11
    assert definition.max_tokens == max_tokens + 1


@pytest.mark.parametrize(
    "bad",
    [
        {"default_timeout": "soon"},
        {"default_timeout": True},
        {"default_timeout": 0},
        {"a_typo": 1},
        {"endpoint": "http://example.test"},
        {},
        "text",
    ],
)
def test_an_invalid_edit_is_refused(key: str, bad: object) -> None:
    with pytest.raises(ModelConfigError):
        validate_deployment_override(key, bad)  # type: ignore[arg-type]


def test_an_edit_that_the_catalog_refuses_is_refused() -> None:
    config = load_model_config()
    key = next(iter(config.models))

    with pytest.raises(ModelConfigError):
        validate_deployment_override(key, {"provider": "a_provider_the_catalog_does_not_hold"})


def test_an_unknown_deployment_is_refused() -> None:
    with pytest.raises(ModelConfigError):
        validate_deployment_override("a_key_the_catalog_does_not_hold", {"default_timeout": 1})


@pytest.mark.parametrize(
    "stale",
    [{"default_timeout": 0}, {"a_typo": 1}, {"endpoint": "http://example.test"}, "text"],
)
def test_a_stale_row_is_skipped_and_never_raises(key: str, stale: object) -> None:
    before = load_model_config().models[key]

    replace_overrides({f"deployment.{key}": stale})  # type: ignore[dict-item]

    assert load_model_config().models[key] == before


def test_a_row_for_a_removed_deployment_is_skipped() -> None:
    replace_overrides({"deployment.a_removed_key": {"default_timeout": 1}})

    assert "a_removed_key" not in load_model_config().models


def test_a_role_binding_and_a_deployment_edit_apply_together(key: str) -> None:
    before = load_model_config().models[key].default_timeout
    replace_overrides(
        {
            f"deployment.{key}": {"default_timeout": before + 1},
            "role.compressor": "inherit",
        }
    )

    config = load_model_config()

    assert config.models[key].default_timeout == before + 1
    assert config.roles["compressor"].deployment == "inherit"
