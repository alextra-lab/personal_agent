"""FRE-1543 — the server builds the folded call history from the turn's own transport events.

The record must hold the same content the live panel (PWA ``buildTurnSummary``) showed: the
phases in start order with their state and duration, the deduplicated tool names, and the
terminal state.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from personal_agent.transport.turn_summary import (
    TurnSummaryPhase,
    TurnSummaryRecord,
    build_turn_summary,
)

T0 = "2026-10-04T10:00:00+00:00"
T1 = "2026-10-04T10:00:02.500000+00:00"
T2 = "2026-10-04T10:00:03+00:00"
T3 = "2026-10-04T10:00:07+00:00"
NOW = datetime(2026, 10, 4, 10, 0, 10, tzinfo=UTC)


def _start(
    phase_id: str,
    phase: str,
    started_at: str,
    *,
    detail: str | None = None,
    parent: str | None = None,
) -> dict[str, object]:
    return {
        "type": "PHASE_START",
        "data": {
            "phase": phase,
            "phase_id": phase_id,
            "started_at": started_at,
            "detail": detail,
            "parent_id": parent,
        },
    }


def _end(phase_id: str, ended_at: str, *, ok: bool = True) -> dict[str, object]:
    return {"type": "PHASE_END", "data": {"phase_id": phase_id, "ok": ok, "ended_at": ended_at}}


def _tool(name: str) -> dict[str, object]:
    return {"type": "TOOL_CALL_START", "data": {"tool_name": name, "args": {}}}


def test_two_round_turn_holds_phases_in_order_with_server_durations() -> None:
    events = [
        _start("p1", "planning", T0),
        {"type": "STATE_DELTA", "data": {"key": "turn_status", "value": {}}},
        _end("p1", T1),
        _tool("web_search"),
        _tool("web_search"),
        _tool("read_url"),
        _start("p2", "synthesis", T2),
        _end("p2", T3),
        {"type": "TEXT_DELTA", "data": {"text": "answer"}},
        {"type": "DONE", "trace_id": "t"},
    ]

    record = build_turn_summary(events, now=NOW)

    assert record == TurnSummaryRecord(
        phases=[
            TurnSummaryPhase(
                phase_id="p1",
                phase="planning",
                detail=None,
                duration_ms=2500,
                state="completed",
                parent_id=None,
            ),
            TurnSummaryPhase(
                phase_id="p2",
                phase="synthesis",
                detail=None,
                duration_ms=4000,
                state="completed",
                parent_id=None,
            ),
        ],
        tools=["web_search", "read_url"],
        terminal_state="completed",
    )


def test_failed_phase_is_error_and_concurrent_child_keeps_its_parent() -> None:
    events = [
        _start("e1", "expansion", T0),
        _start("c1", "sub_agent", T0, detail="worker 1", parent="e1"),
        _end("c1", T1, ok=False),
        _end("e1", T2),
    ]

    record = build_turn_summary(events, now=NOW)

    assert record is not None
    child = record.phases[1]
    assert (child.phase_id, child.parent_id, child.state, child.detail) == (
        "c1",
        "e1",
        "error",
        "worker 1",
    )
    assert record.terminal_state == "completed"


def test_cancelled_turn_resolves_unended_phase_to_cancelled_and_uses_build_time() -> None:
    events = [
        _start("p1", "planning", T0),
        {"type": "CANCELLED", "data": {"reason": "user_cancel"}},
    ]

    record = build_turn_summary(events, now=NOW)

    assert record is not None
    assert record.terminal_state == "cancelled"
    assert record.phases[0].state == "cancelled"
    assert record.phases[0].duration_ms == 10_000


def test_run_error_marks_the_turn_failed() -> None:
    events = [_start("p1", "planning", T0), {"type": "RUN_ERROR", "data": {}}]

    record = build_turn_summary(events, now=NOW)

    assert record is not None
    assert record.terminal_state == "error"
    assert record.phases[0].state == "error"


def test_a_turn_with_no_phases_and_no_tools_stores_no_record() -> None:
    events = [{"type": "TEXT_DELTA", "data": {"text": "hi"}}, {"type": "DONE"}]

    assert build_turn_summary(events, now=NOW) is None


def test_malformed_events_are_skipped_not_raised() -> None:
    events: list[dict[str, object]] = [
        {"type": "PHASE_START", "data": "not-a-dict"},
        {"type": "PHASE_START", "data": {"phase_id": "p1"}},  # no phase / started_at
        {"type": "PHASE_END", "data": {"phase_id": "unknown", "ended_at": T1}},
        {"type": "TOOL_CALL_START", "data": {}},
        _start("p2", "planning", T0),
        _end("p2", "not-a-timestamp"),
    ]

    record = build_turn_summary(events, now=NOW)

    assert record is not None
    assert [p.phase_id for p in record.phases] == ["p2"]
    # An unparseable end stamp falls back to the build time rather than inventing a value.
    assert record.phases[0].duration_ms == 10_000
    assert record.tools == []


def test_record_serializes_to_the_stored_wire_shape() -> None:
    record = build_turn_summary([_start("p1", "planning", T0), _end("p1", T1)], now=NOW)

    assert record is not None
    assert record.model_dump() == {
        "phases": [
            {
                "phase_id": "p1",
                "phase": "planning",
                "detail": None,
                "duration_ms": 2500,
                "state": "completed",
                "parent_id": None,
            }
        ],
        "tools": [],
        "terminal_state": "completed",
    }


# ── per-turn recorder (codex plan review finding 1) ────────────────────────────


@pytest.mark.asyncio
async def test_two_concurrent_turns_on_one_session_never_blend() -> None:
    """Each turn's task records only its own events, including a sub-agent task it spawns."""
    import asyncio

    from personal_agent.transport.turn_summary import record_turn_event, start_turn_recording

    gate = asyncio.Event()

    async def turn(phase_id: str) -> list[str]:
        recorder = start_turn_recording()
        record_turn_event(_start(phase_id, "planning", T0))
        await gate.wait()  # both turns are now live at once

        async def sub_agent() -> None:
            record_turn_event(_start(f"{phase_id}-child", "sub_agent", T0, parent=phase_id))

        await asyncio.create_task(sub_agent())
        record = build_turn_summary(recorder.events, now=NOW)
        assert record is not None
        return [p.phase_id for p in record.phases]

    first = asyncio.create_task(turn("a"))
    second = asyncio.create_task(turn("b"))
    await asyncio.sleep(0)
    gate.set()

    assert await first == ["a", "a-child"]
    assert await second == ["b", "b-child"]


def test_an_event_outside_any_turn_is_not_recorded() -> None:
    from personal_agent.transport.turn_summary import record_turn_event

    record_turn_event(_start("x", "planning", T0))  # no recorder in this context: a no-op
