"""Tests for Anthropic cache_control markers, incl. the ADR-0081 §D2 history-end
breakpoint (FRE-434)."""

from __future__ import annotations

from typing import Any

from personal_agent.llm_client.litellm_client import (
    _apply_anthropic_cache_control,
    _enforce_cache_control_cap,
    _mark_message_cache_control,
)


def _has_cache_control(content: Any) -> bool:
    return (
        isinstance(content, list)
        and bool(content)
        and isinstance(content[-1], dict)
        and "cache_control" in content[-1]
    )


def _count_cache_control(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> int:
    """Count every cache_control breakpoint Anthropic would see across messages + tools."""
    total = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            total += sum(
                1 for block in content if isinstance(block, dict) and "cache_control" in block
            )
    for tool in tools or []:
        if isinstance(tool, dict) and "cache_control" in tool:
            total += 1
    return total


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


# ── FRE-468: breakpoint count must never exceed Anthropic's 4-block cap ──────────


def test_frozen_off_exactly_two_breakpoints() -> None:
    """Flag off, with tools: exactly system + last-tool = 2 breakpoints."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    tools: list[dict[str, Any]] = [{"name": "t1"}, {"name": "t2"}]
    _apply_anthropic_cache_control(messages, tools=tools, frozen_layout=False)
    assert _count_cache_control(messages, tools) == 2


def test_frozen_on_exactly_three_breakpoints() -> None:
    """Flag on, with tools: exactly system + one history-end + last-tool = 3."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "<turn_context>...</turn_context>\n\nq2"},
    ]
    tools: list[dict[str, Any]] = [{"name": "t1"}, {"name": "t2"}]
    _apply_anthropic_cache_control(messages, tools=tools, frozen_layout=True)
    assert _count_cache_control(messages, tools) == 3


def test_reapply_on_same_objects_is_idempotent() -> None:
    """Calling apply twice on the SAME message/tool objects must not add markers.

    The executor passes a shallow copy of its working message list each round
    (``api_messages = list(messages)``), so the dicts are shared and mutated in
    place. Re-marking must be idempotent rather than accumulating.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "<turn_context>...</turn_context>\n\nq2"},
    ]
    tools: list[dict[str, Any]] = [{"name": "t1"}, {"name": "t2"}]
    _apply_anthropic_cache_control(messages, tools=tools, frozen_layout=True)
    first = _count_cache_control(messages, tools)
    _apply_anthropic_cache_control(messages, tools=tools, frozen_layout=True)
    assert _count_cache_control(messages, tools) == first == 3


def test_multi_round_loop_never_exceeds_four_breakpoints() -> None:
    """Regression for FRE-468: the in-turn tool loop must not accumulate >4 markers.

    Reproduces the 2026-06-04 turn failure (Anthropic 400: "A maximum of 4 blocks
    with cache_control may be provided. Found 5."). Each tool round appends the
    assistant tool-call, its result, and a fresh ``<turn_context>`` user message,
    then re-marks the (shared) message dicts. The frozen-layout history-end marker
    advances each round; without clearing prior markers they accumulate.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "<turn_context>v0</turn_context>\n\nbuild me a guide"},
    ]
    tools: list[dict[str, Any]] = [{"name": "bash"}, {"name": "artifact_draft"}]

    for round_idx in range(6):
        # Model emits a tool call; we append the assistant turn + tool result.
        messages.append({"role": "assistant", "content": f"calling tools (round {round_idx})"})
        messages.append({"role": "tool", "content": f'{{"success": true, "round": {round_idx}}}'})
        # Executor re-injects a fresh turn_context user message for the next round.
        messages.append(
            {"role": "user", "content": f"<turn_context>v{round_idx + 1}</turn_context>"}
        )
        # Same dict objects are re-marked every round (shallow-copy semantics).
        _apply_anthropic_cache_control(messages, tools=tools, frozen_layout=True)
        assert _count_cache_control(messages, tools) <= 4, (
            f"round {round_idx}: {_count_cache_control(messages, tools)} cache_control "
            "blocks exceeds Anthropic's cap of 4"
        )


def _marked_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}


def test_enforce_cap_clamps_and_preserves_static_anchors() -> None:
    """Defensive guard: an over-marked list is clamped to 4, keeping system + tools."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": [_marked_block("SYS")]},  # static anchor
        {"role": "assistant", "content": [_marked_block("h1")]},  # earliest history
        {"role": "tool", "content": [_marked_block("h2")]},
        {"role": "assistant", "content": [_marked_block("h3")]},  # newest history
    ]
    tools: list[dict[str, Any]] = [{"name": "t1", "cache_control": {"type": "ephemeral"}}]
    # 3 history + system + tool = 5 markers before enforcement.
    _enforce_cache_control_cap(messages, tools, cap=4)
    assert _count_cache_control(messages, tools) == 4
    # System and tool anchors are preserved; earliest history marker is dropped.
    assert _has_cache_control(messages[0]["content"])  # system kept
    assert "cache_control" in tools[0]  # tool kept
    assert "cache_control" not in messages[1]["content"][0]  # earliest history dropped
    assert _has_cache_control(messages[3]["content"])  # newest history kept


def test_enforce_cap_noop_when_within_limit() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": [_marked_block("SYS")]},
        {"role": "assistant", "content": [_marked_block("h1")]},
    ]
    _enforce_cache_control_cap(messages, tools=None, cap=4)
    assert _count_cache_control(messages, None) == 2
