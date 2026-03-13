"""Conversation context window helpers for multi-turn chat."""

from __future__ import annotations

from typing import Any

from personal_agent.telemetry import get_logger

log = get_logger(__name__)

TRUNCATION_MARKER = {"role": "system", "content": "[Earlier messages truncated]"}


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Estimate token count for one message using a simple heuristic.

    Args:
        message: OpenAI-style chat message dict.

    Returns:
        Estimated token count for the message.
    """
    content = message.get("content", "")
    if not isinstance(content, str):
        content = str(content)
    return max(1, len(content) // 4)


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate token count for a list of messages.

    Args:
        messages: OpenAI-style message list.

    Returns:
        Estimated token count for all messages.
    """
    return sum(estimate_message_tokens(message) for message in messages)


def apply_context_window(
    messages: list[dict[str, Any]],
    max_tokens: int,
    reserved_tokens: int = 4500,
    *,
    strategy: str = "truncate",
    trace_id: str | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Trim conversation history to fit within token budget.

    Keeps the first message (session opener/system context) and prefers recent
    messages. If the conversation overflows, older middle context is dropped
    and a marker is inserted to signal truncation.

    Args:
        messages: Full message history in OpenAI-style format.
        max_tokens: Total token budget available for conversation messages.
        reserved_tokens: Tokens reserved for system/tool/response overhead.
        strategy: Window strategy. MVP supports only ``truncate``.
        trace_id: Optional trace identifier for telemetry.
        session_id: Optional session identifier for telemetry.

    Returns:
        Trimmed message list that fits the available budget.
    """
    if not messages:
        return []

    if strategy != "truncate":
        log.warning(
            "unsupported_context_strategy_fallback",
            strategy=strategy,
            fallback="truncate",
            trace_id=trace_id,
            session_id=session_id,
        )

    if reserved_tokens >= max_tokens:
        log.warning(
            "context_window_reserved_tokens_clamped",
            max_tokens=max_tokens,
            reserved_tokens=reserved_tokens,
            effective_reserved_tokens=0,
            trace_id=trace_id,
            session_id=session_id,
        )
        available_budget = max_tokens
    else:
        available_budget = max(1, max_tokens - reserved_tokens)
    input_tokens = estimate_messages_tokens(messages)
    if input_tokens <= available_budget:
        log.info(
            "context_window_applied",
            trace_id=trace_id,
            session_id=session_id,
            input_messages=len(messages),
            output_messages=len(messages),
            estimated_input_tokens=input_tokens,
            estimated_output_tokens=input_tokens,
            strategy="truncate",
            truncated=False,
        )
        return list(messages)

    if len(messages) == 1:
        log.info(
            "context_window_applied",
            trace_id=trace_id,
            session_id=session_id,
            input_messages=1,
            output_messages=1,
            estimated_input_tokens=input_tokens,
            estimated_output_tokens=input_tokens,
            strategy="truncate",
            truncated=False,
        )
        return list(messages)

    # ── Priority eviction: strip old tool error messages first (ADR-0032 §3.2) ──
    # Failed tool results from *previous* turns carry stale negative signal that
    # biases small models against tool use.  We evict them before the general
    # truncation pass to maximise useful context in the window.
    messages = _evict_old_tool_errors(messages)

    first_message = messages[0]
    remaining = messages[1:]

    first_tokens = estimate_message_tokens(first_message)
    marker_tokens = estimate_message_tokens(TRUNCATION_MARKER)
    tail_budget = max(0, available_budget - first_tokens)

    tail_reversed: list[dict[str, Any]] = []
    used_tail_tokens = 0
    for message in reversed(remaining):
        message_tokens = estimate_message_tokens(message)
        if used_tail_tokens + message_tokens > tail_budget:
            continue
        tail_reversed.append(message)
        used_tail_tokens += message_tokens

    tail_messages = list(reversed(tail_reversed))
    dropped_count = len(remaining) - len(tail_messages)

    output_messages: list[dict[str, Any]] = [first_message]
    if dropped_count > 0 and first_tokens + marker_tokens <= available_budget:
        output_messages.append(TRUNCATION_MARKER)

    output_messages.extend(tail_messages)

    # Keep most-recent context if marker or retained history pushed us over budget.
    while len(output_messages) > 1 and estimate_messages_tokens(output_messages) > available_budget:
        if output_messages[1:2] == [TRUNCATION_MARKER]:
            output_messages.pop(1)
            continue
        output_messages.pop(1)

    output_tokens = estimate_messages_tokens(output_messages)
    log.info(
        "context_window_applied",
        trace_id=trace_id,
        session_id=session_id,
        input_messages=len(messages),
        output_messages=len(output_messages),
        estimated_input_tokens=input_tokens,
        estimated_output_tokens=output_tokens,
        strategy="truncate",
        truncated=len(output_messages) < len(messages),
    )
    return output_messages


# ---------------------------------------------------------------------------
# Error-message eviction (ADR-0032 §3.2)
# ---------------------------------------------------------------------------

_ERROR_KEYWORDS = frozenset({"error", "retry", "failed", "status"})


def _is_tool_error_message(message: dict[str, Any]) -> bool:
    """Return True if *message* is a tool-role message carrying an error/retry hint."""
    if message.get("role") != "tool":
        return False
    content = message.get("content", "")
    if not isinstance(content, str):
        return False
    # Quick heuristic: check for JSON keys that our error format uses.
    content_lower = content.lower()
    return '"error"' in content_lower or '"retry"' in content_lower or '"status": "error"' in content_lower


def _evict_old_tool_errors(
    messages: list[dict[str, Any]],
    *,
    keep_recent: int = 6,
) -> list[dict[str, Any]]:
    """Remove tool error messages from older parts of the conversation.

    Preserves the most recent *keep_recent* messages unconditionally so that
    errors from the *current* tool execution turn are still visible to the model
    for immediate retry.  Only older error messages are evicted.

    Args:
        messages: Full message list (mutated in place for efficiency, but a new
            list is returned).
        keep_recent: Number of trailing messages guaranteed to be preserved.

    Returns:
        Filtered message list.
    """
    if len(messages) <= keep_recent:
        return list(messages)

    protected_tail = messages[-keep_recent:]
    evictable = messages[:-keep_recent]

    filtered = [msg for msg in evictable if not _is_tool_error_message(msg)]
    if len(filtered) < len(evictable):
        log.debug(
            "tool_error_messages_evicted",
            evicted=len(evictable) - len(filtered),
            total=len(messages),
        )
    return filtered + protected_tail
