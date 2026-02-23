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
