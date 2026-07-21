# ruff: noqa: D103
"""Unit tests for the ADR-0110 dispatch orchestrator loop (FRE-787).

Exercises the pure decision (`decide`) and the injected-IO loop (`run_once`)
against fixtures only — no live Linear/gh/tmux. The live assembled seam
(resolve → launch → owner-monitored run → end-at-PR → advance, once per stream)
is the ADR's master-owned verification.

Covers ADR-0110 acceptance criteria carried by FRE-787:
  AC-4  — no merge/deploy/close path; a dispatched run ends at an open PR + In
          Review, and the orchestrator only clears its own record.
  AC-5  — never launches into an occupied stream; never strips the pytest-lock
          hook (`--safe-mode`/`--bare`).
  AC-7b — advance only on the durable open-PR + In-Review signal, never on
          silence; a stall (no PR past timeout) takes the stall path.
Plus the owner's refinement: the stream frees for the next dispatch only at the
terminal merge state (not at In-Review, which can be bounced by master).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

import pytest
from scripts.dispatch.next_resolver import IssueSnapshot
from scripts.dispatch.orchestrator import (
    DEFAULT_STALL_TIMEOUT_S,
    MAX_DELIVERY_ATTEMPTS,
    DispatchRecord,
    _record_for_result,
    check_preconditions,
    decide,
    is_anthropic_endpoint,
    main,
    model_for_labels,
    rc_server_alive,
    run_once,
)

_BUILD1_WORKTREE = ".claude/worktrees/build"


def _issue(
    identifier: str,
    state: str,
    labels: frozenset[str],
    priority: int = 2,
    created_at: str = "2026-01-01T00:00:00Z",
) -> IssueSnapshot:
    return IssueSnapshot(
        identifier=identifier,
        state=state,
        priority=priority,
        created_at=created_at,
        labels=labels,
        blocked_by=(),
    )


_B1 = "stream:build1"
_OPUS = frozenset({_B1, "Tier-1:Opus"})


class _FakeRunResult:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


class _RecordingRunner:
    def __init__(self, results: dict[str, _FakeRunResult] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._results = results or {}

    def __call__(self, argv: Sequence[str]) -> _FakeRunResult:
        self.calls.append(tuple(argv))
        for key, result in self._results.items():
            if key in argv:
                return result
        return _FakeRunResult()


class _SeatRunner(_RecordingRunner):
    """A runner that models the seat FRE-913's launcher now probes.

    ``_RecordingRunner`` alone answers ``tmux has-session`` with returncode 0 and
    ``list-panes`` with empty stdout, which the launcher correctly reads as
    "session exists but is not running claude" (``unhealthy``) — so a plain
    recording runner can no longer reach the create path.

    Defaults to an **absent** seat that registers under the requested Remote
    Control name once created, which is the create path's happy case. The
    session id is scraped from the ``new-session`` argv rather than recomputed,
    so the fake stays correct if the id derivation changes.
    """

    def __init__(
        self, results: dict[str, _FakeRunResult] | None = None, *, state: str = "absent"
    ) -> None:
        super().__init__(results)
        self._state = state
        self._session_id: str | None = None

    def __call__(self, argv: Sequence[str]) -> _FakeRunResult:
        args = list(argv)
        if args[:2] == ["tmux", "has-session"]:
            self.calls.append(tuple(argv))
            return _FakeRunResult(returncode=1 if self._state == "absent" else 0)
        if args[:2] == ["tmux", "list-panes"]:
            self.calls.append(tuple(argv))
            # "<pane_current_command>\t<pane_current_path>" — a live seat must
            # report BOTH, since seat_state also proves the pane sits in this
            # stream's worktree before dispatching into it.
            if self._state == "live":
                return _FakeRunResult(stdout=f"claude\t{_BUILD1_WORKTREE}\n")
            return _FakeRunResult(stdout="bash\n")
        if args[:2] == ["claude", "agents"]:
            self.calls.append(tuple(argv))
            agents = (
                [{"name": "cc-1build", "sessionId": self._session_id, "cwd": "/w"}]
                if self._session_id
                else []
            )
            return _FakeRunResult(stdout=json.dumps(agents))
        if args[:2] == ["tmux", "new-session"]:
            match = re.search(r"--session-id (\S+)", " ".join(args))
            if match:
                self._session_id = match.group(1)
        return super().__call__(argv)


def _no_wait(_seconds: float) -> None:
    """Sleeper seam so the launcher's bounded polls never wall-clock in tests."""


class _Notifier:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def __call__(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


class _NullLogger:
    def info(self, *args: object, **kwargs: object) -> None: ...
    def warning(self, *args: object, **kwargs: object) -> None: ...


def _launched_record(ticket: str = "FRE-786", now: float = 0.0, **kw: object) -> DispatchRecord:
    base: dict[str, object] = {
        "stream": "build1",
        "ticket": ticket,
        "phase": "launched",
        "launched_at": now,
        "session_id": "sess",
        "run_confirmed": False,
        "stall_notified": False,
    }
    base.update(kw)
    return DispatchRecord(**base)  # type: ignore[arg-type]


def _delivering_record(ticket: str = "FRE-923", *, attempts: int = 1) -> DispatchRecord:
    """A record for an in-flight/failed delivery attempt (FRE-923)."""
    return DispatchRecord(
        stream="build1",
        ticket=ticket,
        phase="delivering",
        launched_at=0.0,
        session_id=None,
        attempts=attempts,
    )


# --- model_for_labels ------------------------------------------------------


def test_model_for_labels_each_tier() -> None:
    assert model_for_labels(frozenset({"Tier-1:Opus"})) == "opus"
    assert model_for_labels(frozenset({"Tier-2:Sonnet"})) == "sonnet"
    assert model_for_labels(frozenset({"Tier-3:Haiku"})) == "haiku"


def test_model_for_labels_no_tier_is_none() -> None:
    assert model_for_labels(frozenset({"stream:build1"})) is None


# --- decide: no record -----------------------------------------------------


def test_decide_launch_when_idle_with_tiered_next() -> None:
    issues = [_issue("FRE-1", "Approved", _OPUS)]
    d = decide("build1", issues, None, now=0.0, stall_timeout_s=60, tracked_pr_open=False)
    assert d.kind == "launch"
    assert d.ticket == "FRE-1"
    assert d.model == "opus"
    assert d.context_keep is False


def test_decide_launch_context_keep_from_label() -> None:
    issues = [_issue("FRE-1", "Approved", _OPUS | {"context:keep"})]
    d = decide("build1", issues, None, now=0.0, stall_timeout_s=60, tracked_pr_open=False)
    assert d.kind == "launch"
    assert d.context_keep is True


def test_decide_skip_when_occupied() -> None:
    issues = [_issue("FRE-1", "In Progress", _OPUS), _issue("FRE-2", "Approved", _OPUS)]
    d = decide("build1", issues, None, now=0.0, stall_timeout_s=60, tracked_pr_open=False)
    assert d.kind == "skip"


def test_decide_skip_when_next_has_no_tier() -> None:
    issues = [_issue("FRE-1", "Approved", frozenset({_B1}))]  # no Tier label
    d = decide("build1", issues, None, now=0.0, stall_timeout_s=60, tracked_pr_open=False)
    assert d.kind == "skip"
    assert "tier" in d.reason.lower()


# --- decide: launched record -----------------------------------------------


def test_decide_await_while_in_progress() -> None:
    issues = [_issue("FRE-786", "In Progress", _OPUS)]
    rec = _launched_record(now=0.0)
    d = decide("build1", issues, rec, now=10.0, stall_timeout_s=60, tracked_pr_open=False)
    assert d.kind == "await"


def test_decide_run_complete_on_in_review_plus_pr() -> None:
    issues = [_issue("FRE-786", "In Review", _OPUS)]
    rec = _launched_record(now=0.0)
    d = decide("build1", issues, rec, now=10.0, stall_timeout_s=60, tracked_pr_open=True)
    assert d.kind == "run_complete"


def test_decide_clear_on_terminal_merge() -> None:
    issues = [_issue("FRE-786", "Awaiting Deploy", _OPUS)]
    rec = _launched_record(now=0.0, run_confirmed=True)
    d = decide("build1", issues, rec, now=10.0, stall_timeout_s=60, tracked_pr_open=False)
    assert d.kind == "clear"


def test_decide_bounce_safety_in_review_never_clears() -> None:
    # A master bounce keeps the PR/ticket at In Review; the stream must stay occupied.
    issues = [_issue("FRE-786", "In Review", _OPUS)]
    rec = _launched_record(now=0.0, run_confirmed=True)
    d = decide("build1", issues, rec, now=10.0, stall_timeout_s=60, tracked_pr_open=True)
    assert d.kind == "await"
    assert d.kind not in {"clear", "launch"}


def test_decide_stall_on_silence_past_timeout() -> None:
    issues = [_issue("FRE-786", "Approved", _OPUS)]  # never started
    rec = _launched_record(now=0.0)
    d = decide("build1", issues, rec, now=1000.0, stall_timeout_s=60, tracked_pr_open=False)
    assert d.kind == "stall"


def test_decide_never_completes_on_silence() -> None:
    issues = [_issue("FRE-786", "Approved", _OPUS)]
    rec = _launched_record(now=0.0)
    d = decide("build1", issues, rec, now=1000.0, stall_timeout_s=60, tracked_pr_open=False)
    assert d.kind not in {"run_complete", "clear"}


# --- decide: surfaced record -----------------------------------------------


def test_decide_surfaced_hold_when_still_next() -> None:
    issues = [_issue("FRE-5", "Approved", _OPUS | {"context:keep"})]
    rec = _launched_record(ticket="FRE-5", phase="surfaced")
    d = decide("build1", issues, rec, now=10.0, stall_timeout_s=60, tracked_pr_open=False)
    assert d.kind == "hold"


def test_decide_surfaced_clear_when_owner_acted() -> None:
    issues = [_issue("FRE-5", "In Progress", _OPUS | {"context:keep"})]  # left Approved
    rec = _launched_record(ticket="FRE-5", phase="surfaced")
    d = decide("build1", issues, rec, now=10.0, stall_timeout_s=60, tracked_pr_open=False)
    assert d.kind == "clear"


# --- run_once --------------------------------------------------------------


def _run(
    state,
    runner,
    board,
    *,
    now=0.0,
    execute=True,
    notifier=None,
    stall=60,
    rc_alive=None,
    kill_switch_engaged=None,
):
    persisted: list[dict[str, DispatchRecord]] = []
    result = run_once(
        ["build1"],
        state,
        now=now,
        stall_timeout_s=stall,
        board_fetcher=lambda s: board,
        runner=runner,
        notifier=notifier or _Notifier(),
        persist=lambda st: persisted.append(dict(st)),
        logger=_NullLogger(),
        execute=execute,
        rc_alive=rc_alive,
        kill_switch_engaged=kill_switch_engaged or (lambda: False),
        sleeper=_no_wait,
    )
    return result, persisted


def test_run_once_launch_writes_launched_record_no_hook_strip() -> None:
    runner = _SeatRunner()  # absent seat + clean git status → create proceeds
    board = [_issue("FRE-1", "Approved", _OPUS)]
    state, persisted = _run({}, runner, board)
    assert state["build1"].phase == "launched"
    assert state["build1"].ticket == "FRE-1"
    assert persisted  # persisted immediately after launch
    tmux_calls = [c for c in runner.calls if "new-session" in c]
    assert tmux_calls, "expected a tmux launch"
    joined = " ".join(tmux_calls[0])
    assert "--safe-mode" not in joined
    assert "--bare" not in joined


def test_run_once_keep_ticket_writes_surfaced_record_not_launched() -> None:
    runner = _RecordingRunner()
    board = [_issue("FRE-1", "Approved", _OPUS | {"context:keep"})]
    state, _ = _run({}, runner, board)
    assert state["build1"].phase == "surfaced"
    assert not any("new-session" in c for c in runner.calls)  # never machine-launched


def test_run_once_dirty_worktree_writes_no_record() -> None:
    runner = _SeatRunner({"status": _FakeRunResult(stdout=" M f.py\n")})
    board = [_issue("FRE-1", "Approved", _OPUS)]
    state, _ = _run({}, runner, board)
    assert "build1" not in state  # no false in-flight record
    assert not any("new-session" in c for c in runner.calls)


def test_run_once_no_double_dispatch_on_occupied_stream() -> None:
    runner = _RecordingRunner()
    board = [_issue("FRE-1", "In Progress", _OPUS)]
    state, _ = _run({}, runner, board)
    assert "build1" not in state
    assert not any("new-session" in c for c in runner.calls)


def test_run_once_restart_idempotency_no_relaunch() -> None:
    runner = _RecordingRunner()
    board = [_issue("FRE-786", "In Progress", _OPUS)]
    state, _ = _run({"build1": _launched_record()}, runner, board, now=5.0)
    assert not any("new-session" in c for c in runner.calls)  # never re-launches
    assert state["build1"].phase == "launched"


def test_run_once_clear_on_terminal_drops_record() -> None:
    runner = _RecordingRunner()
    board = [_issue("FRE-786", "Awaiting Deploy", _OPUS)]
    state, _ = _run({"build1": _launched_record(run_confirmed=True)}, runner, board, now=5.0)
    assert "build1" not in state


def test_run_once_run_complete_keeps_record_sets_confirmed() -> None:
    runner = _RecordingRunner({"pr": _FakeRunResult(stdout='[{"number": 385}]')})
    board = [_issue("FRE-786", "In Review", _OPUS)]
    state, _ = _run({"build1": _launched_record()}, runner, board, now=5.0)
    assert state["build1"].phase == "launched"
    assert state["build1"].run_confirmed is True


def test_run_once_stall_notifies_once() -> None:
    notifier = _Notifier()
    board = [_issue("FRE-786", "Approved", _OPUS)]
    runner = _RecordingRunner({"pr": _FakeRunResult(stdout="[]")})
    state, _ = _run(
        {"build1": _launched_record()}, runner, board, now=10_000.0, notifier=notifier, stall=60
    )
    assert len(notifier.events) == 1
    assert state["build1"].stall_notified is True
    # Second tick: already notified → no repeat.
    notifier2 = _Notifier()
    _run(state, runner, board, now=10_050.0, notifier=notifier2, stall=60)
    assert notifier2.events == []


# --- FRE-922: suspected-wedge detection (surface, never kill) ---------------

_BUILD_WORKTREE = "/opt/seshat/.claude/worktrees/build"
_WEDGE_IDLE_PANE = "some earlier output\n❯\n"
_WEDGE_BUSY_PANE = "● Building… (1m 2s · ↑ 4.1k tokens)\n❯\n"


class _WedgeRunner(_RecordingRunner):
    """A live build1 seat that Remote Control reports busy, with a chosen pane.

    Models exactly the incident shape: a live seat whose RC status is ``busy``
    (an orphaned background poller) while the pane text is caller-chosen — idle
    (the wedge) or a live spinner (a genuine turn). Everything else answers so
    the launch reaches the reuse→``seat-busy`` outcome: session present, clean
    worktree, RC reachable.
    """

    def __init__(self, *, pane: str) -> None:
        super().__init__()
        self._pane = pane

    def __call__(self, argv: Sequence[str]) -> _FakeRunResult:
        self.calls.append(tuple(argv))
        args = list(argv)
        if args[:2] == ["tmux", "has-session"]:
            return _FakeRunResult(returncode=0)  # session present → not absent
        if args[:2] == ["tmux", "list-panes"]:
            return _FakeRunResult(stdout=f"claude\t{_BUILD_WORKTREE}\n")
        if args[:2] == ["tmux", "capture-pane"]:
            return _FakeRunResult(stdout=self._pane)
        if args[:2] == ["claude", "agents"]:
            agent = {
                "name": "cc-1build",
                "sessionId": "s",
                "cwd": _BUILD_WORKTREE,
                "status": "busy",
            }
            return _FakeRunResult(stdout=json.dumps([agent]))
        if "status" in args:  # git status --porcelain → clean
            return _FakeRunResult(stdout="")
        return _FakeRunResult()


class _CapturingLogger:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, object]]] = []

    def info(self, *args: object, **kwargs: object) -> None: ...
    def warning(self, event: str, **fields: object) -> None:
        self.warnings.append((event, fields))


def _run_wedge(runner: _RecordingRunner, ticks: int, wedge_ticks: int = 2):  # type: ignore[no-untyped-def]
    board = [_issue("FRE-1", "Approved", _OPUS)]
    state: dict[str, DispatchRecord] = {}
    wedge_counts: dict[str, int] = {}
    notifier = _Notifier()
    logger = _CapturingLogger()
    for _ in range(ticks):
        run_once(
            ["build1"],
            state,
            now=0.0,
            stall_timeout_s=DEFAULT_STALL_TIMEOUT_S,
            board_fetcher=lambda s: board,
            runner=runner,
            notifier=notifier,
            persist=lambda st: None,
            logger=logger,
            execute=True,
            rc_alive=lambda: True,
            wedge_counts=wedge_counts,
            wedge_ticks=wedge_ticks,
        )
    return state, wedge_counts, notifier, logger


def _no_termination_argv(runner: _RecordingRunner) -> bool:
    banned = {"kill", "pkill", "kill-session", "kill-pane", "kill-server", "respawn-pane"}
    return not any(tok in banned for call in runner.calls for tok in call)


def test_wedge_is_surfaced_past_threshold_and_never_killed() -> None:
    """AC-2: an RC-busy + pane-idle seat is surfaced past N ticks, not auto-killed.

    A single observation is ambiguous, so nothing surfaces until the count
    exceeds ``wedge_ticks``. The crossing tick emits a distinct greppable anomaly
    AND pings master exactly once (not the generic ``seat-busy``). The stream
    stays eligible (no record), and NO process-termination command is ever issued.
    """
    runner = _WedgeRunner(pane=_WEDGE_IDLE_PANE)
    # wedge_ticks=2 → tick3 is the crossing tick; a 4th tick must not re-notify.
    state, wedge_counts, notifier, logger = _run_wedge(runner, ticks=4, wedge_ticks=2)

    wedge_events = [e for e in notifier.events if e[0] == "dispatch_seat_wedged"]
    assert len(wedge_events) == 1, "master is pinged exactly once per wedge episode"
    assert wedge_events[0][1]["stream"] == "build1"
    assert wedge_events[0][1]["ticket"] == "FRE-1"
    # The distinct anomaly log fires on every post-threshold tick (ticks 3 and 4).
    wedge_logs = [w for w in logger.warnings if w[0] == "dispatch_seat_wedged"]
    assert len(wedge_logs) == 2
    # The stream is never marked in-flight — it must dispatch the moment the
    # wedge clears — and the counter kept climbing.
    assert "build1" not in state
    assert wedge_counts["build1"] == 4
    # AC-2: detection + surfacing ONLY — the daemon never terminates a process.
    assert _no_termination_argv(runner)


def test_wedge_ping_is_once_per_episode_and_re_fires_on_a_new_episode() -> None:
    """The one-shot ping throttles PER episode, not once ever.

    After a wedge clears (a tick where the seat is genuinely busy resets the
    count), a fresh wedge is a new episode and pings master again. Proves the
    throttle is a per-episode latch that the in-memory count re-arms on reset —
    the property the persisted-counter crossing check could not guarantee.
    """
    runner = _WedgeRunner(pane=_WEDGE_IDLE_PANE)
    state: dict[str, DispatchRecord] = {}
    wedge_counts: dict[str, int] = {}
    notifier = _Notifier()

    def _one_tick() -> None:
        run_once(
            ["build1"],
            state,
            now=0.0,
            stall_timeout_s=DEFAULT_STALL_TIMEOUT_S,
            board_fetcher=lambda s: [_issue("FRE-1", "Approved", _OPUS)],
            runner=runner,
            notifier=notifier,
            persist=lambda st: None,
            logger=_NullLogger(),
            execute=True,
            rc_alive=lambda: True,
            wedge_counts=wedge_counts,
            wedge_ticks=2,
        )

    for _ in range(3):  # episode 1: crosses on tick 3 → one ping
        _one_tick()
    assert sum(e[0] == "dispatch_seat_wedged" for e in notifier.events) == 1
    runner._pane = _WEDGE_BUSY_PANE  # seat genuinely busy → wedge clears, count reset
    _one_tick()
    assert wedge_counts.get("build1", 0) == 0
    runner._pane = _WEDGE_IDLE_PANE  # episode 2: re-wedges
    for _ in range(3):
        _one_tick()
    assert sum(e[0] == "dispatch_seat_wedged" for e in notifier.events) == 2


def test_genuinely_busy_seat_is_never_mistaken_for_a_wedge() -> None:
    """AC-3 (regression): RC-busy + a live spinner is a real turn, not a wedge.

    The pane shows the in-progress spinner, so the wedge signature never fires
    however long the seat stays busy: no anomaly, no master ping, the counter
    stays at zero, and the generic ``seat-busy`` path is unchanged (no record,
    stream stays eligible). A real in-flight build is never surfaced as wedged.
    """
    runner = _WedgeRunner(pane=_WEDGE_BUSY_PANE)
    state, wedge_counts, notifier, logger = _run_wedge(runner, ticks=5, wedge_ticks=2)

    assert not any(e[0] == "dispatch_seat_wedged" for e in notifier.events)
    assert not any(w[0] == "dispatch_seat_wedged" for w in logger.warnings)
    assert wedge_counts.get("build1", 0) == 0  # reset every tick
    assert "build1" not in state  # generic seat-busy still writes no record
    assert _no_termination_argv(runner)


def test_stale_wedge_count_is_reset_on_a_non_wedge_decision() -> None:
    """Codex-major: a stale sidecar count must not survive into a non-wedge tick.

    A crash / daemon restart / manual recovery can leave a wedge count set while
    the stream is actually mid-run (a ``DispatchRecord`` exists → an ``await``
    decision). The count is cleared regardless, so it cannot later trip the
    threshold a tick early against an unrelated episode.
    """
    runner = _RecordingRunner()
    board = [_issue("FRE-786", "In Progress", _OPUS)]  # a launched record → await
    wedge_counts = {"build1": 5}
    run_once(
        ["build1"],
        {"build1": _launched_record()},
        now=5.0,
        stall_timeout_s=DEFAULT_STALL_TIMEOUT_S,
        board_fetcher=lambda s: board,
        runner=runner,
        notifier=_Notifier(),
        persist=lambda st: None,
        logger=_NullLogger(),
        execute=True,
        rc_alive=lambda: True,
        wedge_counts=wedge_counts,
        wedge_ticks=2,
    )
    assert "build1" not in wedge_counts  # cleared


def test_blocked_launch_tick_resets_a_stale_wedge_count() -> None:
    """A blocked tick (rc-down/kill-switch) never probes the seat → no stale count.

    Only the confirmed-wedge increment skips the reset; a blocked launch resets.
    """
    runner = _RecordingRunner()
    board = [_issue("FRE-1", "Approved", _OPUS)]
    wedge_counts = {"build1": 4}
    run_once(
        ["build1"],
        {},
        now=0.0,
        stall_timeout_s=DEFAULT_STALL_TIMEOUT_S,
        board_fetcher=lambda s: board,
        runner=runner,
        notifier=_Notifier(),
        persist=lambda st: None,
        logger=_NullLogger(),
        execute=True,
        rc_alive=lambda: True,
        kill_switch_engaged=lambda: True,  # blocks the launch
        wedge_counts=wedge_counts,
        wedge_ticks=2,
    )
    assert "build1" not in wedge_counts
    assert not any("new-session" in c for c in runner.calls)  # never launched


def test_duplicate_streams_do_not_double_increment_the_wedge_counter() -> None:
    """Codex-minor: a repeated ``--streams`` value must count a stream once/tick."""
    runner = _WedgeRunner(pane=_WEDGE_IDLE_PANE)
    board = [_issue("FRE-1", "Approved", _OPUS)]
    wedge_counts: dict[str, int] = {}
    run_once(
        ["build1", "build1", "build1"],  # duplicated
        {},
        now=0.0,
        stall_timeout_s=DEFAULT_STALL_TIMEOUT_S,
        board_fetcher=lambda s: board,
        runner=runner,
        notifier=_Notifier(),
        persist=lambda st: None,
        logger=_NullLogger(),
        execute=True,
        rc_alive=lambda: True,
        wedge_counts=wedge_counts,
        wedge_ticks=2,
    )
    assert wedge_counts["build1"] == 1  # one increment for one tick, not three


# --- FRE-924: held-too-long escalation (surface by age, never resolve) ------

_HELD_S = 1800.0  # the held-escalation threshold under test (seconds)


def _surfaced(ticket: str, launched_at: float = 0.0) -> DispatchRecord:
    """A surfaced (manual-card) record for the held-escalation tests."""
    return _launched_record(ticket=ticket, phase="surfaced", launched_at=launched_at)


def _run_held(  # type: ignore[no-untyped-def]
    *,
    ticks: int,
    now: float,
    launched_at: float = 0.0,
    held_escalation_s: float = _HELD_S,
    ticket: str = "FRE-786",
    state: dict[str, DispatchRecord] | None = None,
    held_escalated: dict[str, str] | None = None,
):
    """Run N ticks with a surfaced record still-NEXT (so ``_decide_surfaced`` holds).

    A hold decision never shells out (no launch/PR probe), so a plain recording
    runner suffices and never issues a termination command.
    """
    board = [_issue(ticket, "Approved", _OPUS | {"context:keep"})]
    if state is None:
        state = {"build1": _surfaced(ticket, launched_at)}
    held_escalated = {} if held_escalated is None else held_escalated
    notifier = _Notifier()
    logger = _CapturingLogger()
    runner = _RecordingRunner()
    for _ in range(ticks):
        run_once(
            ["build1"],
            state,
            now=now,
            stall_timeout_s=DEFAULT_STALL_TIMEOUT_S,
            board_fetcher=lambda s: board,
            runner=runner,
            notifier=notifier,
            persist=lambda st: None,
            logger=logger,
            execute=True,
            rc_alive=lambda: True,
            held_escalated=held_escalated,
            held_escalation_s=held_escalation_s,
        )
    return state, held_escalated, notifier, logger, runner


def test_held_card_escalates_once_past_threshold() -> None:
    """AC-1: a card held past the age threshold escalates exactly once per episode.

    Across several ticks all past the threshold, exactly one greppable
    ``dispatch_held_too_long`` warning AND one master ping fire (not one per
    tick) — distinct from the per-tick ``card-already-surfaced`` hold trail.
    """
    state, held_escalated, notifier, logger, _ = _run_held(ticks=4, now=5000.0, launched_at=0.0)

    events = [e for e in notifier.events if e[0] == "dispatch_held_too_long"]
    assert len(events) == 1, "master is pinged exactly once per held episode"
    assert events[0][1]["stream"] == "build1"
    assert events[0][1]["ticket"] == "FRE-786"
    logs = [w for w in logger.warnings if w[0] == "dispatch_held_too_long"]
    assert len(logs) == 1, "one greppable escalation log per episode, not per tick"
    assert held_escalated["build1"] == "FRE-786"


def test_held_escalation_never_mutates_state_or_kills() -> None:
    """AC-2: escalation surfaces only — the record is untouched, nothing is killed."""
    rec = _surfaced("FRE-786", launched_at=0.0)
    state, _, notifier, _, runner = _run_held(ticks=3, now=5000.0, state={"build1": rec})

    assert any(e[0] == "dispatch_held_too_long" for e in notifier.events)  # it did escalate
    assert state["build1"] is rec  # never cleared, never replaced/refreshed
    assert state["build1"].phase == "surfaced"
    assert state["build1"].launched_at == 0.0
    assert _no_termination_argv(runner)  # detection + surfacing ONLY


def test_fresh_surfaced_card_within_threshold_does_not_escalate() -> None:
    """AC-3 (regression): a card the owner may act on promptly does not alarm."""
    state, held_escalated, notifier, logger, _ = _run_held(ticks=3, now=1000.0, launched_at=0.0)

    assert not any(e[0] == "dispatch_held_too_long" for e in notifier.events)
    assert not any(w[0] == "dispatch_held_too_long" for w in logger.warnings)
    assert "build1" not in held_escalated


def test_held_escalation_boundary_is_strictly_greater_than() -> None:
    """Age exactly at the threshold does not escalate; one second past does.

    Pins the ``age <= threshold`` early-return / ``age > threshold`` fire boundary.
    """
    _, _, at_notifier, _, _ = _run_held(ticks=1, now=_HELD_S, launched_at=0.0)
    assert not any(e[0] == "dispatch_held_too_long" for e in at_notifier.events)

    _, _, past_notifier, _, _ = _run_held(ticks=1, now=_HELD_S + 1.0, launched_at=0.0)
    assert sum(e[0] == "dispatch_held_too_long" for e in past_notifier.events) == 1


def _held_ticker(state, held_escalated, notifier, board):  # type: ignore[no-untyped-def]
    """A single-tick runner over shared state/board for multi-episode tests."""

    def _tick(now: float) -> None:
        run_once(
            ["build1"],
            state,
            now=now,
            stall_timeout_s=DEFAULT_STALL_TIMEOUT_S,
            board_fetcher=lambda s: board,
            runner=_RecordingRunner(),
            notifier=notifier,
            persist=lambda st: None,
            logger=_NullLogger(),
            execute=True,
            rc_alive=lambda: True,
            held_escalated=held_escalated,
            held_escalation_s=_HELD_S,
        )

    return _tick


def test_held_escalation_re_fires_on_a_new_episode() -> None:
    """The escalation is per-episode: the owner acting re-arms it for the next card.

    Mirrors the FRE-922 re-arm test — proves the in-memory latch is dropped on
    the ``clear`` decision (owner acted) so a later surfaced hold escalates again.
    """
    state: dict[str, DispatchRecord] = {"build1": _surfaced("FRE-1", launched_at=0.0)}
    held_escalated: dict[str, str] = {}
    notifier = _Notifier()
    board = [_issue("FRE-1", "Approved", _OPUS | {"context:keep"})]
    tick = _held_ticker(state, held_escalated, notifier, board)

    tick(5000.0)  # episode 1: past threshold → escalates once
    assert sum(e[0] == "dispatch_held_too_long" for e in notifier.events) == 1

    board[:] = [_issue("FRE-1", "In Progress", _OPUS | {"context:keep"})]  # owner acted → clear
    tick(5100.0)
    assert "build1" not in held_escalated and "build1" not in state  # latch + record dropped

    # Episode 2: a fresh surfaced card, aged past threshold → escalates again.
    state["build1"] = _surfaced("FRE-2", launched_at=5100.0)
    board[:] = [_issue("FRE-2", "Approved", _OPUS | {"context:keep"})]
    tick(7000.0)
    assert sum(e[0] == "dispatch_held_too_long" for e in notifier.events) == 2


def test_held_escalation_re_fires_when_surfaced_ticket_changes() -> None:
    """Codex finding #3: the latch is keyed by (stream, ticket), not stream alone.

    A surfaced ticket swapped in place on one stream while it stays a ``hold``
    (reachable only via external state surgery — the normal path emits ``clear``
    first) must still escalate the new ticket, never be suppressed by the old
    ticket's latch entry.
    """
    state: dict[str, DispatchRecord] = {"build1": _surfaced("FRE-1", launched_at=0.0)}
    held_escalated: dict[str, str] = {}
    notifier = _Notifier()
    board = [_issue("FRE-1", "Approved", _OPUS | {"context:keep"})]
    tick = _held_ticker(state, held_escalated, notifier, board)

    tick(5000.0)
    assert held_escalated["build1"] == "FRE-1"
    assert sum(e[0] == "dispatch_held_too_long" for e in notifier.events) == 1

    # Same stream, different surfaced ticket, still a hold — no intervening clear.
    state["build1"] = _surfaced("FRE-2", launched_at=5000.0)
    board[:] = [_issue("FRE-2", "Approved", _OPUS | {"context:keep"})]
    tick(7000.0)  # age 2000 > 1800 and ticket changed → re-fires for FRE-2
    assert held_escalated["build1"] == "FRE-2"
    assert sum(e[0] == "dispatch_held_too_long" for e in notifier.events) == 2


def test_held_escalation_is_gated_on_execute() -> None:
    """A dry-run tick (execute=False) over a held state file emits no escalation.

    The hold path must stay side-effect-free in dry-run — no warning, no master
    ping, no latch mutation — matching the launch/wedge path's ``if not execute``
    guard (a non-actuating inspection run must not page the owner).
    """
    state: dict[str, DispatchRecord] = {"build1": _surfaced("FRE-786", launched_at=0.0)}
    held_escalated: dict[str, str] = {}
    notifier = _Notifier()
    logger = _CapturingLogger()
    run_once(
        ["build1"],
        state,
        now=5000.0,  # well past threshold
        stall_timeout_s=DEFAULT_STALL_TIMEOUT_S,
        board_fetcher=lambda s: [_issue("FRE-786", "Approved", _OPUS | {"context:keep"})],
        runner=_RecordingRunner(),
        notifier=notifier,
        persist=lambda st: None,
        logger=logger,
        execute=False,  # dry run
        rc_alive=lambda: True,
        held_escalated=held_escalated,
        held_escalation_s=_HELD_S,
    )
    assert not any(e[0] == "dispatch_held_too_long" for e in notifier.events)
    assert not any(w[0] == "dispatch_held_too_long" for w in logger.warnings)
    assert "build1" not in held_escalated


def test_duplicate_streams_do_not_double_escalate_held() -> None:
    """A repeated ``--streams`` value escalates a held stream once per tick, not per repeat."""
    state: dict[str, DispatchRecord] = {"build1": _surfaced("FRE-786", launched_at=0.0)}
    held_escalated: dict[str, str] = {}
    notifier = _Notifier()
    board = [_issue("FRE-786", "Approved", _OPUS | {"context:keep"})]
    run_once(
        ["build1", "build1", "build1"],  # duplicated
        state,
        now=5000.0,
        stall_timeout_s=DEFAULT_STALL_TIMEOUT_S,
        board_fetcher=lambda s: board,
        runner=_RecordingRunner(),
        notifier=notifier,
        persist=lambda st: None,
        logger=_NullLogger(),
        execute=True,
        rc_alive=lambda: True,
        held_escalated=held_escalated,
        held_escalation_s=_HELD_S,
    )
    assert sum(e[0] == "dispatch_held_too_long" for e in notifier.events) == 1


# --- CLI -------------------------------------------------------------------


def test_main_once_dry_run_no_launch(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.dispatch.orchestrator as orch

    monkeypatch.setattr(orch, "load_linear_key", lambda: "key")
    monkeypatch.setattr(orch, "fetch_board", lambda stream, key: [])
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)  # precondition depends on it
    state_file = tmp_path / "state.json"
    rc = main(["--once", "--state-file", str(state_file), "--streams", "build1"])
    assert rc == 0


def test_default_stall_timeout_is_positive() -> None:
    assert DEFAULT_STALL_TIMEOUT_S > 0


def test_main_fails_fast_on_off_anthropic_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-b: an off-Anthropic endpoint fails fast before the loop."""
    import scripts.dispatch.orchestrator as orch

    monkeypatch.setattr(orch, "load_linear_key", lambda: "key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:4000")
    assert main(["--once", "--streams", "build1"]) == 1


# --- preconditions (AC-b: fail fast when unmet) ----------------------------


def test_is_anthropic_endpoint_unset_is_ok() -> None:
    assert is_anthropic_endpoint("") is True
    assert is_anthropic_endpoint("   ") is True


def test_is_anthropic_endpoint_anthropic_host_is_ok() -> None:
    assert is_anthropic_endpoint("https://api.anthropic.com") is True
    assert is_anthropic_endpoint("https://api.anthropic.com/v1") is True


def test_is_anthropic_endpoint_off_anthropic_is_not_ok() -> None:
    assert is_anthropic_endpoint("http://localhost:4000") is False
    assert is_anthropic_endpoint("https://gateway.example.com") is False


def test_check_preconditions_ok_with_key_and_no_base_url() -> None:
    pre = check_preconditions({}, "linear-key")
    assert pre.ok is True
    assert pre.reason == ""


def test_check_preconditions_missing_key_distinct_reason() -> None:
    pre = check_preconditions({}, None)
    assert pre.ok is False
    assert "linear-api-key" in pre.reason


def test_check_preconditions_off_anthropic_distinct_reason() -> None:
    pre = check_preconditions({"ANTHROPIC_BASE_URL": "http://localhost:4000"}, "linear-key")
    assert pre.ok is False
    assert "rc-endpoint" in pre.reason
    # auth/entitlement is NOT conflated into the static precondition.
    assert "auth" not in pre.reason.lower()


# --- liveness probe (AC-a: refuse to dispatch when RC is down) --------------


def test_rc_server_alive_up_when_probe_exits_zero() -> None:
    runner = _RecordingRunner()  # default returncode 0
    assert rc_server_alive(runner) is True
    assert any("agents" in c for c in runner.calls)


def test_rc_server_alive_down_when_probe_exits_nonzero() -> None:
    runner = _RecordingRunner({"agents": _FakeRunResult(returncode=1)})
    assert rc_server_alive(runner) is False


def test_run_once_rc_down_blocks_launch_no_dispatch() -> None:
    """AC-a: RC down → zero launches, a dispatch_blocked notify, no record."""
    runner = _RecordingRunner()
    board = [_issue("FRE-1", "Approved", _OPUS)]
    notifier = _Notifier()
    state, _ = _run({}, runner, board, notifier=notifier, rc_alive=lambda: False)
    assert "build1" not in state  # never marked in-flight
    assert not any("new-session" in c for c in runner.calls)  # no tmux launch
    assert [e for e, _ in notifier.events] == ["dispatch_blocked"]
    assert notifier.events[0][1]["reason"] == "rc-down"


def test_run_once_kill_switch_blocks_launch_no_dispatch() -> None:
    runner = _RecordingRunner()
    board = [_issue("FRE-1", "Approved", _OPUS)]
    notifier = _Notifier()
    state, _ = _run({}, runner, board, notifier=notifier, kill_switch_engaged=lambda: True)
    assert "build1" not in state
    assert not any("new-session" in c for c in runner.calls)
    assert notifier.events[0][1]["reason"] == "kill-switch"


def test_run_once_kill_switch_precedes_rc_check() -> None:
    """Kill switch is reported even if RC is also down (deterministic reason)."""
    runner = _RecordingRunner()
    board = [_issue("FRE-1", "Approved", _OPUS)]
    notifier = _Notifier()
    _run(
        {},
        runner,
        board,
        notifier=notifier,
        rc_alive=lambda: False,
        kill_switch_engaged=lambda: True,
    )
    assert notifier.events[0][1]["reason"] == "kill-switch"


def test_run_once_launch_proceeds_when_alive_and_no_kill_switch() -> None:
    """Regression: the guard does not block a healthy dispatch."""
    runner = _SeatRunner()
    board = [_issue("FRE-1", "Approved", _OPUS)]
    state, _ = _run({}, runner, board, rc_alive=lambda: True)
    assert state["build1"].phase == "launched"
    assert any("new-session" in c for c in runner.calls)


# --- FRE-913: record mapping for the persistent-seat outcomes ---------------


def test_reuse_is_an_owned_in_flight_run() -> None:
    """A live seat dispatched in-session is owned work, exactly like a launch."""
    record = _record_for_result("build1", "FRE-913", "reuse", now=100.0, attempts=0)
    assert record is not None
    assert record.phase == "launched"


def test_an_unusable_seat_is_surfaced_immediately() -> None:
    """FRE-913: a seat that is not a usable claude needs a human, not a retry.

    ``seat-unhealthy`` does not self-clear, and unlike ``delivery-failed``
    (FRE-923) re-attempting cannot help — the seat is not running claude at all.
    """
    record = _record_for_result("build1", "FRE-913", "seat-unhealthy", now=100.0, attempts=0)
    assert record is not None
    assert record.phase == "surfaced"


# --- FRE-923: delivery atomicity — bounded retry + durable in-flight marker --


def test_delivery_failure_retries_before_surfacing() -> None:
    """AC-1: a dropped delivery is retryable, not an immediate terminal card.

    The seat here is idle and ready — only the *delivery* dropped — so surfacing
    a manual card on the first partial send is what stranded FRE-920 for 2.5h.
    """
    record = _record_for_result("build1", "FRE-923", "delivery-failed", now=100.0, attempts=1)
    assert record is not None
    assert record.phase == "delivering"
    assert record.attempts == 1


def test_delivery_failure_surfaces_after_max_attempts() -> None:
    """AC-1: the retry is BOUNDED — it escalates rather than looping forever."""
    record = _record_for_result(
        "build1", "FRE-923", "delivery-failed", now=100.0, attempts=MAX_DELIVERY_ATTEMPTS
    )
    assert record is not None
    assert record.phase == "surfaced"


def test_delivering_record_redispatches_next_tick() -> None:
    """AC-1: the next tick re-attempts the whole sequence instead of holding."""
    issues = [_issue("FRE-923", "Approved", _OPUS)]
    record = _delivering_record("FRE-923", attempts=1)
    d = decide("build1", issues, record, now=0.0, stall_timeout_s=60, tracked_pr_open=False)

    assert d.kind == "launch"
    assert d.ticket == "FRE-923"
    assert d.model == "opus"
    assert d.reason == "retry-delivery"


def test_delivering_record_surfaces_once_the_budget_is_spent() -> None:
    """AC-1: an exhausted budget hands over to the human, never re-attempts."""
    issues = [_issue("FRE-923", "Approved", _OPUS)]
    record = _delivering_record("FRE-923", attempts=MAX_DELIVERY_ATTEMPTS)
    d = decide("build1", issues, record, now=0.0, stall_timeout_s=60, tracked_pr_open=False)

    assert d.kind == "surface"


def test_delivering_record_clears_when_the_owner_acted() -> None:
    """A ticket that moved on is no longer this stream's business."""
    issues = [_issue("FRE-923", "In Progress", _OPUS)]
    record = _delivering_record("FRE-923", attempts=1)
    d = decide("build1", issues, record, now=0.0, stall_timeout_s=60, tracked_pr_open=False)

    assert d.kind == "clear"


def test_crash_mid_delivery_leaves_a_durable_in_flight_record() -> None:
    """AC-1 (atomicity): the attempt is durably marked BEFORE it is made.

    ``execute_plan`` is strict within one process, but a daemon crash between
    typing ``/model`` and confirming it would otherwise leave no trace that a
    delivery was ever in flight — the next tick would re-dispatch with a fresh
    budget and could loop indefinitely across restarts. Persisting first is what
    makes the attempt count survive the crash.
    """
    runner = _SeatRunner()  # absent seat + clean git status → create proceeds
    board = [_issue("FRE-923", "Approved", _OPUS)]
    state, persisted = _run({}, runner, board)

    assert persisted, "the tick must persist before executing, not only after"
    assert persisted[0]["build1"].phase == "delivering"
    assert persisted[0]["build1"].attempts == 1
    assert state["build1"].phase == "launched"  # reconciled once the launch lands


def test_successful_delivery_records_launched_with_no_attempts() -> None:
    """AC-3: a healthy dispatch ends ``launched`` and resets the retry budget."""
    record = _record_for_result("build1", "FRE-923", "reuse", now=100.0, attempts=2)
    assert record is not None
    assert record.phase == "launched"
    assert record.attempts == 0


def test_seat_busy_pops_the_prewritten_record() -> None:
    """The pre-write must not wedge a transient outcome.

    ``seat-busy`` is self-clearing and has always written no record; now that a
    record is written *before* execution, the transient path has to un-do it or
    the stream would hold on a condition that resolves itself in seconds.
    """
    runner = _SeatRunner(
        state="live", results={"capture-pane": _FakeRunResult(stdout=_WEDGE_BUSY_PANE)}
    )
    board = [_issue("FRE-923", "Approved", _OPUS)]
    state, _ = _run({}, runner, board)

    assert "build1" not in state
    # Proves it was genuinely the busy path: a mid-turn seat is never typed into.
    assert not [c for c in runner.calls if "send-keys" in c]


def test_giving_up_on_delivery_is_announced_exactly_once() -> None:
    """AC-1: the retry giving up is never silent — and never repeats.

    FRE-920's whole cost was silence. A bounded retry that expires without
    saying so would just be a quieter version of the same 2.5-hour stall.
    """
    # A live, idle seat that accepts keystrokes but never goes busy — i.e. the
    # /build is never confirmed as submitted, tick after tick.
    runner = _SeatRunner(
        state="live", results={"capture-pane": _FakeRunResult(stdout="earlier\n❯\n")}
    )
    board = [_issue("FRE-923", "Approved", _OPUS)]
    notifier = _Notifier()
    state: dict[str, DispatchRecord] = {}
    for tick in range(1, 6):
        state, _ = _run(state, runner, board, now=tick * 300.0, notifier=notifier)

    assert state["build1"].phase == "surfaced"
    assert [e for e, _ in notifier.events] == ["dispatch_delivery_exhausted"]


def test_a_crash_exhausted_budget_surfaces_instead_of_retrying_forever() -> None:
    """AC-1: the crash path is bounded too, not just the in-process one.

    A daemon that dies mid-delivery leaves a ``delivering`` record behind. Once
    its persisted attempt count is spent, the next tick must hand over to the
    owner — holding the record instead would rebuild the same indefinite silent
    stall this ticket exists to kill, one phase along.
    """
    runner = _SeatRunner()
    board = [_issue("FRE-923", "Approved", _OPUS)]
    notifier = _Notifier()
    state = {"build1": _delivering_record("FRE-923", attempts=MAX_DELIVERY_ATTEMPTS)}
    state, _ = _run(state, runner, board, notifier=notifier)

    assert state["build1"].phase == "surfaced"
    assert [e for e, _ in notifier.events] == ["dispatch_delivery_exhausted"]
    assert not [c for c in runner.calls if "new-session" in c]  # no further attempt


def test_seat_busy_writes_no_record_so_the_stream_retries() -> None:
    """seat-busy is transient — recording it would wedge the stream forever.

    The seat is simply mid-turn and goes idle within seconds. A ``surfaced``
    record would hold the stream in ``_decide_surfaced`` indefinitely, trading a
    self-healing few-minute delay for a permanent stall only the owner can clear.
    """
    assert _record_for_result("build1", "FRE-913", "seat-busy", now=100.0, attempts=0) is None


def test_registration_unverified_is_still_an_in_flight_run() -> None:
    """A misnamed seat is RUNNING the ticket — it needs stall tracking, not a card.

    Only its Remote Control name is wrong; the seat was seeded and is building.
    Recording it as ``surfaced`` would deny the live run stall detection and
    ``run_complete`` handling for work that is genuinely in flight.
    """
    record = _record_for_result(
        "build1", "FRE-913", "registration-unverified", now=100.0, attempts=0
    )
    assert record is not None
    assert record.phase == "launched"


def test_transient_errors_still_leave_the_stream_eligible() -> None:
    """A dirty worktree or a failed tmux call is retryable — no record."""
    assert _record_for_result("build1", "FRE-913", "worktree-dirty", now=100.0, attempts=0) is None
    assert _record_for_result("build1", "FRE-913", "launch-failed", now=100.0, attempts=0) is None
