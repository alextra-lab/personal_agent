"""Unit tests for FRE-435 eval model config fidelity."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest
import structlog.testing

from personal_agent.llm_client.models import ModelConfig
from scripts.eval.fre435_memory_recall import harness


def _model_config(
    *,
    entity_extraction_role: str = "gpt-5.4-mini",
    captains_log_role: str = "claude_sonnet",
    insights_role: str = "claude_sonnet",
) -> ModelConfig:
    """Build the role-only config surface used by the fidelity guard."""
    return cast(
        ModelConfig,
        SimpleNamespace(
            entity_extraction_role=entity_extraction_role,
            captains_log_role=captains_log_role,
            insights_role=insights_role,
        ),
    )


def test_test_substrate_env_pins_prod_model_config() -> None:
    """The FRE-435 harness defaults to the production model config."""
    assert harness._TEST_SUBSTRATE_ENV["AGENT_MODEL_CONFIG_PATH"] == "config/models.cloud.yaml"


def test_guard_passes_on_role_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matching active and production pipeline roles pass."""
    monkeypatch.delenv("EVAL_MODEL_CONFIG_ALLOW_DIVERGE", raising=False)
    cfg = _model_config()

    with patch(
        "scripts.eval.fre435_memory_recall.harness.load_model_config",
        side_effect=[cfg, cfg],
    ):
        harness._check_model_config_fidelity()


def test_guard_raises_on_undeclared_divergence(monkeypatch: pytest.MonkeyPatch) -> None:
    """An undeclared active/prod role mismatch fails closed."""
    monkeypatch.delenv("EVAL_MODEL_CONFIG_ALLOW_DIVERGE", raising=False)
    active_cfg = _model_config(entity_extraction_role="gpt-5.4-nano")
    prod_cfg = _model_config(entity_extraction_role="gpt-5.4-mini")

    with (
        patch(
            "scripts.eval.fre435_memory_recall.harness.load_model_config",
            side_effect=[active_cfg, prod_cfg],
        ),
        pytest.raises(RuntimeError, match="entity_extraction_role"),
    ):
        harness._check_model_config_fidelity()


def test_guard_passes_on_declared_divergence(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deliberately declared active/prod mismatch is allowed."""
    monkeypatch.setenv("EVAL_MODEL_CONFIG_ALLOW_DIVERGE", "1")
    active_cfg = _model_config(entity_extraction_role="gpt-5.4-nano")
    prod_cfg = _model_config(entity_extraction_role="gpt-5.4-mini")

    with patch(
        "scripts.eval.fre435_memory_recall.harness.load_model_config",
        side_effect=[active_cfg, prod_cfg],
    ):
        harness._check_model_config_fidelity()


def test_guard_logs_active_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard emits the active model role values."""
    monkeypatch.delenv("EVAL_MODEL_CONFIG_ALLOW_DIVERGE", raising=False)
    cfg = _model_config()

    with (
        patch(
            "scripts.eval.fre435_memory_recall.harness.load_model_config",
            side_effect=[cfg, cfg],
        ),
        structlog.testing.capture_logs() as logs,
    ):
        harness._check_model_config_fidelity()

    active_events = [entry for entry in logs if entry["event"] == "eval_model_config_active"]
    assert active_events == [
        {
            "event": "eval_model_config_active",
            "log_level": "info",
            "entity_extraction_role": "gpt-5.4-mini",
            "captains_log_role": "claude_sonnet",
            "insights_role": "claude_sonnet",
            "config_path": str(harness.settings.model_config_path),
        }
    ]
