#!/usr/bin/env python3
"""Durable, idempotent trigger ledger (FRE-829, ADR-0113 §2).

The durable actuation substrate ADR-0113's self-driving delivery loop builds on:
every ``tmux send-keys`` actuation (currently only ``gating_watcher.py``, later
also master via the FRE-831 whitelist wrapper) is recorded as a ledger event
*before* the send is attempted and marked consumed *after* it completes, so a
crash between any of ledger-write, send, and mark-consumed reconciles to
complete-exactly-once or is surfaced for owner intervention — never a silent
drop, never a blind replay. The ledger is also the durable source a future
``prime-master`` (FRE-832) reconstructs in-flight actuation from, so it survives
a ``/clear``.

**Three-state crash model** on ``(send_started_at, sent_at, consumed_at)`` — the
reason a bare "was it sent?" boolean is not enough: a crash can land in three
distinguishable places, each demanding a different response.

- ``send_started_at is None`` — the send was never attempted (or the write
  recording the attempt never landed). Safe to retry: nothing happened yet.
- ``send_started_at`` set, ``sent_at is None`` — the crash happened *during* the
  send call itself. Ambiguous: the command may have partially reached ``tmux``.
  Never retried automatically — surfaced for the owner instead, because a blind
  replay here risks double-actuating a not-fully-idempotent action.
- ``sent_at`` set, ``consumed_at is None`` — the send is *known* to have
  succeeded (``sent_at`` is only ever set after ``send_to_session`` returns
  ``"sent"``); the crash was purely in the post-send bookkeeping. Closed out
  directly (``mark_consumed``) — never replayed.

**Unconfirmed delivery — the fourth state (FRE-939).** ``queued_at`` marks an
entry whose command *was* injected but into a **busy** pane, so receipt was
never observed. This is deliberately NOT one of the three crash states: the send
demonstrably completed (no ambiguity about partial keystrokes), only the target's
*receipt* is unknown, and the entry must stay re-offerable rather than becoming
terminal. ``reconcile`` therefore skips it — the caller's own resolution pass
owns it (``gating_watcher.resolve_queued_triggers``), re-offering into an idle
pane and escalating by age.

``queued_at`` is written **before** the keystrokes are injected, never after the
send returns. Writing it after would leave an ordinary crash window — keys in,
``queued_at`` not yet durable — whose on-disk shape is exactly the
``send_started_at``-set/``sent_at``-None ambiguous state above, so ``reconcile``
would mark it terminally ``surfaced`` and delivery for that PR would be disabled
forever: a fresh variant of the bug FRE-939 exists to close.

**Dedup folds in the trigger's own TTL window.** ``record_pending`` refuses a
duplicate write while an entry is unconsumed (still in-flight, or surfaced and
awaiting the owner) *and* refuses a fresh write for ``ttl_s`` after a
successfully-sent entry was consumed — this mirrors (and backs up) the
watcher's own TTL suppression dict, so a lost write to that separate dict can
never cause a same-tick double-send purely because the ledger forgot the
window too. An *abandoned* consume (``sent_at`` never set — the target was
busy/absent, not a crash) carries no such window: it is immediately
re-attemptable, matching the existing watcher's behaviour of retrying a
busy/absent skip on the very next tick.

**Transport tag (FRE-872, ADR-0116).** Each entry also carries which transport actually delivered
it (``channel`` | ``send_keys``, default ``send_keys``). ``record_pending``'s existing per-``event_id``
dedup is what delivers exactly-once, transport-aware behavior — a single entry per event id, tagged
post-hoc via ``mark_transport`` only once a channel delivery is confirmed, never optimistically.

Callable by hand::

    python -m scripts.dispatch.trigger_ledger --unconsumed --json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Literal, Protocol

SendOutcome = Literal["sent", "busy", "absent", "queued"]
Transport = Literal["channel", "send_keys"]


class Logger(Protocol):
    """The structlog subset this module uses."""

    def info(self, event: str, **fields: object) -> None:
        """Emit an info event."""
        ...

    def warning(self, event: str, **fields: object) -> None:
        """Emit a warning event."""
        ...


@dataclasses.dataclass(frozen=True)
class LedgerEntry:
    """One durable actuation event.

    Attributes:
        event_id: The idempotency key (the watcher's existing
            ``<kind>:<pr>:<sha>`` dedup key).
        source: A short reason (e.g. ``master-ready`` / ``worker-bounce``).
        target_pane: The tmux session the command is sent to.
        ticket: The PR or ticket this event concerns.
        command: The exact command line a retry would resend.
        preconditions: A small snapshot of the state that justified this event
            (e.g. the head SHA), for audit/reconstruction.
        created_at: When the entry was first written (ledger-before-send).
        send_started_at: Set immediately before the send attempt; ``None``
            means the send was never attempted.
        sent_at: Set only after the send is confirmed to have succeeded.
        queued_at: Set when the command was injected into a **busy** pane, so
            delivery was issued but never observed (FRE-939). Written before
            the keystrokes, never after. Mutually exclusive with ``sent_at``:
            the same attempt cannot be both observed and unobserved.
        consumed_at: Set once bookkeeping is fully closed (sent or abandoned).
        surfaced_at: Set when reconciliation cannot safely resolve the entry —
            terminal-pending, requires owner intervention, never auto-retried.
        transport: Which transport actually delivered this event (FRE-872,
            ADR-0116). Always created ``"send_keys"`` — there is no
            "optimistic" write. The *only* place this ever becomes
            ``"channel"`` is a caller's ``mark_transport`` call made after a
            channel POST is confirmed delivered; a same-tick fallback to
            send-keys simply never calls it, so the default stays accurate.
            This keeps the three-state crash model intact: a never-confirmed
            entry recovered by ``reconcile()`` is retried via the existing
            universal tmux path and is *correctly* audited as ``send_keys``,
            because that is genuinely how it was (re)delivered.
    """

    event_id: str
    source: str
    target_pane: str
    ticket: str
    command: str
    preconditions: Mapping[str, str]
    created_at: float
    send_started_at: float | None = None
    sent_at: float | None = None
    queued_at: float | None = None
    consumed_at: float | None = None
    surfaced_at: float | None = None
    transport: Transport = "send_keys"


Ledger = dict[str, LedgerEntry]


def record_pending(
    ledger: Ledger,
    *,
    event_id: str,
    source: str,
    target_pane: str,
    ticket: str,
    command: str,
    preconditions: Mapping[str, str],
    now: float,
    ttl_s: float,
) -> tuple[Ledger, Literal["new", "duplicate"]]:
    """Write a fresh pending entry (ledger-before-send), or dedupe a duplicate.

    Args:
        ledger: The current ledger.
        event_id: The idempotency key for this event.
        source: A short reason for the event.
        target_pane: The tmux session the command targets.
        ticket: The PR or ticket this event concerns.
        command: The exact command line to send.
        preconditions: A small state snapshot justifying this event.
        now: Wall-clock epoch seconds.
        ttl_s: The trigger kind's suppression TTL (mirrors the caller's own
            dedup window, so it is never lost to a crash).

    Returns:
        The updated ledger and ``"new"`` when a fresh entry was written, or the
        unchanged ledger and ``"duplicate"`` when this event must not actuate
        again right now.
    """
    existing = ledger.get(event_id)
    if existing is not None:
        if existing.consumed_at is None:
            return ledger, "duplicate"
        if existing.sent_at is not None and (now - existing.consumed_at) < ttl_s:
            return ledger, "duplicate"
    updated = dict(ledger)
    updated[event_id] = LedgerEntry(
        event_id=event_id,
        source=source,
        target_pane=target_pane,
        ticket=ticket,
        command=command,
        preconditions=dict(preconditions),
        created_at=now,
    )
    return updated, "new"


def mark_send_started(ledger: Ledger, event_id: str, now: float) -> Ledger:
    """Record that a send attempt is about to begin."""
    updated = dict(ledger)
    updated[event_id] = dataclasses.replace(updated[event_id], send_started_at=now)
    return updated


def mark_sent(ledger: Ledger, event_id: str, now: float) -> Ledger:
    """Record that the send is confirmed to have succeeded."""
    updated = dict(ledger)
    updated[event_id] = dataclasses.replace(updated[event_id], sent_at=now)
    return updated


def mark_queued(ledger: Ledger, event_id: str, now: float) -> Ledger:
    """Record that the command is about to be injected into a **busy** pane.

    Call this *before* the keystrokes are sent (FRE-939) — see the module
    docstring for why the ordering is load-bearing. The entry deliberately
    stays unconsumed: delivery was issued but never observed, so it remains
    re-offerable and surfaceable.
    """
    updated = dict(ledger)
    updated[event_id] = dataclasses.replace(updated[event_id], queued_at=now)
    return updated


def mark_consumed(ledger: Ledger, event_id: str, now: float) -> Ledger:
    """Close out an entry's bookkeeping (sent, or abandoned without sending)."""
    updated = dict(ledger)
    updated[event_id] = dataclasses.replace(updated[event_id], consumed_at=now)
    return updated


def mark_surfaced(ledger: Ledger, event_id: str, now: float) -> Ledger:
    """Flag an entry as unresolvable automatically — owner intervention required."""
    updated = dict(ledger)
    updated[event_id] = dataclasses.replace(updated[event_id], surfaced_at=now)
    return updated


def mark_transport(ledger: Ledger, event_id: str, transport: Transport) -> Ledger:
    """Record which transport actually delivered an entry (FRE-872, ADR-0116).

    Call this exactly once, only after a channel delivery is confirmed
    (never optimistically, never as a same-tick "correction" on fallback —
    see ``LedgerEntry.transport``'s docstring). Every entry is created at the
    ``"send_keys"`` default; this is the only way it ever becomes
    ``"channel"``.
    """
    updated = dict(ledger)
    updated[event_id] = dataclasses.replace(updated[event_id], transport=transport)
    return updated


def reconcile(
    ledger: Ledger,
    *,
    now: float,
    execute_pending: Callable[[LedgerEntry], SendOutcome],
    persist: Callable[[Ledger], None],
    logger: Logger,
) -> Ledger:
    """Resolve every unresolved entry per the three-state crash model.

    Runs at the start of each tick (a "restart" and "the next tick after a
    crash" are the same code path here, since ledger state is reloaded fresh
    from disk each time). ``persist`` is called after every entry transition —
    the crash-safety contract depends on each step landing durably, not just on
    the returned in-memory ledger.

    Args:
        ledger: The current ledger.
        now: Wall-clock epoch seconds.
        execute_pending: Retries a never-attempted entry's send. Mirrors
            ``send_to_session``'s contract (``sent``/``busy``/``absent``); a
            raised exception is treated as an unresolvable retry failure.
        persist: Persists the ledger after each entry's transition.
        logger: Structured logger.

    Returns:
        The fully-reconciled ledger.
    """
    for event_id, entry in list(ledger.items()):
        if entry.consumed_at is not None or entry.surfaced_at is not None:
            continue  # terminal already
        if entry.sent_at is not None:
            # Known to have succeeded before the crash -- close out, never resend.
            ledger = mark_consumed(ledger, event_id, now)
            persist(ledger)
            logger.info("trigger_ledger_reconcile_consumed_known_sent", event_id=event_id)
            continue
        if entry.queued_at is not None:
            # Injected into a busy pane -- delivery issued but never observed
            # (FRE-939). NOT a crash artifact, so the ambiguity branch below
            # must not claim it: that would mark it terminally ``surfaced`` and
            # kill the re-offer. The caller's resolution pass owns this entry.
            continue
        if entry.send_started_at is not None:
            # Crash occurred *during* the send call -- genuinely ambiguous.
            ledger = mark_surfaced(ledger, event_id, now)
            persist(ledger)
            logger.warning("trigger_ledger_reconcile_surfaced_ambiguous", event_id=event_id)
            continue
        # Never attempted -- safe to retry.
        ledger = mark_send_started(ledger, event_id, now)
        persist(ledger)
        try:
            outcome = execute_pending(entry)
        except (RuntimeError, OSError) as exc:
            ledger = mark_surfaced(ledger, event_id, now)
            persist(ledger)
            logger.warning(
                "trigger_ledger_reconcile_surfaced_error", event_id=event_id, error=str(exc)
            )
            continue
        if outcome == "sent":
            ledger = mark_sent(ledger, event_id, now)
            persist(ledger)
            ledger = mark_consumed(ledger, event_id, now)
            persist(ledger)
            logger.info("trigger_ledger_reconcile_completed", event_id=event_id)
        elif outcome == "queued":
            # An unconfirmed delivery (FRE-939) -- issued into a busy pane, so
            # it must stay unconsumed and re-offerable rather than being closed
            # out as an abandoned non-send below.
            ledger = mark_queued(ledger, event_id, now)
            persist(ledger)
            logger.warning("trigger_ledger_reconcile_queued", event_id=event_id)
        else:
            # Confirmed non-attempt (busy/absent) -- abandon, eligible for a
            # fresh attempt next tick (record_pending never suppresses an
            # abandoned, never-sent consume).
            ledger = mark_consumed(ledger, event_id, now)
            persist(ledger)
    return ledger


def snapshot_unconsumed(ledger: Ledger) -> tuple[LedgerEntry, ...]:
    """Return every entry not yet fully closed out (pending or surfaced).

    This is the durable read a future ``prime-master`` (FRE-832) reconstructs
    in-flight actuation from, and this ticket's own proof that an unconsumed
    trigger survives a simulated context clear (a fresh ``load_ledger`` call).
    """
    return tuple(entry for entry in ledger.values() if entry.consumed_at is None)


def load_ledger(path: Path, logger: Logger) -> Ledger:
    """Load the ledger; empty if absent, loudly-warned-and-empty if corrupt.

    A missing file (the ledger has never been written) is ordinary and silent.
    A file that exists but fails to parse is not: silently treating it as
    empty would be exactly the silent loss of in-flight triggers this ledger
    exists to prevent, so it is logged as a warning instead.
    """
    if not path.exists():
        return {}
    try:
        raw: object = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("trigger_ledger_corrupt", path=str(path), error=str(exc))
        return {}
    if not isinstance(raw, dict):
        logger.warning("trigger_ledger_corrupt", path=str(path), error="not a JSON object")
        return {}
    entries: Ledger = {}
    for event_id, fields in raw.items():
        if not isinstance(fields, dict):
            continue
        transport = fields.get("transport", "send_keys")
        if transport not in ("channel", "send_keys"):
            transport = "send_keys"
        entries[event_id] = LedgerEntry(
            event_id=event_id,
            source=str(fields.get("source", "")),
            target_pane=str(fields.get("target_pane", "")),
            ticket=str(fields.get("ticket", "")),
            command=str(fields.get("command", "")),
            preconditions=dict(fields.get("preconditions") or {}),
            created_at=float(fields.get("created_at", 0.0)),
            send_started_at=fields.get("send_started_at"),
            sent_at=fields.get("sent_at"),
            queued_at=fields.get("queued_at"),
            consumed_at=fields.get("consumed_at"),
            surfaced_at=fields.get("surfaced_at"),
            transport=transport,
        )
    return entries


def save_ledger(path: Path, ledger: Ledger) -> None:
    """Persist the ledger atomically (temp file + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {event_id: dataclasses.asdict(entry) for event_id, entry in ledger.items()}, indent=2
    )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload)
    os.replace(tmp, path)


def prune_ledger(
    ledger: Ledger, *, now: float, retention_s: float, open_prs: Collection[int]
) -> Ledger:
    """Drop terminal (consumed) entries past retention or for closed PRs.

    An entry with ``consumed_at is None`` (still pending or surfaced) is never
    dropped, regardless of age — those are exactly the entries that must not
    silently disappear.

    The closed-PR eviction only applies to a PR-ticketed entry (``ticket`` is
    numeric, e.g. ``"412"``). A non-PR-ticketed entry (e.g. a session-keyed
    ``ticket`` like ``"cc-master"`` for a context-pressure nudge, FRE-848) has
    no PR to close against, so it can never match ``open_prs`` either way —
    without this guard it would be evicted on the very next prune (often the
    same tick it was written), defeating the ledger's crash-safety backup for
    any non-PR-keyed trigger. It ages out by ``retention_s`` alone instead,
    same as a PR-ticketed entry does once its PR closes.

    Args:
        ledger: The current ledger.
        now: Wall-clock epoch seconds.
        retention_s: How long a consumed entry is kept for audit purposes.
        open_prs: The PR numbers still open.

    Returns:
        The pruned ledger.
    """
    open_set = {str(number) for number in open_prs}
    kept: Ledger = {}
    for event_id, entry in ledger.items():
        if entry.consumed_at is None:
            kept[event_id] = entry
            continue
        if now - entry.consumed_at >= retention_s:
            continue
        if entry.ticket.isdigit() and entry.ticket not in open_set:
            continue
        kept[event_id] = entry
    return kept


def _default_ledger_path() -> Path:
    """Return the default trigger-ledger path under the repo's telemetry dir."""
    return Path("telemetry") / "trigger_ledger.json"


def _entry_to_json(entry: LedgerEntry) -> dict[str, object]:
    """Serialize a `LedgerEntry` to a JSON-safe dict."""
    return {
        "event_id": entry.event_id,
        "source": entry.source,
        "target_pane": entry.target_pane,
        "ticket": entry.ticket,
        "command": entry.command,
        "preconditions": dict(entry.preconditions),
        "created_at": entry.created_at,
        "send_started_at": entry.send_started_at,
        "sent_at": entry.sent_at,
        "queued_at": entry.queued_at,
        "surfaced_at": entry.surfaced_at,
        "transport": entry.transport,
    }


class _CLILogger:
    """Detects a corrupt-ledger warning for the one-shot CLI read (FRE-832).

    `load_ledger` already logs-and-swallows a corrupt file as `{}` — the
    right behavior for a long-running caller (the watcher), which must not
    crash on a bad file. A one-shot CLI read needs to tell "genuinely no
    triggers" apart from "the file exists but failed to parse" — the two
    must not both print as an empty/healthy result to a caller (`prime-master`)
    reconstructing in-flight state from this read.
    """

    def __init__(self) -> None:
        self.corrupted = False

    def info(self, event: str, **fields: object) -> None:
        """Ignore info events — not actionable for a one-shot CLI read."""

    def warning(self, event: str, **fields: object) -> None:
        """Record a corrupt-ledger warning and echo it to stderr."""
        if event == "trigger_ledger_corrupt":
            self.corrupted = True
        print(f"warning: {event} {fields}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Prints unconsumed ledger entries (the `/clear`-safe read, FRE-832)."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--ledger-file", default=str(_default_ledger_path()), help="Path to the trigger ledger."
    )
    parser.add_argument(
        "--unconsumed",
        action="store_true",
        help="Print entries not yet fully closed out (pending or surfaced).",
    )
    parser.add_argument("--json", action="store_true", help="Emit the result as JSON.")
    args = parser.parse_args(argv)

    if not args.unconsumed:
        parser.error("--unconsumed is required (the only supported read today)")

    logger = _CLILogger()
    ledger = load_ledger(Path(args.ledger_file), logger)
    if logger.corrupted:
        print(
            "error: trigger ledger file is corrupt -- cannot determine in-flight state",
            file=sys.stderr,
        )
        return 1
    entries = snapshot_unconsumed(ledger)

    if args.json:
        print(json.dumps([_entry_to_json(entry) for entry in entries], indent=2))
    elif not entries:
        print("none")
    else:
        for entry in entries:
            if entry.surfaced_at is not None:
                state = "surfaced"
            elif entry.queued_at is not None:
                state = "queued"  # injected into a busy pane, receipt unconfirmed
            else:
                state = "pending"
            print(f"{entry.event_id} [{state}] ticket={entry.ticket} target={entry.target_pane}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
