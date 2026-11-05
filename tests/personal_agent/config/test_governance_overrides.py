"""A stored governance row wins, and it never raises on a tool call (FRE-1724)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from personal_agent.brainstem.mode_manager import ModeManager
from personal_agent.config.governance_loader import (
    TOOL_PATH_FIELDS,
    GovernanceConfigError,
    LiveGovernanceConfig,
    apply_governance_overrides,
    file_governance_config,
    load_governance_config,
    validate_governance_override,
)
from personal_agent.config.setting_overrides import replace_overrides
from personal_agent.orchestrator import executor as orchestrator_executor
from personal_agent.tools.executor import ToolExecutionLayer
from personal_agent.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    replace_overrides({})
    yield
    replace_overrides({})


@pytest.fixture
def tool_name() -> str:
    return next(iter(file_governance_config().tools))


def test_a_tool_edit_wins_over_the_file_and_clear_restores(tool_name: str) -> None:
    file_value = file_governance_config().tools[tool_name].loop_max_consecutive

    replace_overrides({f"tool.{tool_name}": {"loop_max_consecutive": file_value + 3}})
    assert load_governance_config().tools[tool_name].loop_max_consecutive == file_value + 3
    assert file_governance_config().tools[tool_name].loop_max_consecutive == file_value

    replace_overrides({})
    assert load_governance_config().tools[tool_name].loop_max_consecutive == file_value


def test_an_explicit_directory_holds_no_override(tool_name: str) -> None:
    from personal_agent.config import settings

    directory = settings.governance_config_path
    file_value = file_governance_config().tools[tool_name].loop_max_consecutive
    replace_overrides({f"tool.{tool_name}": {"loop_max_consecutive": file_value + 3}})

    config = load_governance_config(directory)

    assert config.tools[tool_name].loop_max_consecutive == file_value


def test_the_whole_map_rows_win_over_the_file() -> None:
    file_config = file_governance_config()
    name = next(iter(file_config.sub_agent_tools))
    decisions = {
        name: {"granted": not file_config.sub_agent_tools[name].granted, "reason": "owner edit"}
    }
    rule_name = next(iter(file_config.transition_rules))
    rules = {
        rule_name: file_config.transition_rules[rule_name].model_dump(mode="json")
        | {"logic": "all"}
    }

    replace_overrides({"sub_agent_tools": decisions, "mode_transition_rules": rules})
    config = load_governance_config()

    assert set(config.sub_agent_tools) == {name}
    assert config.sub_agent_tools[name].reason == "owner edit"
    assert config.transition_rules[rule_name].logic == "all"


@pytest.mark.parametrize(
    "bad",
    [
        "a string",
        {"loop_max_consecutive": "many"},
        {"loop_max_consecutive": True},
        {"a_typo": 1},
        {"allowed_paths": ["/etc"]},
    ],
)
def test_a_stale_row_is_skipped_and_never_raises(tool_name: str, bad: object) -> None:
    file_value = file_governance_config().tools[tool_name]

    replace_overrides({f"tool.{tool_name}": bad})  # type: ignore[dict-item]

    assert load_governance_config().tools[tool_name] == file_value


def test_a_row_for_a_tool_that_the_file_no_longer_holds_has_no_effect() -> None:
    replace_overrides({"tool.a_removed_tool": {"loop_max_consecutive": 1}})

    assert "a_removed_tool" not in load_governance_config().tools


@pytest.mark.parametrize("field", TOOL_PATH_FIELDS)
def test_the_path_lists_cannot_be_edited(tool_name: str, field: str) -> None:
    with pytest.raises(GovernanceConfigError, match="path lists"):
        validate_governance_override(f"tool.{tool_name}", {field: ["/tmp"]})


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("tool.a_tool_the_file_does_not_hold", {"loop_max_consecutive": 1}),
        ("sub_agent_tools", {}),
        ("sub_agent_tools", {"a_tool": {"granted": "yes", "reason": "x"}}),
        ("sub_agent_tools", {"a_tool": {"granted": True, "reason": ""}}),
        ("mode_transition_rules", {"NORMAL_to_ALERT": {"logic": "any"}}),
        ("mode_transition_rules", "text"),
    ],
)
def test_an_invalid_edit_is_refused(parameter: str, value: object) -> None:
    with pytest.raises(GovernanceConfigError):
        validate_governance_override(parameter, value)  # type: ignore[arg-type]


def test_the_stored_form_of_a_tool_edit_holds_the_edited_keys_only(tool_name: str) -> None:
    assert validate_governance_override(f"tool.{tool_name}", {"loop_max_consecutive": 2}) == {
        "loop_max_consecutive": 2
    }


def test_a_held_config_follows_an_edit_at_its_next_read(tool_name: str) -> None:
    file_value = file_governance_config().tools[tool_name].loop_max_consecutive
    layer = ToolExecutionLayer(ToolRegistry())
    manager = ModeManager()
    live = LiveGovernanceConfig(load_governance_config)

    replace_overrides({f"tool.{tool_name}": {"loop_max_consecutive": file_value + 3}})

    assert layer.governance_config.tools[tool_name].loop_max_consecutive == file_value + 3
    assert manager.governance_config.tools[tool_name].loop_max_consecutive == file_value + 3
    assert live.get().tools[tool_name].loop_max_consecutive == file_value + 3
    assert (
        orchestrator_executor._get_cached_governance_config().tools[tool_name].loop_max_consecutive  # type: ignore[attr-defined]
        == file_value + 3
    )


def test_a_passed_config_stays_pinned(tool_name: str) -> None:
    pinned = file_governance_config()
    layer = ToolExecutionLayer(ToolRegistry(), governance_config=pinned)

    replace_overrides({f"tool.{tool_name}": {"loop_max_consecutive": 99}})

    assert layer.governance_config is pinned


def test_a_reload_that_fails_keeps_the_last_config(tool_name: str) -> None:
    good = file_governance_config()
    calls = [good]

    def _loader():  # noqa: ANN202
        if calls:
            return calls.pop()
        raise GovernanceConfigError("the file went away")

    live = LiveGovernanceConfig(_loader)
    replace_overrides({f"tool.{tool_name}": {"loop_max_consecutive": 2}})

    assert live.get() is good


def _tool_row(tool_name: str) -> dict[str, object]:
    """Return the row that the PWA sends: every field except the path lists."""
    policy = file_governance_config().tools[tool_name]
    return policy.model_dump(mode="json", exclude=set(TOOL_PATH_FIELDS))


def test_fre1733_one_changed_field_of_the_whole_row_stores_one_field(tool_name: str) -> None:
    file_value = file_governance_config().tools[tool_name].loop_max_consecutive

    stored = validate_governance_override(
        f"tool.{tool_name}", {**_tool_row(tool_name), "loop_max_consecutive": file_value + 3}
    )

    assert stored == {"loop_max_consecutive": file_value + 3}


def test_fre1733_every_unchanged_tool_row_stores_nothing() -> None:
    for name in file_governance_config().tools:
        assert validate_governance_override(f"tool.{name}", _tool_row(name)) == {}, name


def test_fre1733_a_later_file_change_to_another_field_takes_effect(tool_name: str) -> None:
    config = file_governance_config()
    policy = config.tools[tool_name]
    stored = validate_governance_override(
        f"tool.{tool_name}",
        {**_tool_row(tool_name), "loop_max_consecutive": policy.loop_max_consecutive + 3},
    )
    changed_policy = policy.model_copy(
        update={"loop_max_per_signature": policy.loop_max_per_signature + 1}
    )
    changed_file = config.model_copy(update={"tools": {**config.tools, tool_name: changed_policy}})

    applied = apply_governance_overrides(changed_file, {f"tool.{tool_name}": stored})

    assert applied.tools[tool_name].loop_max_consecutive == policy.loop_max_consecutive + 3
    assert applied.tools[tool_name].loop_max_per_signature == policy.loop_max_per_signature + 1


def test_fre1733_an_empty_object_is_refused_and_not_read_as_a_clear(tool_name: str) -> None:
    with pytest.raises(GovernanceConfigError, match="non-empty"):
        validate_governance_override(f"tool.{tool_name}", {})
