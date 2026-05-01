"""Within-session progressive context compression — head-middle-tail (ADR-0061).

Public surface:
- :func:`compress_in_place` — given a working message list, produce a new list
  with the head and tail preserved verbatim and the middle compressed
  (deterministic pre-pass + LLM summariser).  Returns the rewritten list and
  the :class:`~personal_agent.telemetry.within_session_compression.WithinSessionCompressionRecord`
  describing what changed.
- :func:`needs_hard_compression` — fast token-threshold check called from
  ``orchestrator.executor.step_llm_call`` before each LLM dispatch.

Internal helpers (``_extract_head``, ``_extract_tail``, ``_assemble_compressed``)
are pure functions — easy to unit-test without an event loop.

The module deliberately keeps all *policy* (thresholds, tail floors, pre-pass
threshold) in :mod:`personal_agent.config.settings` so the orchestrator and
manager call sites can be exercised in tests with overridden settings.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from personal_agent.config import settings
from personal_agent.orchestrator.context_compressor import (
    _pre_pass_tool_outputs,
    summarize_middle,
)
from personal_agent.orchestrator.context_window import (
    estimate_message_tokens,
    estimate_messages_tokens,
)
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.within_session_compression import (
    WithinSessionCompressionRecord,
    record_compression,
)

if TYPE_CHECKING:
    from personal_agent.events.bus import EventBus

log = get_logger(__name__)


SUMMARY_ROLE = "system"


# ---------------------------------------------------------------------------
# Pure helpers — head / tail / assembly
# ---------------------------------------------------------------------------


def _extract_head(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the head band: system messages plus the first user message.

    Per ADR-0061 §D2 — every leading ``system`` message is preserved
    unconditionally; the *first* ``user`` message (the original task
    instruction) is preserved as part of the head.  Assistant or tool
    messages encountered before the first user message are *not* in the
    head — they belong to the middle.

    The head is contiguous from index 0; once the first user message is
    found, the next non-system message ends the head walk.
    """
    head: list[dict[str, Any]] = []
    seen_first_user = False
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            head.append(msg)
            continue
        if role == "user" and not seen_first_user:
            head.append(msg)
            seen_first_user = True
            continue
        # First non-system, non-first-user message ends the head walk.
        break
    return head


def _extract_tail(
    messages: list[dict[str, Any]],
    head_len: int,
    *,
    min_tokens: int,
    min_turns: int,
) -> list[dict[str, Any]]:
    """Return the tail band: trailing messages totalling ≥ min_tokens AND ≥ min_turns.

    Per ADR-0061 §D3 — walk backwards from the end, accumulating messages
    until both invariants hold.  Never crosses into the head band.

    Tool-pair invariant: when a kept ``role="tool"`` message references a
    ``tool_call_id`` whose matching assistant ``tool_calls`` message lies
    further back, that assistant message is pulled into the tail too.
    Without this, ``_sanitize_tool_pairs`` (``context_window.py``) would
    silently drop the orphaned tool reply when the assembled compressed
    output is fed to the LLM.

    Args:
        messages: Full working message list.
        head_len: Number of leading messages already claimed by the head;
            the tail walk stops at this index.
        min_tokens: Tail token floor.
        min_turns: Tail turn-count floor.

    Returns:
        Tail slice, in original order.  Length ≥ min(min_turns,
        len(messages) - head_len); token sum ≥ min_tokens unless the
        available middle+tail is smaller than the floor.
    """
    if head_len >= len(messages):
        return []

    available_indices = list(range(head_len, len(messages)))
    tail_idx_set: set[int] = set()
    used_tokens = 0

    for idx in reversed(available_indices):
        if used_tokens >= min_tokens and len(tail_idx_set) >= min_turns:
            break
        tail_idx_set.add(idx)
        used_tokens += estimate_message_tokens(messages[idx])

    # Tool-pair safety: pull in any assistant message whose tool_calls back
    # a tool message we already kept.
    needed_tool_ids: set[str] = set()
    for idx in tail_idx_set:
        msg = messages[idx]
        if msg.get("role") == "tool":
            tool_call_id = msg.get("tool_call_id")
            if tool_call_id:
                needed_tool_ids.add(str(tool_call_id))

    if needed_tool_ids:
        for idx in available_indices:
            if idx in tail_idx_set:
                continue
            msg = messages[idx]
            if msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls") or []:
                tc_id = tc.get("id") if isinstance(tc, dict) else None
                if tc_id and str(tc_id) in needed_tool_ids:
                    tail_idx_set.add(idx)
                    break

    return [messages[i] for i in sorted(tail_idx_set)]


def _assemble_compressed(
    head: list[dict[str, Any]],
    summary: str | None,
    pre_passed_middle: list[dict[str, Any]],
    tail: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Assemble the final compressed message list.

    Order: head → (summary marker if any) → pre-passed middle (only if
    summariser was skipped or empty — when a summary exists, it replaces
    the middle entirely) → tail.

    When a summary is present, the pre-passed middle is *not* re-appended:
    the LLM summary is the canonical compressed form.  When the
    summariser is skipped (empty middle) or fails (returns the fallback
    marker), the pre-passed middle is kept so we still benefit from the
    deterministic byte savings.
    """
    out: list[dict[str, Any]] = list(head)
    if summary:
        out.append({"role": SUMMARY_ROLE, "content": summary})
    else:
        out.extend(pre_passed_middle)
    out.extend(tail)
    return out


def needs_hard_compression(
    messages: list[dict[str, Any]], max_tokens: int
) -> bool:
    """Return True when the working messages list exceeds the hard threshold.

    Per ADR-0061 §D1 — fires synchronous compression mid-orchestration.

    Args:
        messages: Current working message list.
        max_tokens: Effective context-window ceiling (typically
            ``settings.context_window_max_tokens``).
    """
    if not settings.within_session_compression_enabled:
        return False
    if max_tokens <= 0:
        return False
    threshold = int(max_tokens * settings.within_session_hard_threshold_ratio)
    if threshold <= 0:
        return False
    return estimate_messages_tokens(messages) >= threshold


# ---------------------------------------------------------------------------
# Public entry point — used by manager (soft) and executor (hard)
# ---------------------------------------------------------------------------


async def compress_in_place(
    messages: list[dict[str, Any]],
    *,
    trace_id: str,
    session_id: str,
    trigger: Literal["soft", "hard"],
    bus: "EventBus | None" = None,
    pre_pass_threshold_tokens: int | None = None,
    min_tail_tokens: int | None = None,
    min_tail_turns: int = 4,
) -> tuple[list[dict[str, Any]], WithinSessionCompressionRecord]:
    """Compress *messages* using head-middle-tail with deterministic pre-pass.

    Per ADR-0061 §D2-D5 the algorithm is:
      1. Extract head (system + first user).
      2. Extract tail (last K tokens with K-turn floor).
      3. Pre-pass the middle band — replace large tool outputs with 1-line
         descriptors.
      4. If the pre-passed middle is non-empty, call the LLM compressor.
      5. Assemble head + summary (or pre-passed middle on summariser skip)
         + tail.
      6. Dual-write a :class:`WithinSessionCompressionRecord` to durable
         JSONL and the bus (ADR-0054 §D4).

    Args:
        messages: Current working message list.  Not mutated; a new list
            is returned.
        trace_id: Request trace identifier.
        session_id: Session identifier.
        trigger: ``"soft"`` (async between turns) or ``"hard"``
            (synchronous mid-orchestration).
        bus: Optional event bus for the bus publish half of the dual-write.
            Pass ``None`` in unit tests; the durable JSONL still writes.
        pre_pass_threshold_tokens: Override for
            ``settings.within_session_pre_pass_threshold_tokens``.
        min_tail_tokens: Override for
            ``settings.within_session_min_tail_tokens``.
        min_tail_turns: Hard floor on the number of trailing messages
            kept in the tail.

    Returns:
        Tuple of ``(compressed_messages, record)``.
    """
    threshold = (
        pre_pass_threshold_tokens
        if pre_pass_threshold_tokens is not None
        else settings.within_session_pre_pass_threshold_tokens
    )
    tail_token_floor = (
        min_tail_tokens
        if min_tail_tokens is not None
        else settings.within_session_min_tail_tokens
    )

    head = _extract_head(messages)
    head_len = len(head)
    tail = _extract_tail(
        messages,
        head_len,
        min_tokens=tail_token_floor,
        min_turns=min_tail_turns,
    )
    tail_idx_start = len(messages) - len(tail) if tail else len(messages)
    middle = messages[head_len:tail_idx_start]

    middle_tokens_in = estimate_messages_tokens(middle)
    pre_passed, replacements = _pre_pass_tool_outputs(
        middle, threshold_tokens=threshold
    )

    summariser_called = False
    summariser_duration_ms = 0
    summary: str | None = None
    if pre_passed:
        from personal_agent.orchestrator.context_compressor import FALLBACK_MARKER

        summary_text, duration_ms = await summarize_middle(
            pre_passed, trace_id=trace_id
        )
        summariser_duration_ms = duration_ms
        if summary_text and summary_text != FALLBACK_MARKER:
            summary = summary_text
            summariser_called = True

    compressed = _assemble_compressed(head, summary, pre_passed, tail)

    head_tokens = estimate_messages_tokens(head)
    tail_tokens = estimate_messages_tokens(tail)
    if summary is not None:
        middle_tokens_out = estimate_message_tokens(
            {"role": SUMMARY_ROLE, "content": summary}
        )
    else:
        middle_tokens_out = estimate_messages_tokens(pre_passed)

    record = WithinSessionCompressionRecord(
        trace_id=trace_id,
        session_id=session_id,
        trigger=trigger,
        head_tokens=head_tokens,
        middle_tokens_in=middle_tokens_in,
        middle_tokens_out=middle_tokens_out,
        tail_tokens=tail_tokens,
        pre_pass_replacements=replacements,
        summariser_called=summariser_called,
        summariser_duration_ms=summariser_duration_ms,
        compressed_at=datetime.now(timezone.utc),
    )

    log.info(
        "within_session_compression_completed",
        trace_id=trace_id,
        session_id=session_id,
        trigger=trigger,
        input_messages=len(messages),
        output_messages=len(compressed),
        head_tokens=head_tokens,
        middle_tokens_in=middle_tokens_in,
        middle_tokens_out=middle_tokens_out,
        tail_tokens=tail_tokens,
        pre_pass_replacements=replacements,
        summariser_called=summariser_called,
        summariser_duration_ms=summariser_duration_ms,
    )

    try:
        await record_compression(record, bus)
    except OSError as exc:
        # Durable write failure — record the gap but do not propagate;
        # the rewritten messages list is still useful to the caller and
        # losing the orchestrator turn over a telemetry write would be
        # worse than losing the observability line.
        log.warning(
            "within_session_compression_telemetry_failed",
            trace_id=trace_id,
            session_id=session_id,
            error=str(exc),
        )

    return compressed, record
