"""FRE-1520: `expansion_enabled` is a validated setting, default True, env-parsed."""

from __future__ import annotations

import pytest

from personal_agent.config.settings import AppConfig


def test_expansion_enabled_defaults_true() -> None:
    assert AppConfig().expansion_enabled is True


def test_expansion_enabled_env_parses_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_EXPANSION_ENABLED", "false")
    assert AppConfig().expansion_enabled is False
