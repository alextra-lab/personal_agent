# ruff: noqa: D103
"""Tests for FRE-1556: busy state from Remote Control (RC), the delivery order, and no force-delivery.

ADR-0155 D7. Each test names the ticket criterion it proves. The runner fake answers ``tmux``,
``claude agents`` and ``gh`` from fixtures, so no live seat is touched.
"""

from __future__ import annotations

import ast
import dataclasses
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from scripts.dispatch import gating_watcher, launcher, seat_readings
from scripts.dispatch.gating_watcher import (
    MASTER_SESSION,
    ContextReading,
    PullRequest,
    run_once,
)
from scripts.dispatch.seat_readings import SeatReading
from scripts.dispatch.trigger_ledger import Ledger

_FIXTURES = Path("tests/fixtures")
_IDLE_PANE = (_FIXTURES / "gating_watcher_real_idle_pane.txt").read_text(encoding="utf-8")
_DRAFT_PANE = "❯ /master 1218".join(_IDLE_PANE.rsplit("❯", 1))
_SEAT = "cc-2build"
_SEAT_PANE = "=cc-2build:0.0"
_MASTER_PANE = "=cc-master:0.0"
_T0 = 1000.0
_WORKTREES = "/opt/seshat/.claude/worktrees"
_CWD = {
    "cc-1build": f"{_WORKTREES}/build",
    "cc-2build": f"{_WORKTREES}/build2",
    "cc-adrs": f"{_WORKTREES}/adrs",
}
_WORKER_KEY = "worker:412:abc1234def5678"


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


class _SeatRunner:
    """Panes and RC statuses by seat name. ``rc=None`` makes the registry unreadable."""

    def __init__(self, panes: dict[str, str], rc: dict[str, str] | None) -> None:
        self.panes = panes
        self.rc = rc
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str]) -> _Result:
        argv_t = tuple(argv)
        self.calls.append(argv_t)
        if argv_t[:2] == ("tmux", "has-session"):
            return _Result(returncode=0 if argv_t[3].lstrip("=") in self.panes else 1)
        if argv_t[:2] == ("tmux", "capture-pane"):
            return _Result(stdout=self.panes.get(argv_t[3].lstrip("=").split(":")[0], ""))
        if argv_t[:2] == ("claude", "agents"):
            if self.rc is None:
                return _Result(returncode=1)
            agents = [
                {
                    "name": name,
                    "cwd": _CWD.get(name, "/opt/seshat"),
                    "kind": "interactive",
                    "status": status,
                }
                for name, status in self.rc.items()
            ]
            return _Result(stdout=json.dumps(agents))
        if argv_t[:3] == ("gh", "pr", "view"):
            return _Result(stdout=json.dumps({"state": "OPEN"}))
        return _Result()

    def typed(self, pane: str) -> list[str]:
        """The literal text sent to ``pane`` with ``send-keys -l``."""
        return [
            c[5] for c in self.calls if c[:4] == ("tmux", "send-keys", "-t", pane) and c[4] == "-l"
        ]

    def any_keys_to(self, pane: str) -> bool:
        return any(c[:4] == ("tmux", "send-keys", "-t", pane) for c in self.calls)


class _Logger:
    def __init__(self) -> None:
        self.infos: list[tuple[str, dict[str, object]]] = []
        self.warnings: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        self.infos.append((event, dict(fields)))

    def warning(self, event: str, **fields: object) -> None:
        self.warnings.append((event, dict(fields)))

    def events(self, name: str) -> list[dict[str, object]]:
        return [f for e, f in self.infos + self.warnings if e == name]


def _pr(ci: str = "success") -> PullRequest:
    return PullRequest(
        number=412,
        head_ref="fre-823-event-driven-gating-watcher",
        head_sha="abc1234def5678",
        mergeable="MERGEABLE",
        ci=ci,  # type: ignore[arg-type]
        comment_bodies=(),
    )


def _resolve_build2(ticket: str | None) -> str | None:
    return _SEAT if ticket == "FRE-823" else None


def _topology_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setitem(
        launcher._TOPOLOGY,
        "build2",
        dataclasses.replace(launcher._TOPOLOGY["build2"], mode=mode),  # type: ignore[arg-type]
    )


def _tick(
    state: dict[str, float],
    now: float,
    runner: _SeatRunner,
    ledger: Ledger,
    logger: _Logger,
    *,
    ci: str = "failure",
    **kwargs: object,
) -> None:
    run_once(
        state,
        now=now,
        board_fetcher=lambda: [_pr(ci=ci)],
        session_resolver=_resolve_build2,
        runner=runner,
        persist=lambda _s: None,
        logger=logger,
        execute=True,
        ledger=ledger,
        ledger_persist=ledger.update,
        **kwargs,  # type: ignore[arg-type]
    )


# --- AC-2: nothing is typed into a busy seat or a held draft ------------------


@pytest.mark.parametrize(
    ("rc_status", "seat_pane"),
    [
        ("running", _IDLE_PANE),  # a turn runs; the scrape would have said idle
        ("waiting", _IDLE_PANE),  # a permission prompt; the scrape would have said idle
        ("pending", _IDLE_PANE),
        ("idle", _DRAFT_PANE),  # RC idle, but the owner left a draft
    ],
)
def test_ac2_nothing_is_typed_into_a_busy_seat_or_a_held_draft(
    monkeypatch: pytest.MonkeyPatch, rc_status: str, seat_pane: str
) -> None:
    _topology_mode(monkeypatch, "send_keys")
    runner = _SeatRunner(
        {_SEAT: seat_pane, "cc-master": _IDLE_PANE},
        {_SEAT: rc_status, "cc-master": "idle"},
    )
    state: dict[str, float] = {}
    ledger: Ledger = {}
    logger = _Logger()
    for minute in range(0, 21):  # past the 15-minute alert threshold
        _tick(state, _T0 + minute * 60, runner, ledger, logger)

    assert not runner.any_keys_to(_SEAT_PANE)  # zero send-keys to the seat
    alerts = runner.typed(_MASTER_PANE)
    assert len(alerts) == 1, alerts  # one alert
    assert alerts[0].startswith("[DISPATCH ALERT]")
    assert "PR #412" in alerts[0] and _SEAT in alerts[0]


def test_ac2_the_trigger_lands_once_the_seat_is_idle_with_an_empty_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _topology_mode(monkeypatch, "send_keys")
    runner = _SeatRunner(
        {_SEAT: _DRAFT_PANE, "cc-master": _IDLE_PANE}, {_SEAT: "idle", "cc-master": "idle"}
    )
    state: dict[str, float] = {}
    ledger: Ledger = {}
    for minute in range(0, 5):
        _tick(state, _T0 + minute * 60, runner, ledger, _Logger())
    assert not runner.any_keys_to(_SEAT_PANE)
    runner.panes[_SEAT] = _IDLE_PANE  # the owner sent or cleared the draft
    for minute in range(5, 8):
        _tick(state, _T0 + minute * 60, runner, ledger, _Logger())
    assert runner.typed(_SEAT_PANE) == ["PR #412 failed CI checks - correct them"]


def test_ac2_a_busy_seat_is_not_typed_into_when_the_channel_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _topology_mode(monkeypatch, "channel")
    runner = _SeatRunner(
        {_SEAT: _IDLE_PANE, "cc-master": _IDLE_PANE}, {_SEAT: "running", "cc-master": "idle"}
    )
    ledger: Ledger = {}
    _tick(
        {},
        _T0,
        runner,
        ledger,
        _Logger(),
        channel_poster=lambda _p, _s, _j: "unreachable",
        channel_secret="s3cret",
    )
    assert not runner.any_keys_to(_SEAT_PANE)


# --- AC-3: force-delivery is gone ---------------------------------------------


def test_ac3_a_master_trigger_held_for_45_minutes_is_never_force_delivered() -> None:
    # The pane shows a bare prompt but RC says the seat waits on a permission prompt.
    runner = _SeatRunner(
        {"cc-master": _IDLE_PANE},
        {"cc-master": "waiting"},
    )
    state: dict[str, float] = {}
    ledger: Ledger = {}
    logger = _Logger()
    escalated: set[str] = set()
    for minute in range(0, 46):
        _tick(
            state,
            _T0 + minute * 60,
            runner,
            ledger,
            logger,
            ci="success",
            queued_escalated=escalated,
        )

    assert not runner.any_keys_to(_MASTER_PANE)  # no send-keys at all
    alerts = logger.events("gating_trigger_unconfirmed_too_long")
    assert len(alerts) == 1, alerts  # the alert fires once
    assert alerts[0]["pr"] == "412" and alerts[0]["session"] == MASTER_SESSION
    entry = ledger["master:412:abc1234def5678"]
    assert entry.queued_at is not None and entry.sent_at is None  # still held, not dropped
    assert entry.consumed_at is None


def test_ac3_the_held_master_trigger_still_lands_when_the_seat_frees() -> None:
    runner = _SeatRunner({"cc-master": _IDLE_PANE}, {"cc-master": "waiting"})
    state: dict[str, float] = {}
    ledger: Ledger = {}
    for minute in range(0, 46):
        _tick(state, _T0 + minute * 60, runner, ledger, _Logger(), ci="success")
    assert not runner.any_keys_to(_MASTER_PANE)
    runner.rc = {"cc-master": "idle"}
    _tick(state, _T0 + 46 * 60, runner, ledger, _Logger(), ci="success")
    assert runner.typed(_MASTER_PANE) == ["/master 412"]


def test_ac3_a_worker_trigger_held_for_45_minutes_is_never_force_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _topology_mode(monkeypatch, "send_keys")
    runner = _SeatRunner(
        {_SEAT: _IDLE_PANE, "cc-master": _IDLE_PANE}, {_SEAT: "waiting", "cc-master": "idle"}
    )
    state: dict[str, float] = {}
    ledger: Ledger = {}
    for minute in range(0, 46):
        _tick(state, _T0 + minute * 60, runner, ledger, _Logger())
    assert not runner.any_keys_to(_SEAT_PANE)
    assert len(runner.typed(_MASTER_PANE)) == 1  # the one alert to master


def test_ac3_no_force_delivery_code_remains() -> None:
    assert not hasattr(gating_watcher, "_force_deliver")
    source = Path("scripts/dispatch/gating_watcher.py").read_text(encoding="utf-8")
    assert "force_deliver" not in source


# --- AC-1 at the watcher: a held draft after a poke is not "still working" ---------


def test_ac1_a_held_draft_after_a_poke_is_not_read_as_a_working_seat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _topology_mode(monkeypatch, "send_keys")
    state: dict[str, float] = {}
    first = _SeatRunner({_SEAT: _IDLE_PANE}, {_SEAT: "idle"})
    _tick(state, 100.0, first, {}, _Logger())
    assert first.typed(_SEAT_PANE) == ["PR #412 failed CI checks - correct them"]

    logger = _Logger()
    held = _SeatRunner({_SEAT: _DRAFT_PANE, "cc-master": _IDLE_PANE}, {_SEAT: "idle"})
    _tick(state, 100.0 + 901.0, held, {}, logger)
    assert len(logger.events("gating_poke_ineffective")) == 1
    assert [
        f for f in logger.events("gating_skip") if f.get("reason") == "seat-not-idle-after-poke"
    ] == []


def test_a_working_seat_after_a_poke_is_still_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    _topology_mode(monkeypatch, "send_keys")
    state: dict[str, float] = {}
    _tick(state, 100.0, _SeatRunner({_SEAT: _IDLE_PANE}, {_SEAT: "idle"}), {}, _Logger())
    logger = _Logger()
    busy = _SeatRunner({_SEAT: _IDLE_PANE}, {_SEAT: "running"})
    _tick(state, 100.0 + 901.0, busy, {}, logger)
    assert [f["reason"] for f in logger.events("gating_skip")] == ["seat-not-idle-after-poke"]


# --- D7 point 1: the inbox is observed, the channel still delivers -------------


def test_inbox_is_observed_not_used_while_a_fresh_mod_report_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _topology_mode(monkeypatch, "channel")
    monkeypatch.setattr(seat_readings, "DEFAULT_SEAT_STATE_DIR", tmp_path)
    monkeypatch.setattr(seat_readings, "MOD_ENGINE_VERSIONS", frozenset({"2.1.289"}))
    (tmp_path / f"{_SEAT}.json").write_text(
        json.dumps(
            {
                "state": "idle",
                "draft_present": False,
                "disabled": False,
                "engine_version": "2.1.289",
                "heartbeat_at": _T0 - 5,
            }
        ),
        encoding="utf-8",
    )
    calls: list[str] = []

    def poster(_port: int, _secret: str, payload: str) -> str:
        calls.append(payload)
        return "delivered"

    runner = _SeatRunner({_SEAT: _IDLE_PANE}, {_SEAT: "idle"})
    ledger: Ledger = {}
    logger = _Logger()
    _tick({}, _T0, runner, ledger, logger, channel_poster=poster, channel_secret="s3cret")

    (observed,) = logger.events("gating_inbox_would_use")
    assert observed["seat"] == _SEAT
    assert observed["trigger_id"] == ledger[_WORKER_KEY].trigger_id
    assert len(calls) == 1  # the channel delivered
    assert json.loads(calls[0])["trigger_id"] == observed["trigger_id"]
    assert not runner.any_keys_to(_SEAT_PANE)


def test_no_inbox_observation_without_a_fresh_mod_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _topology_mode(monkeypatch, "channel")
    monkeypatch.setattr(seat_readings, "DEFAULT_SEAT_STATE_DIR", tmp_path)
    runner = _SeatRunner({_SEAT: _IDLE_PANE}, {_SEAT: "idle"})
    logger = _Logger()
    _tick(
        {},
        _T0,
        runner,
        {},
        logger,
        channel_poster=lambda _p, _s, _j: "delivered",
        channel_secret="s3cret",
    )
    assert logger.events("gating_inbox_would_use") == []


# --- AC-4: every tick logs every seat's readings -------------------------------


def test_ac4_every_tick_for_an_hour_logs_every_seats_rc_status() -> None:
    seats = ("cc-master", "cc-1build", "cc-2build", "cc-adrs", "cc-explore")
    runner = _SeatRunner(
        {seat: _IDLE_PANE for seat in seats},
        {seat: "idle" for seat in seats if seat != "cc-adrs"},  # cc-adrs left the registry
    )
    logger = _Logger()
    reader = gating_watcher._seat_reader(runner)
    state: dict[str, float] = {}
    for minute in range(0, 60):  # one tick a minute for an hour
        run_once(
            state,
            now=_T0 + minute * 60,
            board_fetcher=lambda: [],
            session_resolver=_resolve_build2,
            runner=runner,
            persist=lambda _s: None,
            logger=logger,
            execute=True,
            seat_reader=reader,
        )
    readings = logger.events("gating_seat_readings")
    assert len(readings) == 60 * len(seats)
    for tick in range(60):
        batch = readings[tick * len(seats) : (tick + 1) * len(seats)]
        assert sorted(str(r["seat"]) for r in batch) == sorted(seats)
        assert all("rc_status" in r and "rc_state" in r and "pane" in r for r in batch)
        assert all(r["trace_id"] == batch[0]["trace_id"] for r in batch)
    adrs = [r for r in readings if r["seat"] == "cc-adrs"][0]
    assert (adrs["rc_status"], adrs["rc_state"]) == (None, "ended")


def test_ac4_dry_run_logs_the_readings_too() -> None:
    runner = _SeatRunner({"cc-master": _IDLE_PANE}, {"cc-master": "idle"})
    logger = _Logger()
    run_once(
        {},
        now=_T0,
        board_fetcher=lambda: [],
        session_resolver=_resolve_build2,
        runner=runner,
        persist=lambda _s: None,
        logger=logger,
        execute=False,
        seat_reader=gating_watcher._seat_reader(runner),
    )
    assert len(logger.events("gating_seat_readings")) == len(gating_watcher.OBSERVED_SEATS)


def test_ac4_a_reading_is_logged_even_when_the_kill_switch_halts_actuation() -> None:
    runner = _SeatRunner({"cc-master": _IDLE_PANE}, {"cc-master": "idle"})
    logger = _Logger()
    run_once(
        {},
        now=_T0,
        board_fetcher=lambda: [],
        session_resolver=_resolve_build2,
        runner=runner,
        persist=lambda _s: None,
        logger=logger,
        execute=True,
        kill_switch_engaged=lambda: True,
        seat_reader=gating_watcher._seat_reader(runner),
    )
    assert len(logger.events("gating_seat_readings")) == len(gating_watcher.OBSERVED_SEATS)
    assert not any(c[:2] == ("tmux", "send-keys") for c in runner.calls)


def test_a_failing_reader_does_not_stop_the_tick() -> None:
    def boom() -> Sequence[SeatReading]:
        raise OSError("tmux is gone")

    logger = _Logger()
    runner = _SeatRunner({"cc-master": _IDLE_PANE}, {"cc-master": "idle"})
    ledger: Ledger = {}
    _tick({}, _T0, runner, ledger, logger, ci="success", seat_reader=boom)
    assert [f["error"] for f in logger.events("gating_seat_readings_failed")] == ["tmux is gone"]
    assert runner.typed(_MASTER_PANE) == ["/master 412"]  # the trigger still went out


def test_the_daemon_wires_the_seat_reader_into_every_tick() -> None:
    tree = ast.parse(Path("scripts/dispatch/gating_watcher.py").read_text(encoding="utf-8"))
    main = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = [
        n
        for n in ast.walk(main)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "run_once"
    ]
    assert calls, "main() must call run_once"
    for call in calls:
        assert "seat_reader" in {kw.arg for kw in call.keywords}


def test_observed_seats_are_the_five_adr_0155_seats() -> None:
    assert set(gating_watcher.OBSERVED_SEATS) == {
        "cc-master",
        "cc-1build",
        "cc-2build",
        "cc-adrs",
        "cc-explore",
    }


# --- every typing path obeys the gate ------------------------------------------


def test_the_context_pressure_nudge_obeys_the_send_gate() -> None:
    """The nudge types into master, so a held draft there must stop it."""
    runner = _SeatRunner({"cc-master": _DRAFT_PANE}, {"cc-master": "idle"})
    logger = _Logger()
    run_once(
        {},
        now=_T0,
        board_fetcher=lambda: [],
        session_resolver=_resolve_build2,
        runner=runner,
        persist=lambda _s: None,
        logger=logger,
        execute=True,
        ledger={},
        context_reader=lambda: [ContextReading(session="cc-master", ctx=900_000, model="m")],
    )
    assert logger.events("context_pressure_skip") != []  # the nudge was refused...
    assert not runner.any_keys_to(_MASTER_PANE)  # ...and nothing was typed


def test_the_context_pressure_nudge_lands_when_master_is_idle_with_an_empty_draft() -> None:
    runner = _SeatRunner({"cc-master": _IDLE_PANE}, {"cc-master": "idle"})
    run_once(
        {},
        now=_T0,
        board_fetcher=lambda: [],
        session_resolver=_resolve_build2,
        runner=runner,
        persist=lambda _s: None,
        logger=_Logger(),
        execute=True,
        ledger={},
        context_reader=lambda: [ContextReading(session="cc-master", ctx=900_000, model="m")],
    )
    assert len(runner.typed(_MASTER_PANE)) == 1


def test_send_to_session_refuses_a_held_draft_even_when_rc_reads_idle() -> None:
    runner = _SeatRunner({"cc-master": _DRAFT_PANE}, {"cc-master": "idle"})
    assert gating_watcher.send_to_session("cc-master", "hello", runner) == "busy"
    assert not runner.any_keys_to(_MASTER_PANE)
