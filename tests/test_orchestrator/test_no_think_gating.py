"""Tests for /no_think profile gating (FRE-417).

`/no_think` is a Qwen control token — it must be injected only when the active
primary model is a Qwen model, never on the cloud (Sonnet) path.
"""

from __future__ import annotations

from personal_agent.config.profile import (
    _current_profile,
    load_profile,
    set_current_profile,
)
from personal_agent.orchestrator.executor import (
    _append_no_think_to_last_user_message,
    _no_think_applies,
)


def test_no_think_skipped_on_cloud_profile() -> None:
    """Cloud (Sonnet) → suffix is not injected and the message is unchanged."""
    token = set_current_profile(load_profile("cloud"))
    try:
        assert _no_think_applies() is False
        out = _append_no_think_to_last_user_message([{"role": "user", "content": "hello"}])
        assert out[-1]["content"] == "hello"
        assert "/no_think" not in out[-1]["content"]
    finally:
        _current_profile.reset(token)


def test_no_think_injected_on_local_profile() -> None:
    """Local (Qwen) → suffix is appended to the last user message."""
    token = set_current_profile(load_profile("local"))
    try:
        assert _no_think_applies() is True
        out = _append_no_think_to_last_user_message([{"role": "user", "content": "hello"}])
        assert "/no_think" in out[-1]["content"]
    finally:
        _current_profile.reset(token)
