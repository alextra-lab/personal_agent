"""The AppConfig overlay: an edit reaches the one ``settings`` object (FRE-1724)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from personal_agent.config import get_settings, settings
from personal_agent.config.setting_overrides import (
    apply_setting_overrides,
    applied_settings,
    replace_overrides,
    setting_file_value,
)

_TURN_FIELD = "orchestrator_max_tool_iterations"
_RESTART_FIELD = "mode_evaluation_interval_seconds"


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    yield
    replace_overrides({})
    apply_setting_overrides(at_start=True)


def test_a_next_turn_edit_reaches_the_singleton_at_once_and_clear_restores() -> None:
    before = settings.orchestrator_max_tool_iterations

    replace_overrides({f"setting.{_TURN_FIELD}": before + 7})
    apply_setting_overrides(at_start=False)

    assert get_settings().orchestrator_max_tool_iterations == before + 7
    assert setting_file_value(_TURN_FIELD) == before
    assert _TURN_FIELD in applied_settings()

    replace_overrides({})
    apply_setting_overrides(at_start=False)

    assert settings.orchestrator_max_tool_iterations == before
    assert _TURN_FIELD not in applied_settings()


def test_a_next_restart_edit_waits_for_the_start() -> None:
    before = settings.mode_evaluation_interval_seconds

    replace_overrides({f"setting.{_RESTART_FIELD}": before + 5})
    apply_setting_overrides(at_start=False)

    assert settings.mode_evaluation_interval_seconds == before
    assert _RESTART_FIELD not in applied_settings()

    apply_setting_overrides(at_start=True)

    assert settings.mode_evaluation_interval_seconds == before + 5
    assert setting_file_value(_RESTART_FIELD) == before


def test_a_cleared_restart_edit_keeps_the_running_value_until_the_start() -> None:
    before = settings.mode_evaluation_interval_seconds
    replace_overrides({f"setting.{_RESTART_FIELD}": before + 5})
    apply_setting_overrides(at_start=True)

    replace_overrides({})
    apply_setting_overrides(at_start=False)
    assert settings.mode_evaluation_interval_seconds == before + 5

    apply_setting_overrides(at_start=True)
    assert settings.mode_evaluation_interval_seconds == before


def test_an_edit_does_not_change_the_env_check_of_the_menu() -> None:
    before = set(settings.model_fields_set)

    replace_overrides({f"setting.{_TURN_FIELD}": 3})
    apply_setting_overrides(at_start=False)

    assert set(settings.model_fields_set) == before


def test_a_stale_row_is_skipped_and_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    before = settings.orchestrator_max_tool_iterations

    replace_overrides({f"setting.{_TURN_FIELD}": "not a number sentinel-value"})
    apply_setting_overrides(at_start=True)

    assert settings.orchestrator_max_tool_iterations == before
    assert "sentinel-value" not in caplog.text


def test_one_stale_row_does_not_stop_a_good_row() -> None:
    before = settings.mode_evaluation_interval_seconds

    replace_overrides(
        {
            f"setting.{_TURN_FIELD}": "not a number",
            f"setting.{_RESTART_FIELD}": before + 5,
        }
    )
    apply_setting_overrides(at_start=True)

    assert settings.mode_evaluation_interval_seconds == before + 5


def test_a_read_only_row_never_applies() -> None:
    before = settings.database_echo

    replace_overrides({"setting.database_echo": not before})
    apply_setting_overrides(at_start=True)

    assert settings.database_echo == before


def test_a_row_for_a_field_that_no_longer_exists_is_ignored() -> None:
    replace_overrides({"setting.a_field_that_a_deploy_removed": 1})

    apply_setting_overrides(at_start=True)


def test_two_rows_that_pass_only_in_one_order_both_apply() -> None:
    """Codex plan review: a sorted pass skipped a row that needs a later row."""
    low_threshold = settings.orchestrator_spend_threshold - 1
    low_ceiling = low_threshold + 1
    assert low_ceiling <= settings.orchestrator_spend_threshold

    replace_overrides(
        {
            "setting.orchestrator_max_tool_iterations": low_ceiling,
            "setting.orchestrator_spend_threshold": low_threshold,
        }
    )
    apply_setting_overrides(at_start=True)

    assert settings.orchestrator_max_tool_iterations == low_ceiling
    assert settings.orchestrator_spend_threshold == low_threshold


def test_a_stale_row_does_not_stop_two_rows_that_need_each_other() -> None:
    low_threshold = settings.orchestrator_spend_threshold - 1
    low_ceiling = low_threshold + 1

    replace_overrides(
        {
            "setting.orchestrator_max_tool_iterations": low_ceiling,
            "setting.orchestrator_spend_threshold": low_threshold,
            "setting.mode_evaluation_interval_seconds": "not a number",
        }
    )
    apply_setting_overrides(at_start=True)

    assert settings.orchestrator_max_tool_iterations == low_ceiling
    assert settings.orchestrator_spend_threshold == low_threshold
