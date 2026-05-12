"""Tests for FRE-261 PIVOT-2 primitive tool registration wiring.

Verifies that the four primitive tools (read, write, bash, run_python) are
only registered when ``settings.primitive_tools_enabled`` is True.

After FRE-265 (ADR-0063 PIVOT-6) the eight legacy curated tools (read_file,
list_directory, system_metrics_snapshot, self_telemetry_query,
query_elasticsearch, fetch_url, run_sysdiag, infra_health) are gone — their
absence is now a structural guarantee, not a runtime flag.

These are pure unit tests — no LLM, no infrastructure required.
"""

import pytest

from personal_agent.config import settings


class TestPrimitivesNotRegisteredByDefault:
    """Primitives must NOT appear in the registry when the flag is off."""

    def test_primitives_not_registered_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When AGENT_PRIMITIVE_TOOLS_ENABLED=false (field default), primitives are absent.

        The dev .env may enable primitives; reset the singleton and force the flag
        off to test the out-of-box default.
        """
        import personal_agent.config as config_module
        import personal_agent.tools as tools_module

        monkeypatch.setattr(tools_module, "_default_registry", None)
        monkeypatch.setattr(config_module.settings, "primitive_tools_enabled", False)

        registry = tools_module.get_default_registry()
        tool_names = registry.list_tool_names()

        assert "bash" not in tool_names
        assert "run_python" not in tool_names
        assert "read" not in tool_names
        assert "write" not in tool_names


class TestPrimitivesRegisteredWhenFlagEnabled:
    """Primitives ARE registered when the flag is flipped on."""

    def test_primitives_registered_when_flag_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When primitive_tools_enabled=True, all four primitives are in the registry."""
        monkeypatch.setattr(settings, "primitive_tools_enabled", True)

        # Create a fresh registry (bypass the module-level singleton so we
        # don't pollute other tests).
        from personal_agent.tools import register_mvp_tools
        from personal_agent.tools.registry import ToolRegistry

        new_registry = ToolRegistry()
        register_mvp_tools(new_registry)
        tool_names = new_registry.list_tool_names()

        assert "bash" in tool_names
        assert "read" in tool_names
        assert "write" in tool_names
        assert "run_python" in tool_names

    def test_primitives_absent_when_flag_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When primitive_tools_enabled=False, all four primitives are absent."""
        monkeypatch.setattr(settings, "primitive_tools_enabled", False)

        from personal_agent.tools import register_mvp_tools
        from personal_agent.tools.registry import ToolRegistry

        new_registry = ToolRegistry()
        register_mvp_tools(new_registry)
        tool_names = new_registry.list_tool_names()

        assert "bash" not in tool_names
        assert "read" not in tool_names
        assert "write" not in tool_names
        assert "run_python" not in tool_names


_LEGACY_DELETED = [
    "read_file",
    "list_directory",
    "system_metrics_snapshot",
    "self_telemetry_query",
    "query_elasticsearch",
    "fetch_url",
    "run_sysdiag",
    "infra_health",
]


class TestLegacyToolsDeleted:
    """FRE-265 (ADR-0063 PIVOT-6) deleted 8 legacy curated tool modules.

    They can no longer be registered under any setting combination.
    """

    @pytest.mark.parametrize("primitive,prefer", [(False, False), (True, False), (False, True), (True, True)])
    def test_legacy_tools_absent_under_all_flag_combinations(
        self, monkeypatch: pytest.MonkeyPatch, primitive: bool, prefer: bool
    ) -> None:
        monkeypatch.setattr(settings, "primitive_tools_enabled", primitive)
        monkeypatch.setattr(settings, "prefer_primitives_enabled", prefer)

        from personal_agent.tools import register_mvp_tools
        from personal_agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        register_mvp_tools(registry)
        tool_names = registry.list_tool_names()

        for name in _LEGACY_DELETED:
            assert name not in tool_names, f"{name} must be absent after FRE-265"

    def test_always_available_tools_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """search_memory, web_search, Linear tools, etc. are always registered."""
        monkeypatch.setattr(settings, "primitive_tools_enabled", False)

        from personal_agent.tools import register_mvp_tools
        from personal_agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        register_mvp_tools(registry)
        tool_names = registry.list_tool_names()

        for name in ("search_memory", "web_search", "perplexity_query", "create_linear_issue"):
            assert name in tool_names, f"{name} should always be present"
