"""Tests for FRE-433 Anthropic cache breakpoints."""

from __future__ import annotations

from personal_agent.llm_client.litellm_client import _apply_anthropic_cache_control


def test_fre433_tail_layout_marks_history_end_not_volatile_tail() -> None:
    """Volatile-tail layout marks the last real history message only."""
    messages = [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "real user turn"},
        {"role": "user", "content": "volatile memory and skills"},
    ]

    _apply_anthropic_cache_control(messages, tools=None, volatile_tail_layout=True)

    assert messages[0]["content"] == [
        {
            "type": "text",
            "text": "stable system",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert messages[1]["content"] == [
        {
            "type": "text",
            "text": "real user turn",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert messages[2] == {"role": "user", "content": "volatile memory and skills"}
