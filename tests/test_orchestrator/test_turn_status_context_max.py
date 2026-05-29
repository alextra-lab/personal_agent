"""Tests for the PWA status-meter context_max resolution (FRE-414).

The ``turn_status`` meter must reflect the *active* model's context window
(200K cloud Sonnet / 131K local Qwen), not the static local budget.
"""

from __future__ import annotations

import pytest

from personal_agent.config.model_loader import load_model_config
from personal_agent.config.profile import (
    _current_profile,
    load_profile,
    resolve_model_key,
    set_current_profile,
)
from personal_agent.orchestrator.executor import _resolve_context_max


@pytest.mark.parametrize("profile_name", ["cloud", "local"])
def test_context_max_matches_active_primary(profile_name: str) -> None:
    """context_max equals the active profile's primary model context_length."""
    token = set_current_profile(load_profile(profile_name))
    try:
        expected = load_model_config().models[resolve_model_key("primary")].context_length
        assert _resolve_context_max() == expected
    finally:
        _current_profile.reset(token)


def test_context_max_differs_cloud_vs_local() -> None:
    """The meter is profile-aware: cloud (Sonnet 200K) ≠ local — proving the fix."""
    cloud_token = set_current_profile(load_profile("cloud"))
    try:
        cloud_max = _resolve_context_max()
    finally:
        _current_profile.reset(cloud_token)

    local_token = set_current_profile(load_profile("local"))
    try:
        local_max = _resolve_context_max()
    finally:
        _current_profile.reset(local_token)

    assert cloud_max == 200000
    assert cloud_max != local_max
