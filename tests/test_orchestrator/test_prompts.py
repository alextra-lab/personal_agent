"""Tests for orchestrator prompt helpers."""

from personal_agent.orchestrator.prompts import get_tool_awareness_prompt


def test_tool_awareness_returns_string() -> None:
    """get_tool_awareness_prompt() always returns a str (empty when no tools registered)."""
    prompt = get_tool_awareness_prompt()
    assert isinstance(prompt, str)

