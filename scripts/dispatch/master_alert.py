"""The alert a dispatch daemon sends to ``cc-master`` (FRE-1540).

Two daemons use it: ``gating_watcher.py`` (a worker trigger that stays undelivered) and
``orchestrator.py`` (a notify-ledger entry that stays unconsumed). Both wait
``ALERT_AFTER_S``, send one line, and record ``alerted_at`` in their own ledger file.

**Message form.** A plain one-line prefix, ``[DISPATCH ALERT]``. The PR trigger is the slash
command ``/master <n>``. A prefix that does not start with ``/`` cannot invoke a skill by
mistake. The text carries its own instruction, so master can act on it without the skill loaded.
One line only: ``tmux send-keys -l`` followed by ``Enter`` submits at the first newline.

**Owner path (FRE-1550).** When ``cc-master`` itself is the stuck party, the send to master is
refused on every tick. After a further ``OWNER_ALERT_AFTER_S`` the daemon sends one line to the
owner instead (``owner_notify``). ``is_owner_due`` reads the clock and the two latches.
"""

from __future__ import annotations

import datetime

from scripts.dispatch.trigger_ledger import LedgerEntry

ALERT_PREFIX = "[DISPATCH ALERT]"
ALERT_AFTER_S: float = 900.0  # 15 minutes
# How long a refused alert to master may stay deferred before the owner is told directly.
OWNER_ALERT_AFTER_S: float = 900.0  # 15 minutes after the alert to master is due

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


def is_owner_due(entry: LedgerEntry, now: float) -> bool:
    """Return whether ``entry`` needs its one direct owner notification now (FRE-1550).

    The caller decides whether the entry is still open and whether master refused the send.
    This checks only the clock and the two latches.

    Args:
        entry: The ledger entry for the episode.
        now: Wall-clock epoch seconds.

    Returns:
        ``True`` when master was never alerted, the owner was never notified, and the episode
        is older than ``ALERT_AFTER_S`` plus ``OWNER_ALERT_AFTER_S``.
    """
    return (
        entry.alerted_at is None
        and entry.owner_alerted_at is None
        and (now - entry.created_at) >= ALERT_AFTER_S + OWNER_ALERT_AFTER_S
    )


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


_OWNER_TAIL = "cc-master did not take the alert. Check the cc-master input box."


def format_owner_undelivered_trigger(
    *, pr: int, seat: str, first_attempt_at: float, now: float
) -> str:
    """Return the owner line for a worker trigger that stays undelivered (FRE-1550).

    Args:
        pr: The PR number.
        seat: The seat the trigger was meant for, or ``none`` when no seat owns the PR.
        first_attempt_at: Epoch seconds of the first attempt in this episode.
        now: Wall-clock epoch seconds.

    Returns:
        One short line for the owner. It names the PR.
    """
    minutes = int((now - first_attempt_at) // 60)
    return (
        f"PR #{pr}: a worker trigger for {_one_line(seat)} is undelivered after {minutes} minutes. "
        f"{_OWNER_TAIL}"
    )


def format_owner_notify_entry(entry: LedgerEntry, now: float) -> str:
    """Return the owner line for a notify-ledger entry that stays unconsumed (FRE-1550).

    Args:
        entry: The open notify-ledger entry (``target_pane`` holds the stream).
        now: Wall-clock epoch seconds.

    Returns:
        One short line for the owner. It names the condition, the stream and the ticket.
    """
    minutes = int((now - entry.created_at) // 60)
    return (
        f"Dispatch condition {_one_line(entry.source)} is open for {minutes} minutes. "
        f"Stream: {_one_line(entry.target_pane)}. Ticket: {_one_line(entry.ticket)}. {_OWNER_TAIL}"
    )


def format_owner_held_master_trigger(entry: LedgerEntry, now: float) -> str:
    """Return the owner line for a trigger to ``cc-master`` that stays unconfirmed (FRE-1550).

    Args:
        entry: The unconfirmed ledger entry for the master trigger.
        now: Wall-clock epoch seconds.

    Returns:
        One short line for the owner. It names the PR, or the ticket when it is not a PR number.
    """
    minutes = int((now - entry.created_at) // 60)
    subject = f"PR #{entry.ticket}" if entry.ticket.isdigit() else _one_line(entry.ticket)
    return (
        f"{subject}: the {_one_line(entry.source)} trigger for cc-master is unconfirmed after "
        f"{minutes} minutes. cc-master did not take it. Check the cc-master input box."
    )
