# ruff: noqa: D103
"""Unit tests for the seat readings behind the delivery order (FRE-1556, ADR-0155 D7).

Covers the busy state read from Remote Control (RC) status, the draft-known-empty rule
that gates ``tmux send-keys``, the mod report reader, and the per-tick readings.
Fixtures only: no live tmux, ``claude`` or mod.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from scripts.dispatch.seat_readings import (
    ModReport,
    draft_known_empty,
    map_rc_status,
    rc_state_for,
    read_mod_report,
    read_seat_readings,
    seat_busy_state,
)

_FIXTURES_DIR = Path("tests/fixtures")
_IDLE_PANE = (_FIXTURES_DIR / "gating_watcher_real_idle_pane.txt").read_text(encoding="utf-8")
_BUSY_PANE = "✽ Working… (esc to interrupt)"
# The box holds an unsent draft: the caret line carries text.
_DRAFT_PANE = "❯ /master 1218".join(_IDLE_PANE.rsplit("❯", 1))

_NOW = 10_000.0
_ENGINE = "2.1.289"


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


class _Runner:
    """Answers ``tmux has-session``, ``capture-pane`` and ``claude agents`` from fixtures."""

    def __init__(
        self,
        *,
        panes: dict[str, str] | None = None,
        agents: object = None,
        agents_ok: bool = True,
    ) -> None:
        self.panes = panes or {}
        self.agents = agents if agents is not None else []
        self.agents_ok = agents_ok
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str]) -> _Result:
        argv_t = tuple(argv)
        self.calls.append(argv_t)
        if argv_t[:2] == ("tmux", "has-session"):
            name = argv_t[3].lstrip("=")
            return _Result(returncode=0 if name in self.panes else 1)
        if argv_t[:2] == ("tmux", "capture-pane"):
            name = argv_t[3].lstrip("=").split(":")[0]
            return _Result(stdout=self.panes.get(name, ""))
        if argv_t[:2] == ("claude", "agents"):
            if not self.agents_ok:
                return _Result(returncode=1)
            return _Result(stdout=json.dumps(self.agents))
        return _Result()


_WORKTREES = "/opt/seshat/.claude/worktrees"
# A dispatch stream's seat registers with its worktree as cwd; the others run elsewhere.
_CWD = {
    "cc-1build": f"{_WORKTREES}/build",
    "cc-2build": f"{_WORKTREES}/build2",
    "cc-adrs": f"{_WORKTREES}/adrs",
}


def _agent(name: str, status: str | None, cwd: str | None = None) -> dict[str, object]:
    entry: dict[str, object] = {
        "name": name,
        "cwd": cwd or _CWD.get(name, "/opt/seshat"),
        "kind": "interactive",
    }
    if status is not None:
        entry["status"] = status
    return entry


def _write_mod(
    state_dir: Path,
    seat: str,
    *,
    heartbeat_at: float = _NOW - 10,
    state: str = "idle",
    draft_present: bool = False,
    disabled: bool = False,
    engine_version: str = _ENGINE,
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{seat}.json").write_text(
        json.dumps(
            {
                "seat": seat,
                "state": state,
                "draft_present": draft_present,
                "disabled": disabled,
                "engine_version": engine_version,
                "heartbeat_at": heartbeat_at,
            }
        ),
        encoding="utf-8",
    )


# --- the RC status map -------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("idle", "idle"),
        ("busy", "busy"),
        ("running", "busy"),
        ("waiting", "busy"),
        ("pending", "busy"),
        ("completed", "ended"),
        ("failed", "ended"),
        ("killed", "ended"),
        (" IDLE ", "idle"),
        ("surprise", "unknown"),
        ("", "unknown"),
    ],
)
def test_map_rc_status(status: str, state: str) -> None:
    assert map_rc_status(status) == state


def test_seat_missing_from_the_registry_is_ended() -> None:
    assert rc_state_for("cc-2build", [_agent("cc-1build", "idle")]).state == "ended"


def test_stopped_background_record_without_status_is_not_live() -> None:
    reading = rc_state_for("cc-master", [_agent("cc-master", None)])
    assert reading.state == "ended"
    assert reading.status is None


def test_live_entry_wins_over_a_stopped_record_of_the_same_name() -> None:
    agents = [_agent("cc-master", None), _agent("cc-master", "idle")]
    assert rc_state_for("cc-master", agents).state == "idle"


def test_two_live_entries_with_one_name_are_unknown() -> None:
    agents = [_agent("cc-master", "idle"), _agent("cc-master", "busy")]
    assert rc_state_for("cc-master", agents).state == "unknown"


def test_unreadable_registry_is_unknown() -> None:
    reading = rc_state_for("cc-master", None)
    assert (reading.state, reading.status) == ("unknown", None)


def test_non_stream_seat_is_matched_by_exact_name_not_a_prefix() -> None:
    assert rc_state_for("cc-mast", [_agent("cc-master", "idle")]).state == "ended"


def test_stream_seat_is_matched_by_cwd_when_its_registry_name_drifts() -> None:
    drifted = _agent("build-41", "idle", cwd=_CWD["cc-1build"])
    assert rc_state_for("cc-1build", [drifted]).state == "idle"


def test_stream_seat_does_not_match_a_sibling_worktree() -> None:
    # build2's cwd ends in "build2", which must not satisfy the "build" worktree.
    assert rc_state_for("cc-1build", [_agent("cc-2build", "idle")]).state == "ended"


def test_stream_seat_is_not_matched_by_name_alone() -> None:
    wrong_cwd = _agent("cc-1build", "idle", cwd="/elsewhere")
    assert rc_state_for("cc-1build", [wrong_cwd]).state == "ended"


# --- the busy state (AC-1) ---------------------------------------------------


def test_ac1_held_draft_with_rc_idle_reads_idle() -> None:
    runner = _Runner(panes={"cc-1build": _DRAFT_PANE}, agents=[_agent("cc-1build", "idle")])
    assert seat_busy_state("cc-1build", runner) == "idle"


def test_ac1_the_pane_alone_would_have_read_busy() -> None:
    runner = _Runner(panes={"cc-1build": _DRAFT_PANE}, agents_ok=False)
    assert seat_busy_state("cc-1build", runner) == "busy"


@pytest.mark.parametrize("status", ["busy", "running", "waiting", "pending"])
def test_busy_status_reads_busy_even_when_the_pane_shows_a_bare_prompt(status: str) -> None:
    runner = _Runner(panes={"cc-1build": _IDLE_PANE}, agents=[_agent("cc-1build", status)])
    assert seat_busy_state("cc-1build", runner) == "busy"


def test_unreadable_registry_falls_back_to_the_pane() -> None:
    idle = _Runner(panes={"cc-1build": _IDLE_PANE}, agents_ok=False)
    busy = _Runner(panes={"cc-1build": _BUSY_PANE}, agents_ok=False)
    assert seat_busy_state("cc-1build", idle) == "idle"
    assert seat_busy_state("cc-1build", busy) == "busy"


def test_missing_tmux_session_is_absent() -> None:
    assert seat_busy_state("cc-1build", _Runner(agents=[_agent("cc-1build", "idle")])) == "absent"


def test_live_tmux_session_missing_from_the_registry_is_ended() -> None:
    runner = _Runner(panes={"cc-1build": _IDLE_PANE}, agents=[_agent("cc-2build", "idle")])
    assert seat_busy_state("cc-1build", runner) == "ended"


# --- the send-keys gate ------------------------------------------------------


def _gate(runner: _Runner, tmp_path: Path, seat: str = "cc-1build", **kwargs: object) -> bool:
    return draft_known_empty(
        seat,
        runner,
        now=_NOW,
        state_dir=tmp_path,
        engine_versions=frozenset({_ENGINE}),
        **kwargs,
    )


def test_rc_idle_and_a_bare_prompt_is_known_empty(tmp_path: Path) -> None:
    runner = _Runner(panes={"cc-1build": _IDLE_PANE}, agents=[_agent("cc-1build", "idle")])
    assert _gate(runner, tmp_path) is True


def test_rc_idle_with_a_held_draft_is_not_known_empty(tmp_path: Path) -> None:
    runner = _Runner(panes={"cc-1build": _DRAFT_PANE}, agents=[_agent("cc-1build", "idle")])
    assert _gate(runner, tmp_path) is False


@pytest.mark.parametrize("status", ["busy", "running", "waiting", "pending", "completed"])
def test_rc_not_idle_is_never_known_empty(tmp_path: Path, status: str) -> None:
    runner = _Runner(panes={"cc-1build": _IDLE_PANE}, agents=[_agent("cc-1build", status)])
    assert _gate(runner, tmp_path) is False


def test_an_unreadable_registry_is_not_known_empty_even_with_a_bare_prompt(tmp_path: Path) -> None:
    # ADR-0155 D7: an unknown reading counts as not known empty.
    runner = _Runner(panes={"cc-1build": _IDLE_PANE}, agents_ok=False)
    assert _gate(runner, tmp_path) is False


def test_an_ambiguous_registry_is_not_known_empty(tmp_path: Path) -> None:
    agents = [_agent("cc-1build", "idle"), _agent("cc-1build-dup", "idle", cwd=_CWD["cc-1build"])]
    runner = _Runner(panes={"cc-1build": _IDLE_PANE}, agents=agents)
    assert _gate(runner, tmp_path) is False


def test_a_missing_claude_binary_reads_as_an_unreadable_registry(tmp_path: Path) -> None:
    class _NoClaude(_Runner):
        def __call__(self, argv: Sequence[str]) -> _Result:
            if tuple(argv)[:1] == ("claude",):
                raise FileNotFoundError("claude")
            return super().__call__(argv)

    runner = _NoClaude(panes={"cc-1build": _IDLE_PANE})
    assert _gate(runner, tmp_path) is False
    assert seat_busy_state("cc-1build", runner) == "idle"  # the pane fallback


def test_fresh_mod_report_that_sees_a_draft_vetoes_a_bare_pane(tmp_path: Path) -> None:
    _write_mod(tmp_path, "cc-1build", draft_present=True)
    runner = _Runner(panes={"cc-1build": _IDLE_PANE}, agents=[_agent("cc-1build", "idle")])
    assert _gate(runner, tmp_path) is False


def test_fresh_mod_report_and_a_contradicting_pane_disagree(tmp_path: Path) -> None:
    _write_mod(tmp_path, "cc-1build", draft_present=False)
    runner = _Runner(panes={"cc-1build": _DRAFT_PANE}, agents=[_agent("cc-1build", "idle")])
    assert _gate(runner, tmp_path) is False


def test_fresh_mod_report_decides_when_the_pane_cannot_be_read(tmp_path: Path) -> None:
    _write_mod(tmp_path, "cc-1build", draft_present=False)
    runner = _Runner(panes={"cc-1build": ""}, agents=[_agent("cc-1build", "idle")])
    assert _gate(runner, tmp_path) is True


def test_mod_report_in_turn_state_is_not_known_empty(tmp_path: Path) -> None:
    _write_mod(tmp_path, "cc-1build", state="turn")
    runner = _Runner(panes={"cc-1build": _IDLE_PANE}, agents=[_agent("cc-1build", "idle")])
    assert _gate(runner, tmp_path) is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"heartbeat_at": _NOW - 91},
        {"disabled": True},
        {"engine_version": "9.9.9"},
    ],
)
def test_stale_disabled_or_untested_mod_report_is_ignored(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    # The report claims a draft; if it were trusted the gate would refuse.
    _write_mod(tmp_path, "cc-1build", draft_present=True, **overrides)  # type: ignore[arg-type]
    runner = _Runner(panes={"cc-1build": _IDLE_PANE}, agents=[_agent("cc-1build", "idle")])
    assert _gate(runner, tmp_path) is True


# --- the mod report reader ---------------------------------------------------


def test_read_mod_report_parses_the_adr_fields(tmp_path: Path) -> None:
    _write_mod(tmp_path, "cc-2build", draft_present=True)
    report = read_mod_report("cc-2build", state_dir=tmp_path)
    assert report == ModReport(
        state="idle",
        draft_present=True,
        disabled=False,
        engine_version=_ENGINE,
        heartbeat_at=_NOW - 10,
    )


def test_read_mod_report_is_none_without_a_file(tmp_path: Path) -> None:
    assert read_mod_report("cc-2build", state_dir=tmp_path) is None


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"state": "idle"}',
        '{"heartbeat_at": "x"}',
        # A huge integer heartbeat overflows float().
        '{"state": "idle", "draft_present": false, "heartbeat_at": ' + "9" * 400 + "}",
    ],
)
def test_read_mod_report_is_none_for_a_malformed_file(tmp_path: Path, text: str) -> None:
    (tmp_path / "cc-2build.json").write_text(text, encoding="utf-8")
    assert read_mod_report("cc-2build", state_dir=tmp_path) is None


# --- the per-tick readings (AC-4) --------------------------------------------


def test_ac4_every_seat_gets_an_rc_status_reading(tmp_path: Path) -> None:
    seats = ("cc-master", "cc-1build", "cc-2build", "cc-adrs", "cc-explore")
    runner = _Runner(
        panes={"cc-master": _IDLE_PANE, "cc-1build": _BUSY_PANE, "cc-2build": _DRAFT_PANE},
        agents=[
            _agent("cc-master", "idle"),
            _agent("cc-1build", "busy"),
            _agent("cc-2build", "idle"),
            _agent("cc-explore", "waiting"),
            # cc-adrs is missing from the registry
        ],
    )
    readings = read_seat_readings(runner, now=_NOW, seats=seats, state_dir=tmp_path)

    assert [r.seat for r in readings] == list(seats)
    by_seat = {r.seat: r for r in readings}
    assert by_seat["cc-master"].rc_status == "idle"
    assert by_seat["cc-1build"].rc_status == "busy"
    assert by_seat["cc-explore"].rc_status == "waiting"
    assert by_seat["cc-adrs"].rc_status is None
    assert by_seat["cc-adrs"].rc_state == "ended"
    assert all(r.rc_state in {"idle", "busy", "ended", "unknown"} for r in readings)


def test_readings_carry_the_pane_and_the_disagreement(tmp_path: Path) -> None:
    runner = _Runner(panes={"cc-2build": _DRAFT_PANE}, agents=[_agent("cc-2build", "idle")])
    (reading,) = read_seat_readings(runner, now=_NOW, seats=("cc-2build",), state_dir=tmp_path)
    assert reading.pane == "busy"  # the scrape reads the held draft as busy
    assert reading.rc_state == "idle"  # Remote Control does not
    assert reading.mod_state is None


def test_readings_include_the_mod_state_when_a_file_exists(tmp_path: Path) -> None:
    _write_mod(tmp_path, "cc-2build", state="turn")
    runner = _Runner(panes={"cc-2build": _BUSY_PANE}, agents=[_agent("cc-2build", "busy")])
    (reading,) = read_seat_readings(runner, now=_NOW, seats=("cc-2build",), state_dir=tmp_path)
    assert reading.mod_state == "turn"


def test_one_registry_read_serves_every_seat(tmp_path: Path) -> None:
    runner = _Runner(agents=[])
    read_seat_readings(runner, now=_NOW, seats=("cc-master", "cc-2build"), state_dir=tmp_path)
    assert [c for c in runner.calls if c[:2] == ("claude", "agents")] == [
        ("claude", "agents", "--json", "--all")
    ]


def test_an_unreadable_registry_still_logs_every_seat(tmp_path: Path) -> None:
    runner = _Runner(panes={"cc-master": _IDLE_PANE}, agents_ok=False)
    readings = read_seat_readings(
        runner, now=_NOW, seats=("cc-master", "cc-2build"), state_dir=tmp_path
    )
    assert [r.rc_state for r in readings] == ["unknown", "unknown"]
    assert [r.pane for r in readings] == ["idle", "absent"]


def test_undecodable_registry_output_reads_as_an_unreadable_registry(tmp_path: Path) -> None:
    class _BadBytes(_Runner):
        def __call__(self, argv: Sequence[str]) -> _Result:
            if tuple(argv)[:2] == ("claude", "agents"):
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
            return super().__call__(argv)

    runner = _BadBytes(panes={"cc-1build": _IDLE_PANE})
    assert _gate(runner, tmp_path) is False
    assert seat_busy_state("cc-1build", runner) == "idle"  # the pane fallback
