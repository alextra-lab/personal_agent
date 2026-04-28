"""Tests for FRE-263 PIVOT-4 legacy tool flag-gated deprecation.

Verifies that the 8 curated tools superseded by primitives + skill docs
are only registered when ``settings.legacy_tools_enabled`` is True,
and that a ``tool_deprecated`` WARNING is emitted when it is.

Pure unit tests — no LLM, no infrastructure required.
"""

from __future__ import annotations

import pytest
import structlog.testing

from personal_agent.config import settings

_LEGACY_TOOLS = [
    "read_file",
    "list_directory",
    "system_metrics_snapshot",
    "self_telemetry_query",
    "query_elasticsearch",
    "fetch_url",
    "run_sysdiag",
    "infra_health",
]

_ALWAYS_PRESENT = [
    "search_memory",
    "web_search",
    "perplexity_query",
    "create_linear_issue",
]


def _fresh_registry(monkeypatch: pytest.MonkeyPatch, *, legacy: bool, prefer: bool = False):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(settings, "legacy_tools_enabled", legacy)
    monkeypatch.setattr(settings, "prefer_primitives_enabled", prefer)
    monkeypatch.setattr(settings, "primitive_tools_enabled", False)
    from personal_agent.tools import register_mvp_tools
    from personal_agent.tools.registry import ToolRegistry
    reg = ToolRegistry()
    register_mvp_tools(reg)
    return reg


class TestLegacyToolsNotRegisteredByDefault:
    """All 8 legacy tools must be absent when legacy_tools_enabled=False."""

    def test_all_eight_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reg = _fresh_registry(monkeypatch, legacy=False)
        names = reg.list_tool_names()
        for tool in _LEGACY_TOOLS:
            assert tool not in names, f"{tool} should be absent when legacy_tools_enabled=False"

    def test_always_present_tools_still_registered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reg = _fresh_registry(monkeypatch, legacy=False)
        names = reg.list_tool_names()
        for tool in _ALWAYS_PRESENT:
            assert tool in names, f"{tool} should always be present"


class TestLegacyToolsRegisteredWhenFlagEnabled:
    """All 8 tools present and tool_deprecated WARNING emitted when legacy_tools_enabled=True."""

    def test_all_eight_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reg = _fresh_registry(monkeypatch, legacy=True)
        names = reg.list_tool_names()
        for tool in _LEGACY_TOOLS:
            assert tool in names, f"{tool} should be present when legacy_tools_enabled=True"

    def test_tool_deprecated_warning_emitted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with structlog.testing.capture_logs() as captured:
            _fresh_registry(monkeypatch, legacy=True)
        warnings = [e for e in captured if e.get("log_level") == "warning" and e.get("event") == "tool_deprecated"]
        assert len(warnings) == 1, f"Expected exactly one tool_deprecated warning, got: {warnings}"
        warned_tools = warnings[0]["tools"]
        for tool in _LEGACY_TOOLS:
            assert tool in warned_tools, f"{tool} missing from tool_deprecated warning"

    def test_no_warning_when_flag_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with structlog.testing.capture_logs() as captured:
            _fresh_registry(monkeypatch, legacy=False)
        warnings = [e for e in captured if e.get("event") == "tool_deprecated"]
        assert not warnings, f"Unexpected tool_deprecated warning when legacy=False: {warnings}"


class TestLegacyToolsIndependentOfPreferPrimitives:
    """legacy_tools_enabled is the authoritative gate; prefer_primitives no longer gates tools."""

    def test_legacy_false_prefer_true_all_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reg = _fresh_registry(monkeypatch, legacy=False, prefer=True)
        names = reg.list_tool_names()
        for tool in _LEGACY_TOOLS:
            assert tool not in names

    def test_legacy_false_prefer_false_all_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Key change from FRE-283: read_file is now gated too when legacy=False."""
        reg = _fresh_registry(monkeypatch, legacy=False, prefer=False)
        names = reg.list_tool_names()
        for tool in _LEGACY_TOOLS:
            assert tool not in names

    def test_legacy_true_prefer_true_all_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reg = _fresh_registry(monkeypatch, legacy=True, prefer=True)
        names = reg.list_tool_names()
        for tool in _LEGACY_TOOLS:
            assert tool in names
