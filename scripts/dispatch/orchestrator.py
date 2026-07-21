#!/usr/bin/env python3
"""Dispatch orchestrator loop — poll, launch, advance (FRE-787, ADR-0110 T3).

Integrates the NEXT resolver (T1, ``next_resolver.py``) and the launch
primitive (T2, ``launcher.py``) into the poll-based dispatch loop of ADR-0110
§2: for each stream, when it is idle with a NEXT ticket, launch a worker
through the launcher; hold the concurrency guard; and advance to the next
dispatch on the durable completion signal — an open PR plus the ticket reaching
``In Review`` — with a stall timeout for liveness.

Two distinct transitions are kept separate (the owner's refinement, 2026-07-05):

- ``run_complete`` — the dispatched run delivered a PR (``In Review`` + an open
  PR). Stall-watching stops, but the stream **stays occupied**: a PR at
  ``In Review`` is at master's gate and can be bounced, so the stream is not
  free for a new dispatch yet.
- ``clear`` — the ticket reached a **terminal merge state**
  (``Awaiting Deploy``/``Done``/``Canceled``/``Duplicate``). Only now does the
  stream free for the next dispatch. This is identical to the current
  ``prime-worker`` busy-guard: a stream is occupied through the whole
  review/bounce cycle and frees only at merge.

The orchestrator is **dispatch-only** — it has no merge/deploy/close code path
(ADR-0110 §5, AC-4). It never launches a worker in a mode that strips hooks, so
the ``check-pytest-lock`` PreToolUse hook stays live (AC-5), and it never
dispatches into an occupied stream (the resolver's busy guard, AC-6). It
advances a stream only on the durable open-PR + ``In Review`` evidence, never on
silence (AC-7 part b).

RC programmatic completion (``claude agents --json`` per-session status) is a
deferred latency optimisation — the ADR calls auto-detect "only a latency
optimization, not a dependency"; v1 advances on the durable signal.

Callable by hand::

    python -m scripts.dispatch.orchestrator --once            # one dry-run tick, prints decisions
    python -m scripts.dispatch.orchestrator --once --execute  # one real tick (launches)
    python -m scripts.dispatch.orchestrator --loop            # daemon loop (systemd, FRE-788)
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlparse

import structlog

from scripts.dispatch.launcher import (
    CommandRunner,
    execute_plan,
    find_warm_session,
    known_streams,
    plan_launch,
    seat_state,
    seat_wedge_signature,
    subprocess_runner,
    topology_for,
)
from scripts.dispatch.next_resolver import (
    IssueSnapshot,
    fetch_board,
    resolve_next,
)
from scripts.reconcile_board import load_linear_key

# Terminal merge states — the stream frees for the next dispatch only here.
# Matches the resolver's blocked-relation terminal set (merge, not deploy).
_TERMINAL_STATES: frozenset[str] = frozenset(
    {"awaiting deploy", "done", "canceled", "cancelled", "duplicate"}
)

# Tier label → launcher model tier.
_TIER_MODEL: dict[str, str] = {
    "Tier-1:Opus": "opus",
    "Tier-2:Sonnet": "sonnet",
    "Tier-3:Haiku": "haiku",
}

# Default dispatch streams (the three worker worktrees). Order is INTENTIONAL —
# it is the per-tick consideration order — so this stays an explicit tuple
# rather than being derived from ``known_streams()`` (which sorts, and would
# silently promote ``adr`` ahead of ``build1``). Drift from the launcher
# topology is caught by a test asserting every entry is a real stream, which is
# the actual risk; the ordering is a deliberate choice, not duplication.
DEFAULT_STREAMS: tuple[str, ...] = ("build1", "build2", "adr")

# Stall grace: a launched run with no PR after this long triggers a liveness
# notification (never a re-dispatch). Generous — a long Opus build is normal;
# the stall path only notifies, so a false positive is harmless noise.
DEFAULT_STALL_TIMEOUT_S: float = 3600.0

# Poll interval for the daemon loop (``--loop``).
DEFAULT_POLL_INTERVAL_S: float = 300.0

# Consecutive suspected-wedge ticks to tolerate before surfacing (FRE-922, CC
# #61568). A wedge is a seat Remote Control reports busy while its pane is idle
# — an orphaned ``run_in_background`` poller — which makes the stream's reuse
# dispatch return ``seat-busy`` every tick and never land. The threshold rides
# out a one-tick RC/pane race (a genuine turn re-renders its spinner and resets
# the count); surfacing past N (default: the 3rd consecutive tick ≈ 15 min at
# the 300 s cadence) is fast enough to be useful, slow enough to avoid noise.
#
# The count is held IN-MEMORY across ticks within a daemon run and reset on
# restart — deliberately NOT persisted. Persisting it (an early design) made the
# one-shot master ping fragile: a restart, a changed ``--wedge-ticks``, or a
# crash between persist-and-notify could leave the count first observed *above*
# the crossing value, silently losing the single alert forever (code review,
# FRE-922). In-memory, the count only ever climbs by 1 from 0 per consecutive
# tick, so the crossing is hit exactly once and no stale value can survive. A
# wedge outliving a restart simply re-counts (~15 min) — acceptable, and the
# greppable warning still fires every tick meanwhile.
DEFAULT_WEDGE_TICKS: int = 2

# Held-too-long escalation threshold (FRE-924). A ``surfaced`` manual card
# (KEEP / manual-model-required / delivery-failed / seat-unhealthy) that stays
# the stream's NEXT re-emits a ``hold`` (``card-already-surfaced``) every tick
# and never self-clears — it awaits the owner. Nothing escalated its *age*:
# FRE-920's card sat held ~2.5 h, surfaced only because the owner noticed an
# idle stream. Past this age (wall-clock ``now - launched_at``, since the
# surfaced record pins ``launched_at`` at first-surface), the hold is escalated
# once as a distinct, greppable ``dispatch_held_too_long`` anomaly + one master
# ping. 30 min is a fair window for the owner to act on a fresh card (never a
# premature alarm) yet far short of 2.5 h. The escalation is age-based, not
# tick-based (unlike the wedge counter): the record carries a durable timestamp,
# so age is the honest, cadence-independent signal.
#
# The one-shot latch (``held_escalated``, stream → escalated ticket) is
# IN-MEMORY across ticks and reset on restart — the FRE-922 lesson: a persisted
# crossing state can be first-observed past its trigger after a restart/crash
# and silently lose the single alert. In-memory, a card outliving a restart
# simply re-escalates once (age is already past threshold) — at-least-once
# across restarts, exactly-once per (stream, ticket) episode within a run.
DEFAULT_HELD_ESCALATION_S: float = 1800.0
# Delivery attempts allowed for one ticket before the retry gives up and hands
# over to the owner (FRE-923). A dropped delivery is genuinely retryable — the
# seat is idle and ready, only the keystroke sequence dropped — so surfacing a
# terminal card on the FIRST partial send is what stranded FRE-920 for ~2.5h.
# Bounded rather than unlimited because a seat that refuses three full sequences
# is not mid-settle; past that, only a human can tell what is wrong. The count is
# PERSISTED (unlike the wedge counter, whose one-shot ping a stale value would
# break): here the budget must survive a daemon crash mid-delivery, or a restart
# loop would re-attempt forever with a perpetually fresh budget.
MAX_DELIVERY_ATTEMPTS: int = 3

# The only endpoint host at which Remote Control is enabled — it is disabled
# when ``ANTHROPIC_BASE_URL`` points anywhere else (an LLM gateway/proxy),
# per the RC docs (v2.1.196+).
_ANTHROPIC_API_HOST: str = "api.anthropic.com"

# Default kill-switch flag file: its mere presence halts all dispatch.
DEFAULT_KILL_SWITCH_FILE: str = "telemetry/dispatch.disabled"

DecisionKind = Literal[
    "launch", "await", "stall", "run_complete", "clear", "skip", "hold", "surface"
]


@dataclasses.dataclass(frozen=True)
class Precondition:
    """The result of the enable-once precondition check (ADR-0110 T4).

    Attributes:
        ok: Whether the statically-checkable preconditions are met.
        reason: Empty when ``ok``; otherwise a distinct, actionable reason
            string (never conflating unrelated failures).
    """

    ok: bool
    reason: str


def is_anthropic_endpoint(base_url: str) -> bool:
    """Return whether ``base_url`` keeps Remote Control enabled.

    Remote Control is disabled when ``ANTHROPIC_BASE_URL`` points at a host
    other than ``api.anthropic.com`` (RC docs, v2.1.196+). An empty/unset value
    means the default Anthropic endpoint, which is fine.

    Args:
        base_url: The ``ANTHROPIC_BASE_URL`` value (may be empty).

    Returns:
        ``True`` if unset/empty or the host is ``api.anthropic.com``.
    """
    if not base_url.strip():
        return True
    return (urlparse(base_url).hostname or "") == _ANTHROPIC_API_HOST


def check_preconditions(env: Mapping[str, str], api_key: str | None) -> Precondition:
    """Check the statically-verifiable enable-once preconditions (AC-b).

    Covers only what is deterministic from configuration: the Linear API key
    (the resolver needs it) and the Remote-Control endpoint
    (``ANTHROPIC_BASE_URL``). Remote-Control **auth/entitlement/subscription**
    are *not* checkable from the environment — those are the human enable-once
    steps in the runbook, verified with ``claude doctor`` and, at runtime, by
    the liveness guard (``rc_server_alive``) which refuses to dispatch when RC
    is unreachable. The two failure reasons are kept distinct, never merged.

    Args:
        env: The process environment (e.g. ``os.environ``).
        api_key: The resolved Linear API key, or ``None``.

    Returns:
        A ``Precondition`` — ``ok`` with an empty reason, or not-ok with a
        distinct, actionable reason string.
    """
    if not api_key:
        return Precondition(
            False,
            "linear-api-key-missing: AGENT_LINEAR_API_KEY is not configured; "
            "the dispatch resolver cannot read the board",
        )
    base_url = env.get("ANTHROPIC_BASE_URL", "")
    if not is_anthropic_endpoint(base_url):
        return Precondition(
            False,
            f"rc-endpoint-off-anthropic: ANTHROPIC_BASE_URL={base_url!r} points off "
            f"{_ANTHROPIC_API_HOST}; Remote Control is disabled off-endpoint — unset it "
            "(see docs/runbooks/dispatch-orchestrator.md)",
        )
    return Precondition(True, "")


def rc_server_alive(runner: CommandRunner) -> bool:
    """Probe **global** Remote-Control reachability (AC-a liveness guard).

    Runs ``claude agents --json --all`` (no TTY needed) and treats a zero exit
    as reachable. This proves RC is reachable at all, **not** that any specific
    stream's session or the templated RC unit is healthy — it is deliberately a
    global reachability signal. The orchestrator refuses to launch when this is
    down; the small time-of-check/time-of-use window (RC dying between the probe
    and the launch) is backstopped by the stall timeout.

    Args:
        runner: The command runner seam (shells ``claude``).

    Returns:
        ``True`` if the probe exits zero, else ``False``.
    """
    return runner(["claude", "agents", "--json", "--all"]).returncode == 0


def _kill_switch_engaged(path: Path) -> bool:
    """Return whether the kill-switch flag file exists (halts all dispatch)."""
    return path.exists()


def _launch_block_reason(
    rc_alive: Callable[[], bool], kill_switch_engaged: Callable[[], bool]
) -> str | None:
    """Return why a launch must be blocked this tick, or ``None`` to proceed.

    The kill switch is checked first so its reason is deterministic even when
    RC is also down.

    Args:
        rc_alive: Predicate — is Remote Control reachable.
        kill_switch_engaged: Predicate — is the kill switch engaged.

    Returns:
        ``"kill-switch"``, ``"rc-down"``, or ``None`` (launch permitted).
    """
    if kill_switch_engaged():
        return "kill-switch"
    if not rc_alive():
        return "rc-down"
    return None


class Notifier(Protocol):
    """A liveness-notification sink (default: a structlog warning)."""

    def __call__(self, event: str, **fields: object) -> None:
        """Emit a notification ``event`` with structured ``fields``."""
        ...


class Logger(Protocol):
    """The structlog subset the loop uses."""

    def info(self, event: str, **fields: object) -> None:
        """Emit an info event."""
        ...

    def warning(self, event: str, **fields: object) -> None:
        """Emit a warning event."""
        ...


@dataclasses.dataclass(frozen=True)
class DispatchRecord:
    """The orchestrator's per-stream tracking of a dispatch it acted on.

    Attributes:
        stream: Dispatch stream key.
        ticket: The tracked ticket identifier.
        phase: ``launched`` = an owned in-flight session (await
            completion/stall); ``surfaced`` = a manual card was shown (KEEP /
            manual-model-required), awaiting the owner; ``delivering`` = a
            dispatch attempt is in flight or dropped mid-sequence and is
            retryable (FRE-923).
        launched_at: Wall-clock (epoch seconds) the record was created.
        session_id: The launcher's session id, when known.
        run_confirmed: The run delivered a PR (reached ``In Review`` + open PR)
            — stall-watching stops once set.
        stall_notified: A stall notification has already fired (throttle).
        attempts: Dispatch attempts consumed for this ticket. Claimed *before*
            the attempt is made, so it survives a daemon crash mid-sequence and
            the retry budget cannot be silently reset by a restart. An attempt
            that never reached the seat (a transient outcome — busy seat, dirty
            worktree, failed create) is given back rather than counted.
    """

    stream: str
    ticket: str
    phase: Literal["launched", "surfaced", "delivering"]
    launched_at: float
    session_id: str | None
    run_confirmed: bool = False
    stall_notified: bool = False
    attempts: int = 0


@dataclasses.dataclass(frozen=True)
class StreamDecision:
    """A pure, side-effect-free decision for one stream in one tick.

    Attributes:
        stream: Dispatch stream key.
        kind: The decided action.
        ticket: The ticket to launch (``launch``) or being tracked, if any.
        model: The resolved model tier for a ``launch``.
        context_keep: Whether the ticket carries ``context:keep`` (``launch``).
        reason: A short human/log reason.
    """

    stream: str
    kind: DecisionKind
    ticket: str | None = None
    model: str | None = None
    context_keep: bool = False
    reason: str = ""


def model_for_labels(labels: frozenset[str]) -> str | None:
    """Return the launcher model tier for an issue's labels, or ``None``.

    Args:
        labels: The issue's label names.

    Returns:
        ``opus``/``sonnet``/``haiku``, or ``None`` when no ``Tier-*`` label is
        present (the orchestrator then refuses to launch at an unknown tier).
    """
    for label, model in _TIER_MODEL.items():
        if label in labels:
            return model
    return None


def _state_of(issues: Sequence[IssueSnapshot], ticket: str) -> str | None:
    """Return the board state name of ``ticket``, or ``None`` if absent."""
    for issue in issues:
        if issue.identifier == ticket:
            return issue.state
    return None


def decide(
    stream: str,
    issues: Sequence[IssueSnapshot],
    record: DispatchRecord | None,
    *,
    now: float,
    stall_timeout_s: float,
    tracked_pr_open: bool,
) -> StreamDecision:
    """Decide one stream's action for this tick (pure).

    Args:
        stream: Dispatch stream key.
        issues: The stream's board snapshot (all states, from the resolver).
        record: The orchestrator's current tracking for this stream, if any.
        now: Wall-clock epoch seconds.
        stall_timeout_s: Seconds after which a launched run with no PR stalls.
        tracked_pr_open: Whether an open PR exists for a launched record's
            ticket (resolved by the caller; irrelevant without a launched
            record).

    Returns:
        The decided ``StreamDecision``.
    """
    if record is None:
        return _decide_no_record(stream, issues)
    if record.phase == "delivering":
        return _decide_delivering(stream, issues, record)
    if record.phase == "surfaced":
        return _decide_surfaced(stream, issues, record)
    return _decide_launched(
        stream,
        issues,
        record,
        now=now,
        stall_timeout_s=stall_timeout_s,
        tracked_pr_open=tracked_pr_open,
    )


def _decide_no_record(stream: str, issues: Sequence[IssueSnapshot]) -> StreamDecision:
    """Resolve NEXT for an untracked stream."""
    nxt = resolve_next(issues, stream)
    if nxt is None:
        return StreamDecision(stream, "skip", reason="occupied-or-no-candidate")
    model = model_for_labels(nxt.labels)
    if model is None:
        return StreamDecision(stream, "skip", ticket=nxt.identifier, reason="no-tier-label")
    return StreamDecision(
        stream,
        "launch",
        ticket=nxt.identifier,
        model=model,
        context_keep="context:keep" in nxt.labels,
        reason="idle-with-next",
    )


def _decide_launched(
    stream: str,
    issues: Sequence[IssueSnapshot],
    record: DispatchRecord,
    *,
    now: float,
    stall_timeout_s: float,
    tracked_pr_open: bool,
) -> StreamDecision:
    """Decide for an owned in-flight (``launched``) record."""
    state = _state_of(issues, record.ticket)
    normalized = state.strip().lower() if state else None

    if normalized in _TERMINAL_STATES:
        return StreamDecision(stream, "clear", ticket=record.ticket, reason="merged")

    if normalized == "in review" and tracked_pr_open and not record.run_confirmed:
        return StreamDecision(
            stream, "run_complete", ticket=record.ticket, reason="pr-open-in-review"
        )

    # At the gate or building — hold (a bounce keeps it In Review; never re-dispatch).
    if normalized in {"in review", "in progress"}:
        return StreamDecision(stream, "await", ticket=record.ticket, reason="in-flight")

    # Not progressing (still Approved / unknown): stall only past the timeout.
    if not record.run_confirmed and now - record.launched_at > stall_timeout_s:
        return StreamDecision(stream, "stall", ticket=record.ticket, reason="no-pr-past-timeout")
    return StreamDecision(stream, "await", ticket=record.ticket, reason="starting")


def _decide_delivering(
    stream: str, issues: Sequence[IssueSnapshot], record: DispatchRecord
) -> StreamDecision:
    """Decide for a dropped-or-in-flight delivery (``delivering``), FRE-923.

    Reached one tick after a delivery dropped mid-sequence, or after the daemon
    died mid-delivery — indistinguishable from here, and identical in what they
    warrant: re-attempt the whole sequence while the budget holds. The seat is
    idle and ready; only the keystrokes dropped.

    Escalates to a manual card once the budget is spent, so this can never
    become the unbounded per-tick re-dispatch loop the ``surfaced`` phase was
    protecting against.
    """
    state = _state_of(issues, record.ticket)
    normalized = state.strip().lower() if state else None
    nxt = resolve_next(issues, stream)
    still_next = normalized == "approved" and nxt is not None and nxt.identifier == record.ticket
    # Owner-acted is checked BEFORE the budget, mirroring ``_decide_surfaced``:
    # a ticket that has already moved on is not this stream's business, and
    # surfacing a card for it would put a stale item in front of the owner.
    if not still_next:
        return StreamDecision(stream, "clear", ticket=record.ticket, reason="owner-acted")
    if record.attempts >= MAX_DELIVERY_ATTEMPTS:
        return StreamDecision(
            stream, "surface", ticket=record.ticket, reason="delivery-attempts-exhausted"
        )
    model = model_for_labels(nxt.labels) if nxt is not None else None
    if model is None:
        return StreamDecision(stream, "skip", ticket=record.ticket, reason="no-tier-label")
    return StreamDecision(
        stream,
        "launch",
        ticket=record.ticket,
        model=model,
        context_keep=nxt is not None and "context:keep" in nxt.labels,
        reason="retry-delivery",
    )


def _decide_surfaced(
    stream: str, issues: Sequence[IssueSnapshot], record: DispatchRecord
) -> StreamDecision:
    """Decide for a ``surfaced`` (manual-card) record."""
    state = _state_of(issues, record.ticket)
    normalized = state.strip().lower() if state else None
    nxt = resolve_next(issues, stream)
    still_next = normalized == "approved" and nxt is not None and nxt.identifier == record.ticket
    if not still_next:
        return StreamDecision(stream, "clear", ticket=record.ticket, reason="owner-acted")
    return StreamDecision(stream, "hold", ticket=record.ticket, reason="card-already-surfaced")


def _open_pr_exists(ticket: str, runner: CommandRunner) -> bool:
    """Return True if an open PR whose branch maps to ``ticket`` exists.

    Args:
        ticket: The ticket identifier (e.g. ``FRE-786``).
        runner: The command runner seam (shells ``gh``).

    Returns:
        Whether ``gh`` reports at least one open PR matching the ticket.
    """
    result = runner(
        ["gh", "pr", "list", "--search", ticket, "--state", "open", "--json", "number,headRefName"]
    )
    if result.returncode != 0:
        return False
    try:
        raw: object = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return False
    if not isinstance(raw, list):
        return False
    token = ticket.lower()
    return any(
        isinstance(pr, dict) and token in str(pr.get("headRefName", "")).lower() for pr in raw
    ) or bool(raw)


def _record_for_result(
    stream: str, ticket: str, outcome: str, now: float, *, attempts: int
) -> DispatchRecord | None:
    """Map a ``LaunchResult`` outcome to the record to store (or ``None``).

    A record is written **only** for an outcome that actually launches/prepares
    an owned session (``launched``), surfaces a manual card (``surfaced``), or
    is a retryable dropped delivery (``delivering``); a transient error
    (``worktree-dirty``/``launch-failed``) writes no record so the stream stays
    eligible and is never falsely marked in-flight.

    This reconciles the record ``_apply`` pre-wrote before executing, so a
    ``None`` here is not merely "write nothing" — it un-does the pre-write via
    the caller's ``state.pop`` (FRE-923).

    FRE-913 / FRE-923 outcomes:

    - ``reuse`` (a live seat dispatched in-session) is an owned in-flight run,
      exactly like ``launch``/``prepare``. Success resets ``attempts`` to 0.
    - ``registration-unverified`` is ``launched``: the seat is running and was
      seeded with the ticket, and only its Remote-Control *name* is wrong. Its
      run needs stall detection and ``run_complete`` tracking exactly like any
      other; the wrong name is a visibility warning carried on the card.
    - ``delivery-failed`` is ``delivering`` — **retryable** while the budget
      holds. The seat is idle and ready; only the keystroke sequence dropped, so
      the next tick re-attempts the whole ``/clear``→``/model``→``/build``. Past
      ``MAX_DELIVERY_ATTEMPTS`` it escalates to ``surfaced`` (FRE-923).
    - ``seat-unhealthy`` is ``surfaced`` immediately. Unlike a dropped delivery
      this cannot self-heal and re-attempting cannot help — the pane is not a
      usable claude in this worktree, so it needs a human.
    - ``seat-busy`` writes **no record**. It is the one genuinely transient
      outcome: the seat is simply mid-turn and will be idle shortly. Recording
      it as ``surfaced`` would hold the stream in ``_decide_surfaced`` forever
      over a condition that clears itself within seconds — trading a self-healing
      delay for a permanent stall that only the owner can clear.

    Args:
        stream: Dispatch stream key.
        ticket: The dispatched ticket.
        outcome: The ``LaunchResult`` outcome.
        now: Wall-clock epoch seconds.
        attempts: Delivery attempts consumed so far, including this one.

    Returns:
        The record to store, or ``None`` to leave the stream untracked.
    """
    if outcome in {"launch", "prepare", "reuse", "registration-unverified"}:
        return DispatchRecord(stream, ticket, "launched", now, session_id=None, attempts=0)
    if outcome == "delivery-failed" and attempts < MAX_DELIVERY_ATTEMPTS:
        return DispatchRecord(stream, ticket, "delivering", now, session_id=None, attempts=attempts)
    if outcome in {
        "manual-continuation",
        "manual-model-required",
        "delivery-failed",
        "seat-unhealthy",
    }:
        return DispatchRecord(stream, ticket, "surfaced", now, session_id=None, attempts=attempts)
    return None


def run_once(
    streams: Sequence[str],
    state: dict[str, DispatchRecord],
    *,
    now: float,
    stall_timeout_s: float,
    board_fetcher: Callable[[str], Sequence[IssueSnapshot]],
    runner: CommandRunner,
    notifier: Notifier,
    persist: Callable[[dict[str, DispatchRecord]], None],
    logger: Logger,
    execute: bool,
    rc_alive: Callable[[], bool] | None = None,
    kill_switch_engaged: Callable[[], bool] = lambda: False,
    wedge_counts: dict[str, int] | None = None,
    wedge_ticks: int = DEFAULT_WEDGE_TICKS,
    held_escalated: dict[str, str] | None = None,
    held_escalation_s: float = DEFAULT_HELD_ESCALATION_S,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, DispatchRecord]:
    """Run one orchestration tick across ``streams``, mutating and returning state.

    All wall-clock and network access is injected (``now``, ``board_fetcher``,
    ``runner``, ``persist``) so the tick is fully unit-testable. In dry-run
    (``execute=False``) it logs each decision and writes no record.

    Args:
        streams: The dispatch streams to process.
        state: Per-stream records, mutated in place.
        now: Wall-clock epoch seconds.
        stall_timeout_s: Stall grace seconds.
        board_fetcher: Returns a stream's board snapshot.
        runner: Command runner seam for the launcher, warm-session, and PR probe.
        notifier: Liveness-notification sink.
        persist: Persists the state dict after a mutation.
        logger: Structured logger.
        execute: Whether to actually launch (else dry-run, no side effects).
        rc_alive: Predicate for Remote-Control reachability (AC-a). Defaults to
            probing via ``rc_server_alive(runner)``; a launch is refused when it
            returns ``False``.
        kill_switch_engaged: Predicate for the kill switch (defaults to off);
            when engaged, all launches are refused.
        wedge_counts: Per-stream consecutive suspected-wedge tick counts (FRE-922),
            mutated in place across ticks within a daemon run (in-memory, reset on
            restart); defaults to a throwaway map when unused.
        wedge_ticks: Consecutive ticks to tolerate before surfacing a wedge.
        held_escalated: Per-stream one-shot held-too-long latch (FRE-924), stream →
            the ticket already escalated this hold-episode; mutated in place across
            ticks (in-memory, reset on restart); defaults to a throwaway map.
        held_escalation_s: Age (seconds since first-surface) past which a still-held
            surfaced card is escalated once.
        sleeper: The sleep seam used by the launcher's bounded delivery polls.
            Injected so a tick is fully unit-testable without wall-clocking —
            the delivery path polls for up to ten seconds per command.

    Returns:
        The updated state dict.
    """
    if rc_alive is None:
        rc_alive = lambda: rc_server_alive(runner)  # noqa: E731
    if wedge_counts is None:
        wedge_counts = {}
    if held_escalated is None:
        held_escalated = {}
    # De-dup while preserving order: a repeated ``--streams`` value must not
    # double-process a stream — and, since FRE-922, must not double-increment its
    # wedge counter and trip the threshold a tick early.
    for stream in dict.fromkeys(streams):
        trace_id = str(uuid.uuid4())
        issues = board_fetcher(stream)
        record = state.get(stream)
        tracked_pr_open = (
            _open_pr_exists(record.ticket, runner)
            if record is not None and record.phase == "launched"
            else False
        )
        decision = decide(
            stream,
            issues,
            record,
            now=now,
            stall_timeout_s=stall_timeout_s,
            tracked_pr_open=tracked_pr_open,
        )
        logger.info(
            "dispatch_decision",
            trace_id=trace_id,
            stream=stream,
            kind=decision.kind,
            ticket=decision.ticket,
            reason=decision.reason,
        )
        _apply(
            decision,
            state,
            now=now,
            trace_id=trace_id,
            runner=runner,
            notifier=notifier,
            persist=persist,
            logger=logger,
            execute=execute,
            rc_alive=rc_alive,
            kill_switch_engaged=kill_switch_engaged,
            wedge_counts=wedge_counts,
            wedge_ticks=wedge_ticks,
            held_escalated=held_escalated,
            held_escalation_s=held_escalation_s,
            sleeper=sleeper,
        )
    return state


def _apply(
    decision: StreamDecision,
    state: dict[str, DispatchRecord],
    *,
    now: float,
    trace_id: str,
    runner: CommandRunner,
    notifier: Notifier,
    persist: Callable[[dict[str, DispatchRecord]], None],
    logger: Logger,
    execute: bool,
    rc_alive: Callable[[], bool],
    kill_switch_engaged: Callable[[], bool],
    wedge_counts: dict[str, int],
    wedge_ticks: int,
    held_escalated: dict[str, str],
    held_escalation_s: float,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Apply one decision's side effects (launch / notify / record mutation)."""
    stream = decision.stream
    # A live wedge counter only accumulates on the launch→seat-busy→wedge path
    # (below); any other decision for this stream means it is not wedged this
    # tick, so clear the count. Resetting on every non-wedge tick — the
    # confirmed-wedge increment is the ONLY path that does not — keeps the
    # per-episode count honest (a genuinely-busy or freed stream never carries a
    # stale count into a later episode).
    if decision.kind != "launch":
        _reset_wedge(stream, wedge_counts)
    # The held-too-long escalation is a per-episode one-shot latch (FRE-924). Any
    # decision other than ``hold`` ends the episode — the card was acted on
    # (``clear``) or the stream moved on — so drop the latch; a later surfaced
    # hold is a fresh episode that must escalate again. Unlike the wedge counter,
    # ``launch`` is not special-cased: a launch is never a hold, so it resets like
    # every other non-hold decision.
    if decision.kind != "hold":
        held_escalated.pop(stream, None)
    match decision.kind:
        case "launch":
            assert decision.ticket is not None and decision.model is not None
            if execute:
                blocked = _launch_block_reason(rc_alive, kill_switch_engaged)
                if blocked is not None:
                    logger.warning(
                        "dispatch_blocked",
                        trace_id=trace_id,
                        stream=stream,
                        ticket=decision.ticket,
                        reason=blocked,
                    )
                    notifier(
                        "dispatch_blocked",
                        trace_id=trace_id,
                        stream=stream,
                        ticket=decision.ticket,
                        reason=blocked,
                    )
                    # A blocked tick observes no wedge (it never probes the seat),
                    # so it must not leave a stale count — the confirmed-wedge
                    # increment is the ONLY path that skips the reset.
                    _reset_wedge(stream, wedge_counts)
                    return  # no launch, no record — the stream stays eligible.
            warm = find_warm_session(stream, runner) if decision.context_keep else None
            # FRE-913: probe the seat so a LIVE one is dispatched into in-session
            # rather than recreated. Only ``execute`` probes — a dry run must not
            # shell out to tmux.
            seat = seat_state(topology_for(stream), runner) if execute else "absent"
            plan = plan_launch(
                stream,
                decision.ticket,
                decision.model,
                context_keep=decision.context_keep,
                warm_session_id=warm,
                seat=seat,
            )
            logger.info(
                "dispatch_plan",
                trace_id=trace_id,
                stream=stream,
                ticket=decision.ticket,
                model=decision.model,
                outcome=plan.outcome,
                seat=seat,
                card=plan.card,
            )
            if not execute:
                return
            # FRE-923: claim the attempt BEFORE making it. ``execute_plan`` is
            # strict within one process, but it types commands one at a time and
            # the orchestrator only persists after it returns — so a daemon crash
            # mid-sequence would otherwise leave no trace that a delivery was ever
            # in flight, and every restart would re-attempt with a fresh budget
            # forever. Persisting first is what makes the retry genuinely bounded
            # across restarts, not merely within one process.
            prior = state.get(stream)
            attempts = (
                prior.attempts if prior is not None and prior.ticket == decision.ticket else 0
            )
            attempts += 1
            state[stream] = DispatchRecord(
                stream, decision.ticket, "delivering", now, session_id=None, attempts=attempts
            )
            persist(state)
            result = execute_plan(plan, runner, sleeper=sleeper)
            logger.info(
                "dispatch_execute",
                trace_id=trace_id,
                stream=stream,
                ticket=decision.ticket,
                outcome=result.outcome,
                launched=result.launched,
                attempts=attempts,
            )
            new_record = _record_for_result(
                stream, decision.ticket, result.outcome, now, attempts=attempts
            )
            # FRE-923: the retry gave up. Announce it BEFORE committing the
            # ``surfaced`` record — persist-then-notify is exactly the window
            # FRE-922's review condemned: a crash in between leaves a record
            # already marked surfaced, and ``_decide_surfaced`` only ever holds
            # or clears, so the alert would be lost forever with no path to
            # re-fire it. Notify-then-persist can at worst repeat the alert on
            # the retried tick, which is the survivable direction. (The
            # ``surface`` case below and the pre-existing ``stall`` case order it
            # the same way.) This is distinct from FRE-924's age-based escalation
            # of an already-surfaced card: this fires at the transition.
            if result.outcome == "delivery-failed" and attempts >= MAX_DELIVERY_ATTEMPTS:
                notifier(
                    "dispatch_delivery_exhausted",
                    trace_id=trace_id,
                    stream=stream,
                    ticket=decision.ticket,
                    attempts=attempts,
                )
                logger.warning(
                    "dispatch_delivery_exhausted",
                    trace_id=trace_id,
                    stream=stream,
                    ticket=decision.ticket,
                    attempts=attempts,
                )
            if new_record is not None:
                state[stream] = new_record
            elif (
                prior is not None
                and prior.phase == "delivering"
                and prior.ticket == decision.ticket
            ):
                # FRE-923: a transient outcome (seat-busy / worktree-dirty /
                # launch-failed) typed NOTHING into the seat, so it must not
                # consume the retry budget — and it must not discard a retry
                # already in progress. Popping here would reset ``attempts`` to
                # zero, and a seat that alternates busy/delivery-failed across
                # ticks would then retry FOREVER without ever escalating, which
                # is precisely the unbounded loop the budget exists to prevent.
                # Restore the pre-write's predecessor rather than the pre-write:
                # an attempt that never reached the seat was never spent.
                state[stream] = prior
            else:
                state.pop(stream, None)
            persist(state)
            # FRE-922: a ``seat-busy`` outcome whose seat shows the suspected-wedge
            # signature (RC busy while the pane is idle) is counted; past the
            # threshold it is SURFACED — a distinct, greppable anomaly and a
            # one-shot master ping — instead of silently re-emitting ``seat-busy``
            # forever. Any other outcome (a real dispatch, or a genuinely-busy
            # seat whose spinner shows) resets the count. This path NEVER kills a
            # process: detection and surfacing only, master decides (AC-2).
            # (``execute`` is already True here — the dry-run early return above
            # precedes this — so the wedge check never runs in a dry-run tick.)
            if result.outcome == "seat-busy" and seat_wedge_signature(topology_for(stream), runner):
                _note_wedge(
                    stream,
                    decision.ticket,
                    wedge_counts,
                    wedge_ticks=wedge_ticks,
                    trace_id=trace_id,
                    notifier=notifier,
                    logger=logger,
                )
            else:
                _reset_wedge(stream, wedge_counts)
        case "run_complete":
            record = state.get(stream)
            if record is not None:
                state[stream] = dataclasses.replace(record, run_confirmed=True)
                persist(state)
        case "clear":
            if state.pop(stream, None) is not None:
                persist(state)
        case "stall":
            record = state.get(stream)
            if record is not None and not record.stall_notified:
                notifier(
                    "dispatch_stall",
                    trace_id=trace_id,
                    stream=stream,
                    ticket=decision.ticket,
                    launched_at=record.launched_at,
                )
                logger.warning(
                    "dispatch_stall", trace_id=trace_id, stream=stream, ticket=decision.ticket
                )
                state[stream] = dataclasses.replace(record, stall_notified=True)
                persist(state)
        case "hold":
            # FRE-924: a surfaced card still held past the age threshold is
            # escalated once — a distinct greppable anomaly + one master ping —
            # instead of only re-emitting the per-tick ``card-already-surfaced``
            # hold. Surface only: the record is never cleared/refreshed and no
            # process is terminated (AC-2); master decides.
            #
            # Gated on ``execute`` so a dry-run tick (``--once`` without
            # ``--execute``, used for inspection) stays side-effect-free — no
            # escalation warning, no master ping, no latch mutation — matching the
            # launch/wedge path's ``if not execute`` guard. (The pre-existing
            # ``stall`` case notifies ungated in dry-run; that inconsistency is
            # out of scope here — a possible follow-up.)
            record = state.get(stream)
            if execute and record is not None:
                _note_held(
                    stream,
                    record.ticket,
                    held_escalated,
                    now=now,
                    launched_at=record.launched_at,
                    held_escalation_s=held_escalation_s,
                    trace_id=trace_id,
                    notifier=notifier,
                    logger=logger,
                )
        case "surface":
            # FRE-923: the retry budget is spent — stop re-attempting and hand
            # over to the owner. Only reachable when the daemon died mid-delivery
            # (the in-process path escalates at reconcile), so it must be a real
            # terminal transition: holding the ``delivering`` record instead
            # would re-create the silent indefinite stall this ticket exists to
            # kill, just one phase along.
            record = state.get(stream)
            # Gated on ``execute`` so a dry-run tick stays side-effect-free — it
            # must neither ping the owner nor mutate/persist state. (FRE-844 and
            # the FRE-924 sibling both established this for reconcile/escalation
            # paths; the ``launch`` case gates the same way via its early
            # return.)
            if record is not None and execute:
                notifier(
                    "dispatch_delivery_exhausted",
                    trace_id=trace_id,
                    stream=stream,
                    ticket=decision.ticket,
                    attempts=record.attempts,
                )
                logger.warning(
                    "dispatch_delivery_exhausted",
                    trace_id=trace_id,
                    stream=stream,
                    ticket=decision.ticket,
                    attempts=record.attempts,
                )
                state[stream] = dataclasses.replace(record, phase="surfaced")
                persist(state)
        case _:  # await / skip — no state change.
            return


def _reset_wedge(stream: str, wedge_counts: dict[str, int]) -> None:
    """Clear a stream's in-memory suspected-wedge counter."""
    wedge_counts.pop(stream, None)


def _note_wedge(
    stream: str,
    ticket: str,
    wedge_counts: dict[str, int],
    *,
    wedge_ticks: int,
    trace_id: str,
    notifier: Notifier,
    logger: Logger,
) -> None:
    """Count a suspected-wedge tick and surface it past the threshold (FRE-922).

    Increments the stream's consecutive-wedge count (in-memory; see
    ``DEFAULT_WEDGE_TICKS``). Every tick past ``wedge_ticks`` emits a distinct,
    greppable ``dispatch_seat_wedged`` warning (so the anomaly is durable in the
    log stream, not lost in generic ``seat-busy`` noise). The **crossing** tick —
    the first to exceed the threshold — additionally pings master **once**
    (mirroring the stall throttle: actionable, not spammy). Because the count
    climbs by exactly 1 from 0 per consecutive tick, the crossing is hit exactly
    once — no persisted stale value can make it jump past and lose the ping.
    Detection and surfacing only — no process is ever terminated here; master
    decides whether to intervene (AC-2).

    Args:
        stream: The wedged stream.
        ticket: The ticket whose dispatch the wedge is blocking.
        wedge_counts: Per-stream counts, mutated in place.
        wedge_ticks: Consecutive ticks tolerated before surfacing.
        trace_id: The tick's trace id.
        notifier: The master-notification sink (pinged once, on crossing).
        logger: Structured logger (warns every post-threshold tick).
    """
    count = wedge_counts.get(stream, 0) + 1
    wedge_counts[stream] = count
    if count <= wedge_ticks:
        return
    logger.warning(
        "dispatch_seat_wedged",
        trace_id=trace_id,
        stream=stream,
        ticket=ticket,
        consecutive_ticks=count,
        detail="remote-control reports busy while the pane is idle — a suspected "
        "orphaned background poller (CC #61568); dispatch is blocked",
    )
    if count == wedge_ticks + 1:  # crossing tick: ping master exactly once.
        notifier(
            "dispatch_seat_wedged",
            trace_id=trace_id,
            stream=stream,
            ticket=ticket,
            consecutive_ticks=count,
        )


def _note_held(
    stream: str,
    ticket: str,
    held_escalated: dict[str, str],
    *,
    now: float,
    launched_at: float,
    held_escalation_s: float,
    trace_id: str,
    notifier: Notifier,
    logger: Logger,
) -> None:
    """Escalate a surfaced card held past the age threshold, once per episode (FRE-924).

    The card's age is ``now - launched_at`` (clamped at zero, so a future or
    corrupt ``launched_at`` reads as freshly-surfaced rather than logging a
    negative age). Within the threshold, or already escalated for this exact
    ``(stream, ticket)`` this episode, this is a no-op — leaving only the per-tick
    ``card-already-surfaced`` hold trail. Past the threshold and not yet
    escalated, it emits BOTH a distinct greppable ``dispatch_held_too_long``
    warning AND one master ping (once, unlike the wedge which warns every
    post-threshold tick) and latches the ticket in ``held_escalated``.

    The latch maps stream → the escalated ticket (not a bare stream set), so the
    suppression check is ``held_escalated.get(stream) == ticket``: if the surfaced
    ticket on a stream is swapped while it stays held (reachable only via external
    state surgery — the normal path emits ``clear`` first, dropping the latch),
    the value no longer matches and the new ticket still escalates. In-memory
    only — a card outliving a daemon restart re-escalates once (at-least-once
    across restarts, exactly-once per (stream, ticket) episode within a run).
    Detection + surfacing ONLY — the record is never mutated and no process is
    terminated here; master decides whether to intervene (AC-2).

    Scope (FRE-924): the age measures **continuous** hold of the current surfaced
    record — the FRE-920 incident shape (one card held ~2.5 h). It does NOT
    accumulate across board churn: if a higher-priority ticket outranks the held
    one, ``_decide_surfaced`` emits ``clear`` and the stream re-dispatches, writing
    a fresh surfaced record with a new ``launched_at`` (age reset). A persistently
    broken seat that re-fails delivery across *different* churned tickets
    ("repeating the same failure") is therefore NOT yet caught here — closing that
    needs the re-dispatch/atomicity half (FRE-923) or a ``_decide_surfaced``
    churn-vs-owner-action distinction; tracked as a follow-up, not silently
    covered.

    Args:
        stream: The held stream.
        ticket: The surfaced ticket the stream is held on.
        held_escalated: Per-stream latch (stream → escalated ticket), mutated in place.
        now: Wall-clock epoch seconds.
        launched_at: When the surfaced record was first created (first-surface time).
        held_escalation_s: Age threshold in seconds.
        trace_id: The tick's trace id.
        notifier: The master-notification sink (pinged once, on escalation).
        logger: Structured logger (warns once, on escalation).
    """
    age = max(0.0, now - launched_at)
    if age <= held_escalation_s or held_escalated.get(stream) == ticket:
        return
    held_escalated[stream] = ticket
    logger.warning(
        "dispatch_held_too_long",
        trace_id=trace_id,
        stream=stream,
        ticket=ticket,
        held_seconds=round(age, 1),
        detail="a surfaced manual card has been held awaiting the owner past the "
        "escalation threshold — the stream is stalled and needs attention",
    )
    notifier(
        "dispatch_held_too_long",
        trace_id=trace_id,
        stream=stream,
        ticket=ticket,
        held_seconds=round(age, 1),
    )


def _record_to_json(record: DispatchRecord) -> dict[str, object]:
    """Serialize a record for the state file."""
    return dataclasses.asdict(record)


def load_state(path: Path) -> dict[str, DispatchRecord]:
    """Load per-stream records from the state file (empty if absent/invalid)."""
    if not path.exists():
        return {}
    try:
        raw: object = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    state: dict[str, DispatchRecord] = {}
    for stream, value in raw.items():
        if isinstance(value, dict):
            try:
                record = DispatchRecord(**value)
            except TypeError:
                continue
            # A dataclass validates field NAMES, never their types, so a
            # corrupted file (partial write, hand-edit) can construct a record
            # whose ``attempts`` is a string — which then raises TypeError on the
            # first ``>=``/``+=`` in the tick. That escapes ``run_once`` uncaught
            # and, under the daemon's ``Restart=always``, turns one bad file into
            # an indefinite crash-loop with dispatch fully down. Drop the record
            # instead: the stream simply becomes eligible again, which is the
            # same fail-safe the ``TypeError`` branch above already chooses.
            if not isinstance(record.attempts, int) or isinstance(record.attempts, bool):
                continue
            if record.phase not in {"launched", "surfaced", "delivering"}:
                continue
            state[stream] = record
    return state


def save_state(path: Path, state: dict[str, DispatchRecord]) -> None:
    """Persist the state dict atomically (temp file + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({s: _record_to_json(r) for s, r in state.items()}, indent=2)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload)
    os.replace(tmp, path)


def _structlog_notifier(logger: Logger) -> Notifier:
    """A default notifier that emits a structlog warning."""

    def notify(event: str, **fields: object) -> None:
        logger.warning(event, **fields)

    return notify


def _default_state_path() -> Path:
    """Return the default state-file path under the repo's telemetry dir."""
    return Path("telemetry") / "dispatch_state.json"


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Runs one tick (``--once``) or the daemon loop (``--loop``)."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Run a single tick (default).")
    mode.add_argument("--loop", action="store_true", help="Run the daemon poll loop.")
    parser.add_argument(
        "--execute", action="store_true", help="Actually launch (default: dry-run)."
    )
    parser.add_argument(
        "--streams",
        nargs="+",
        choices=known_streams(),
        default=list(DEFAULT_STREAMS),
        help="Streams to orchestrate. Constrained: an unknown stream must fail, not idle silently.",
    )
    parser.add_argument(
        "--state-file", default=str(_default_state_path()), help="Path to the state file."
    )
    parser.add_argument(
        "--stall-timeout", type=float, default=DEFAULT_STALL_TIMEOUT_S, help="Stall grace seconds."
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_S,
        help="Loop poll interval seconds.",
    )
    parser.add_argument(
        "--kill-switch-file",
        default=DEFAULT_KILL_SWITCH_FILE,
        help="Flag file whose presence halts all dispatch (kill switch).",
    )
    parser.add_argument(
        "--wedge-ticks",
        type=int,
        default=DEFAULT_WEDGE_TICKS,
        help="Consecutive RC-busy+pane-idle ticks tolerated before surfacing a wedge.",
    )
    parser.add_argument(
        "--held-escalation-timeout",
        type=float,
        default=DEFAULT_HELD_ESCALATION_S,
        help="Seconds a surfaced manual card may be held before escalating it once.",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Check preconditions + RC liveness, report, and exit (for ExecStartPre).",
    )
    args = parser.parse_args(argv)

    api_key = load_linear_key()
    precondition = check_preconditions(os.environ, api_key)
    if not precondition.ok:
        print(f"precondition unmet: {precondition.reason}", flush=True)
        return 1
    assert api_key is not None  # narrowed: check_preconditions is not-ok without a key

    if args.preflight:
        alive = rc_server_alive(subprocess_runner)
        print(f"preflight: preconditions ok; remote-control reachable={alive}", flush=True)
        return 0 if alive else 1

    logger = structlog.get_logger(__name__)
    notifier = _structlog_notifier(logger)
    state_path = Path(args.state_file)
    kill_switch_path = Path(args.kill_switch_file)
    # In-memory across ticks within this run, reset on restart (FRE-922/FRE-924) —
    # never persisted, so a stale value can never mislead the one-shot ping.
    wedge_counts: dict[str, int] = {}
    held_escalated: dict[str, str] = {}

    def tick() -> None:
        state = load_state(state_path)
        run_once(
            args.streams,
            state,
            now=time.time(),
            stall_timeout_s=args.stall_timeout,
            board_fetcher=lambda stream: fetch_board(stream, api_key),
            runner=subprocess_runner,
            notifier=notifier,
            persist=lambda st: save_state(state_path, st),
            logger=logger,
            execute=args.execute,
            kill_switch_engaged=lambda: _kill_switch_engaged(kill_switch_path),
            wedge_counts=wedge_counts,
            wedge_ticks=args.wedge_ticks,
            held_escalated=held_escalated,
            held_escalation_s=args.held_escalation_timeout,
        )

    if args.loop:
        while True:
            tick()
            time.sleep(args.interval)
    tick()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
