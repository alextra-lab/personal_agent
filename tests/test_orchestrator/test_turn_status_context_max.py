"""Tests for the PWA status-meter context_max resolution (FRE-414).

The ``turn_status`` meter must reflect the *active* model's context window
(200K cloud Sonnet / 131K local Qwen), not the static local budget.

ADR-0121 T5 (FRE-920) removed Path — the active model is now the per-turn
``primary`` selection (``config/selection.py``), not an ExecutionProfile.
"""

from __future__ import annotations

import pytest

from personal_agent.config.model_loader import load_model_config
from personal_agent.config.selection import reset_current_selection, set_current_selection
from personal_agent.orchestrator.executor import _resolve_context_max

_CLOUD_KEY = "claude_sonnet"
_LOCAL_KEY = "qwen3.6-35b-thinking"


@pytest.mark.parametrize("primary_key", [_CLOUD_KEY, _LOCAL_KEY])
def test_context_max_matches_active_primary(primary_key: str) -> None:
    """context_max equals the active selection's primary model context_length."""
    token = set_current_selection({"primary": primary_key})
    try:
        expected = load_model_config().models[primary_key].context_length
        assert _resolve_context_max() == expected
    finally:
        reset_current_selection(token)


def test_context_max_differs_cloud_vs_local() -> None:
    """The meter is selection-aware: cloud (Sonnet 200K) ≠ local — proving the fix."""
    cloud_token = set_current_selection({"primary": _CLOUD_KEY})
    try:
        cloud_max = _resolve_context_max()
    finally:
        reset_current_selection(cloud_token)

    local_token = set_current_selection({"primary": _LOCAL_KEY})
    try:
        local_max = _resolve_context_max()
    finally:
        reset_current_selection(local_token)

    assert cloud_max == 200000
    assert cloud_max != local_max


def test_context_max_resolves_current_default_primary_binding() -> None:
    """FRE-1326 AC-2: with no per-turn selection override (the live default path),

    the meter resolves *today's actual bound primary* (``config/model_roles.yaml``'s
    ``primary`` binding), not a value that merely happens to match by coincidence.
    The two prior tests above prove the mechanism generalizes across an old pair
    (Sonnet 200K vs. the retired ``qwen3.6-35b-thinking`` 131K); this one anchors it
    to the deployment actually bound today.
    """
    from personal_agent.config.model_loader import resolve_role_target

    default_key, default_def = resolve_role_target("primary")
    assert default_def is not None
    expected = load_model_config().models[default_key].context_length

    assert _resolve_context_max() == expected
    # Today's bound primary (2026-09-05 swap) — pin the key so this test fails loudly,
    # not silently, the next time the primary binding changes.
    assert default_key == "qwen3.6-35b-thinking"
    assert expected == 262144
