"""Async compression manager — schedules and tracks background summarization.

Manages the lifecycle of context compression tasks:
- Detects when token count crosses the soft compression threshold
- Fires async compression between turns via
  :func:`~personal_agent.orchestrator.within_session_compression.compress_in_place`
  with ``trigger="soft"``
- Stores completed summaries for use on the next turn
- Per-session cursor (ADR-0061 §D1) prevents single-shot lock-in: a second
  compression fires once the message count grows past the previous cursor
  by at least ``settings.within_session_compression_refire_after_messages``.

State is in-memory per-process, keyed by ``session_id``.  Compression tasks
fire-and-forget: if the task completes before the next turn, the summary is
used; otherwise the static truncation marker applies (graceful fallback).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from personal_agent.config import settings
from personal_agent.orchestrator.context_window import estimate_messages_tokens
from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from personal_agent.events.bus import EventBus

log = get_logger(__name__)

_pending_tasks: dict[str, asyncio.Task[str]] = {}
_summaries: dict[str, str] = {}
_last_compressed_at_msgcount: dict[str, int] = {}


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
    bus: "EventBus | None" = None,
) -> None:
    """Check token threshold and fire async compression if needed.

    Called after each LLM response.  When the current message list crosses
    the soft threshold and the per-session re-fire cursor allows it, fire
    a background ADR-0061 compression with ``trigger="soft"``.  The
    head-middle-tail split, pre-pass, and dual-write telemetry all live in
    :mod:`personal_agent.orchestrator.within_session_compression`.

    Args:
        session_id: Session identifier for state tracking.
        messages: Current full message list (post-response).
        trace_id: Request trace identifier.
        bus: Optional event bus, threaded through to the dual-write so the
            soft compression event lands on
            ``stream:context.within_session_compressed``.
    """
    if not settings.context_compression_enabled:
        return
    if not settings.within_session_compression_enabled:
        return

    if session_id in _pending_tasks and not _pending_tasks[session_id].done():
        return

    estimated_tokens = estimate_messages_tokens(messages)
    threshold = int(
        settings.context_window_max_tokens * settings.context_compression_threshold_ratio
    )

    if estimated_tokens <= threshold:
        return

    refire_after = max(1, settings.within_session_compression_refire_after_messages)
    last_count = _last_compressed_at_msgcount.get(session_id)
    if last_count is not None and len(messages) - last_count < refire_after:
        return

    if len(messages) <= 1:
        return

    log.info(
        "context_compression_triggered",
        session_id=session_id,
        estimated_tokens=estimated_tokens,
        threshold=threshold,
        message_count=len(messages),
        last_compressed_at=last_count,
        trace_id=trace_id,
    )

    task = asyncio.create_task(
        _run_compression(session_id, list(messages), trace_id, bus),
        name=f"ctx-compress-{session_id[:8]}",
    )
    _pending_tasks[session_id] = task
    _last_compressed_at_msgcount[session_id] = len(messages)


async def _run_compression(
    session_id: str,
    messages: list[dict[str, Any]],
    trace_id: str,
    bus: "EventBus | None",
) -> str:
    """Execute soft within-session compression and store the summary.

    Args:
        session_id: Session identifier.
        messages: Snapshot of the working message list at trigger time.
        trace_id: Trace identifier.
        bus: Optional event bus threaded through to telemetry.

    Returns:
        The compressed summary string.  When the LLM compressor is
        skipped (``compressor`` role missing) or fails, returns
        :data:`~personal_agent.orchestrator.context_compressor.FALLBACK_MARKER`
        so existing callers (``apply_context_window``) take the static
        marker path.
    """
    # Local imports to avoid an import cycle: within_session_compression
    # imports from compression_manager would happen otherwise via the
    # executor module graph.
    from personal_agent.orchestrator.context_compressor import FALLBACK_MARKER
    from personal_agent.orchestrator.within_session_compression import (
        compress_in_place,
    )

    compressed, record = await compress_in_place(
        messages,
        trace_id=trace_id,
        session_id=session_id,
        trigger="soft",
        bus=bus,
    )
    if not record.summariser_called:
        return FALLBACK_MARKER

    # Recover the inserted summary marker from the compressed list.  Per
    # ``_assemble_compressed`` it is the unique system message starting
    # with the canonical "## Conversation Summary" header from
    # ``_COMPRESSOR_SYSTEM_PROMPT``.
    for msg in compressed:
        content = msg.get("content")
        if (
            msg.get("role") == "system"
            and isinstance(content, str)
            and content.lstrip().startswith("## Conversation Summary")
        ):
            _summaries[session_id] = content
            return content

    return FALLBACK_MARKER


def cleanup_session(session_id: str) -> None:
    """Remove all compression state for a session.

    Args:
        session_id: Session to clean up.
    """
    task = _pending_tasks.pop(session_id, None)
    if task is not None and not task.done():
        task.cancel()
    _summaries.pop(session_id, None)
    _last_compressed_at_msgcount.pop(session_id, None)


def clear_all() -> None:
    """Clear all state. Intended for testing."""
    for task in _pending_tasks.values():
        if not task.done():
            task.cancel()
    _pending_tasks.clear()
    _summaries.clear()
    _last_compressed_at_msgcount.clear()
