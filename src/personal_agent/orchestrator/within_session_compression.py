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

from dataclasses import dataclass
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


# NOT "system": _validate_and_fix_conversation_roles keeps only the first
# system message and silently drops later ones.  An assistant-role recap
# survives role-fixing in place, matching FROZEN_RECAP_ROLE (FRE-576 F2).
SUMMARY_ROLE = "assistant"


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
    max_tokens: int | None = None,
) -> list[dict[str, Any]]:
    """Return the tail band as a bounded, user-anchored, contiguous suffix.

    Per ADR-0061 §D3 as amended by FRE-942. Precedence, highest first:

    1. **Contiguity.** The result is always a suffix ``messages[start:]`` with
       ``start >= head_len``. Callers derive the middle boundary as
       ``len(messages) - len(tail)``, which is only sound for a contiguous suffix.
    2. **User-alignment.** ``start`` is advanced to the first ``user`` turn at or
       after the walk's stopping point, so the recap→tail seam stays alternating
       and no orphaned assistant/tool prefix is handed to the assembler. If no user
       turn is available, the tail is ``[]`` (the whole band falls to the middle).
       Because an assistant/tool pair never straddles a user turn, a user-anchored
       suffix can never contain a ``tool`` message whose assistant lies outside it —
       which is why the old backward tool-pair repair is gone (FRE-942).
    3. **Ceiling.** When ``max_tokens`` is set, the backward walk stops before a
       message that would push the running total past it — *unless* the tail is
       still empty, so a single oversized trailing message is never dropped outright
       (it may later fall to the middle at step 2 if it carries no user turn). The
       exemption is one *message*, not one semantic turn.
    4. **Floors.** ``min_tokens`` / ``min_turns`` bound the *walk*, not the returned
       value: the ceiling may stop the walk before either is met, and user-alignment
       may trim the result below either afterwards. Both are best-effort.

    Args:
        messages: Full working message list.
        head_len: Number of leading messages already claimed by the head; the walk
            never crosses this index.
        min_tokens: Tail token floor (best-effort — see rule 4).
        min_turns: Tail turn-count floor (best-effort — see rule 4).
        max_tokens: Optional tail ceiling. ``None`` disables the bound (legacy
            behaviour); ``<= 0`` collapses to the single-message exemption.

    Returns:
        A contiguous, user-anchored suffix in original order, or ``[]``.
    """
    if head_len >= len(messages):
        return []

    total = len(messages)
    start = total
    used_tokens = 0
    for idx in range(total - 1, head_len - 1, -1):
        candidate = estimate_message_tokens(messages[idx])
        # Ceiling — skipped only while the tail is still empty (start == total),
        # so the most recent message is always admitted.
        if max_tokens is not None and start < total and used_tokens + candidate > max_tokens:
            break
        start = idx
        used_tokens += candidate
        if used_tokens >= min_tokens and (total - start) >= min_turns:
            break

    # User-alignment — advance to the first user turn in the selected band.
    for idx in range(start, total):
        if messages[idx].get("role") == "user":
            return messages[idx:]
    return []


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


# ---------------------------------------------------------------------------
# Frozen-prefix re-establishment (ADR-0081 §D2 Decision 5 / §D3, FRE-434)
# ---------------------------------------------------------------------------

# Role of the persisted recap under the frozen layout. NOT "system": the
# role-fixer keeps only the first system message and silently drops later ones
# (executor._validate_and_fix_conversation_roles), so a system-role recap at
# index 1+ would be deleted on the next dispatch. An assistant "context recap"
# survives the role-fix in place.
FROZEN_RECAP_ROLE = "assistant"


@dataclass(frozen=True)
class FrozenResetResult:
    """Outcome of a scheduled frozen-prefix reset.

    Attributes:
        messages: The canonical post-reset history —
            ``[first user][assistant recap][K verbatim tail turns]`` — persisted
            into ``session.messages`` so the next turn forward-extends it.
        salient_highlights: A bounded, volatile distillation of the narrative
            that rides the *next* turn's volatile block (regenerated each turn,
            never frozen). Empty when there is no narrative.
        narrative: The cumulative frozen-recap text (also the recap message
            content). Empty when the middle was empty.
    """

    messages: list[dict[str, Any]]
    salient_highlights: str
    narrative: str


def _bound_highlights(narrative: str, max_chars: int) -> str:
    """Return a hard-bounded distillation of *narrative* for the volatile tail.

    Deterministic character-bound (a proxy for the token cap) so the highlights
    cannot erode the compression win. A dedicated key-decision extraction is a
    follow-up refinement (ADR-0081 §D3); the bound is the invariant that matters.
    """
    text = narrative.strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip()


async def build_frozen_reset(
    messages: list[dict[str, Any]],
    *,
    trace_id: str,
    session_id: str,
    pre_pass_threshold_tokens: int | None = None,
    min_tail_tokens: int | None = None,
    max_tail_tokens: int | None = None,
    min_tail_turns: int = 4,
    salient_highlights_max_chars: int = 1200,
) -> FrozenResetResult:
    """Compact cold turns into a cumulative recap and re-establish a frozen prefix.

    Per ADR-0081 §D2 Decision 5 / §D3 a scheduled reset produces the canonical
    history ``[first user][assistant recap][last K verbatim turns]``. The recap
    is **cumulative**: any prior recap sits in the middle band and is fed back into
    the summariser, so no cold context is lost across successive resets. The turn
    after the reset forward-extends this sequence, so local KV reuse resumes (the
    sawtooth rising edge).

    Args:
        messages: Current working message list (system prompt is separate).
        trace_id: Request trace identifier.
        session_id: Session identifier.
        pre_pass_threshold_tokens: Override for the deterministic pre-pass
            threshold.
        min_tail_tokens: Absolute tail floor; defaults to
            ``within_session_min_tail_ratio · context_window_max_tokens``.
        max_tail_tokens: Absolute tail ceiling; defaults to
            ``within_session_max_tail_ratio · context_window_max_tokens`` (FRE-942).
        min_tail_turns: Minimum verbatim tail turns to keep.
        salient_highlights_max_chars: Hard bound on the volatile highlights.

    Returns:
        A :class:`FrozenResetResult`.
    """
    threshold = (
        pre_pass_threshold_tokens
        if pre_pass_threshold_tokens is not None
        else settings.within_session_pre_pass_threshold_tokens
    )
    tail_token_floor = (
        min_tail_tokens
        if min_tail_tokens is not None
        else int(settings.within_session_min_tail_ratio * settings.context_window_max_tokens)
    )
    tail_token_ceiling = (
        max_tail_tokens
        if max_tail_tokens is not None
        else int(settings.within_session_max_tail_ratio * settings.context_window_max_tokens)
    )

    head = _extract_head(messages)
    head_len = len(head)
    # _extract_tail returns a user-anchored contiguous suffix (FRE-942), so it
    # already starts on a user turn — the former _tail_starting_on_user pass is
    # folded into the walk.
    tail = _extract_tail(
        messages,
        head_len,
        min_tokens=tail_token_floor,
        min_turns=min_tail_turns,
        max_tokens=tail_token_ceiling,
    )
    tail_idx_start = len(messages) - len(tail) if tail else len(messages)
    middle = messages[head_len:tail_idx_start]

    pre_passed, _replacements = _pre_pass_tool_outputs(middle, threshold_tokens=threshold)

    narrative = ""
    if pre_passed:
        from personal_agent.orchestrator.context_compressor import FALLBACK_MARKER

        summary_text, _duration_ms = await summarize_middle(
            pre_passed, trace_id=trace_id, session_id=session_id
        )
        if summary_text and summary_text != FALLBACK_MARKER:
            narrative = summary_text

    out: list[dict[str, Any]] = list(head)
    if narrative:
        out.append({"role": FROZEN_RECAP_ROLE, "content": narrative})
    out.extend(tail)

    log.info(
        "frozen_reset_built",
        trace_id=trace_id,
        session_id=session_id,
        input_messages=len(messages),
        output_messages=len(out),
        kept_tail_turns=len(tail),
        narrative_chars=len(narrative),
    )

    return FrozenResetResult(
        messages=out,
        salient_highlights=_bound_highlights(narrative, salient_highlights_max_chars),
        narrative=narrative,
    )


def needs_hard_compression(messages: list[dict[str, Any]], max_tokens: int) -> bool:
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
    max_tail_tokens: int | None = None,
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
        min_tail_tokens: Absolute override for the tail floor. When ``None``,
            the floor is computed as
            ``int(settings.within_session_min_tail_ratio *
            settings.context_window_max_tokens)``.
        max_tail_tokens: Absolute override for the tail ceiling (FRE-942). When
            ``None``, computed as
            ``int(settings.within_session_max_tail_ratio *
            settings.context_window_max_tokens)``.
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
        else int(settings.within_session_min_tail_ratio * settings.context_window_max_tokens)
    )
    tail_token_ceiling = (
        max_tail_tokens
        if max_tail_tokens is not None
        else int(settings.within_session_max_tail_ratio * settings.context_window_max_tokens)
    )

    head = _extract_head(messages)
    head_len = len(head)
    # _extract_tail already returns a user-anchored contiguous suffix (FRE-942), so
    # the assistant-role summary never lands immediately before another assistant
    # message and the former _tail_starting_on_user pass is unnecessary.
    tail = _extract_tail(
        messages,
        head_len,
        min_tokens=tail_token_floor,
        min_turns=min_tail_turns,
        max_tokens=tail_token_ceiling,
    )
    tail_idx_start = len(messages) - len(tail) if tail else len(messages)
    middle = messages[head_len:tail_idx_start]

    middle_tokens_in = estimate_messages_tokens(middle)
    pre_passed, replacements = _pre_pass_tool_outputs(middle, threshold_tokens=threshold)

    summariser_called = False
    summariser_duration_ms = 0
    summary: str | None = None
    if pre_passed:
        from personal_agent.orchestrator.context_compressor import FALLBACK_MARKER

        summary_text, duration_ms = await summarize_middle(
            pre_passed, trace_id=trace_id, session_id=session_id
        )
        summariser_duration_ms = duration_ms
        if summary_text and summary_text != FALLBACK_MARKER:
            summary = summary_text
            summariser_called = True

    compressed = _assemble_compressed(head, summary, pre_passed, tail)

    head_tokens = estimate_messages_tokens(head)
    tail_tokens = estimate_messages_tokens(tail)
    if summary is not None:
        middle_tokens_out = estimate_message_tokens({"role": SUMMARY_ROLE, "content": summary})
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
