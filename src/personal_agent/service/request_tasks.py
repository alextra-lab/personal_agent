"""Registry of chat request tasks (ADR-0143 D1; FRE-1408).

The chat turn task used to be a bare ``asyncio.create_task`` with no reference, no done
callback and no bound. Every chat turn task is now created through this registry and
nowhere else. The registry keeps a strong reference, records one ``RequestTask`` per trace,
logs how each task ended, and cancels the live tasks at service shutdown.

The registry is process-local, like ``_active_connections``. Seshat runs one worker.

Scope of this module today: D1 (the registry, the entry guard, the ending log, the gauge and
the shutdown contract without the stream close) and the record fields that D2 reads. The
stage-two close (D3) is a separate ticket (FRE-1410). The ``close_won`` flag and the
``stage_two_deadline`` field ship here and nothing reads them yet.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Coroutine
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal

from personal_agent.config import settings
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

RequestOutcome = Literal["completed", "timed_out", "cancelled", "failed"]
RequestTaskState = Literal["live", "leaked"]

# Set by ``RequestTaskRegistry.spawn`` while it creates the task, so the task's context copy
# carries the trace id of the record that owns it. The turn body checks it on entry.
_entry_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_task_entry_trace_id", default=None
)


class RequestTaskEntryError(RuntimeError):
    """The chat turn body ran on a task that the registry did not create for it."""


@dataclass(eq=False)
class RequestTask:
    """The registry's record of one chat request task.

    The record is mutable on purpose: ``state`` and ``outcome`` change over its life.

    Attributes:
        trace_id: The turn's trace id. The registry key.
        session_id: The session the turn belongs to.
        started_at: ``loop.time()`` at insertion. The start of every clock in ADR-0143.
        stage_one_deadline: ``started_at`` plus ``service_request_task_lifetime_seconds``.
        stage_two_deadline: The stage-one deadline plus ``service_request_close_timeout_seconds``.
        task: The turn task. ``None`` only while ``spawn`` is creating it.
        state: ``leaked`` once a task outlived shutdown. A leaked record stays enumerable.
        outcome: How the task ended, once known. ``None`` until stamped or derived.
        close_won: Whether the close-once arbiter has been won (FRE-1410). Unused today.
        deadline_timer: The timer that stamps ``timed_out`` at the stage-one deadline.
    """

    trace_id: str
    session_id: str
    started_at: float
    stage_one_deadline: float
    stage_two_deadline: float
    task: asyncio.Task[None] | None = None
    state: RequestTaskState = "live"
    outcome: RequestOutcome | None = None
    close_won: bool = False
    deadline_timer: asyncio.TimerHandle | None = field(default=None, repr=False)

    def stamp_timed_out(self) -> None:
        """Record that stage one fired. This wins over an earlier ``failed`` stamp."""
        self.outcome = "timed_out"

    def stamp_failed(self) -> None:
        """Record that the turn failed, unless an outcome is already recorded."""
        if self.outcome is None:
            self.outcome = "failed"


class RequestTaskRegistry:
    """Owns every chat request task: creates it, keeps it, bounds its record, ends it."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._records: dict[str, RequestTask] = {}

    def spawn(
        self, coro: Coroutine[Any, Any, None], *, trace_id: str, session_id: str
    ) -> RequestTask:
        """Create the request task for one turn and register it.

        Args:
            coro: The turn body, as an un-awaited coroutine.
            trace_id: The turn's trace id. Two turns on one session have two trace ids.
            session_id: The session the turn belongs to.

        Returns:
            The new record. Its ``task`` is already scheduled.

        Raises:
            ValueError: When a record with this trace id exists. The coroutine is closed.
        """
        if trace_id in self._records:
            coro.close()
            raise ValueError(f"a request task for trace {trace_id} is already registered")
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        stage_one = started_at + settings.service_request_task_lifetime_seconds
        record = RequestTask(
            trace_id=trace_id,
            session_id=session_id,
            started_at=started_at,
            stage_one_deadline=stage_one,
            stage_two_deadline=stage_one + settings.service_request_close_timeout_seconds,
        )
        token = _entry_trace_id.set(trace_id)
        try:
            task = asyncio.create_task(coro)
        finally:
            _entry_trace_id.reset(token)
        record.task = task
        self._records[trace_id] = record
        record.deadline_timer = loop.call_at(stage_one, self._on_stage_one_deadline, record)
        task.add_done_callback(partial(self._on_done, record))
        self._log_gauge()
        return record

    def get(self, trace_id: str) -> RequestTask | None:
        """Return the record for a trace.

        Args:
            trace_id: The turn's trace id.

        Returns:
            The record, or ``None`` when no task is registered for the trace.
        """
        return self._records.get(trace_id)

    def records(self) -> tuple[RequestTask, ...]:
        """Return every record.

        Returns:
            The live and the leaked records, in insertion order.
        """
        return tuple(self._records.values())

    def counts(self) -> tuple[int, int]:
        """Count the records by state.

        Returns:
            The number of live records and the number of leaked records.
        """
        leaked = sum(1 for r in self._records.values() if r.state == "leaked")
        return len(self._records) - leaked, leaked

    async def shutdown(self) -> None:
        """Cancel every live task, wait for them, and mark the survivors leaked.

        The wait is bounded by ``service_request_close_timeout_seconds``. This does not
        close the client streams. The stream close at shutdown is FRE-1410's (ADR-0143 D3).
        """
        live = [r.task for r in self._records.values() if r.state == "live" and r.task is not None]
        for task in live:
            task.cancel()
        if live:
            await asyncio.wait(live, timeout=settings.service_request_close_timeout_seconds)
        for record in tuple(self._records.values()):
            if record.state == "live" and record.task is not None and not record.task.done():
                record.state = "leaked"
                log.error(
                    "chat_stream.task_leaked",
                    trace_id=record.trace_id,
                    session_id=record.session_id,
                    elapsed_seconds=self._elapsed(record),
                    reason="shutdown",
                )
        self._log_gauge()

    @staticmethod
    def _elapsed(record: RequestTask) -> float:
        return round(asyncio.get_running_loop().time() - record.started_at, 3)

    def _log_gauge(self) -> None:
        live, leaked = self.counts()
        log.info("request_tasks_gauge", live=live, leaked=leaked)

    def _on_stage_one_deadline(self, record: RequestTask) -> None:
        """Stamp ``timed_out`` when the stage-one deadline finds the task still running.

        ``timed_out`` means that this timer saw the task pending. It does not depend on the
        task's exception path, so a task that swallows its cancellation still ends as
        ``timed_out``.
        """
        if record.task is None or record.task.done():
            return
        record.stamp_timed_out()
        log.warning(
            "chat_stream.task_stage_one_deadline",
            trace_id=record.trace_id,
            session_id=record.session_id,
            elapsed_seconds=self._elapsed(record),
        )

    def _on_done(self, record: RequestTask, task: asyncio.Task[None]) -> None:
        """Log how the task ended and drop its record. The one done callback."""
        if record.deadline_timer is not None:
            record.deadline_timer.cancel()
        self._records.pop(record.trace_id, None)
        # Retrieve the exception of every task that was not cancelled, so a stamped outcome
        # cannot hide an escaped exception from this log or from asyncio's GC warning.
        exc = None if task.cancelled() else task.exception()
        outcome = record.outcome
        if outcome is None:
            if task.cancelled():
                outcome = "cancelled"
            elif exc is not None:
                outcome = "failed"
            else:
                outcome = "completed"
        fields: dict[str, str | float | bool] = {
            "trace_id": record.trace_id,
            "session_id": record.session_id,
            "elapsed_seconds": self._elapsed(record),
            "outcome": outcome,
            "was_leaked": record.state == "leaked",
        }
        if exc is not None:
            fields["error_type"] = type(exc).__name__
        if outcome == "completed":
            log.info("chat_stream.task_ended", **fields)
        else:
            log.warning("chat_stream.task_ended", **fields)
        self._log_gauge()


_registry = RequestTaskRegistry()


def get_request_task_registry() -> RequestTaskRegistry:
    """Return the process-wide request-task registry."""
    return _registry


def current_request_task(trace_id: str) -> RequestTask:
    """Return the record of the running turn, or refuse a body the registry did not start.

    The turn body calls this first. The check holds only when the registry created the
    running task for this trace: the entry marker names the trace, a record exists, and the
    running task is that record's task. A bare ``create_task`` has no marker. A child task
    inherits the marker but is not the record's task.

    Args:
        trace_id: The trace id that the turn body received.

    Returns:
        The turn's record.

    Raises:
        RequestTaskEntryError: When the running task is not the registry's task for ``trace_id``.
    """
    record = _registry.get(trace_id)
    if (
        _entry_trace_id.get() != trace_id
        or record is None
        or record.task is not asyncio.current_task()
    ):
        raise RequestTaskEntryError(
            f"the chat turn body for trace {trace_id} must run on a task that "
            "RequestTaskRegistry.spawn created; a bare asyncio.create_task is refused"
        )
    return record
