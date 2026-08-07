"""Elasticsearch logging handler for structlog integration.

Delivery model (FRE-1055)
-------------------------
This handler sits on the **root logger of every process**, so what it does with
a record is what the whole system's ES-backed observability is worth. It used to
ship each record with a bare ``asyncio.create_task``, which lost events four
ways: off-loop emission was skipped silently, shutdown closed the client without
awaiting in-flight writes, task references were dropped (the interpreter may
collect a task mid-execution), and the circuit breaker counted unrelated errors.

It now enqueues onto a **bounded queue drained by a single consumer task bound
to one explicitly captured owner loop**:

- ``connect()`` captures the running loop as the owner and starts the consumer.
- ``emit()`` is callable from any thread or loop. On the owner loop it enqueues
  synchronously; elsewhere it hands off via ``call_soon_threadsafe``.
- ``drain()`` and ``disconnect()`` refuse to run on any other loop
  (:class:`ESHandlerLoopError`), because they touch the queue, the consumer and
  the client — all owner-loop state.
- Every drop is counted and the counters are exported (see :meth:`stats`).

Honest boundary on what this can claim
--------------------------------------
An in-memory queue cannot survive abrupt process death, and
``call_soon_threadsafe`` only *schedules* — an event never enters the queue if
the owner loop stops first. **This handler claims no loss on GRACEFUL shutdown
only.** Crash durability would need a durable spool, which is deliberately out
of scope. Records always reach the file/console sinks regardless; only the ES
copy is at stake here.
"""

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from personal_agent.exceptions import ESHandlerLoopError
from personal_agent.telemetry.es_logger import ElasticsearchLogger

DEFAULT_QUEUE_MAXSIZE = 10_000
"""Bound on undelivered records held in memory.

Sized for a burst, not a backlog: at roughly a kilobyte of event dict per
record this is single-digit megabytes, and a queue that stays full is a signal
to read in :meth:`stats`, not a buffer to enlarge.
"""

DEFAULT_DRAIN_TIMEOUT_S = 5.0
"""Ceiling on a graceful drain.

Deliberately far below the client's own ``request_timeout=30`` with two
retries: an unbounded drain against an unreachable Elasticsearch would hang
process shutdown for minutes. Exceeding it is counted, not hidden.
"""

OVERFLOW_POLICY = "drop-oldest"
"""What a full queue discards — stated explicitly because silence here is a bug.

**Drop-oldest**, not drop-newest. Under drop-newest a full queue stops accepting
anything new, so during an incident the ES-backed dashboards flatline and read
as healthy-but-idle — the silent-empty failure shape this telemetry work exists
to remove. Drop-oldest keeps fresh events flowing and discards the head of a
backlog that is, by construction, already the stalest thing in memory.
"""

_STATS_INTERVAL_S = 60.0
"""Minimum gap between exported counter snapshots."""

_STATS_EXPORT_TIMEOUT_S = 2.0
"""Ceiling on a single counter-snapshot write.

Bounded because :meth:`ElasticsearchHandler.drain` forces an export, and an
unbounded one against a hung Elasticsearch would push a drain past its deadline
— turning the diagnostic into the thing that delays shutdown.
"""

_SELF_DIAGNOSTIC_LOGGERS = (
    "personal_agent.telemetry.es_handler",
    "personal_agent.telemetry.es_logger",
)
"""Loggers whose records are never queued.

``es_logger`` reports its own indexing failures through structlog, which reaches
this handler, which queues another ES write, which fails the same way. Excluded
at the door so a broken Elasticsearch cannot drive its own log traffic into
queue overflow. The counter export deliberately does **not** use these loggers —
it bypasses the queue entirely (see :meth:`_export_stats`).
"""

_THIRD_PARTY_LOGGERS = ("elastic_transport", "elasticsearch", "neo4j", "httpx", "httpcore")
"""Noisy client libraries; the ES client's own logs would also feed the loop."""

_RESERVED_EVENT_KEYS = frozenset(
    {
        "event",
        "trace_id",
        "span_id",
        "component",
        "level",
        "logger",
        "message",
        "timestamp",
        "module",
        "function",
        "line_number",
    }
)

_RECORD_ATTRS_TO_SKIP = frozenset(
    {
        "name",
        "msg",
        "args",
        "created",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "thread",
        "threadName",
        "exc_info",
        "exc_text",
        "stack_info",
        "getMessage",
        "taskName",
        "stack",
    }
)


@dataclass(frozen=True)
class ESDeliveryStats:
    """Point-in-time snapshot of the handler's delivery counters.

    Every field is monotonic since process start except ``queue_depth``. The
    invariant worth reading is ``enqueued == delivered + write_failures +
    dropped_shutdown + queue_depth`` once a drain has settled; a gap means a
    record left through a path that forgot to count itself.

    Attributes:
        enqueued: Records accepted onto the queue.
        delivered: Records Elasticsearch acknowledged.
        write_failures: Writes that failed at the Elasticsearch call.
        dropped_queue_full: Records discarded by the overflow policy.
        dropped_circuit_open: Records discarded while the breaker was open.
        dropped_not_connected: Records discarded with no Elasticsearch client.
        dropped_shutdown: Records still queued when the consumer was cancelled.
        enqueue_errors: Hand-offs that never reached the queue (dead/closed
            owner loop, serialization failure). Never opens the breaker.
        drain_timeouts: Drains that hit their deadline with work outstanding.
        queue_depth: Records currently waiting, at snapshot time.
    """

    enqueued: int
    delivered: int
    write_failures: int
    dropped_queue_full: int
    dropped_circuit_open: int
    dropped_not_connected: int
    dropped_shutdown: int
    enqueue_errors: int
    drain_timeouts: int
    queue_depth: int


@dataclass(frozen=True)
class _QueuedRecord:
    """One record awaiting delivery, with its destination already resolved."""

    event_type: str
    data: dict[str, Any]
    trace_id: str | None
    span_id: str | None
    index: str


class ElasticsearchHandler(logging.Handler):
    """Logging handler that forwards logs to Elasticsearch.

    Forwards every structlog record on the root logger to the ``agent-logs-*``
    family via a bounded queue and a single owner-loop consumer. See the module
    docstring for the delivery model and the loss boundary it claims.
    """

    def __init__(
        self,
        es_url: str = "http://localhost:9200",
        index_prefix: str = "agent-logs",
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
    ):
        """Initialize Elasticsearch handler.

        Args:
            es_url: Elasticsearch URL.
            index_prefix: Index name prefix.
            queue_maxsize: Bound on undelivered records held in memory.
        """
        super().__init__()
        self.es_logger = ElasticsearchLogger(es_url, index_prefix)
        self._connected = False
        self._connect_attempted = False

        # Owner-loop state. All of it is None until connect() runs, and only the
        # owner loop may touch any of it thereafter.
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[_QueuedRecord] | None = None
        self._consumer: asyncio.Task[None] | None = None
        self._queue_maxsize = queue_maxsize

        # Cross-thread hand-off barrier: incremented before scheduling, decremented
        # once the record is on the queue, so drain() can wait for submissions that
        # have been scheduled but not yet run.
        self._submission_lock = threading.Lock()
        self._pending_submissions = 0

        # Circuit breaker — armed only by real Elasticsearch write outcomes.
        self._failure_count = 0
        self._circuit_open_until = 0.0
        self._circuit_breaker_threshold = 3
        self._circuit_breaker_cooldown_s = 30.0

        # Counters are mutated from the owner loop (delivery) and from arbitrary
        # threads (emit-side drops), so they take a lock. It is only ever held
        # for an integer increment, and never around an await.
        self._counters_lock = threading.Lock()
        self._counters: dict[str, int] = {
            "enqueued": 0,
            "delivered": 0,
            "write_failures": 0,
            "dropped_queue_full": 0,
            "dropped_circuit_open": 0,
            "dropped_not_connected": 0,
            "dropped_shutdown": 0,
            "enqueue_errors": 0,
            "drain_timeouts": 0,
        }
        self._last_stats_export = 0.0
        self._last_exported_counters: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Counters
    # ------------------------------------------------------------------

    def stats(self) -> ESDeliveryStats:
        """Return a snapshot of the delivery counters.

        Safe to call from any thread.

        Returns:
            Immutable snapshot including current queue depth.
        """
        depth = self._queue.qsize() if self._queue is not None else 0
        with self._counters_lock:
            counters = dict(self._counters)
        return ESDeliveryStats(queue_depth=depth, **counters)

    def _count(self, name: str) -> None:
        """Increment one delivery counter, from any thread."""
        with self._counters_lock:
            self._counters[name] += 1

    async def _export_stats(self, *, force: bool = False) -> None:
        """Write a counter snapshot to Elasticsearch, bypassing the queue.

        An in-process counter is not observability, so the counters have to
        leave the process. This writes them **directly** through
        ``es_logger.log_event`` rather than through :meth:`emit`, and that
        bypass is what makes the export loop-free: routing it through structlog
        would re-enter this handler (every handler sits on the root logger) and
        mutate the very counters it just exported. A failure of this write is
        reported by ``es_logger``'s own logger, which is excluded at the door.

        Args:
            force: Export regardless of interval and change detection (used on
                drain, so a shutdown always leaves a final reading).
        """
        if not self._connected or self.es_logger.client is None:
            return
        now = time.monotonic()
        with self._counters_lock:
            snapshot = dict(self._counters)
        changed = snapshot != self._last_exported_counters
        if not force and (not changed or now - self._last_stats_export < _STATS_INTERVAL_S):
            return
        self._last_stats_export = now
        self._last_exported_counters = snapshot
        try:
            await asyncio.wait_for(
                self.es_logger.log_event(
                    "es_delivery_counters",
                    {
                        "component": "es_handler",
                        "overflow_policy": OVERFLOW_POLICY,
                        "queue_maxsize": self._queue_maxsize,
                        "queue_depth": self._queue.qsize() if self._queue is not None else 0,
                        **snapshot,
                    },
                    None,
                    None,
                ),
                timeout=_STATS_EXPORT_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 - diagnostics must never break delivery
            # Deliberately silent and deliberately not counted as a write
            # failure: this is the observability of the pipeline, not traffic
            # through it, and it must not arm the breaker.
            pass

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------

    def _is_circuit_open(self) -> bool:
        """Return True when ES writes are temporarily paused."""
        return time.monotonic() < self._circuit_open_until

    def _record_failure(self) -> None:
        """Track a failed ES write and open circuit when threshold reached.

        Called **only** from the consumer, for the outcome of an actual
        Elasticsearch write. Enqueue-side problems have their own counters and
        never reach here: under the old handler one burst of foreign-loop errors
        opened the breaker and silently discarded thirty seconds of unrelated,
        perfectly deliverable events.
        """
        self._failure_count += 1
        if self._failure_count >= self._circuit_breaker_threshold:
            self._circuit_open_until = time.monotonic() + self._circuit_breaker_cooldown_s
            self._failure_count = 0
            logging.getLogger(__name__).warning(
                "elasticsearch_circuit_opened",
                extra={"cooldown_seconds": self._circuit_breaker_cooldown_s},
            )

    def _record_success(self) -> None:
        """Reset transient failure tracking after successful write."""
        self._failure_count = 0

    # ------------------------------------------------------------------
    # Emission
    # ------------------------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        """Emit a log record to Elasticsearch.

        Callable from any thread and any event loop — that is the point of the
        redesign. Never raises, never blocks, and never awaits.

        Args:
            record: Log record to emit.
        """
        # Excluded loggers are rejected before anything is counted: they are not
        # traffic this handler ever intended to carry, so counting them as drops
        # would make a healthy pipeline look lossy.
        if record.name.startswith(_SELF_DIAGNOSTIC_LOGGERS):
            return
        if record.name.startswith(_THIRD_PARTY_LOGGERS):
            return

        if not self._connected:
            self._count("dropped_not_connected")
            return
        if self._is_circuit_open():
            self._count("dropped_circuit_open")
            return

        loop = self._owner_loop
        if loop is None:
            self._count("enqueue_errors")
            return

        try:
            item = self._build_item(record)
        except Exception:  # noqa: BLE001 - a logging handler must not raise
            self._count("enqueue_errors")
            return

        # Same loop: enqueue synchronously. call_soon_threadsafe would only
        # *schedule* the enqueue, so `emit(); await drain()` could observe an
        # empty queue and let disconnect() close the client underneath a record
        # that had not been queued yet.
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is loop:
            self._enqueue(item)
            return

        with self._submission_lock:
            self._pending_submissions += 1
        try:
            loop.call_soon_threadsafe(self._enqueue, item)
        except RuntimeError:
            # Owner loop is closed or closing — nothing can be delivered.
            with self._submission_lock:
                self._pending_submissions -= 1
            self._count("enqueue_errors")

    def _build_item(self, record: logging.LogRecord) -> _QueuedRecord:
        """Flatten a log record into a queued item with its index resolved.

        The destination index is resolved **here**, at emission time, not when
        the consumer finally writes: a backlog draining across a month boundary
        would otherwise be misfiled into the following month's index.

        Args:
            record: Log record to flatten.

        Returns:
            Queued record ready for delivery.
        """
        event_dict: dict[str, Any] = {}
        if isinstance(record.msg, dict):
            event_dict = record.msg.copy()
        else:
            for key, value in record.__dict__.items():
                if not key.startswith("_") and key not in _RECORD_ATTRS_TO_SKIP:
                    event_dict[key] = value

        trace_id = event_dict.get("trace_id")
        span_id = event_dict.get("span_id")
        event_type = event_dict.get("event", record.levelname.lower())

        event_data: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "component": event_dict.get("component", "unknown"),
            "module": record.module,
            "function": record.funcName,
            "line_number": record.lineno,
            # The event name, not the stringified dict — the individual fields
            # are extracted separately below.
            "message": event_dict.get("event", record.levelname.lower()),
        }

        if record.exc_info:
            import traceback

            event_data["exception"] = "".join(traceback.format_exception(*record.exc_info))

        for key, value in event_dict.items():
            if key in _RESERVED_EVENT_KEYS:
                continue
            try:
                import json

                json.dumps(value)
                event_data[key] = value
            except (TypeError, ValueError):
                event_data[key] = str(value)

        return _QueuedRecord(
            event_type=event_type,
            data=event_data,
            trace_id=trace_id,
            span_id=span_id,
            index=self.es_logger.current_index_name(),
        )

    def _enqueue(self, item: _QueuedRecord) -> None:
        """Put one record on the queue, applying the overflow policy.

        Runs only on the owner loop — either called directly by ``emit()`` when
        it is already there, or as a ``call_soon_threadsafe`` callback. It never
        awaits, so the ``get_nowait``/``put_nowait`` pair below cannot interleave
        with the consumer.

        Args:
            item: Record to enqueue.
        """
        queue = self._queue
        try:
            if queue is None:
                self._count("enqueue_errors")
                return
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                # OVERFLOW_POLICY = drop-oldest.
                try:
                    queue.get_nowait()
                    queue.task_done()
                except asyncio.QueueEmpty:  # pragma: no cover - full then empty
                    pass
                self._count("dropped_queue_full")
                try:
                    queue.put_nowait(item)
                except asyncio.QueueFull:  # pragma: no cover - defensive
                    self._count("enqueue_errors")
                    return
            self._count("enqueued")
        finally:
            # Only cross-thread hand-offs took a slot on the barrier; the
            # synchronous path never incremented it.
            with self._submission_lock:
                if self._pending_submissions > 0:
                    self._pending_submissions -= 1

    # ------------------------------------------------------------------
    # Consumer
    # ------------------------------------------------------------------

    async def _consume(self) -> None:
        """Drain the queue serially until cancelled.

        Serial by design: a single consumer removes the need for the write
        semaphore the old handler used, and that semaphore was itself a
        cross-loop hazard (an ``asyncio.Semaphore`` binds to whichever loop
        first awaits it).
        """
        queue = self._queue
        if queue is None:  # pragma: no cover - defensive
            return
        while True:
            item = await queue.get()
            try:
                await self._deliver(item)
            finally:
                # In a finally so cancellation mid-write cannot desynchronise
                # join() accounting and hang a later drain.
                queue.task_done()
            await self._export_stats()

    async def _deliver(self, item: _QueuedRecord) -> None:
        """Write one queued record to Elasticsearch.

        Args:
            item: Record to write.
        """
        if not self._connected:
            self._count("dropped_not_connected")
            return
        if self._is_circuit_open():
            self._count("dropped_circuit_open")
            return
        # Pre-checked so an absent client is not miscounted as an ES write
        # failure: log_event returns None for both, and only one of them is a
        # reason to pause traffic.
        if self.es_logger.client is None:
            self._count("dropped_not_connected")
            return

        try:
            result = await self.es_logger.log_event(
                item.event_type,
                item.data,
                item.trace_id,
                item.span_id,
                index=item.index,
            )
        except Exception:  # noqa: BLE001 - delivery must not break logging
            self._count("write_failures")
            self._record_failure()
            return

        if result is None:
            self._count("write_failures")
            self._record_failure()
            return
        self._count("delivered")
        self._record_success()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _require_owner_loop(self, action: str) -> asyncio.AbstractEventLoop:
        """Assert the caller is on the owner loop.

        Args:
            action: Name of the lifecycle action, for the error message.

        Returns:
            The owner loop.

        Raises:
            ESHandlerLoopError: If no owner loop is set, or the running loop is
                a different one.
        """
        owner = self._owner_loop
        if owner is None:
            raise ESHandlerLoopError(f"{action} called before connect() captured an owner loop")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise ESHandlerLoopError(f"{action} called outside an event loop") from exc
        if running is not owner:
            raise ESHandlerLoopError(f"{action} called from a loop that does not own this handler")
        return owner

    async def connect(self) -> bool:
        """Connect to Elasticsearch and take ownership of the running loop.

        Calling this on an already-connected handler is a defined transition,
        not undefined behaviour: the previous consumer is drained and cancelled
        before ownership moves, so a reconnect onto a new loop cannot leave a
        task attached to the old one.

        Returns:
            True if connected successfully.
        """
        if self._consumer is not None:
            await self._stop_consumer()

        self._connect_attempted = True
        self._connected = await self.es_logger.connect()
        if not self._connected:
            return False

        self._owner_loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=self._queue_maxsize)
        self._consumer = asyncio.create_task(self._consume())
        return True

    async def drain(self, timeout: float = DEFAULT_DRAIN_TIMEOUT_S) -> bool:
        """Wait for queued records to reach Elasticsearch.

        Two waits, because there are two places a record can be in flight. The
        first covers hand-offs scheduled from other threads that the owner loop
        has not run yet; ``Queue.join()`` alone would not see them and would
        report success over a record that was never queued. The second covers
        the queue itself, including whatever the consumer is mid-write on.

        Residual, and inherent: a foreign thread can schedule one more record
        *after* the barrier reads zero. That is the same boundary the module
        docstring states for abrupt process death.

        Args:
            timeout: Seconds to wait for queued work. The forced counter export
                that follows carries its own smaller bound
                (``_STATS_EXPORT_TIMEOUT_S``), so worst-case wall time is the
                sum of the two rather than this value alone.

        Returns:
            True if everything drained, False on timeout (also counted in
            :meth:`stats`).

        Raises:
            ESHandlerLoopError: If called from a loop that does not own this
                handler.
        """
        self._require_owner_loop("drain")
        queue = self._queue
        if queue is None:  # pragma: no cover - defensive
            return True

        async def _wait() -> None:
            while True:
                with self._submission_lock:
                    pending = self._pending_submissions
                if pending == 0:
                    break
                await asyncio.sleep(0)
            await queue.join()

        try:
            await asyncio.wait_for(_wait(), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            self._count("drain_timeouts")
            await self._export_stats(force=True)
            return False
        await self._export_stats(force=True)
        return True

    async def _stop_consumer(self) -> None:
        """Cancel the consumer and account for whatever is left queued.

        Every abandoned record is counted and ``task_done()``-ed, so the queue's
        accounting stays exact even after a timed-out drain — a later
        ``join()`` on a fresh queue can never inherit a phantom debt.
        """
        consumer = self._consumer
        self._consumer = None
        if consumer is not None:
            consumer.cancel()
            try:
                await consumer
            except asyncio.CancelledError:
                pass

        queue = self._queue
        if queue is not None:
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                queue.task_done()
                self._count("dropped_shutdown")

    async def disconnect(self) -> None:
        """Drain in-flight writes, stop the consumer, then close the client.

        Ordering is the whole point: the old teardown closed the client without
        awaiting anything, so a graceful shutdown truncated whatever was in
        flight. A drain that times out does not block shutdown — it is counted
        and the remainder is accounted as ``dropped_shutdown``.

        Raises:
            ESHandlerLoopError: If called from a loop that does not own this
                handler.
        """
        if self._owner_loop is not None:
            self._require_owner_loop("disconnect")
            await self.drain()
            await self._stop_consumer()
        self._owner_loop = None
        self._queue = None
        await self.es_logger.disconnect()
        self._connected = False

    def close(self) -> None:
        """Close handler (sync version for logging.Handler interface).

        Also detaches from the root logger. ``add_elasticsearch_handler`` only
        ever adds, so without this a repeated in-process lifespan accumulates
        dead handlers bound to closed loops, each charging an ``enqueue_errors``
        increment against every log record forever.

        Cannot await, so it does not drain — call :meth:`disconnect` first for
        that.
        """
        self._connected = False
        logging.getLogger().removeHandler(self)
        super().close()
