"""Async compression manager — schedules and tracks background summarization.

Manages the lifecycle of context compression tasks:
- Detects when token count crosses the compression threshold
- Fires async compression between turns
- Stores completed summaries for use on the next turn

State is in-memory per-process, keyed by session_id. Compression tasks
fire-and-forget: if the task completes before the next turn, the summary
is used; otherwise the static truncation marker applies (graceful fallback).
"""

from __future__ import annotations

import asyncio
from typing import Any

from personal_agent.config import settings
from personal_agent.orchestrator.context_compressor import compress_turns
from personal_agent.orchestrator.context_window import estimate_messages_tokens
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

_pending_tasks: dict[str, asyncio.Task[str]] = {}
_summaries: dict[str, str] = {}


def get_summary(session_id: str) -> str | None:
    """Retrieve and consume a completed compression summary.

    If a background compression task has completed for this session,
    return the summary and clear it. Otherwise return None.

    Args:
        session_id: Session identifier.

    Returns:
        Compressed summary string, or None if not available.
    """
    task = _pending_tasks.pop(session_id, None)
    if task is not None and task.done():
        try:
            summary = task.result()
            _summaries[session_id] = summary
        except Exception:
            log.debug("compression_task_failed_on_retrieval", session_id=session_id)

    return _summaries.pop(session_id, None)


def maybe_trigger_compression(
    session_id: str,
    messages: list[dict[str, Any]],
    trace_id: str = "",
    *,
    keep_recent: int = 4,
) -> None:
    """Check token threshold and fire async compression if needed.

    Called after each LLM response. If the current message list exceeds the
    compression threshold and no compression is already pending, identifies
    compressible (older) messages and fires a background task.

    Args:
        session_id: Session identifier for state tracking.
        messages: Current full message list (post-response).
        trace_id: Request trace identifier.
        keep_recent: Number of recent messages to exclude from compression.
    """
    if not settings.context_compression_enabled:
        return

    if session_id in _pending_tasks and not _pending_tasks[session_id].done():
        return

    estimated_tokens = estimate_messages_tokens(messages)
    threshold = int(
        settings.context_window_max_tokens * settings.context_compression_threshold_ratio
    )

    if estimated_tokens <= threshold:
        return

    if len(messages) <= keep_recent + 1:
        return

    compressible = messages[1:-keep_recent] if keep_recent > 0 else messages[1:]
    if not compressible:
        return

    log.info(
        "context_compression_triggered",
        session_id=session_id,
        estimated_tokens=estimated_tokens,
        threshold=threshold,
        compressible_count=len(compressible),
        trace_id=trace_id,
    )

    task = asyncio.create_task(
        _run_compression(session_id, compressible, trace_id),
        name=f"ctx-compress-{session_id[:8]}",
    )
    _pending_tasks[session_id] = task


async def _run_compression(
    session_id: str,
    messages: list[dict[str, Any]],
    trace_id: str,
) -> str:
    """Execute compression and store the result.

    Args:
        session_id: Session identifier.
        messages: Messages to compress.
        trace_id: Trace identifier.

    Returns:
        The compressed summary string.
    """
    summary = await compress_turns(messages, trace_id=trace_id)
    _summaries[session_id] = summary
    return summary


def cleanup_session(session_id: str) -> None:
    """Remove all compression state for a session.

    Args:
        session_id: Session to clean up.
    """
    task = _pending_tasks.pop(session_id, None)
    if task is not None and not task.done():
        task.cancel()
    _summaries.pop(session_id, None)


def clear_all() -> None:
    """Clear all state. Intended for testing."""
    for task in _pending_tasks.values():
        if not task.done():
            task.cancel()
    _pending_tasks.clear()
    _summaries.clear()
