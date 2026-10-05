"""The folded per-turn call history, built on the server (FRE-1543, ADR-0123 T4).

The PWA used to build this record only in memory, at DONE, so a page reload lost it. The
server now builds it from the turn's own persisted transport events (the ``session_events``
rows the live panel was drawn from) and stores it on the assistant message, so REST hydration
renders the same folded panel the live turn showed.

The events come from a per-turn recorder (:class:`TurnRecorder`), held in a ``ContextVar``
set by the turn's own task. Several turns of one session can run at once, and the phase
events carry no ``trace_id``, so a seq range of ``session_events`` could blend two turns. A
context variable cannot: each turn's task — and every sub-agent task it spawns, which copies
its context — records into that turn's recorder only.

The derivation mirrors the PWA's ``buildTurnSummary`` (``seshat-pwa/src/lib/phase-summary.ts``):

* phases in first-start order; ``PHASE_END.ok`` decides ``completed`` / ``error``; a phase with
  no end takes the turn's terminal state;
* tools are the calls, one row per ``TOOL_CALL_START`` in start order (FRE-1551). An end closes
  the open row with its ``tool_call_id``, or, for an event with no id, the first open row of
  its name. ``result == "failed"`` marks the row ``failed``, any other end ``completed``, and
  a row with no end stays ``unfinished``;
* the terminal state is ``cancelled`` after a ``CANCELLED`` event, else ``error`` after a
  ``RUN_ERROR``, else ``completed``.

Durations use the server's own start and end stamps (ADR-0142 put ``ended_at`` on every
``PHASE_END``). A phase whose end is missing or unreadable is measured to the build time.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

PhaseState = Literal["completed", "cancelled", "error"]
TerminalState = Literal["completed", "cancelled", "error"]
ToolStatus = Literal["completed", "failed", "unfinished"]


class TurnSummaryPhase(BaseModel):
    """One phase row of the folded call history.

    Attributes:
        phase_id: The phase instance id (pairs a start with its end).
        phase: The phase name (``planning``, ``synthesis``, …).
        detail: Optional qualifier shown beside the phase label.
        duration_ms: Server end stamp minus server start stamp, in milliseconds.
        state: How the phase ended.
        parent_id: The parent phase id for a concurrent child, else ``None``.
    """

    model_config = ConfigDict(frozen=True)

    phase_id: str
    phase: str
    detail: str | None
    duration_ms: int
    state: PhaseState
    parent_id: str | None


class TurnSummaryTool(BaseModel):
    """One tool call row of the folded call history.

    Attributes:
        name: The tool name.
        status: ``completed`` or ``failed`` once the call ended, ``unfinished`` if the turn
            stopped before its end event.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    status: ToolStatus


class TurnSummaryRecord(BaseModel):
    """The folded call history of one turn, stored on its assistant message.

    Attributes:
        phases: Phase rows in first-start order.
        tools: The turn's tool calls, one row per call, in start order.
        terminal_state: How the turn ended.
    """

    model_config = ConfigDict(frozen=True)

    phases: list[TurnSummaryPhase]
    tools: list[TurnSummaryTool]
    terminal_state: TerminalState


def _parse_time(value: object) -> datetime | None:
    """Parse an ISO-8601 stamp, or return ``None`` for anything else."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _data(event: Mapping[str, object]) -> Mapping[str, object] | None:
    """Return the event's ``data`` mapping, or ``None`` when it is not a mapping."""
    data = event.get("data")
    return data if isinstance(data, Mapping) else None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _close_call(
    calls: list[tuple[str | None, str, ToolStatus]], data: Mapping[str, object]
) -> None:
    """Mark the open call that a ``TOOL_CALL_END`` closes; ignore an end with no open call."""
    name = data.get("tool_name")
    call_id = _optional_str(data.get("tool_call_id"))
    for i, (open_id, open_name, status) in enumerate(calls):
        if status != "unfinished":
            continue
        matches = open_id == call_id if call_id is not None else open_name == name
        if matches:
            # The executor sends ``"failed"`` as the result of a failed dispatch (FRE-1547).
            ended: ToolStatus = "failed" if data.get("result") == "failed" else "completed"
            calls[i] = (open_id, open_name, ended)
            return


def build_turn_summary(
    events: Sequence[Mapping[str, object]], *, now: datetime
) -> TurnSummaryRecord | None:
    """Build the folded call history from one turn's transport envelopes.

    Malformed events are skipped, never raised on: the record is cosmetic and must never fail
    a turn.

    Args:
        events: The turn's AG-UI envelopes in ``seq`` order.
        now: The build time, used as the end of a phase with no readable end stamp.

    Returns:
        The record, or ``None`` when the turn had no phases and no tools (the panel never
        renders for such a turn).
    """
    starts: dict[str, tuple[str, str | None, datetime, str | None]] = {}
    ends: dict[str, tuple[PhaseState, datetime | None]] = {}
    calls: list[tuple[str | None, str, ToolStatus]] = []
    cancelled = False
    errored = False

    for event in events:
        kind = event.get("type")
        data = _data(event)
        if kind == "CANCELLED":
            cancelled = True
        elif kind == "RUN_ERROR":
            errored = True
        elif data is None:
            continue
        elif kind == "PHASE_START":
            phase_id = data.get("phase_id")
            phase = data.get("phase")
            started = _parse_time(data.get("started_at"))
            if isinstance(phase_id, str) and isinstance(phase, str) and started is not None:
                starts.setdefault(
                    phase_id,
                    (
                        phase,
                        _optional_str(data.get("detail")),
                        started,
                        _optional_str(data.get("parent_id")),
                    ),
                )
        elif kind == "PHASE_END":
            phase_id = data.get("phase_id")
            if isinstance(phase_id, str) and phase_id in starts and phase_id not in ends:
                state: PhaseState = "error" if data.get("ok") is False else "completed"
                ends[phase_id] = (state, _parse_time(data.get("ended_at")))
        elif kind == "TOOL_CALL_START":
            name = data.get("tool_name")
            if isinstance(name, str) and name:
                calls.append((_optional_str(data.get("tool_call_id")), name, "unfinished"))
        elif kind == "TOOL_CALL_END":
            _close_call(calls, data)

    if not starts and not calls:
        return None

    terminal: TerminalState = "cancelled" if cancelled else "error" if errored else "completed"
    phases: list[TurnSummaryPhase] = []
    for phase_id, (phase, detail, started, parent_id) in starts.items():
        state, ended = ends.get(phase_id, (terminal, None))
        duration = (ended or now) - started
        phases.append(
            TurnSummaryPhase(
                phase_id=phase_id,
                phase=phase,
                detail=detail,
                duration_ms=max(0, round(duration.total_seconds() * 1000)),
                state=state,
                parent_id=parent_id,
            )
        )
    return TurnSummaryRecord(
        phases=phases,
        tools=[TurnSummaryTool(name=name, status=status) for _, name, status in calls],
        terminal_state=terminal,
    )


class TurnRecorder:
    """Collects the transport envelopes one turn emitted, in emission order."""

    def __init__(self) -> None:
        """Start with no events."""
        self.events: list[Mapping[str, object]] = []


_current_recorder: ContextVar[TurnRecorder | None] = ContextVar(
    "fre1543_turn_recorder", default=None
)


def start_turn_recording() -> TurnRecorder:
    """Start recording the current task's transport events and return the recorder.

    Call it from the turn's own task, before the turn emits anything. Tasks the turn
    spawns later copy the context and record into the same recorder.

    Returns:
        The recorder that :func:`record_turn_event` fills for this context.
    """
    recorder = TurnRecorder()
    _current_recorder.set(recorder)
    return recorder


def record_turn_event(envelope: Mapping[str, object]) -> None:
    """Record a persisted envelope for the turn running in this context, if any.

    Args:
        envelope: The AG-UI envelope that was persisted and enqueued.
    """
    recorder = _current_recorder.get()
    if recorder is not None:
        recorder.events.append(envelope)


def stop_turn_recording() -> None:
    """Stop recording in the current context (the turn has ended)."""
    _current_recorder.set(None)
