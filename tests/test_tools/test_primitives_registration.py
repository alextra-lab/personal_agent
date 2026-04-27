"""Tests for FRE-261 PIVOT-2 primitive tool registration wiring.

Verifies that the four primitive tools (read, write, bash, run_python) are
only registered when ``settings.primitive_tools_enabled`` is True.

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

    def test_primitives_registered_when_flag_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_primitives_absent_when_flag_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
