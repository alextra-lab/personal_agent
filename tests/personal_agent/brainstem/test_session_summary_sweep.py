"""The session-digest idle sweep (ADR-0124 D1, FRE-947).

Covers **AC-1** (generation frequency tracks quiet periods, not turns), **AC-2** (no
session left behind its own activity) and **AC-3** (a resumed session is regenerated
and the content reflects the new turns).

AC-1 is tested at both bounds it actually states. A single-idle-window test alone is
insufficient: it cannot distinguish a correct implementation from one that generates
once per *sweep tick*, and it never exercises the "generations <= idle gaps + 1"
bound at all. So there is a multi-gap case as well, plus the
does-not-fire-before-the-threshold case that a bare count bound would let an eager
implementation pass.
"""

# ruff: noqa: D103

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from personal_agent.brainstem import scheduler as sched
from personal_agent.brainstem.scheduler import BrainstemScheduler, _parse_graph_timestamp
from personal_agent.captains_log.capture import TaskCapture
from personal_agent.memory.session_digest import (
    DigestItem,
    SessionDigest,
    SessionSummaryOutcome,
    SessionSummaryStatus,
    SummaryFailureReason,
)

_USER_ID = uuid4()
_IDLE = 900.0  # 15 minutes


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _capture(session_id: str, at: datetime, *, text: str = "hello") -> TaskCapture:
    return TaskCapture(
        trace_id=f"cap-{at.isoformat()}",
        session_id=session_id,
        timestamp=at,
        user_message=text,
        assistant_response=f"answer to {text}",
        outcome="completed",
        user_id=_USER_ID,
    )


def _generated(label: str = "A session") -> SessionSummaryOutcome:
    return SessionSummaryOutcome(
        status=SessionSummaryStatus.GENERATED,
        label=label,
        digest=SessionDigest(
            decisions=[DigestItem(text="Chose the sweep.", basis="user_statement")]
        ),
    )


class _FakeMemory:
    """Minimal in-memory stand-in for the graph's session rows.

    Models the two properties the sweep's correctness turns on — ``ended_at`` (which
    the conditional write is predicated on) and ``summary_generated_at`` (freshness).
    """

    def __init__(self, sessions: dict[str, dict[str, Any]] | None = None) -> None:
        self.sessions: dict[str, dict[str, Any]] = sessions or {}
        self.writes: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []

    async def find_dirty_idle_sessions(
        self, *, idle_threshold_seconds: float, max_attempts: int, limit: int = 25, trace_id=None
    ) -> list[dict[str, Any]]:
        cutoff = _now() - timedelta(seconds=idle_threshold_seconds)
        rows = []
        for sid, s in self.sessions.items():
            if s["ended_at"] >= cutoff:
                continue  # not idle yet
            fresh = s.get("summary_generated_at")
            if fresh is not None and fresh >= s["ended_at"]:
                continue  # not dirty
            reason = s.get("summary_failure_reason")
            if (
                reason in {"oversized_input", "schema_invalid", "span_validation_failed"}
                and s.get("summary_attempt_count", 0) >= max_attempts
            ):
                continue  # recorded terminal failure
            rows.append(
                {"session_id": sid, "started_at": s["started_at"], "ended_at": s["ended_at"]}
            )
        return sorted(rows, key=lambda r: r["ended_at"])[:limit]

    async def write_session_digest(
        self,
        session_id: str,
        *,
        expected_ended_at: datetime,
        generated_at: datetime,
        turn_count: int,
        label: str | None = None,
        digest: SessionDigest | None = None,
        trace_id=None,
    ) -> bool:
        session = self.sessions[session_id]
        if session["ended_at"] != expected_ended_at:
            return False  # refused — the session advanced
        session["summary_generated_at"] = generated_at
        session["session_label"] = label
        session["session_digest"] = digest
        session["turn_count"] = turn_count
        session["summary_failure_reason"] = None
        session["summary_attempt_count"] = 0
        self.writes.append({"session_id": session_id, "label": label, "digest": digest})
        return True

    async def record_session_summary_failure(
        self, session_id: str, *, expected_ended_at: datetime, failure_reason: str, trace_id=None
    ) -> bool:
        session = self.sessions[session_id]
        if session["ended_at"] != expected_ended_at:
            return False
        session["summary_failure_reason"] = failure_reason
        session["summary_attempt_count"] = session.get("summary_attempt_count", 0) + 1
        self.failures.append({"session_id": session_id, "reason": failure_reason})
        return True


def _session(*, ended_minutes_ago: float, turns: int = 3) -> dict[str, Any]:
    ended = _now() - timedelta(minutes=ended_minutes_ago)
    return {
        "started_at": ended - timedelta(minutes=10),
        "ended_at": ended,
        "turn_count": turns,
        "summary_generated_at": None,
    }


@pytest.fixture
def scheduler(monkeypatch: pytest.MonkeyPatch) -> BrainstemScheduler:
    monkeypatch.setattr(sched.settings, "session_summary_enabled", True)
    monkeypatch.setattr(sched.settings, "session_summary_idle_threshold_seconds", _IDLE)
    monkeypatch.setattr(sched.settings, "session_summary_max_attempts", 2)
    return BrainstemScheduler()


def _patch_producer(
    monkeypatch: pytest.MonkeyPatch,
    outcome: SessionSummaryOutcome | None = None,
    *,
    captures_by_session: dict[str, list[TaskCapture]] | None = None,
) -> list[dict[str, Any]]:
    """Patch the producer and the capture reader; return the recorded generation calls."""
    calls: list[dict[str, Any]] = []

    async def fake_generate(captures, *, session_id, ended_at, trace_id="x"):
        calls.append({"session_id": session_id, "captures": list(captures), "ended_at": ended_at})
        return outcome or _generated()

    def fake_read(session_id, *, started_at, ended_at, limit=1000):
        if captures_by_session is not None:
            return captures_by_session.get(session_id, [])
        return [_capture(session_id, started_at), _capture(session_id, ended_at)]

    monkeypatch.setattr(
        "personal_agent.second_brain.session_summary.generate_session_digest", fake_generate
    )
    monkeypatch.setattr("personal_agent.captains_log.capture.read_session_captures", fake_read)
    return calls


# --------------------------------------------------------------------------
# AC-1 — generation tracks quiet periods, not turns
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_idle_window_generates_exactly_once(
    scheduler: BrainstemScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session whose turns all arrived inside one idle window generates ONCE.

    Repeated sweeps must not regenerate it: after the first write the session is
    clean, and the dirty predicate stops selecting it.
    """
    memory = _FakeMemory({"sess-1": _session(ended_minutes_ago=30)})
    scheduler.memory_service = memory  # type: ignore[assignment]
    calls = _patch_producer(monkeypatch)

    for _ in range(3):
        await scheduler.run_session_summary_sweep(trace_id="t-1")

    assert len(calls) == 1, "three sweeps over one quiet period must generate once"
    assert len(memory.writes) == 1


@pytest.mark.asyncio
async def test_multi_gap_session_generates_once_per_gap(
    scheduler: BrainstemScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two idle gaps => two generations. This is the bound AC-1 actually states.

    The single-window test cannot exercise it: `generations <= idle gaps + 1` is
    satisfied trivially at one gap, so the count bound only bites once a session
    goes quiet, resumes, and goes quiet again.
    """
    memory = _FakeMemory({"sess-1": _session(ended_minutes_ago=30)})
    scheduler.memory_service = memory  # type: ignore[assignment]
    calls = _patch_producer(monkeypatch)

    await scheduler.run_session_summary_sweep(trace_id="t-1")
    assert len(calls) == 1

    # Simulate the first quiet period ending an hour ago, then the session resuming:
    # a new turn lands AFTER that generation, so ended_at overtakes
    # summary_generated_at and the session is dirty again. Backdating the stamp is
    # how elapsed wall-clock is modelled — a turn timestamped before its own
    # summary would be an impossible state, and the dirty predicate rightly
    # ignores it.
    memory.sessions["sess-1"]["summary_generated_at"] = _now() - timedelta(hours=1)
    memory.sessions["sess-1"]["ended_at"] = _now() - timedelta(minutes=20)
    await scheduler.run_session_summary_sweep(trace_id="t-2")

    assert len(calls) == 2, "one generation per quiet period, not per turn and not per tick"

    # Quiet again with no new turns — no third generation.
    await scheduler.run_session_summary_sweep(trace_id="t-3")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_no_generation_before_the_idle_threshold(
    scheduler: BrainstemScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An eager implementation satisfying only the count bound would fail here."""
    memory = _FakeMemory({"sess-1": _session(ended_minutes_ago=2)})  # threshold is 15
    scheduler.memory_service = memory  # type: ignore[assignment]
    calls = _patch_producer(monkeypatch)

    await scheduler.run_session_summary_sweep(trace_id="t-1")

    assert calls == []
    assert memory.sessions["sess-1"]["summary_generated_at"] is None


# --------------------------------------------------------------------------
# AC-2 — no session left behind its own activity
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_dirty_idle_sessions_remain_after_a_sweep(
    scheduler: BrainstemScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = _FakeMemory(
        {
            "sess-1": _session(ended_minutes_ago=30),
            "sess-2": _session(ended_minutes_ago=45),
            "sess-3": _session(ended_minutes_ago=60),
        }
    )
    scheduler.memory_service = memory  # type: ignore[assignment]
    _patch_producer(monkeypatch)

    await scheduler.run_session_summary_sweep(trace_id="t-1")

    remaining = await memory.find_dirty_idle_sessions(idle_threshold_seconds=_IDLE, max_attempts=2)
    assert remaining == [], "AC-2 fails if any dirty-and-idle row survives the sweep"


@pytest.mark.asyncio
async def test_below_floor_session_does_not_stay_dirty_forever(
    scheduler: BrainstemScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-b: a floor skip is a completed projection, so freshness advances.

    Without this, every single-turn session is permanently dirty and AC-2 can never
    pass — while AC-7 forbids giving it a digest. The reconciliation is that
    `summary_generated_at` means "the projection ran", not "a digest exists".
    """
    memory = _FakeMemory({"sess-1": _session(ended_minutes_ago=30, turns=1)})
    scheduler.memory_service = memory  # type: ignore[assignment]
    _patch_producer(
        monkeypatch, SessionSummaryOutcome(status=SessionSummaryStatus.SKIPPED_BELOW_FLOOR)
    )

    await scheduler.run_session_summary_sweep(trace_id="t-1")

    session = memory.sessions["sess-1"]
    assert session["summary_generated_at"] is not None, "freshness must advance"
    assert session["session_digest"] is None, "AC-7: no digest for a single-turn session"
    assert session["session_label"] is None
    assert await memory.find_dirty_idle_sessions(idle_threshold_seconds=_IDLE, max_attempts=2) == []


@pytest.mark.asyncio
async def test_a_failed_session_stays_dirty_and_retryable(
    scheduler: BrainstemScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4 at the sweep layer: freshness must NOT advance on failure."""
    memory = _FakeMemory({"sess-1": _session(ended_minutes_ago=30)})
    scheduler.memory_service = memory  # type: ignore[assignment]
    _patch_producer(
        monkeypatch,
        SessionSummaryOutcome(
            status=SessionSummaryStatus.FAILED,
            failure_reason=SummaryFailureReason.BUDGET_DENIED,
        ),
    )

    result = await scheduler.run_session_summary_sweep(trace_id="t-1")

    assert result["failed"] == 1
    session = memory.sessions["sess-1"]
    assert session["summary_generated_at"] is None, "a failed session must not look clean"
    assert session["summary_failure_reason"] == "budget_denied"
    # Still selected next sweep — a budget denial is never terminal.
    still_dirty = await memory.find_dirty_idle_sessions(
        idle_threshold_seconds=_IDLE, max_attempts=2
    )
    assert [r["session_id"] for r in still_dirty] == ["sess-1"]


@pytest.mark.asyncio
async def test_terminal_failures_are_excluded_and_counted(
    scheduler: BrainstemScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2 excludes recorded terminal failures — deterministic reason AND attempts."""
    memory = _FakeMemory({"sess-1": _session(ended_minutes_ago=30)})
    memory.sessions["sess-1"]["summary_failure_reason"] = "oversized_input"
    memory.sessions["sess-1"]["summary_attempt_count"] = 2
    scheduler.memory_service = memory  # type: ignore[assignment]
    calls = _patch_producer(monkeypatch)

    result = await scheduler.run_session_summary_sweep(trace_id="t-1")

    assert calls == []
    assert result["considered"] == 0


# --------------------------------------------------------------------------
# AC-3 — resumption regenerates, and the content reflects the new turns
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regenerates_after_new_turns_and_reflects_them(
    scheduler: BrainstemScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: freshness advances AND the regenerated content sees the appended turns."""
    session = _session(ended_minutes_ago=30)
    memory = _FakeMemory({"sess-1": session})
    scheduler.memory_service = memory  # type: ignore[assignment]

    first_turns = [_capture("sess-1", session["started_at"], text="original topic")]
    captures = {"sess-1": first_turns}
    calls = _patch_producer(monkeypatch, captures_by_session=captures)

    await scheduler.run_session_summary_sweep(trace_id="t-1")
    assert memory.sessions["sess-1"]["summary_generated_at"] is not None

    # Model an hour of elapsed wall-clock, then a distinctive new turn landing after
    # that generation, after which the session goes quiet again.
    first_stamp = _now() - timedelta(hours=1)
    memory.sessions["sess-1"]["summary_generated_at"] = first_stamp
    new_ended = _now() - timedelta(minutes=20)
    memory.sessions["sess-1"]["ended_at"] = new_ended
    captures["sess-1"] = [*first_turns, _capture("sess-1", new_ended, text="DISTINCTIVE FACT")]

    await scheduler.run_session_summary_sweep(trace_id="t-2")

    assert memory.sessions["sess-1"]["summary_generated_at"] > first_stamp, (
        "summary_generated_at must advance"
    )
    regenerated_input = calls[-1]["captures"]
    assert any("DISTINCTIVE FACT" in c.user_message for c in regenerated_input), (
        "regeneration must see the appended turns, not just re-stamp freshness"
    )
    # Wholesale, not incremental: the prior turns are re-read too.
    assert len(regenerated_input) == 2


@pytest.mark.asyncio
async def test_write_refused_when_a_turn_lands_mid_generation(
    scheduler: BrainstemScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep must not publish a digest built from captures that went stale."""
    memory = _FakeMemory({"sess-1": _session(ended_minutes_ago=30)})
    scheduler.memory_service = memory  # type: ignore[assignment]

    async def generate_then_a_turn_lands(captures, *, session_id, ended_at, trace_id="x"):
        memory.sessions[session_id]["ended_at"] = _now() - timedelta(minutes=1)
        return _generated()

    monkeypatch.setattr(
        "personal_agent.second_brain.session_summary.generate_session_digest",
        generate_then_a_turn_lands,
    )
    monkeypatch.setattr(
        "personal_agent.captains_log.capture.read_session_captures",
        lambda sid, *, started_at, ended_at, limit=1000: [_capture(sid, started_at)],
    )

    result = await scheduler.run_session_summary_sweep(trace_id="t-1")

    assert result["refused"] == 1
    assert memory.writes == []
    assert memory.sessions["sess-1"]["summary_generated_at"] is None


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_defers_to_an_in_flight_consolidation(
    scheduler: BrainstemScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consolidation is itself advancing ended_at; sweeping across it only earns refusals."""
    memory = _FakeMemory({"sess-1": _session(ended_minutes_ago=30)})
    scheduler.memory_service = memory  # type: ignore[assignment]
    scheduler._consolidation_in_progress = True
    calls = _patch_producer(monkeypatch)

    await scheduler.run_session_summary_sweep(trace_id="t-1")

    assert calls == []


@pytest.mark.asyncio
async def test_sweep_is_single_flight(
    scheduler: BrainstemScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = _FakeMemory({"sess-1": _session(ended_minutes_ago=30)})
    scheduler.memory_service = memory  # type: ignore[assignment]
    scheduler._summary_sweep_in_progress = True
    calls = _patch_producer(monkeypatch)

    await scheduler.run_session_summary_sweep(trace_id="t-1")

    assert calls == []


@pytest.mark.asyncio
async def test_disabled_sweep_does_nothing(
    scheduler: BrainstemScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sched.settings, "session_summary_enabled", False)
    memory = _FakeMemory({"sess-1": _session(ended_minutes_ago=30)})
    scheduler.memory_service = memory  # type: ignore[assignment]
    calls = _patch_producer(monkeypatch)

    await scheduler.run_session_summary_sweep(trace_id="t-1")

    assert calls == []


@pytest.mark.asyncio
async def test_sweep_without_a_memory_service_is_a_no_op(
    scheduler: BrainstemScheduler,
) -> None:
    scheduler.memory_service = None

    assert await scheduler.run_session_summary_sweep(trace_id="t-1") == {
        "considered": 0,
        "generated": 0,
        "skipped": 0,
        "failed": 0,
        "refused": 0,
    }


@pytest.mark.asyncio
async def test_single_flight_flag_is_released_on_error(
    scheduler: BrainstemScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stuck flag would silently disable the sweep for the process's lifetime."""
    failing = AsyncMock(side_effect=RuntimeError("neo4j down"))
    scheduler.memory_service = type("M", (), {"find_dirty_idle_sessions": failing})()  # type: ignore[assignment]

    with pytest.raises(RuntimeError):
        await scheduler.run_session_summary_sweep(trace_id="t-1")

    assert scheduler._summary_sweep_in_progress is False


# --------------------------------------------------------------------------
# Timestamp coercion
# --------------------------------------------------------------------------


def test_graph_timestamps_are_parsed_and_made_aware() -> None:
    aware = datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc)

    assert _parse_graph_timestamp("2026-07-23T10:00:00+00:00") == aware
    assert _parse_graph_timestamp(aware) == aware
    # A naive value is assumed UTC — treating it as local would shift idle arithmetic.
    assert _parse_graph_timestamp("2026-07-23T10:00:00") == aware
    assert _parse_graph_timestamp("not a timestamp") is None
    assert _parse_graph_timestamp(None) is None
