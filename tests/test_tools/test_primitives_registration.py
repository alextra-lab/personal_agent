"""Tests for FRE-261 PIVOT-2 primitive tool registration wiring.

Verifies that the four primitive tools (read, write, bash, run_python) are
only registered when ``settings.primitive_tools_enabled`` is True.

Also verifies the FRE-283 treatment-side gating: curated tools superseded by
primitives are hidden when ``settings.prefer_primitives_enabled`` is True so
the eval cannot fall back to them.

These are pure unit tests — no LLM, no infrastructure required.
"""

import pytest

from personal_agent.config import settings


class TestPrimitivesNotRegisteredByDefault:
    """Primitives must NOT appear in the registry when the flag is off."""

    def test_primitives_not_registered_by_default(self) -> None:
        """When AGENT_PRIMITIVE_TOOLS_ENABLED=false (default), primitives are absent."""
        # The default singleton is created with primitive_tools_enabled=False,
        # so the four primitive tools must not have been registered.
        from personal_agent.tools import get_default_registry

        registry = get_default_registry()
        tool_names = registry.list_tool_names()

        assert "bash" not in tool_names
        assert "run_python" not in tool_names
        # read and write primitives are also gated by the flag
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


_CURATED_GATED = [
    "query_elasticsearch",
    "fetch_url",
    "list_directory",
    "system_metrics_snapshot",
    "self_telemetry_query",
    "run_sysdiag",
    "infra_health",
]


class TestPreferPrimitivesDeregistersCurated:
    """FRE-283/FRE-263: curated tools gated by legacy_tools_enabled (FRE-263 primary gate).

    prefer_primitives_enabled now only controls skill-doc injection; tool registration
    is governed exclusively by legacy_tools_enabled (default False per PIVOT-4).
    """

    def test_curated_absent_when_prefer_primitives_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "prefer_primitives_enabled", True)
        monkeypatch.setattr(settings, "legacy_tools_enabled", False)
        monkeypatch.setattr(settings, "primitive_tools_enabled", False)

        from personal_agent.tools import register_mvp_tools
        from personal_agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        register_mvp_tools(registry)
        tool_names = registry.list_tool_names()

        for name in _CURATED_GATED:
            assert name not in tool_names, f"{name} should be absent (legacy_tools_enabled=False)"

    def test_curated_absent_by_default_prefer_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FRE-263: curated tools absent by default regardless of prefer_primitives_enabled."""
        monkeypatch.setattr(settings, "prefer_primitives_enabled", False)
        monkeypatch.setattr(settings, "legacy_tools_enabled", False)
        monkeypatch.setattr(settings, "primitive_tools_enabled", False)

        from personal_agent.tools import register_mvp_tools
        from personal_agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        register_mvp_tools(registry)
        tool_names = registry.list_tool_names()

        for name in _CURATED_GATED:
            assert name not in tool_names, (
                f"{name} should be absent when legacy_tools_enabled=False (PIVOT-4 default)"
            )

    def test_curated_present_when_legacy_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Curated tools visible only when legacy_tools_enabled=True (rollback mode)."""
        monkeypatch.setattr(settings, "prefer_primitives_enabled", False)
        monkeypatch.setattr(settings, "legacy_tools_enabled", True)
        monkeypatch.setattr(settings, "primitive_tools_enabled", False)

        from personal_agent.tools import register_mvp_tools
        from personal_agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        register_mvp_tools(registry)
        tool_names = registry.list_tool_names()

        for name in _CURATED_GATED:
            assert name in tool_names, f"{name} should be present when legacy_tools_enabled=True"

    def test_always_available_tools_present_regardless_of_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """search_memory, web_search, Linear tools, etc. are always registered."""
        monkeypatch.setattr(settings, "prefer_primitives_enabled", True)
        monkeypatch.setattr(settings, "primitive_tools_enabled", False)

        from personal_agent.tools import register_mvp_tools
        from personal_agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        register_mvp_tools(registry)
        tool_names = registry.list_tool_names()

        for name in ("search_memory", "web_search", "perplexity_query", "create_linear_issue"):
            assert name in tool_names, f"{name} should always be present"
