"""Tests for orchestrator prompt helpers."""

from unittest.mock import MagicMock, patch

from personal_agent.orchestrator.prompts import get_tool_awareness_prompt
from personal_agent.tools.types import ToolDefinition


def test_tool_awareness_returns_string() -> None:
    """get_tool_awareness_prompt() always returns a str (empty when no tools registered)."""
    prompt = get_tool_awareness_prompt()
    assert isinstance(prompt, str)


def _make_tool(name: str, category: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="d",
        category=category,
        risk_level="low",
        allowed_modes=["NORMAL"],
    )


def test_network_category_not_truncated_behind_ellipsis() -> None:
    """FRE-1290: a category with more than 3 tools (e.g. network's 8) must list every
    name, not collapse to 'first 3 + ...' — the truncation previously hid web_search
    from the tool-awareness prompt on categories that grew past the old <= 3 cap.
    """
    import personal_agent.orchestrator.prompts as prompts_module

    prompts_module._tool_awareness_cache = None
    prompts_module._tool_awareness_cache_time = 0.0

    network_tools = [
        _make_tool("web_search", "network"),
        _make_tool("perplexity_query", "network"),
        _make_tool("get_library_docs", "network"),
        _make_tool("get_location", "network"),
        _make_tool("save_issue", "network"),
        _make_tool("get_issue", "network"),
        _make_tool("list_issues", "network"),
        _make_tool("resolve_library_id", "network"),
    ]
    mock_registry = MagicMock()
    mock_registry.list_tools.return_value = network_tools

    with patch("personal_agent.tools.get_default_registry", return_value=mock_registry):
        prompt = get_tool_awareness_prompt()

    assert "..." not in prompt, f"truncation ellipsis still present: {prompt!r}"
    for tool in network_tools:
        assert tool.name in prompt, f"{tool.name} missing from tool-awareness prompt"
