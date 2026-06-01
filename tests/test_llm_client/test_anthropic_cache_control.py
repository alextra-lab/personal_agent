"""Tests for Anthropic cache_control markers, incl. the ADR-0081 §D2 history-end
breakpoint (FRE-434)."""

from __future__ import annotations

from typing import Any

from personal_agent.llm_client.litellm_client import (
    _apply_anthropic_cache_control,
    _mark_message_cache_control,
)


def _has_cache_control(content: Any) -> bool:
    return (
        isinstance(content, list)
        and bool(content)
        and isinstance(content[-1], dict)
        and "cache_control" in content[-1]
    )


def test_mark_message_promotes_string_content() -> None:
    msg: dict[str, Any] = {"role": "assistant", "content": "hello"}
    assert _mark_message_cache_control(msg) is True
    assert _has_cache_control(msg["content"])
    assert msg["content"][0]["text"] == "hello"


def test_mark_message_empty_content_is_unmarkable() -> None:
    msg: dict[str, Any] = {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]}
    assert _mark_message_cache_control(msg) is False
    assert msg["content"] == ""


def test_default_marks_system_only_no_history_end() -> None:
    """Flag off: system (and tools) are marked; no history-end breakpoint."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    _apply_anthropic_cache_control(messages, tools=None, frozen_layout=False)
    assert _has_cache_control(messages[0]["content"])  # system marked
    assert messages[2]["content"] == "a1"  # assistant untouched (no history-end)
    assert messages[3]["content"] == "q2"


def test_frozen_layout_marks_history_end_before_current_user() -> None:
    """Flag on: the last frozen message before the current user turn is marked."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "<turn_context>...</turn_context>\n\nq2"},
    ]
    _apply_anthropic_cache_control(messages, tools=None, frozen_layout=True)
    assert _has_cache_control(messages[0]["content"])  # system
    assert _has_cache_control(messages[2]["content"])  # history-end on assistant a1
    # Current user turn (volatile tail) stays uncached / unmarked.
    assert messages[3]["content"] == "<turn_context>...</turn_context>\n\nq2"


def test_frozen_layout_no_history_no_marker() -> None:
    """Flag on but no history before the current user turn → no history-end marker."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "only turn"},
    ]
    _apply_anthropic_cache_control(messages, tools=None, frozen_layout=True)
    assert _has_cache_control(messages[0]["content"])  # system still marked
    assert messages[1]["content"] == "only turn"  # user unmarked
