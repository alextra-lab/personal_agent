"""Tests for Elasticsearch handler delivery, lifecycle and circuit breaker.

FRE-1055 replaced fire-and-forget ``create_task`` shipping with a bounded queue
drained by a single owner-loop consumer. The tests below are organised around
the four defects that motivated it: off-loop emission, shutdown truncation,
dropped task references, and an over-broad circuit breaker.
"""

import asyncio
import logging
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from personal_agent.exceptions import ESHandlerLoopError
from personal_agent.telemetry.es_handler import OVERFLOW_POLICY, ElasticsearchHandler


def _record(event: str = "unit_test_event", **fields: Any) -> logging.LogRecord:
    """Build a structlog-shaped LogRecord.

    Args:
        event: Structlog event name.
        **fields: Extra event-dict fields.

    Returns:
        A LogRecord whose ``msg`` is a structlog event dict.
    """
    return logging.LogRecord(
        name="personal_agent.tests.es_handler",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg={"event": event, **fields},
        args=(),
        exc_info=None,
    )


def _record_writes(handler: ElasticsearchHandler) -> list[Any]:
    """Return only the log-record writes, excluding counter-snapshot exports.

    The handler exports its own delivery counters through the same
    ``es_logger.log_event`` seam (deliberately, so the export bypasses the
    queue), so a raw call count conflates traffic with telemetry about traffic.

    Args:
        handler: Handler whose mocked ``log_event`` to inspect.

    Returns:
        Matching mock call objects, in call order.
    """
    return [
        call
        for call in handler.es_logger.log_event.call_args_list
        if not (call.args and call.args[0] == "es_delivery_counters")
    ]


async def _connected_handler(
    *,
    queue_maxsize: int = 100,
    log_event: AsyncMock | None = None,
) -> ElasticsearchHandler:
    """Build a handler connected to a mocked Elasticsearch, owning this loop.

    Args:
        queue_maxsize: Bound to give the delivery queue.
        log_event: Optional pre-built ``log_event`` mock.

    Returns:
        A connected handler with its consumer running on the current loop.
    """
    handler = ElasticsearchHandler(queue_maxsize=queue_maxsize)
    es = cast(Any, handler.es_logger)
    es.connect = AsyncMock(return_value=True)
    es.disconnect = AsyncMock(return_value=None)
    es.client = object()
    es.log_event = log_event or AsyncMock(return_value="doc-1")
    assert await handler.connect() is True
    return handler


# ---------------------------------------------------------------------------
# AC1 — off-loop emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_from_worker_thread_reaches_es() -> None:
    """AC1: a record emitted under asyncio.to_thread is delivered.

    The defect this replaces: ``emit`` only shipped when the calling thread had
    a running loop, so everything under ``asyncio.to_thread`` was skipped with
    no trace. Four defensive comments in ``captains_log/`` documented it and one
    event was hopped back to the main loop by hand; the general case was unfixed.
    """
    handler = await _connected_handler()

    await asyncio.to_thread(handler.emit, _record("missing_skill_requested"))
    assert await handler.drain(timeout=2.0) is True

    assert len(_record_writes(handler)) == 1
    assert handler.stats().delivered == 1
    assert handler.stats().enqueue_errors == 0


@pytest.mark.asyncio
async def test_drain_waits_for_a_cross_thread_handoff_not_yet_queued() -> None:
    """drain() covers submissions scheduled from a thread but not yet run.

    ``Queue.join()`` alone sees only queued work, so without the submission
    barrier a drain could report success over a record still sitting in the
    loop's callback queue — and disconnect() would then close the client under it.
    """
    handler = await _connected_handler()

    barrier = asyncio.Event()

    def _emit_then_signal() -> None:
        handler.emit(_record("cross_thread_event"))
        handler._owner_loop.call_soon_threadsafe(barrier.set)  # type: ignore[union-attr]

    thread = asyncio.get_running_loop().run_in_executor(None, _emit_then_signal)
    await barrier.wait()
    await thread

    assert await handler.drain(timeout=2.0) is True
    assert handler.stats().delivered == 1


# ---------------------------------------------------------------------------
# AC2 — concurrency burst
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_burst_of_emits_all_arrive() -> None:
    """AC2: every event in a concurrent burst arrives, none dropped."""
    burst = 200
    handler = await _connected_handler(queue_maxsize=burst * 2)

    async def _emit_batch(start: int) -> None:
        for i in range(start, start + 50):
            await asyncio.to_thread(handler.emit, _record(f"burst_event_{i}"))

    await asyncio.gather(*(_emit_batch(s) for s in range(0, burst, 50)))
    assert await handler.drain(timeout=5.0) is True

    stats = handler.stats()
    assert stats.enqueued == burst
    assert stats.delivered == burst
    assert stats.dropped_queue_full == 0
    assert stats.enqueue_errors == 0
    assert stats.write_failures == 0


# ---------------------------------------------------------------------------
# AC3 — graceful shutdown drains
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_drains_events_in_flight() -> None:
    """AC3: a graceful shutdown delivers what was still queued.

    The defect this replaces: ``disconnect`` closed the client without awaiting
    in-flight writes, so a clean shutdown truncated whatever was mid-flight.
    """
    started = asyncio.Event()

    async def _slow_write(*_args: Any, **_kwargs: Any) -> str:
        started.set()
        await asyncio.sleep(0.01)
        return "doc-slow"

    handler = await _connected_handler(log_event=AsyncMock(side_effect=_slow_write))

    for i in range(20):
        handler.emit(_record(f"in_flight_{i}"))
    await started.wait()
    assert handler.stats().delivered < 20, "precondition: writes still outstanding"

    await handler.disconnect()

    stats = handler.stats()
    assert stats.delivered == 20
    assert stats.dropped_shutdown == 0
    assert handler.es_logger.disconnect.await_count == 1


@pytest.mark.asyncio
async def test_drain_timeout_is_counted_and_leaves_accounting_exact() -> None:
    """A drain that times out reports False and does not wedge a later drain."""
    release = asyncio.Event()

    async def _blocked_write(*_args: Any, **_kwargs: Any) -> str:
        await release.wait()
        return "doc-late"

    handler = await _connected_handler(log_event=AsyncMock(side_effect=_blocked_write))

    for i in range(3):
        handler.emit(_record(f"blocked_{i}"))

    assert await handler.drain(timeout=0.05) is False
    assert handler.stats().drain_timeouts == 1

    # Shutdown must still terminate, and must account for what it abandons.
    release.set()
    await handler.disconnect()
    stats = handler.stats()
    assert stats.enqueued == stats.delivered + stats.dropped_shutdown + stats.write_failures


# ---------------------------------------------------------------------------
# AC4 — the circuit breaker only answers to real ES write outcomes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_failure_does_not_open_circuit_breaker() -> None:
    """AC4: hand-off failures are counted but never arm the breaker.

    The defect this replaces: the breaker counted *any* failure including a
    foreign-loop error, so one burst of unrelated errors silently discarded
    every queued event for thirty seconds.
    """
    handler = await _connected_handler()
    handler._circuit_breaker_threshold = 2

    # A closed/dead owner loop is the foreign-loop case: call_soon_threadsafe
    # raises, which must be an enqueue error and nothing more.
    class _DeadLoop:
        def call_soon_threadsafe(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("Event loop is closed")

    real_loop = handler._owner_loop
    handler._owner_loop = cast(Any, _DeadLoop())
    for i in range(5):
        await asyncio.to_thread(handler.emit, _record(f"foreign_{i}"))
    handler._owner_loop = real_loop

    stats = handler.stats()
    assert stats.enqueue_errors == 5
    assert stats.write_failures == 0
    assert handler._failure_count == 0
    assert handler._is_circuit_open() is False


@pytest.mark.asyncio
async def test_a_real_write_failure_still_opens_the_circuit_breaker() -> None:
    """Narrowing the breaker must not disarm it for genuine ES failures."""
    handler = await _connected_handler(log_event=AsyncMock(return_value=None))
    handler._circuit_breaker_threshold = 2
    handler._circuit_breaker_cooldown_s = 0.05

    handler.emit(_record("fails_one"))
    handler.emit(_record("fails_two"))
    await handler.drain(timeout=2.0)

    assert handler.stats().write_failures == 2
    assert handler._is_circuit_open() is True

    await asyncio.sleep(0.06)
    assert handler._is_circuit_open() is False

    cast(Any, handler.es_logger).log_event = AsyncMock(return_value="doc-ok")
    handler.emit(_record("succeeds"))
    await handler.drain(timeout=2.0)
    assert handler._failure_count == 0


@pytest.mark.asyncio
async def test_absent_client_is_not_counted_as_a_write_failure() -> None:
    """No client is a connectivity state, not a reason to pause traffic."""
    handler = await _connected_handler()
    cast(Any, handler.es_logger).client = None

    handler.emit(_record("no_client"))
    await handler.drain(timeout=2.0)

    stats = handler.stats()
    assert stats.dropped_not_connected == 1
    assert stats.write_failures == 0
    assert handler._failure_count == 0


# ---------------------------------------------------------------------------
# AC5 — overflow is counted and exported
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_overflow_increments_exported_counter() -> None:
    """AC5: overflow increments a counter exposed through stats()."""
    release = asyncio.Event()

    async def _blocked_write(*_args: Any, **_kwargs: Any) -> str:
        await release.wait()
        return "doc-late"

    handler = await _connected_handler(
        queue_maxsize=2, log_event=AsyncMock(side_effect=_blocked_write)
    )

    # First emit is taken by the consumer immediately; the next two fill the
    # queue, and everything after that overflows.
    for i in range(8):
        handler.emit(_record(f"overflow_{i}"))
        await asyncio.sleep(0)

    stats = handler.stats()
    assert stats.dropped_queue_full > 0
    assert stats.enqueued == 8

    release.set()
    await handler.disconnect()


@pytest.mark.asyncio
async def test_overflow_policy_is_drop_oldest() -> None:
    """The declared policy is the implemented one: the newest record survives."""
    assert OVERFLOW_POLICY == "drop-oldest"

    release = asyncio.Event()
    delivered: list[str] = []

    async def _blocked_write(event_type: str, *_args: Any, **_kwargs: Any) -> str:
        await release.wait()
        delivered.append(event_type)
        return "doc-late"

    handler = await _connected_handler(
        queue_maxsize=1, log_event=AsyncMock(side_effect=_blocked_write)
    )

    handler.emit(_record("taken_by_consumer"))
    await asyncio.sleep(0)
    handler.emit(_record("will_be_dropped"))
    handler.emit(_record("newest_survives"))

    assert handler.stats().dropped_queue_full == 1

    release.set()
    assert await handler.drain(timeout=2.0) is True
    assert "newest_survives" in delivered
    assert "will_be_dropped" not in delivered


# ---------------------------------------------------------------------------
# Owner-loop ownership
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_from_a_foreign_loop_is_refused() -> None:
    """drain() touches owner-loop state, so a foreign loop must be rejected."""
    handler = await _connected_handler()
    errors: list[BaseException] = []

    def _drain_on_another_loop() -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(handler.drain(timeout=0.1))
        except BaseException as exc:  # noqa: BLE001 - captured for assertion
            errors.append(exc)
        finally:
            loop.close()

    await asyncio.to_thread(_drain_on_another_loop)

    assert len(errors) == 1
    assert isinstance(errors[0], ESHandlerLoopError)


@pytest.mark.asyncio
async def test_drain_before_connect_is_refused() -> None:
    """No owner loop means no defined drain — say so rather than pretend."""
    handler = ElasticsearchHandler()
    with pytest.raises(ESHandlerLoopError):
        await handler.drain(timeout=0.1)


@pytest.mark.asyncio
async def test_emit_before_connect_is_counted_not_crashed() -> None:
    """A record emitted before connect() is a counted drop, never an exception."""
    handler = ElasticsearchHandler()
    handler.emit(_record("too_early"))
    assert handler.stats().dropped_not_connected == 1
    assert handler.stats().enqueued == 0


@pytest.mark.asyncio
async def test_reconnect_moves_ownership_to_the_new_loop() -> None:
    """connect() on a live handler is a defined transition, not a leak."""
    handler = await _connected_handler()
    first_consumer = handler._consumer

    handler.emit(_record("before_reconnect"))
    assert await handler.drain(timeout=2.0) is True

    assert await handler.connect() is True
    assert handler._consumer is not first_consumer
    assert first_consumer is not None and first_consumer.cancelled()

    handler.emit(_record("after_reconnect"))
    assert await handler.drain(timeout=2.0) is True
    assert handler.stats().delivered == 2


@pytest.mark.asyncio
async def test_close_detaches_the_handler_from_the_root_logger() -> None:
    """close() removes itself, so repeated lifespans cannot accumulate handlers."""
    handler = ElasticsearchHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    assert handler in root.handlers

    handler.close()

    assert handler not in root.handlers


# ---------------------------------------------------------------------------
# Index capture and self-diagnostic exclusion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_destination_index_is_captured_at_emission_time() -> None:
    """A backlog draining across a month boundary is not misfiled.

    The index is resolved when the record is emitted, not when it is written,
    so a queue that outlives the month still writes into the month the events
    belong to.
    """
    handler = await _connected_handler()
    emit_time_index = handler.es_logger.current_index_name()

    handler.emit(_record("crosses_the_boundary"))
    # The consumer resolves nothing itself; simulate the clock moving on.
    cast(Any, handler.es_logger).current_index_name = lambda: "agent-logs-2099-12"
    assert await handler.drain(timeout=2.0) is True

    _, kwargs = _record_writes(handler)[0]
    assert kwargs["index"] == emit_time_index


@pytest.mark.asyncio
async def test_handler_own_diagnostics_are_never_queued() -> None:
    """The ES logger's own failure logs must not feed the pipeline that failed."""
    handler = await _connected_handler()

    for name in ("personal_agent.telemetry.es_logger", "personal_agent.telemetry.es_handler"):
        record = _record("elasticsearch_log_failed")
        record.name = name
        handler.emit(record)

    assert await handler.drain(timeout=2.0) is True
    stats = handler.stats()
    assert stats.enqueued == 0
    assert stats.delivered == 0
    # Excluded traffic is not a drop: it was never ours to carry.
    assert stats.dropped_not_connected == 0
    assert stats.dropped_queue_full == 0


# ---------------------------------------------------------------------------
# Counter export leaves the process
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_counters_are_exported_to_elasticsearch_on_drain() -> None:
    """An in-process counter is not observability — the snapshot must be written.

    Written directly through ``es_logger``, bypassing ``emit()``: routing it
    through structlog would re-enter this handler and mutate the counters it
    just exported.
    """
    handler = await _connected_handler()
    handler.emit(_record("some_traffic"))
    assert await handler.drain(timeout=2.0) is True

    exported = [
        call
        for call in handler.es_logger.log_event.call_args_list
        if call.args and call.args[0] == "es_delivery_counters"
    ]
    assert len(exported) >= 1
    payload = exported[-1].args[1]
    assert payload["enqueued"] == 1
    assert payload["delivered"] == 1
    assert payload["overflow_policy"] == OVERFLOW_POLICY


# ---------------------------------------------------------------------------
# Payload shape (pre-existing coverage, preserved)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_forwards_session_id_to_es_logger() -> None:
    """FRE-552: session_id on a structlog record reaches es_logger.log_event payload.

    Closes the producer->ES pass-through gap that ``capture_logs`` cannot see:
    ``capture_logs`` intercepts before the stdlib bridge, so it does not cover
    the dict-pass-through in ``ElasticsearchHandler.emit``.
    """
    handler = await _connected_handler()

    record = _record("perplexity_query_timeout", trace_id="trace-1", session_id="sess-552")
    record.name = "personal_agent.tools.perplexity"
    handler.emit(record)
    assert await handler.drain(timeout=2.0) is True

    args, _ = _record_writes(handler)[0]
    event_type, data, trace_id = args[0], args[1], args[2]
    assert event_type == "perplexity_query_timeout"
    assert trace_id == "trace-1"
    assert data["session_id"] == "sess-552"


@pytest.mark.asyncio
async def test_emit_never_lets_a_payload_field_overwrite_event_type() -> None:
    """FRE-1066: reproduces the redis_backend.py::publish() log shape end to end.

    Runs it through the real emit()/log_event() pipeline (see that method's
    comment for why payload_event_type, not event_type, is the fix).
    """
    handler = await _connected_handler()

    record = _record(
        "event_published",
        stream="stream:metrics.sampled",
        payload_event_type="metrics.sampled",
        trace_id=None,
    )
    record.name = "personal_agent.events.redis_backend"
    handler.emit(record)
    assert await handler.drain(timeout=2.0) is True

    args, _ = _record_writes(handler)[0]
    event_type, data = args[0], args[1]
    # The canonical, message-derived event_type — never the stream-equal value.
    assert event_type == "event_published"
    # The domain event's own type survives, distinctly named.
    assert data["payload_event_type"] == "metrics.sampled"
    assert data["stream"] == "stream:metrics.sampled"
