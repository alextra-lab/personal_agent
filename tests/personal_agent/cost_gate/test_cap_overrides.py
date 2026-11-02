"""A stored cap override changes the cap that the gate reads (FRE-1723).

These tests need no database. The refusal of a real reservation is in
``tests/personal_agent/service/test_settings_edit_db.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest
from pydantic import JsonValue, ValidationError

from personal_agent.config.setting_overrides import replace_overrides
from personal_agent.cost_gate import BudgetConfig, BudgetConfigError, CapEntry, CostGate
from personal_agent.cost_gate.policy import (
    apply_budget_overrides,
    apply_cap_overrides,
    cap_parameter,
    validate_cap_override,
    validate_role_estimate_override,
)

_DAILY = "cap.daily.main_inference"
_TOTAL = "cap.weekly._total"


def _config() -> BudgetConfig:
    return BudgetConfig(
        roles={},
        caps=[
            CapEntry(time_window="daily", role="main_inference", cap_usd=Decimal("10.00")),
            CapEntry(time_window="weekly", role="_total", cap_usd=Decimal("100.00")),
        ],
    )


def _gate() -> CostGate:
    return CostGate(config=_config(), db_url="postgresql://unused/unused")


def _caps(config: BudgetConfig) -> dict[str, Decimal]:
    return {cap_parameter(cap): cap.cap_usd for cap in config.caps}


@pytest.fixture(autouse=True)
def _no_overrides() -> Iterator[None]:
    replace_overrides({})
    yield
    replace_overrides({})


def test_the_parameter_name_is_the_menu_row_name() -> None:
    assert list(_caps(_config())) == [_DAILY, _TOTAL]


def test_every_reader_of_the_gate_config_follows_an_edit_and_a_clear() -> None:
    gate = _gate()
    assert _caps(gate.config)[_DAILY] == Decimal("10.00")

    replace_overrides({_DAILY: 2.5})
    assert _caps(gate.config) == {_DAILY: Decimal("2.5"), _TOTAL: Decimal("100.00")}
    assert [cap.cap_usd for cap in gate.config.caps_for("main_inference")] == [
        Decimal("2.5"),
        Decimal("100.00"),
    ]

    replace_overrides({})
    assert _caps(gate.config)[_DAILY] == Decimal("10.00")


def test_the_file_config_stays_the_file_values() -> None:
    gate = _gate()
    replace_overrides({_DAILY: 2.5})
    assert _caps(gate.file_config)[_DAILY] == Decimal("10.00")


def test_a_gate_built_after_the_load_reads_the_override() -> None:
    replace_overrides({_DAILY: 2.5})
    assert _caps(_gate().config)[_DAILY] == Decimal("2.5")


def test_the_config_is_rebuilt_only_when_the_overrides_change() -> None:
    gate = _gate()
    replace_overrides({_DAILY: 2.5})
    assert gate.config is gate.config


def test_an_override_of_another_family_changes_no_cap() -> None:
    config = _config()
    assert apply_cap_overrides(config, {"role.primary": "x"}) is config


@pytest.mark.parametrize("value", ["2.5", True, None, -1, {"cap": 1}, float("nan"), float("inf")])
def test_a_stored_cap_that_fails_is_skipped_and_never_raises(value: JsonValue) -> None:
    replace_overrides({_DAILY: value, _TOTAL: 50})
    assert _caps(_gate().config) == {_DAILY: Decimal("10.00"), _TOTAL: Decimal("50")}


def test_a_stored_cap_that_the_file_no_longer_holds_has_no_effect() -> None:
    replace_overrides({"cap.daily.gone": 1})
    assert _caps(_gate().config) == _caps(_config())


@pytest.mark.parametrize(
    ("value", "stored"),
    [(5, 5.0), (2.5, 2.5), (0, 0.0), (0.000001, 0.000001), (9999.999999, 9999.999999)],
)
def test_validate_returns_a_json_number_that_passes_again(value: float, stored: float) -> None:
    config = _config()
    result = validate_cap_override(config, _DAILY, value)
    assert result == stored
    assert isinstance(result, float)
    assert validate_cap_override(config, _DAILY, result) == stored


@pytest.mark.parametrize(
    ("parameter", "value", "reason"),
    [
        (_DAILY, -1, "greater than or equal to 0"),
        (_DAILY, -0.01, "greater than or equal to 0"),
        (_DAILY, "5", "number"),
        (_DAILY, True, "number"),
        (_DAILY, None, "number"),
        (_DAILY, {"cap_usd": 5}, "number"),
        (_DAILY, float("nan"), "finite"),
        (_DAILY, float("inf"), "finite"),
        (_DAILY, 10000, "digits"),
        (_DAILY, 0.0000001, "decimal"),
        ("cap.daily.no_such_role", 5, "not a cap"),
        ("cap.nope", 5, "not a cap"),
    ],
)
def test_validate_refuses_with_a_reason(parameter: str, value: JsonValue, reason: str) -> None:
    with pytest.raises(BudgetConfigError, match=reason) as caught:
        validate_cap_override(_config(), parameter, value)
    assert "errors.pydantic.dev" not in str(caught.value)


def test_the_loader_model_refuses_a_negative_cap_in_the_file() -> None:
    with pytest.raises(ValidationError):
        CapEntry(time_window="daily", role="x", cap_usd=Decimal("-1"))


def _budget_with_role() -> BudgetConfig:
    return BudgetConfig.model_validate(
        {
            "version": 1,
            "roles": {
                "main_inference": {
                    "default_output_tokens": 100,
                    "safety_factor": 1.2,
                    "on_denial": "raise",
                }
            },
            "caps": [],
        }
    )


def test_fre1724_a_role_estimate_edit_wins_and_a_gate_reads_it() -> None:
    config = _budget_with_role()
    overrides: dict[str, JsonValue] = {
        "role_estimate.main_inference": {"default_output_tokens": 500}
    }

    applied = apply_budget_overrides(config, overrides)

    assert applied.roles["main_inference"].default_output_tokens == 500
    assert applied.roles["main_inference"].safety_factor == 1.2
    assert config.roles["main_inference"].default_output_tokens == 100

    gate = CostGate(config=config, db_url="postgresql://unused")
    replace_overrides(overrides)
    try:
        assert gate.config.roles["main_inference"].default_output_tokens == 500
    finally:
        replace_overrides({})
    assert gate.config.roles["main_inference"].default_output_tokens == 100


@pytest.mark.parametrize(
    "stale",
    [
        {"default_output_tokens": 0},
        {"default_output_tokens": "many"},
        {"a_typo": 1},
        "text",
    ],
)
def test_fre1724_a_stale_role_estimate_is_skipped_and_never_raises(stale: JsonValue) -> None:
    config = _budget_with_role()

    assert apply_budget_overrides(config, {"role_estimate.main_inference": stale}) is config


def test_fre1724_a_role_estimate_for_a_role_the_file_lacks_has_no_effect() -> None:
    config = _budget_with_role()

    assert apply_budget_overrides(config, {"role_estimate.other": {"safety_factor": 2.0}}) is config


def test_fre1724_validate_a_role_estimate() -> None:
    config = _budget_with_role()

    assert validate_role_estimate_override(
        config, "role_estimate.main_inference", {"safety_factor": 1.5}
    ) == {"safety_factor": 1.5}
    with pytest.raises(BudgetConfigError, match="not in the budget file"):
        validate_role_estimate_override(config, "role_estimate.other", {"safety_factor": 1.5})
    with pytest.raises(BudgetConfigError):
        validate_role_estimate_override(
            config, "role_estimate.main_inference", {"safety_factor": 0.5}
        )
