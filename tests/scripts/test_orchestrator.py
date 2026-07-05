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

from collections.abc import Sequence

import pytest
from scripts.dispatch.next_resolver import IssueSnapshot
from scripts.dispatch.orchestrator import (
    DEFAULT_STALL_TIMEOUT_S,
    DispatchRecord,
    check_preconditions,
    decide,
    is_anthropic_endpoint,
    main,
    model_for_labels,
    rc_server_alive,
    run_once,
)


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
    )
    return result, persisted


def test_run_once_launch_writes_launched_record_no_hook_strip() -> None:
    runner = _RecordingRunner()  # clean git status → launch proceeds
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
    runner = _RecordingRunner({"status": _FakeRunResult(stdout=" M f.py\n")})
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
    runner = _RecordingRunner()
    board = [_issue("FRE-1", "Approved", _OPUS)]
    state, _ = _run({}, runner, board, rc_alive=lambda: True)
    assert state["build1"].phase == "launched"
    assert any("new-session" in c for c in runner.calls)
