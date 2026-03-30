"""Tests for conversation context window truncation."""

from personal_agent.orchestrator.context_window import (
    TRUNCATION_MARKER,
    apply_context_window,
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
