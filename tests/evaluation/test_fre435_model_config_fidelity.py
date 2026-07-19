"""Unit tests for FRE-435 eval model config fidelity (ADR-0099 D1 stage 2, FRE-650).

**Narrowed by FRE-916 phase 2 (ADR-0121).** ``_check_model_config_fidelity``
used to compare the active model config against a pinned production one and
refuse to run on an undeclared divergence, with ``EVAL_MODEL_CONFIG_ALLOW_DIVERGE=1``
as the escape hatch. Both sides now read the same single catalog, so that
comparison could never fail — and a check that cannot fail is worse than no
check, because it reads as coverage. The divergence tests are retired with the
mechanism.

What is still asserted: the guard resolves every pipeline role through the real
resolver (so a dangling ``all:`` reference raises rather than silently yielding a
key nothing defines) and logs the resolved set, so each eval run records the
models it actually used.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import structlog.testing
from scripts.eval.fre435_memory_recall import harness

from personal_agent.config.model_loader import CATALOG_RELPATH, ModelRoleError


def _roles(*, entity_extraction: str = "gpt-5.4-mini") -> dict[str, str]:
    """Build the matrix-resolved role dict the fidelity guard logs."""
    return {
        "entity_extraction": entity_extraction,
        "captains_log": "claude_sonnet",
        "insights": "claude_sonnet",
    }


def test_guard_passes_when_every_pipeline_role_resolves() -> None:
    """The happy path: all pipeline roles dereference cleanly."""
    with patch(
        "scripts.eval.fre435_memory_recall.harness._pipeline_role_values",
        return_value=_roles(),
    ):
        harness._check_model_config_fidelity()


def test_guard_resolves_against_the_real_catalog() -> None:
    """Unmocked: the real matrix + catalog must satisfy the guard.

    This is what replaced the divergence comparison — it exercises the resolver
    end to end, so a role removed from the matrix or pointed at a deleted
    deployment key fails the eval before it burns a single live call.
    """
    harness._check_model_config_fidelity()


def test_guard_raises_when_a_pipeline_role_cannot_resolve() -> None:
    """Fails closed on a dangling reference rather than running a degraded eval."""
    with (
        patch(
            "scripts.eval.fre435_memory_recall.harness._pipeline_role_values",
            side_effect=ModelRoleError("role 'entity_extraction' resolves to model 'ghost'"),
        ),
        pytest.raises(ModelRoleError, match="entity_extraction"),
    ):
        harness._check_model_config_fidelity()


def test_guard_logs_active_roles() -> None:
    """The guard emits the active model role values, so each run records them."""
    with (
        patch(
            "scripts.eval.fre435_memory_recall.harness._pipeline_role_values",
            return_value=_roles(),
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
            "config_path": CATALOG_RELPATH,
        }
    ]
