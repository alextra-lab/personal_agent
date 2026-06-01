"""Tests for /no_think profile gating (FRE-417) and its retirement (FRE-434).

`/no_think` is a Qwen control token. It was gated to the Qwen primary path and
never the cloud (Sonnet) path. As of FRE-434 it is **disabled by default**
(``llm_append_no_think_to_tool_prompts=False``): the primary now runs with
reasoning enabled and the sub-agent is an instruct variant, and the suffix is a
byte-identity hazard for the ADR-0081 §D2 frozen layout. These tests pin the new
default-off behavior and keep coverage of the gating logic when explicitly enabled.
"""

from __future__ import annotations

import pytest

from personal_agent.config import settings
from personal_agent.config.profile import (
    _current_profile,
    load_profile,
    set_current_profile,
)
from personal_agent.orchestrator.executor import (
    _append_no_think_to_last_user_message,
    _no_think_applies,
)


def test_no_think_disabled_by_default() -> None:
    """Default (FRE-434): /no_think is not injected even on the local profile."""
    assert settings.llm_append_no_think_to_tool_prompts is False
    token = set_current_profile(load_profile("local"))
    try:
        out = _append_no_think_to_last_user_message([{"role": "user", "content": "hello"}])
        assert out[-1]["content"] == "hello"
        assert "/no_think" not in out[-1]["content"]
    finally:
        _current_profile.reset(token)


def test_no_think_skipped_on_cloud_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even if explicitly enabled, cloud (Sonnet) → suffix is not injected."""
    monkeypatch.setattr(settings, "llm_append_no_think_to_tool_prompts", True)
    token = set_current_profile(load_profile("cloud"))
    try:
        assert _no_think_applies() is False
        out = _append_no_think_to_last_user_message([{"role": "user", "content": "hello"}])
        assert out[-1]["content"] == "hello"
        assert "/no_think" not in out[-1]["content"]
    finally:
        _current_profile.reset(token)


def test_no_think_injected_when_explicitly_enabled_on_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gating logic still works when re-enabled on the local (Qwen) profile."""
    monkeypatch.setattr(settings, "llm_append_no_think_to_tool_prompts", True)
    token = set_current_profile(load_profile("local"))
    try:
        assert _no_think_applies() is True
        out = _append_no_think_to_last_user_message([{"role": "user", "content": "hello"}])
        assert "/no_think" in out[-1]["content"]
    finally:
        _current_profile.reset(token)
