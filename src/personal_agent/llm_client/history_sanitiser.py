"""History sanitiser for cross-provider tool call consistency.

When a session spans multiple LLM providers (e.g. local Qwen → cloud Anthropic),
the conversation history can contain `tool_result` messages referencing
`tool_use_id`s that the current provider never issued, causing Anthropic and
other strict validators to reject the request with a 400.

This module walks the assembled message list before each dispatch and strips
any orphaned `tool` role messages (result with no matching call) and any
orphaned `tool_calls` entries in assistant messages (call with no matching result).

If stripping leaves an assistant message with neither tool_calls nor content,
that assistant message is dropped entirely to avoid an empty turn.

Telemetry: emits ``history_sanitised`` on every invocation (even when clean)
so real-world occurrence rates can be tracked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from personal_agent.telemetry import get_logger
from personal_agent.telemetry.events import HISTORY_SANITISED

log = get_logger(__name__)


@dataclass(frozen=True)
class SanitiseReport:
    """Summary of changes made by the sanitiser.

    Attributes:
        orphaned_results_stripped: Number of ``role: "tool"`` messages removed
            because their ``tool_call_id`` was never issued by any assistant
            message in the history.
        orphaned_calls_stripped: Number of ``tool_calls`` entries removed from
            assistant messages because no matching ``role: "tool"`` result
            exists anywhere in the history.
        assistant_messages_modified: Number of assistant messages whose
            ``tool_calls`` list was trimmed (may be less than
            orphaned_calls_stripped when one message had multiple orphans).
        truncated: True if the history was truncated to the last clean user
            turn because sanitisation alone could not produce a valid history.
        was_dirty: True if any change was made (convenience flag).
    """

    orphaned_results_stripped: int
    orphaned_calls_stripped: int
    assistant_messages_modified: int
    truncated: bool

    @property
    def was_dirty(self) -> bool:
        """True if the sanitiser made any change to the history."""
        return bool(
            self.orphaned_results_stripped
            or self.orphaned_calls_stripped
            or self.truncated
        )


def sanitise_messages(
    messages: list[dict[str, Any]],
    trace_id: str | None = None,
) -> tuple[list[dict[str, Any]], SanitiseReport]:
    """Strip orphaned tool_result / tool_call entries from a message history.

    This is a defence-in-depth guard that runs on every dispatch, regardless
    of provider. When both sides are clean (the common case) the function
    returns the original list object unchanged.

    Algorithm (two-pass):
    1. Collect every ``id`` issued via ``tool_calls`` in assistant messages
       (``issued_ids``), and every ``tool_call_id`` referenced by ``role: "tool"``
       messages (``result_ids``).
    2. Compute orphan sets: ``orphaned_results = result_ids - issued_ids``,
       ``orphaned_calls = issued_ids - result_ids``.
    3. If both sets are empty, return immediately (no-op, O(n) cost).
    4. Otherwise rebuild the message list, dropping orphaned tool messages and
       trimming orphaned entries from assistant tool_calls lists.

    Fallback: If after the strip pass the history still contains inconsistencies
    (detected by a final validation sweep), truncate to the last user turn that
    precedes any remaining orphan so the session can continue cleanly.

    Args:
        messages: OpenAI-format message list (role / content / tool_calls /
            tool_call_id fields). Not mutated.
        trace_id: Optional trace ID for structured log correlation.

    Returns:
        Tuple of (sanitised_messages, SanitiseReport). When the history was
        already clean, sanitised_messages is the same object as the input.
    """
    # Pass 1: collect issued and result IDs.
    issued_ids: set[str] = set()
    result_ids: set[str] = set()

    for msg in messages:
        role = msg.get("role", "")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    tc_id = tc.get("id") or ""
                    if tc_id:
                        issued_ids.add(tc_id)
        elif role == "tool":
            tid = msg.get("tool_call_id") or ""
            if tid:
                result_ids.add(tid)

    orphaned_results: set[str] = result_ids - issued_ids
    orphaned_calls: set[str] = issued_ids - result_ids

    report = SanitiseReport(
        orphaned_results_stripped=0,
        orphaned_calls_stripped=0,
        assistant_messages_modified=0,
        truncated=False,
    )

    if not orphaned_results and not orphaned_calls:
        log.debug(
            HISTORY_SANITISED,
            orphaned_results_stripped=0,
            orphaned_calls_stripped=0,
            assistant_messages_modified=0,
            truncated=False,
            message_count=len(messages),
            trace_id=trace_id,
        )
        return messages, report

    # Pass 2: rebuild, stripping orphans.
    sanitised: list[dict[str, Any]] = []
    results_stripped = 0
    calls_stripped = 0
    assistants_modified = 0

    for msg in messages:
        role = msg.get("role", "")

        if role == "tool":
            tid = msg.get("tool_call_id", "")
            if tid in orphaned_results:
                results_stripped += 1
                continue
            sanitised.append(msg)

        elif role == "assistant" and msg.get("tool_calls"):
            original_calls: list[dict[str, Any]] = msg["tool_calls"]
            clean_calls = [
                tc
                for tc in original_calls
                if isinstance(tc, dict) and tc.get("id") not in orphaned_calls
            ]
            n_stripped = len(original_calls) - len(clean_calls)
            if n_stripped > 0:
                calls_stripped += n_stripped
                assistants_modified += 1
                msg_copy = dict(msg)
                if clean_calls:
                    msg_copy["tool_calls"] = clean_calls
                else:
                    msg_copy.pop("tool_calls", None)
                    # Drop this assistant turn entirely if it is now empty.
                    if not msg_copy.get("content"):
                        continue
                sanitised.append(msg_copy)
            else:
                sanitised.append(msg)

        else:
            sanitised.append(msg)

    # Post-strip validation: check for any remaining inconsistencies that the
    # simple strip pass could not fix (e.g. a tool result sandwiched between
    # turns where its call was already stripped above for a different reason).
    # In that case fall back to truncating at the last clean user turn.
    sanitised, was_truncated = _truncate_if_still_broken(sanitised, trace_id)

    report = SanitiseReport(
        orphaned_results_stripped=results_stripped,
        orphaned_calls_stripped=calls_stripped,
        assistant_messages_modified=assistants_modified,
        truncated=was_truncated,
    )

    log.info(
        HISTORY_SANITISED,
        orphaned_results_stripped=results_stripped,
        orphaned_calls_stripped=calls_stripped,
        assistant_messages_modified=assistants_modified,
        truncated=was_truncated,
        original_message_count=len(messages),
        sanitised_message_count=len(sanitised),
        trace_id=trace_id,
    )

    return sanitised, report


def _truncate_if_still_broken(
    messages: list[dict[str, Any]],
    trace_id: str | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Truncate to the last clean user turn if the history still has orphans.

    A "clean user turn" is the most recent ``role: "user"`` message that is
    preceded only by consistent tool_calls/tool_result pairs (no orphans in
    the prefix up to that point).

    This is a last-resort fallback; the strip pass in ``sanitise_messages``
    handles the common cases. If the history is clean after the strip pass,
    this function returns immediately.

    Args:
        messages: Messages after the strip pass.
        trace_id: Trace ID for log correlation.

    Returns:
        Tuple of (messages, truncated). If no truncation was needed, the
        original list is returned unchanged and truncated is False.
    """
    # Quick check: recompute orphans on the stripped list.
    issued: set[str] = set()
    results: set[str] = set()
    for msg in messages:
        role = msg.get("role", "")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    issued.add(tc["id"])
        elif role == "tool":
            tid = msg.get("tool_call_id") or ""
            if tid:
                results.add(tid)

    if not (results - issued) and not (issued - results):
        return messages, False

    # Still broken — find the last user turn before the first remaining orphan.
    last_clean_user_idx: int | None = None
    seen_issued: set[str] = set()

    for idx, msg in enumerate(messages):
        role = msg.get("role", "")
        if role == "user":
            # Record as a candidate clean point only if no orphans exist yet.
            current_results: set[str] = set()
            for m in messages[: idx + 1]:
                if m.get("role") == "tool":
                    tid = m.get("tool_call_id") or ""
                    if tid:
                        current_results.add(tid)
            if not (current_results - seen_issued):
                last_clean_user_idx = idx
        elif role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    seen_issued.add(tc["id"])

    if last_clean_user_idx is not None:
        truncated = messages[: last_clean_user_idx + 1]
        log.warning(
            "history_truncated_after_sanitise",
            original_count=len(messages),
            truncated_count=len(truncated),
            truncated_at_index=last_clean_user_idx,
            trace_id=trace_id,
        )
        return truncated, True

    # Cannot find a clean user turn — return the stripped list as-is and log.
    log.error(
        "history_sanitise_could_not_rescue",
        message_count=len(messages),
        trace_id=trace_id,
    )
    return messages, False
