"""Tiered startup-hook tests for AppConfig's config-guard hooks (FRE-649, AC-6).

Three cases (ADR-0099 D4):
(a) safety — TEST env + prod-fingerprint substrate URI raises. This is the
    pre-existing FRE-375 guard (test_environment_substrate_validator.py);
    reasserted here as the safety half of FRE-649's own tiered contract.
(b) policy — a planted orphan .env.example key boots and warns, never raises.
(c) safety, per-profile — a secret required by the ACTIVE profile (cloud)
    but unset raises; the same secret unset under the local profile boots.
    Exercises ``enforce_required_secrets`` directly (see its docstring for
    why this is a plain function called from ``load_app_config()``, not an
    ``AppConfig`` model_validator: ad-hoc construction is pervasive across
    the test suite, and several eval-harness test files legitimately leak
    ``AGENT_MODEL_CONFIG_PATH=config/models.cloud.yaml`` process-wide).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from personal_agent.config.env_loader import Environment
from personal_agent.config.settings import AppConfig, enforce_required_secrets

# `personal_agent.config.__init__` shadows the `settings` submodule attribute
# with the eagerly-loaded AppConfig singleton (`settings = get_settings()`),
# so `import personal_agent.config.settings as x` resolves to that instance,
# not the module. Go through sys.modules via importlib to get the real module.
settings_module = importlib.import_module("personal_agent.config.settings")

_FIXTURES = Path(__file__).resolve().parent / "fixtures"

_TEST_SAFE_URLS: dict[str, object] = {
    "environment": Environment.TEST,
    "neo4j_uri": "bolt://localhost:7688",
    "elasticsearch_url": "http://localhost:9201",
    "database_url": "postgresql+asyncpg://agent:pw@localhost:5433/personal_agent_test",
}


def make_config(**overrides: object) -> AppConfig:
    """Build an AppConfig bypassing env-file loading (see _TEST_SAFE_URLS)."""
    data: dict[str, object] = {**_TEST_SAFE_URLS, **overrides}
    return AppConfig.model_validate(data)


class TestSafetyRaisesOnProdSubstrateInTestEnv:
    """(a) Safety — reasserts the pre-existing FRE-375 guard under this ticket's contract."""

    def test_raises_when_test_env_with_prod_neo4j_uri(self) -> None:
        prod_neo4j_uri = "bolt://localhost:7687"  # fre-375-allow: tests this guard itself
        with pytest.raises(ValidationError, match="prod/dev defaults"):
            make_config(neo4j_uri=prod_neo4j_uri)


class TestPolicyWarnsAndBoots:
    """(b) Policy — an orphan .env key boots and emits a WARNING, never raises."""

    def test_orphan_env_key_boots_and_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fixture_root = _FIXTURES / "orphan_env"
        monkeypatch.setattr(settings_module, "repo_root", lambda: fixture_root)

        warnings: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            settings_module.log,
            "warning",
            lambda event, **kw: warnings.append((event, kw)),
        )

        cfg = make_config()  # does not raise

        assert cfg.environment == Environment.TEST
        # pydantic-settings may run validators more than once per construction
        # (e.g. once while building settings sources); assert the warning
        # fired at least once with the right content, not an exact count.
        assert warnings
        event, kwargs = warnings[0]
        assert event == "config_guard_orphan_env_keys"
        assert any("AGENT_TOTALLY_MADE_UP_KEY" in msg for msg in kwargs["findings"])

    def test_no_warning_against_real_repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        warnings: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            settings_module.log,
            "warning",
            lambda event, **kw: warnings.append((event, kw)),
        )
        make_config()
        orphan_warnings = [w for w in warnings if w[0] == "config_guard_orphan_env_keys"]
        assert orphan_warnings == []


class TestRequiredSecretPerActiveProfile:
    """(c) Safety, per-profile — cloud requires anthropic/openai keys; local requires none."""

    def test_missing_cloud_secret_raises(self) -> None:
        cfg = make_config(
            model_config_path="config/models.cloud.yaml",
            anthropic_api_key=None,
            openai_api_key=None,
        )
        with pytest.raises(ValueError, match="requires secrets"):
            enforce_required_secrets(cfg)

    def test_missing_secret_under_local_profile_boots(self) -> None:
        cfg = make_config(
            model_config_path="config/models.yaml",
            anthropic_api_key=None,
            openai_api_key=None,
        )
        enforce_required_secrets(cfg)  # does not raise
        assert cfg.anthropic_api_key is None

    def test_load_app_config_calls_enforce_required_secrets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Confirms the check is actually wired into the real boot path."""
        calls: list[AppConfig] = []
        monkeypatch.setattr(settings_module, "enforce_required_secrets", calls.append)
        settings_module.load_app_config()
        assert len(calls) == 1
        assert isinstance(calls[0], AppConfig)
