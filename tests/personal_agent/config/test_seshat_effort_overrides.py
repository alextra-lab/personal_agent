"""A stored effort override wins over the presets file at the next turn (FRE-1723)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import JsonValue

from personal_agent.config import get_settings
from personal_agent.config.seshat_effort import (
    SeshatEffortConfigError,
    effort_spec,
    file_seshat_effort,
    get_seshat_effort,
    hydrate_effort,
    reset_current_effort,
    rounds_for_turn,
    set_current_effort,
    validate_effort_override,
)
from personal_agent.config.setting_overrides import replace_overrides


@pytest.fixture(autouse=True)
def _no_overrides() -> Iterator[None]:
    replace_overrides({})
    yield
    replace_overrides({})


def _preset(**values: JsonValue) -> dict[str, JsonValue]:
    return {
        "expansion": True,
        "sub_agent_rounds": 3,
        "planner_policy": "baseline",
        "primary_mode": None,
        **values,
    }


def test_with_no_override_the_file_object_is_returned() -> None:
    assert get_seshat_effort() is file_seshat_effort()


def test_a_stored_preset_reaches_the_next_turn_and_the_file_returns_when_it_goes() -> None:
    file_rounds = effort_spec("deep").sub_agent_rounds
    replace_overrides({"effort.level.deep": _preset()})

    token = set_current_effort(effort_spec("deep"))
    try:
        assert rounds_for_turn("thorough") == 3
    finally:
        reset_current_effort(token)

    replace_overrides({})
    assert effort_spec("deep").sub_agent_rounds == file_rounds


def test_a_turn_keeps_the_spec_it_resolved_at_launch() -> None:
    token = set_current_effort(effort_spec("deep"))
    try:
        launched = rounds_for_turn("thorough")
        replace_overrides({"effort.level.deep": _preset()})
        assert rounds_for_turn("thorough") == launched
    finally:
        reset_current_effort(token)


def test_a_stored_default_level_names_the_default() -> None:
    replace_overrides({"effort.default_level": "deep"})
    assert effort_spec(None).level == "deep"
    assert hydrate_effort("not a level") == ("deep", "default")


@pytest.mark.parametrize(
    "parameter",
    ["effort.level.deep", "effort.default_level", "effort.level.no_such"],
)
@pytest.mark.parametrize("value", ["no_such", 5, None, {"expansion": "yes"}, {"nope": 1}])
def test_a_stored_row_that_fails_is_skipped_and_never_raises(
    parameter: str, value: JsonValue
) -> None:
    file_config = file_seshat_effort()
    replace_overrides({parameter: value})
    assert get_seshat_effort().model_dump() == file_config.model_dump()


def test_one_bad_row_does_not_hide_a_good_row() -> None:
    replace_overrides({"effort.level.fast": {"nope": 1}, "effort.level.deep": _preset()})
    config = get_seshat_effort()
    assert config.levels["deep"].sub_agent_rounds == 3
    assert config.levels["fast"] == file_seshat_effort().levels["fast"]


def test_a_stored_preset_above_a_lowered_ceiling_is_skipped() -> None:
    ceiling = get_settings().sub_agent_max_tool_iterations
    replace_overrides({"effort.level.deep": _preset(sub_agent_rounds=ceiling + 1)})
    assert get_seshat_effort().levels["deep"] == file_seshat_effort().levels["deep"]


def test_validate_returns_the_canonical_value_to_store() -> None:
    assert validate_effort_override("effort.level.deep", _preset()) == _preset()
    assert validate_effort_override("effort.level.fast", {"expansion": False}) == {
        "expansion": False,
        "sub_agent_rounds": None,
        "planner_policy": None,
        "primary_mode": None,
    }
    assert validate_effort_override("effort.default_level", "deep") == "deep"


@pytest.mark.parametrize(
    ("parameter", "value", "reason"),
    [
        ("effort.level.no_such", _preset(), "no_such"),
        ("effort.level.deep", "fast", "takes the values of a preset"),
        ("effort.level.deep", _preset(level="fast"), "takes the values of a preset"),
        ("effort.level.deep", _preset(expansion="yes"), "expansion"),
        ("effort.level.deep", _preset(sub_agent_rounds=True), "sub_agent_rounds"),
        ("effort.level.deep", _preset(sub_agent_rounds="3"), "sub_agent_rounds"),
        ("effort.level.deep", _preset(sub_agent_rounds=0), "sub_agent_rounds"),
        ("effort.level.deep", _preset(sub_agent_rounds=None), "needs sub_agent_rounds"),
        ("effort.level.deep", _preset(planner_policy="p9"), "planner_policy"),
        ("effort.level.deep", _preset(primary_mode="slow"), "primary_mode"),
        ("effort.level.deep", _preset(extra=1), "extra"),
        ("effort.default_level", "no_such", "default_level"),
        ("effort.default_level", 5, "default_level"),
    ],
)
def test_validate_refuses_with_a_reason(parameter: str, value: JsonValue, reason: str) -> None:
    with pytest.raises(SeshatEffortConfigError, match=reason) as caught:
        validate_effort_override(parameter, value)
    assert "errors.pydantic.dev" not in str(caught.value)


def test_validate_refuses_rounds_above_the_ceiling() -> None:
    ceiling = get_settings().sub_agent_max_tool_iterations
    with pytest.raises(SeshatEffortConfigError, match="sub_agent_max_tool_iterations"):
        validate_effort_override("effort.level.deep", _preset(sub_agent_rounds=ceiling + 1))
