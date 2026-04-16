"""Tests for conversation context window truncation."""

from personal_agent.orchestrator.context_window import (
    TRUNCATION_MARKER,
    _sanitize_tool_pairs,
    apply_context_window,
    estimate_message_tokens,
    estimate_messages_tokens,
)


def _message(role: str, size: int, *, suffix: str = "") -> dict[str, str]:
    """Build a deterministic message of a given character length."""
    return {"role": role, "content": ("x" * size) + suffix}


def test_apply_context_window_keeps_short_history() -> None:
    """Messages under budget should pass through unchanged."""
    messages = [
        _message("system", 200, suffix="-0"),
        _message("user", 120, suffix="-1"),
        _message("assistant", 120, suffix="-2"),
    ]

    output = apply_context_window(messages, max_tokens=6000, reserved_tokens=4500)

    assert output == messages


def test_apply_context_window_truncates_and_marks_history() -> None:
    """Long histories should preserve opener + recency with marker."""
    messages = [_message("system", 80, suffix="-sys")]
    for index in range(30):
        role = "user" if index % 2 == 0 else "assistant"
        messages.append(_message(role, 220, suffix=f"-{index}"))

    output = apply_context_window(messages, max_tokens=800, reserved_tokens=0)

    assert output[0] == messages[0]
    assert TRUNCATION_MARKER in output
    assert output[-1] == messages[-1]
    assert len(output) < len(messages)
    assert estimate_messages_tokens(output) <= 800


def test_apply_context_window_empty_and_single_message() -> None:
    """Edge cases should not error."""
    assert apply_context_window([], max_tokens=1000) == []

    single = [_message("user", 200, suffix="-single")]
    assert apply_context_window(single, max_tokens=10, reserved_tokens=0) == single


# ── Compressed summary tests (ADR-0038) ──────────────────────────────────


def test_apply_context_window_uses_compressed_summary() -> None:
    """When compressed_summary is provided, it replaces the truncation marker."""
    messages = [_message("system", 80, suffix="-sys")]
    for index in range(30):
        role = "user" if index % 2 == 0 else "assistant"
        messages.append(_message(role, 200, suffix=f"-{index}"))

    # Budget of 800 forces truncation (31 msgs ~1520 tokens > 800).
    # Summary is short enough (~6 tokens) to fit alongside retained tail.
    summary = "Key facts from earlier"
    output = apply_context_window(
        messages,
        max_tokens=800,
        reserved_tokens=0,
        compressed_summary=summary,
    )

    assert output[0] == messages[0]
    assert TRUNCATION_MARKER not in output

    summary_msgs = [m for m in output if m.get("content") == summary]
    assert len(summary_msgs) == 1, "Compressed summary should appear in output"
    assert output[-1] == messages[-1]
    assert len(output) < len(messages)
    assert estimate_messages_tokens(output) <= 800


def test_apply_context_window_falls_back_without_summary() -> None:
    """Without compressed_summary, the static truncation marker is used."""
    messages = [_message("system", 80, suffix="-sys")]
    for index in range(30):
        role = "user" if index % 2 == 0 else "assistant"
        messages.append(_message(role, 220, suffix=f"-{index}"))

    output = apply_context_window(
        messages,
        max_tokens=800,
        reserved_tokens=0,
        compressed_summary=None,
    )

    assert TRUNCATION_MARKER in output


def test_compressed_summary_not_used_when_no_truncation() -> None:
    """If messages fit the budget, compressed_summary is ignored."""
    messages = [
        _message("system", 40, suffix="-sys"),
        _message("user", 40, suffix="-usr"),
    ]

    output = apply_context_window(
        messages,
        max_tokens=6000,
        reserved_tokens=4500,
        compressed_summary="## Summary\nSome stuff",
    )

    assert output == messages
    assert all("Summary" not in m.get("content", "") for m in output)


# ── Tool-pair sanitization tests ─────────────────────────────────────────────


def test_sanitize_tool_pairs_drops_orphaned_tool_result() -> None:
    """A role=tool message whose tool_call_id has no assistant match is dropped."""
    messages = [
        {"role": "system", "content": "sys"},
        # This tool result references a call_id that no longer appears in any
        # assistant message (simulating what happens after truncation drops the
        # paired assistant message).
        {"role": "tool", "tool_call_id": "call_orphan", "name": "run_sysdiag", "content": "{}"},
        {"role": "user", "content": "hello"},
    ]

    result = _sanitize_tool_pairs(messages)

    roles = [m["role"] for m in result]
    assert "tool" not in roles, "orphaned tool result should have been removed"
    assert len(result) == 2  # system + user remain


def test_sanitize_tool_pairs_keeps_paired_tool_result() -> None:
    """A role=tool message whose call_id IS present in an assistant message is kept."""
    messages = [
        {"role": "system", "content": "sys"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "function": {"name": "ps", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "ps", "content": "PID …"},
        {"role": "user", "content": "thanks"},
    ]

    result = _sanitize_tool_pairs(messages)

    assert result == messages, "no messages should be dropped when all pairs are intact"


def test_sanitize_tool_pairs_mixed_keeps_paired_drops_orphan() -> None:
    """Only orphaned tool results are dropped; paired ones survive."""
    messages = [
        {"role": "system", "content": "sys"},
        # Old turn — assistant message was truncated but tool result survived
        {"role": "tool", "tool_call_id": "call_old", "name": "df", "content": "{}"},
        # New turn — both assistant tool_calls and tool result are present
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_new", "function": {"name": "ps", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call_new", "name": "ps", "content": "PID …"},
        {"role": "user", "content": "ok"},
    ]

    result = _sanitize_tool_pairs(messages)

    tool_msgs = [m for m in result if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_new"


def test_apply_context_window_no_orphaned_tool_results_after_truncation() -> None:
    """Truncation must not produce orphaned tool results.

    Regression test for the AnthropicException 'unexpected tool_use_id found
    in tool_result blocks' error that occurs when context truncation drops an
    assistant message containing tool_calls but retains the paired role=tool
    result message.
    """
    # Build a history where an old tool exchange exists and a newer exchange is
    # also present.  Force a tight budget so truncation hits the old exchange.
    system = {"role": "system", "content": "x" * 80}
    # Old turn: assistant called a tool, result came back
    old_assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call_old", "function": {"name": "df", "arguments": "{}"}}],
    }
    old_tool_result = {"role": "tool", "tool_call_id": "call_old", "name": "df", "content": "x" * 400}
    # Newer user/assistant exchange
    new_user = {"role": "user", "content": "x" * 200}
    new_assistant = {"role": "assistant", "content": "x" * 200}
    latest_user = {"role": "user", "content": "x" * 100}

    messages = [system, old_assistant, old_tool_result, new_user, new_assistant, latest_user]

    # Budget that forces some truncation but keeps the tail
    output = apply_context_window(messages, max_tokens=400, reserved_tokens=0)

    # Collect all tool_call_ids that survive in assistant messages
    live_ids = set()
    for msg in output:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                live_ids.add(tc.get("id"))

    # Every role=tool message must have a matching live call id
    for msg in output:
        if msg.get("role") == "tool":
            assert msg.get("tool_call_id") in live_ids, (
                f"Orphaned tool result detected: tool_call_id={msg.get('tool_call_id')} "
                f"not in live assistant tool_calls {live_ids}"
            )


def test_estimate_message_tokens_includes_tool_calls() -> None:
    """Token estimate for assistant messages must include tool_calls payload."""
    plain = {"role": "assistant", "content": "ok"}
    with_tools = {
        "role": "assistant",
        "content": "ok",
        "tool_calls": [{"id": "call_1", "function": {"name": "run_sysdiag", "arguments": '{"command": "ps", "args": "aux"}'}}],
    }
    assert estimate_message_tokens(with_tools) > estimate_message_tokens(plain)
