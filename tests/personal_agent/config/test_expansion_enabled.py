"""FRE-1520: `expansion_enabled` is a validated setting, default True, env-parsed."""

from __future__ import annotations

import pytest

from personal_agent.config.settings import AppConfig


def test_expansion_enabled_defaults_true(monkeypatch: pytest.MonkeyPatch) -> None:
    # FRE-1521: /opt/seshat/.env sets AGENT_EXPANSION_ENABLED=false for the live
    # FRE-1517 study (owner decision, 2026-09-15 05:45 UTC); load_env_files()
    # (settings.py:3733) loads it into os.environ for any local run in that
    # tree, so this default-True assertion needs its own clean slate. CI is
    # unaffected — it has no .env — but a local `make test` sees it too.
    monkeypatch.delenv("AGENT_EXPANSION_ENABLED", raising=False)
    assert AppConfig().expansion_enabled is True


def test_expansion_enabled_env_parses_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_EXPANSION_ENABLED", "false")
    assert AppConfig().expansion_enabled is False
