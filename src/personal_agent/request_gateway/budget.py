"""Stage 7: Context Budget Management.

Token estimation and three-phase trimming when over budget.
Trimming priority (least → most destructive):
  1. Drop oldest history  (keep system messages + last user message)
  2. Drop memory context  (Seshat enrichment)
  3. Drop tool definitions

All operations return a new AssembledContext (frozen dataclass — no mutation).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from personal_agent.request_gateway.types import AssembledContext
from personal_agent.telemetry.compaction import CompactionRecord, log_compaction

logger = structlog.get_logger(__name__)


def estimate_tokens(text: str) -> int:
    """Estimate token count from text using word-count approximation.

    Uses word_count * 1.3 as a lightweight proxy for actual tokenisation.
    Accuracy is sufficient for budget gating; no tokeniser dependency required.

    Args:
        text: Any text string to estimate.

    Returns:
        Estimated token count (>= 0).
    """
    if not text:
        return 0
    return int(len(text.split()) * 1.3)


def _total_context_tokens(
    messages: list[dict[str, Any]],
    memory_context: list[dict[str, Any]] | None,
    tool_definitions: list[dict[str, Any]] | None,
) -> int:
    """Estimate total tokens across all context components."""
    parts: list[str] = [m.get("content", "") or "" for m in messages]

    if memory_context:
        for item in memory_context:
            parts.append(str(item))

    if tool_definitions:
        for tool in tool_definitions:
            parts.append(str(tool))

    return estimate_tokens(" ".join(parts))


def _trim_history(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Drop oldest history, preserving system messages and the last user message.

    Args:
        messages: Full message list (OpenAI format).

    Returns:
        Tuple of (trimmed_messages, was_trimmed).
    """
    system_msgs = [m for m in messages if m.get("role") == "system"]
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)

    if last_user is None:
        # No user message to preserve — cannot trim meaningfully
        return messages, False

    trimmed = system_msgs + [last_user]
    if len(trimmed) >= len(messages):
        # Nothing dropped
        return messages, False

    return trimmed, True


def apply_budget(
    context: AssembledContext,
    max_tokens: int,
    trace_id: str,
    session_id: str = "",
) -> AssembledContext:
    """Apply context budget, trimming in priority order if over limit.

    Trimming phases (applied sequentially until under budget):
      1. Drop oldest history (keep system + last user message)
      2. Drop memory context
      3. Drop tool definitions

    Emits a ``context_budget_applied`` structlog event with trimming outcome.
    Also emits a ``context.compaction`` CompactionRecord via
    :func:`~personal_agent.telemetry.compaction.log_compaction` for each
    trimming phase that fires (ADR-0047 D3).

    Args:
        context: Assembled context from Stage 6.
        max_tokens: Token budget ceiling.
        trace_id: Request trace identifier for logging.
        session_id: Client session identifier, used for compaction telemetry.

    Returns:
        New AssembledContext — unchanged if within budget, trimmed otherwise.
        ``trimmed`` and ``overflow_action`` fields reflect what was done.
    """
    messages = list(context.messages)
    memory_context = context.memory_context
    tool_definitions = context.tool_definitions
    overflow_action: str | None = None

    tokens_before_all = _total_context_tokens(messages, memory_context, tool_definitions)
    total_tokens = tokens_before_all

    # Phase 1: drop oldest history
    if total_tokens > max_tokens:
        tokens_phase_before = total_tokens
        messages, did_trim = _trim_history(messages)
        if did_trim:
            overflow_action = "dropped_oldest_history"
            total_tokens = _total_context_tokens(messages, memory_context, tool_definitions)
            # D3: emit compaction record for history trimming
            log_compaction(
                CompactionRecord(
                    trace_id=trace_id,
                    session_id=session_id,
                    timestamp=datetime.now(timezone.utc),
                    trigger="budget_exceeded",
                    tier_affected="near",
                    tokens_before=tokens_phase_before,
                    tokens_after=total_tokens,
                    tokens_removed=tokens_phase_before - total_tokens,
                    strategy="drop_oldest",
                    content_summary="Dropped oldest conversation history turns to fit budget",
                    entities_preserved=(),
                    entities_dropped=(),
                )
            )

    # Phase 2: drop memory context
    if total_tokens > max_tokens and memory_context is not None:
        tokens_phase_before = total_tokens
        memory_context = None
        overflow_action = "dropped_memory_context"
        total_tokens = _total_context_tokens(messages, memory_context, tool_definitions)
        # D3: emit compaction record for memory context drop
        log_compaction(
            CompactionRecord(
                trace_id=trace_id,
                session_id=session_id,
                timestamp=datetime.now(timezone.utc),
                trigger="budget_exceeded",
                tier_affected="episodic",
                tokens_before=tokens_phase_before,
                tokens_after=total_tokens,
                tokens_removed=tokens_phase_before - total_tokens,
                strategy="drop_oldest",
                content_summary="Dropped Seshat memory context to fit token budget",
                entities_preserved=(),
                entities_dropped=(),
            )
        )

    # Phase 3: drop tool definitions
    if total_tokens > max_tokens and tool_definitions is not None:
        tokens_phase_before = total_tokens
        tool_definitions = None
        overflow_action = "dropped_tool_definitions"
        total_tokens = _total_context_tokens(messages, memory_context, tool_definitions)
        # D3: emit compaction record for tool definitions drop
        log_compaction(
            CompactionRecord(
                trace_id=trace_id,
                session_id=session_id,
                timestamp=datetime.now(timezone.utc),
                trigger="budget_exceeded",
                tier_affected="long_term",
                tokens_before=tokens_phase_before,
                tokens_after=total_tokens,
                tokens_removed=tokens_phase_before - total_tokens,
                strategy="drop_oldest",
                content_summary="Dropped tool definitions to fit token budget",
                entities_preserved=(),
                entities_dropped=(),
            )
        )

    trimmed = overflow_action is not None

    logger.info(
        "context_budget_applied",
        trimmed=trimmed,
        total_tokens=total_tokens,
        max_tokens=max_tokens,
        overflow_action=overflow_action,
        message_count=len(messages),
        has_memory=memory_context is not None,
        has_tools=tool_definitions is not None,
        trace_id=trace_id,
    )

    return AssembledContext(
        messages=messages,
        memory_context=memory_context,
        tool_definitions=tool_definitions,
        skills=context.skills,
        delegation_context=context.delegation_context,
        token_count=total_tokens,
        trimmed=trimmed,
        overflow_action=overflow_action,
    )
