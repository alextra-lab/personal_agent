"""FRE-1521: `planner_brief_mode` / `planner_history_max_chars` are validated settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from personal_agent.config.settings import AppConfig


def test_planner_brief_mode_defaults_current(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guard against a local /opt/seshat/.env override, the same class of
    # leak that broke test_expansion_enabled_defaults_true (FRE-1520/1521).
    monkeypatch.delenv("AGENT_PLANNER_BRIEF_MODE", raising=False)
    assert AppConfig().planner_brief_mode == "current"


def test_planner_brief_mode_env_parses_briefing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_PLANNER_BRIEF_MODE", "briefing")
    assert AppConfig().planner_brief_mode == "briefing"


def test_planner_brief_mode_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_PLANNER_BRIEF_MODE", "verbose")
    with pytest.raises(ValidationError):
        AppConfig()


def test_planner_history_max_chars_defaults_60000(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_PLANNER_HISTORY_MAX_CHARS", raising=False)
    assert AppConfig().planner_history_max_chars == 60000


def test_planner_history_max_chars_env_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_PLANNER_HISTORY_MAX_CHARS", "1000")
    assert AppConfig().planner_history_max_chars == 1000


def test_planner_history_max_chars_rejects_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_PLANNER_HISTORY_MAX_CHARS", "-1")
    with pytest.raises(ValidationError):
        AppConfig()
