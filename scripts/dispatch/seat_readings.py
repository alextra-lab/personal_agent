"""What the dispatch daemons know about a seat, and when they may type into it (FRE-1556).

ADR-0155 D6 and D7. Three readings describe one seat:

- **Remote Control (RC) status** from ``claude agents --json --all``: a structured signal from the
  tool itself. A held draft does not make it ``busy``.
- **The pane**: ``tmux capture-pane`` text. It is the fallback, and the check for a bare prompt.
- **The mod report**: ``telemetry/seat_state/<seat>.json``, written by the seat-agent mod once
  FRE-1558 ships. No mod exists yet, so no report is fresh until ``MOD_ENGINE_VERSIONS`` names a
  tested engine version.

``seat_busy_state`` answers "is the seat busy?". ``draft_known_empty`` answers the stricter
question that gates ``tmux send-keys``: "may a daemon type into this seat?".
"""

from __future__ import annotations

import dataclasses
import json
import re
import time
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Literal

from scripts.dispatch.launcher import (
    CommandRunner,
    _cwd_matches,
    _rc_agents,
    stream_for_tmux_session,
    topology_for,
)
from scripts.dispatch.pane_state import session_is_idle
from scripts.dispatch.tmux_target import exact_pane, exact_session

__all__ = [
    "DEFAULT_SEAT_STATE_DIR",
    "MOD_ENGINE_VERSIONS",
    "MOD_FRESH_S",
    "ModReport",
    "RcReading",
    "SeatReading",
    "draft_known_empty",
    "map_rc_status",
    "mod_report_fresh",
    "rc_state_for",
    "read_mod_report",
    "read_seat_readings",
    "seat_busy_state",
]

RcState = Literal["idle", "busy", "ended", "unknown"]
PaneState = Literal["idle", "busy", "absent"]
BusyState = Literal["idle", "busy", "ended", "absent"]

# RC statuses that mean a turn runs or a prompt waits. ``busy`` is the value the CLI prints today;
# ``running``, ``waiting`` and ``pending`` are the ADR-0155 D6 names.
_BUSY_STATUSES: frozenset[str] = frozenset({"busy", "running", "waiting", "pending"})
_ENDED_STATUSES: frozenset[str] = frozenset({"completed", "failed", "killed"})

# A mod report older than this is stale (ADR-0155 D7).
MOD_FRESH_S: float = 90.0
# Engine versions that the promoted mod was tested against (ADR-0155 D7). Empty until FRE-1558
# promotes the first version, so no mod report is trusted before then.
MOD_ENGINE_VERSIONS: frozenset[str] = frozenset()
DEFAULT_SEAT_STATE_DIR: Path = Path("telemetry") / "seat_state"

# The mod writes one file per seat; the name pattern also keeps a seat name from escaping the folder.
_SEAT_NAME_RE = re.compile(r"^cc-[a-z0-9]+$")


@dataclasses.dataclass(frozen=True)
class RcReading:
    """One seat's Remote Control reading.

    Attributes:
        status: The raw status string, lowercased, or ``None`` when the registry has no single
            live entry for the seat.
        state: The mapped state. ``unknown`` means the registry cannot be trusted for this seat.
    """

    status: str | None
    state: RcState


@dataclasses.dataclass(frozen=True)
class ModReport:
    """The fields of a mod report that the daemons read (ADR-0155 D6).

    Attributes:
        state: ``idle``, ``turn`` or ``ended``.
        draft_present: Whether the owner has an unsent draft. The text is never written.
        disabled: Whether the kill switch was on at the last write.
        engine_version: The Claude Code build that wrote the report.
        heartbeat_at: Epoch seconds of the last write.
    """

    state: str
    draft_present: bool
    disabled: bool
    engine_version: str
    heartbeat_at: float


@dataclasses.dataclass(frozen=True)
class SeatReading:
    """All readings for one seat on one tick, logged side by side (ADR-0155 phase 1).

    Attributes:
        seat: The tmux session name.
        pane: ``idle`` for a bare prompt, ``busy`` for anything else, ``absent`` without a session.
        rc_status: The raw Remote Control status, or ``None``.
        rc_state: The mapped Remote Control state.
        mod_state: The mod report's ``state``, or ``None`` when no report exists.
        mod_fresh: Whether the mod report is fresh enough to decide.
    """

    seat: str
    pane: PaneState
    rc_status: str | None
    rc_state: RcState
    mod_state: str | None
    mod_fresh: bool


def map_rc_status(status: str) -> RcState:
    """Map one Remote Control status string to a state (ADR-0155 D6).

    Args:
        status: The raw status string.

    Returns:
        ``idle``, ``busy``, ``ended``, or ``unknown`` for a value that no rule names.
    """
    value = status.strip().lower()
    if value == "idle":
        return "idle"
    if value in _BUSY_STATUSES:
        return "busy"
    if value in _ENDED_STATUSES:
        return "ended"
    return "unknown"


def rc_state_for(session: str, agents: Sequence[Mapping[str, object]] | None) -> RcReading:
    """Read one seat's state from the Remote Control registry.

    A dispatch stream's seat is matched by working directory, as the launcher does, because a
    registry name can drift from the tmux name (``cc-1build`` can register as ``build-41``).
    Any other seat, such as ``cc-master``, is matched by exact registry name. An entry without a
    ``status`` is a stopped record, not a live session. A seat with no live entry is ``ended``.
    Two live entries for one seat are ambiguous, so the reading is ``unknown``.

    Args:
        session: The seat name, e.g. ``cc-master``.
        agents: The registry from ``claude agents --json --all``, or ``None`` when unreadable.

    Returns:
        The seat's Remote Control reading.
    """
    if agents is None:
        return RcReading(None, "unknown")
    stream = stream_for_tmux_session(session)
    worktree = None if stream is None else topology_for(stream).worktree

    def is_seat(agent: Mapping[str, object]) -> bool:
        if worktree is None:
            return agent.get("name") == session
        return _cwd_matches(str(agent.get("cwd", "")), worktree)

    live = [agent for agent in agents if is_seat(agent) and isinstance(agent.get("status"), str)]
    if not live:
        return RcReading(None, "ended")
    if len(live) > 1:
        return RcReading(None, "unknown")
    status = str(live[0]["status"]).strip().lower()
    return RcReading(status, map_rc_status(status))


def _registry(runner: CommandRunner) -> list[dict[str, object]] | None:
    """Read the Remote Control registry. A missing binary or undecodable output reads as unreadable."""
    try:
        return _rc_agents(runner)
    except (OSError, ValueError):
        return None


def _read_pane(session: str, runner: CommandRunner) -> tuple[PaneState, str]:
    """Return the pane reading and its raw text. ``absent`` means no tmux session."""
    if runner(["tmux", "has-session", "-t", exact_session(session)]).returncode != 0:
        return "absent", ""
    text = runner(["tmux", "capture-pane", "-t", exact_pane(session), "-p"]).stdout
    return ("idle" if session_is_idle(text) else "busy"), text


def read_mod_report(seat: str, *, state_dir: Path | None = None) -> ModReport | None:
    """Read a seat's mod report.

    Args:
        seat: The seat name. A name that is not ``cc-<letters and digits>`` yields ``None``.
        state_dir: The folder that holds ``<seat>.json``. ``None`` uses
            ``DEFAULT_SEAT_STATE_DIR``.

    Returns:
        The parsed report, or ``None`` when the file is absent or malformed.
    """
    if not _SEAT_NAME_RE.fullmatch(seat):
        return None
    folder = DEFAULT_SEAT_STATE_DIR if state_dir is None else state_dir
    try:
        raw: object = json.loads((folder / f"{seat}.json").read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        state, draft, beat = raw.get("state"), raw.get("draft_present"), raw.get("heartbeat_at")
        if not isinstance(state, str) or not isinstance(draft, bool):
            return None
        if isinstance(beat, bool) or not isinstance(beat, int | float):
            return None
        return ModReport(
            state=state,
            draft_present=draft,
            disabled=raw.get("disabled") is True,
            engine_version=str(raw.get("engine_version") or ""),
            heartbeat_at=float(beat),
        )
    except (OSError, ValueError, OverflowError):
        return None


def mod_report_fresh(
    report: ModReport, now: float, engine_versions: Collection[str] | None = None
) -> bool:
    """Return whether a mod report may decide (ADR-0155 D7).

    Args:
        report: The mod report.
        now: Wall-clock epoch seconds.
        engine_versions: The engine versions that the promoted mod was tested against.
            ``None`` uses ``MOD_ENGINE_VERSIONS``.

    Returns:
        ``True`` when the heartbeat is at most ``MOD_FRESH_S`` old, the kill switch is off, and
        the engine version is on the list.
    """
    tested = MOD_ENGINE_VERSIONS if engine_versions is None else engine_versions
    return (
        0 <= now - report.heartbeat_at <= MOD_FRESH_S
        and not report.disabled
        and report.engine_version in tested
    )


def seat_busy_state(session: str, runner: CommandRunner) -> BusyState:
    """Read whether a seat is busy: Remote Control first, the pane only as a fallback.

    A held draft such as ``❯ /master 1218`` does not make Remote Control report ``busy``, so
    the seat reads ``idle`` here. Whether a daemon may type into it is a separate question,
    answered by ``draft_known_empty``.

    Args:
        session: The seat name.
        runner: The command runner seam.

    Returns:
        ``absent`` without a tmux session. Otherwise Remote Control's ``idle``, ``busy`` or
        ``ended``. When Remote Control cannot say, the pane's ``idle`` or ``busy``.
    """
    if runner(["tmux", "has-session", "-t", exact_session(session)]).returncode != 0:
        return "absent"
    rc = rc_state_for(session, _registry(runner))
    if rc.state != "unknown":
        return rc.state
    pane, _ = _read_pane(session, runner)
    return "idle" if pane == "idle" else "busy"


def draft_known_empty(
    session: str,
    runner: CommandRunner,
    *,
    now: float | None = None,
    state_dir: Path | None = None,
    engine_versions: Collection[str] | None = None,
) -> bool:
    """Return whether a daemon may type into a seat (ADR-0155 D7 point 3).

    Remote Control must read ``idle``. A ``busy`` or ``ended`` seat, and an unreadable or
    ambiguous registry, all refuse: an unknown reading counts as not known empty. A fresh mod
    report decides whether a draft exists. Without one, the pane must show a bare empty prompt.
    A pane that shows a draft or a busy marker against an empty mod report is a disagreement,
    and a disagreement counts as not known empty.

    The registry is read fresh at each call, never from a tick snapshot, so a seat that turned
    busy a moment ago is not typed into. The caller checks that the tmux session exists.

    Args:
        session: The seat name.
        runner: The command runner seam.
        now: Wall-clock epoch seconds. ``None`` reads the clock.
        state_dir: The folder that holds the mod reports. ``None`` uses the default.
        engine_versions: The engine versions on the mod's tested list. ``None`` uses the default.

    Returns:
        ``True`` only when Remote Control reads ``idle`` and every other reading agrees that the
        draft is empty.
    """
    if rc_state_for(session, _registry(runner)).state != "idle":
        return False
    text = runner(["tmux", "capture-pane", "-t", exact_pane(session), "-p"]).stdout
    pane_empty = session_is_idle(text)
    report = read_mod_report(session, state_dir=state_dir)
    if report is not None and mod_report_fresh(
        report, time.time() if now is None else now, engine_versions
    ):
        pane_contradicts = bool(text.strip()) and not pane_empty
        return report.state == "idle" and not report.draft_present and not pane_contradicts
    return pane_empty


def read_seat_readings(
    runner: CommandRunner,
    *,
    now: float,
    seats: Sequence[str],
    state_dir: Path | None = None,
    engine_versions: Collection[str] | None = None,
) -> tuple[SeatReading, ...]:
    """Read all readings of every seat once, for the per-tick log (AC-4).

    One Remote Control read serves all seats.

    Args:
        runner: The command runner seam.
        now: Wall-clock epoch seconds.
        seats: The seat names to read.
        state_dir: The folder that holds the mod reports. ``None`` uses the default.
        engine_versions: The engine versions on the mod's tested list. ``None`` uses the default.

    Returns:
        One reading per seat, in the order of ``seats``.
    """
    agents = _registry(runner)
    readings: list[SeatReading] = []
    for seat in seats:
        rc = rc_state_for(seat, agents)
        pane, _ = _read_pane(seat, runner)
        report = read_mod_report(seat, state_dir=state_dir)
        readings.append(
            SeatReading(
                seat=seat,
                pane=pane,
                rc_status=rc.status,
                rc_state=rc.state,
                mod_state=None if report is None else report.state,
                mod_fresh=report is not None and mod_report_fresh(report, now, engine_versions),
            )
        )
    return tuple(readings)
