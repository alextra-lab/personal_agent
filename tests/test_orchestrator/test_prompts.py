"""Tests for orchestrator prompt helpers."""

from personal_agent.orchestrator.prompts import get_tool_awareness_prompt


def test_tool_awareness_skipped_for_non_capability_queries() -> None:
    """Non-capability requests should not receive awareness prompt overhead."""
    prompt = get_tool_awareness_prompt(user_message="Explain TLS handshake in brief.")
    assert prompt == ""

