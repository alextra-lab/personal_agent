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
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, Protocol

import structlog

from scripts.dispatch.launcher import (
    CommandRunner,
    execute_plan,
    find_warm_session,
    plan_launch,
    subprocess_runner,
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

# Default dispatch streams (the three worker worktrees).
DEFAULT_STREAMS: tuple[str, ...] = ("build1", "build2", "adr")

# Stall grace: a launched run with no PR after this long triggers a liveness
# notification (never a re-dispatch). Generous — a long Opus build is normal;
# the stall path only notifies, so a false positive is harmless noise.
DEFAULT_STALL_TIMEOUT_S: float = 3600.0

# Poll interval for the daemon loop (``--loop``).
DEFAULT_POLL_INTERVAL_S: float = 300.0

DecisionKind = Literal["launch", "await", "stall", "run_complete", "clear", "skip", "hold"]


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
            manual-model-required), awaiting the owner.
        launched_at: Wall-clock (epoch seconds) the record was created.
        session_id: The launcher's session id, when known.
        run_confirmed: The run delivered a PR (reached ``In Review`` + open PR)
            — stall-watching stops once set.
        stall_notified: A stall notification has already fired (throttle).
    """

    stream: str
    ticket: str
    phase: Literal["launched", "surfaced"]
    launched_at: float
    session_id: str | None
    run_confirmed: bool = False
    stall_notified: bool = False


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


def _record_for_result(stream: str, ticket: str, outcome: str, now: float) -> DispatchRecord | None:
    """Map a ``LaunchResult`` outcome to the record to store (or ``None``).

    A record is written **only** for an outcome that actually launches/prepares
    an owned session (``launched``) or surfaces a manual card (``surfaced``);
    an error outcome (``worktree-dirty``/``launch-failed``) writes no record so
    the stream stays eligible and is never falsely marked in-flight.
    """
    if outcome in {"launch", "prepare"}:
        return DispatchRecord(stream, ticket, "launched", now, session_id=None)
    if outcome in {"manual-continuation", "manual-model-required"}:
        return DispatchRecord(stream, ticket, "surfaced", now, session_id=None)
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

    Returns:
        The updated state dict.
    """
    for stream in streams:
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
) -> None:
    """Apply one decision's side effects (launch / notify / record mutation)."""
    stream = decision.stream
    match decision.kind:
        case "launch":
            assert decision.ticket is not None and decision.model is not None
            warm = find_warm_session(stream, runner) if decision.context_keep else None
            plan = plan_launch(
                stream,
                decision.ticket,
                decision.model,
                context_keep=decision.context_keep,
                warm_session_id=warm,
            )
            logger.info(
                "dispatch_plan",
                trace_id=trace_id,
                stream=stream,
                ticket=decision.ticket,
                model=decision.model,
                outcome=plan.outcome,
                card=plan.card,
            )
            if not execute:
                return
            result = execute_plan(plan, runner)
            logger.info(
                "dispatch_execute",
                trace_id=trace_id,
                stream=stream,
                ticket=decision.ticket,
                outcome=result.outcome,
                launched=result.launched,
            )
            new_record = _record_for_result(stream, decision.ticket, result.outcome, now)
            if new_record is not None:
                state[stream] = new_record
            else:
                state.pop(stream, None)
            persist(state)
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
        case _:  # await / hold / skip — no state change.
            return


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
                state[stream] = DispatchRecord(**value)
            except TypeError:
                continue
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
        default=list(DEFAULT_STREAMS),
        help="Streams to orchestrate (default: build1 build2 adr).",
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
    args = parser.parse_args(argv)

    api_key = load_linear_key()
    if not api_key:
        print("no AGENT_LINEAR_API_KEY configured", flush=True)
        return 1

    logger = structlog.get_logger(__name__)
    notifier = _structlog_notifier(logger)
    state_path = Path(args.state_file)

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
        )

    if args.loop:
        while True:
            tick()
            time.sleep(args.interval)
    tick()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
