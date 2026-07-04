"""Unit tests for FRE-435 eval model config fidelity (ADR-0099 D1 stage 2, FRE-650).

``_check_model_config_fidelity`` now compares matrix-resolved role dicts
(``_pipeline_role_values``, keyed by matrix role name) rather than reading
``ModelConfig`` role attributes directly — those attributes no longer exist
(FRE-650 removed them from ``ModelConfig``). Tests mock
``_pipeline_role_values`` itself, the seam the guard actually calls.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import structlog.testing
from scripts.eval.fre435_memory_recall import harness


def _roles(*, entity_extraction: str = "gpt-5.4-mini") -> dict[str, str]:
    """Build the matrix-resolved role dict the fidelity guard compares."""
    return {
        "entity_extraction": entity_extraction,
        "captains_log": "claude_sonnet",
        "insights": "claude_sonnet",
    }


def test_test_substrate_env_pins_prod_model_config() -> None:
    """The FRE-435 harness defaults to the production model config."""
    assert harness._TEST_SUBSTRATE_ENV["AGENT_MODEL_CONFIG_PATH"] == "config/models.cloud.yaml"


def test_guard_passes_on_role_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matching active and production pipeline roles pass."""
    monkeypatch.delenv("EVAL_MODEL_CONFIG_ALLOW_DIVERGE", raising=False)
    roles = _roles()

    with patch(
        "scripts.eval.fre435_memory_recall.harness._pipeline_role_values",
        return_value=roles,
    ):
        harness._check_model_config_fidelity()


def test_guard_raises_on_undeclared_divergence(monkeypatch: pytest.MonkeyPatch) -> None:
    """An undeclared active/prod role mismatch fails closed."""
    monkeypatch.delenv("EVAL_MODEL_CONFIG_ALLOW_DIVERGE", raising=False)
    active_roles = _roles(entity_extraction="gpt-5.4-nano")
    prod_roles = _roles(entity_extraction="gpt-5.4-mini")

    with (
        patch(
            "scripts.eval.fre435_memory_recall.harness._pipeline_role_values",
            side_effect=[active_roles, prod_roles],
        ),
        pytest.raises(RuntimeError, match="entity_extraction"),
    ):
        harness._check_model_config_fidelity()


def test_guard_passes_on_declared_divergence(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deliberately declared active/prod mismatch is allowed."""
    monkeypatch.setenv("EVAL_MODEL_CONFIG_ALLOW_DIVERGE", "1")
    active_roles = _roles(entity_extraction="gpt-5.4-nano")
    prod_roles = _roles(entity_extraction="gpt-5.4-mini")

    with patch(
        "scripts.eval.fre435_memory_recall.harness._pipeline_role_values",
        side_effect=[active_roles, prod_roles],
    ):
        harness._check_model_config_fidelity()


def test_guard_logs_active_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard emits the active model role values."""
    monkeypatch.delenv("EVAL_MODEL_CONFIG_ALLOW_DIVERGE", raising=False)
    roles = _roles()

    with (
        patch(
            "scripts.eval.fre435_memory_recall.harness._pipeline_role_values",
            return_value=roles,
        ),
        structlog.testing.capture_logs() as logs,
    ):
        harness._check_model_config_fidelity()

    active_events = [entry for entry in logs if entry["event"] == "eval_model_config_active"]
    assert active_events == [
        {
            "event": "eval_model_config_active",
            "log_level": "info",
            "entity_extraction": "gpt-5.4-mini",
            "captains_log": "claude_sonnet",
            "insights": "claude_sonnet",
            "config_path": str(harness.settings.model_config_path),
        }
    ]
