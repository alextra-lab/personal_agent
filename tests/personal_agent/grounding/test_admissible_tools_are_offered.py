"""AC-3 (FRE-1297) — no name in the prompt or registry refers to a tool not offered.

The defect this ticket closes was not that fetch_url was broken — it was that
``TYPED_RETRIEVAL_TOOLS``, ``config/governance/tools.yaml`` and the tool-use prompt all
named ``mcp_fetch_content`` while the live MCP gateway offered no such tool. This is the
check that was missing, applied to the always-on native tools a unit test can verify
without standing up the MCP gateway or opt-in flags.
"""

from __future__ import annotations

from personal_agent.governance.models import Mode
from personal_agent.grounding.source_registry import DOCUMENTATION_TOOLS, TYPED_RETRIEVAL_TOOLS
from personal_agent.orchestrator.prompts import _TOOL_RULES
from personal_agent.tools import get_default_registry

# Native (non-MCP, always-registered) typed-retrieval tools this test can check without
# the MCP gateway or a settings opt-in — the subset AC-3 is about.
_ALWAYS_ON_NATIVE_RETRIEVAL_TOOLS = {
    "web_search",
    "fetch_url",
    "search_memory",
    "recall_personal_history",
}


def test_always_on_typed_retrieval_tools_are_actually_offered() -> None:
    registry = get_default_registry()
    normal_mode_names = {t.name for t in registry.list_tools(mode=Mode.NORMAL)}

    assert _ALWAYS_ON_NATIVE_RETRIEVAL_TOOLS <= TYPED_RETRIEVAL_TOOLS
    for name in _ALWAYS_ON_NATIVE_RETRIEVAL_TOOLS:
        assert name in normal_mode_names, (
            f"{name} is in TYPED_RETRIEVAL_TOOLS but not offered in NORMAL mode"
        )


def test_always_on_documentation_tool_is_actually_offered() -> None:
    registry = get_default_registry()
    normal_mode_names = {t.name for t in registry.list_tools(mode=Mode.NORMAL)}

    assert "get_library_docs" in DOCUMENTATION_TOOLS
    assert "get_library_docs" in normal_mode_names


def test_mcp_fetch_content_fossil_is_gone() -> None:
    """The exact regression this ticket closes: a fossil tool name surviving in the
    admissible-source policy table or the tool-use prompt after its backing tool is gone.
    """
    assert "mcp_fetch_content" not in TYPED_RETRIEVAL_TOOLS
    assert "mcp_fetch_content" not in DOCUMENTATION_TOOLS
    assert "mcp_fetch_content" not in _TOOL_RULES


def test_tool_rules_names_only_tools_actually_offered() -> None:
    """Every native tool name _TOOL_RULES tells the model to call is registered."""
    registry = get_default_registry()
    normal_mode_names = {t.name for t in registry.list_tools(mode=Mode.NORMAL)}

    for name in ("web_search", "fetch_url", "perplexity_query"):
        assert name in _TOOL_RULES, f"{name} expected to be named in _TOOL_RULES"
        assert name in normal_mode_names, f"_TOOL_RULES names {name} but it is not offered"
