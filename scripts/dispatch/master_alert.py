"""The alert a dispatch daemon sends to ``cc-master`` (FRE-1540).

Two daemons use it: ``gating_watcher.py`` (a worker trigger that stays undelivered) and
``orchestrator.py`` (a notify-ledger entry that stays unconsumed). Both wait
``ALERT_AFTER_S``, send one line, and record ``alerted_at`` in their own ledger file.

**Message form.** A plain one-line prefix, ``[DISPATCH ALERT]``. The PR trigger is the slash
command ``/master <n>``. A prefix that does not start with ``/`` cannot invoke a skill by
mistake. The text carries its own instruction, so master can act on it without the skill loaded.
One line only: ``tmux send-keys -l`` followed by ``Enter`` submits at the first newline.
"""

from __future__ import annotations

import datetime

from scripts.dispatch.trigger_ledger import LedgerEntry

ALERT_PREFIX = "[DISPATCH ALERT]"
ALERT_AFTER_S: float = 900.0  # 15 minutes

# Longest value copied into the text. A notify ``question`` can hold a whole seat prompt.
_MAX_FIELD_CHARS = 200

_INSTRUCTION = (
    "Follow the Dispatch alert section of the master skill: check the live state, act if you can, "
    "then send the owner one push line."
)


def is_due(entry: LedgerEntry, now: float, after_s: float = ALERT_AFTER_S) -> bool:
    """Return whether ``entry`` needs its one alert now.

    The caller decides whether the entry is still open. This checks only the clock and the latch.

    Args:
        entry: The ledger entry for the episode.
        now: Wall-clock epoch seconds.
        after_s: Seconds the episode must last before master is alerted.

    Returns:
        ``True`` when no alert was sent yet and the episode is at least ``after_s`` old.
    """
    return entry.alerted_at is None and (now - entry.created_at) >= after_s


def _one_line(value: object) -> str:
    """Collapse whitespace so the value fits one line, and cap its length."""
    text = " ".join(str(value).split())
    if len(text) <= _MAX_FIELD_CHARS:
        return text
    return text[: _MAX_FIELD_CHARS - 1] + "…"


def _utc(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def format_undelivered_trigger(
    *, pr: int, seat: str, reason: str, first_attempt_at: float, attempts: int, now: float
) -> str:
    """Return the alert for a worker trigger that stays undelivered.

    Args:
        pr: The PR number.
        seat: The seat the trigger was meant for, or ``none`` when no seat owns the PR.
        reason: Why the latest attempt failed, e.g. ``channel_delivery_failed+busy``.
        first_attempt_at: Epoch seconds of the first attempt in this episode.
        attempts: Delivery attempts so far.
        now: Wall-clock epoch seconds.

    Returns:
        One line of text for ``cc-master``.
    """
    minutes = int((now - first_attempt_at) // 60)
    return (
        f"{ALERT_PREFIX} A worker trigger for PR #{pr} is undelivered after {minutes} minutes. "
        f"Seat: {_one_line(seat)}. Reason: {_one_line(reason)}. "
        f"First attempt: {_utc(first_attempt_at)}. Attempts: {attempts}. {_INSTRUCTION}"
    )


def format_notify_entry(entry: LedgerEntry, now: float) -> str:
    """Return the alert for a notify-ledger entry that stays unconsumed.

    Args:
        entry: The open notify-ledger entry (``target_pane`` holds the stream).
        now: Wall-clock epoch seconds.

    Returns:
        One line of text for ``cc-master``.
    """
    minutes = int((now - entry.created_at) // 60)
    question = entry.preconditions.get("question")
    asked = f" Question: {_one_line(question)}." if question else ""
    return (
        f"{ALERT_PREFIX} A dispatch condition is open for {minutes} minutes. "
        f"Source: {_one_line(entry.source)}. Stream: {_one_line(entry.target_pane)}. "
        f"Ticket: {_one_line(entry.ticket)}.{asked} {_INSTRUCTION}"
    )
