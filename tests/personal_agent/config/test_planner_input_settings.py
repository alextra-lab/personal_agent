"""FRE-1541: the planner's input bounds are validated settings, and `planner_brief_mode` is gone."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from personal_agent.config.settings import AppConfig


def test_planner_brief_mode_setting_is_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    # ADR-0154 D1: `briefing` is the only behaviour. A stale AGENT_PLANNER_BRIEF_MODE in a
    # deployed .env is ignored (extra="ignore"), not an error.
    monkeypatch.setenv("AGENT_PLANNER_BRIEF_MODE", "briefing")
    assert not hasattr(AppConfig(), "planner_brief_mode")


def test_planner_history_max_chars_defaults_60000(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guard against a local /opt/seshat/.env override, the same class of
    # leak that broke test_expansion_enabled_defaults_true (FRE-1520/1521).
    monkeypatch.delenv("AGENT_PLANNER_HISTORY_MAX_CHARS", raising=False)
    assert AppConfig().planner_history_max_chars == 60000


def test_planner_history_max_chars_env_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_PLANNER_HISTORY_MAX_CHARS", "1000")
    assert AppConfig().planner_history_max_chars == 1000


def test_planner_history_max_chars_rejects_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_PLANNER_HISTORY_MAX_CHARS", "-1")
    with pytest.raises(ValidationError):
        AppConfig()


def test_planner_input_max_chars_defaults_64000(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_PLANNER_INPUT_MAX_CHARS", raising=False)
    assert AppConfig().planner_input_max_chars == 64000


def test_planner_input_max_chars_env_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_PLANNER_INPUT_MAX_CHARS", "2000")
    assert AppConfig().planner_input_max_chars == 2000


def test_planner_input_max_chars_rejects_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_PLANNER_INPUT_MAX_CHARS", "-1")
    with pytest.raises(ValidationError):
        AppConfig()
