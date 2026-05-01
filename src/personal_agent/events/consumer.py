"""Consumer runner for Redis Streams event bus (ADR-0041).

Manages one ``asyncio.Task`` per subscription.  Each task runs an
``XREADGROUP`` loop that dispatches events to the registered handler,
acknowledges on success, and routes to the dead-letter stream after
``max_retries`` failed attempts.

BudgetDenied (ADR-0065 / FRE-306) is handled specially: it's not a poison
pill, just transient cost pressure. The runner ACKs the message (so it
doesn't accumulate in the dead-letter queue) and emits a structured
``consumer_budget_denied`` log event. Recovery happens via the next
scheduled consolidation tick — background work is idempotent against
re-attempt and the next pass naturally re-picks the trace once the
budget window rolls.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import orjson

from personal_agent.config.settings import get_settings
from personal_agent.cost_gate import BudgetDenied
from personal_agent.events.models import (
    CG_SESSION_WRITER,
    RequestCompletedEvent,
    parse_stream_event,
)
from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from personal_agent.events.redis_backend import RedisStreamBus, Subscription

log = get_logger(__name__)


class ConsumerRunner:
    """Drives XREADGROUP loops for all registered subscriptions.

    Usage::

        runner = ConsumerRunner(bus)
        await runner.start()
        # ... service runs ...
        await runner.stop()
    """

    def __init__(self, bus: RedisStreamBus) -> None:
        """Initialize the consumer runner.

        Args:
            bus: RedisStreamBus with registered subscriptions.
        """
        self._bus = bus
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False

    async def start(self) -> None:
        """Start a read loop for each registered subscription."""
        self._running = True
        for sub in self._bus.subscriptions:
            task = asyncio.create_task(
                self._read_loop(sub),
                name=f"consumer:{sub.group}:{sub.stream}",
            )
            self._tasks.append(task)
            log.info(
                "consumer_loop_started",
                stream=sub.stream,
                group=sub.group,
                consumer=sub.consumer_name,
            )

    async def stop(self) -> None:
        """Cancel all read loops and wait for them to finish."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        log.info("consumer_runner_stopped")

    # -- Internal ---------------------------------------------------------

    async def _read_loop(self, sub: Subscription) -> None:
        """XREADGROUP loop for a single subscription.

        Args:
            sub: Subscription metadata (stream, group, consumer, handler).
        """
        settings = get_settings()
        block_ms = settings.event_bus_consumer_poll_interval_ms
        max_retries = settings.event_bus_max_retries

        while self._running:
            try:
                # Read new messages (> = undelivered only)
                results = await self._bus.client.xreadgroup(
                    groupname=sub.group,
                    consumername=sub.consumer_name,
                    streams={sub.stream: ">"},
                    count=10,
                    block=block_ms,
                )
                if not results:
                    continue

                for _stream_name, messages in results:
                    for message_id, fields in messages:
                        await self._process_message(sub, message_id, fields, max_retries)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(
                    "consumer_read_loop_error",
                    stream=sub.stream,
                    group=sub.group,
                    error=str(exc),
                    exc_info=True,
                )
                # Back off before retrying the loop itself
                await asyncio.sleep(1.0)

    async def _process_message(
        self,
        sub: Subscription,
        message_id: str,
        fields: dict[str, str],
        max_retries: int,
    ) -> None:
        """Deserialize, dispatch to handler, ACK or dead-letter.

        Args:
            sub: Subscription metadata.
            message_id: Redis stream message ID.
            fields: Raw field-value dict from XREADGROUP.
            max_retries: Maximum delivery attempts.
        """
        raw = fields.get("data")
        if raw is None:
            log.warning(
                "consumer_message_missing_data",
                stream=sub.stream,
                message_id=message_id,
            )
            await self._bus.ack(sub.stream, sub.group, message_id)
            return

        try:
            payload = orjson.loads(raw)
            event = parse_stream_event(payload)
        except Exception as exc:
            log.error(
                "consumer_deserialize_error",
                stream=sub.stream,
                message_id=message_id,
                error=str(exc),
            )
            # ACK to avoid infinite redelivery of unparseable messages
            await self._bus.ack(sub.stream, sub.group, message_id)
            return

        for attempt in range(1, max_retries + 1):
            try:
                await sub.handler(event)
                await self._bus.ack(sub.stream, sub.group, message_id)
                log.debug(
                    "event_processed",
                    stream=sub.stream,
                    group=sub.group,
                    event_type=event.event_type,
                    event_id=event.event_id,
                    message_id=message_id,
                )
                return
            except BudgetDenied as exc:
                # ADR-0065 D5: budget pressure is not a poison pill. ACK to
                # avoid dead-letter accumulation; the next scheduled
                # consolidation pass will re-pick the trace once the window
                # rolls. The structured log feeds the FRE-307 retry-health
                # telemetry surface.
                log.warning(
                    "consumer_budget_denied",
                    stream=sub.stream,
                    group=sub.group,
                    event_type=event.event_type,
                    event_id=event.event_id,
                    message_id=message_id,
                    role=exc.role,
                    time_window=exc.time_window,
                    denial_reason=exc.denial_reason,
                    cap=str(exc.cap),
                    spend=str(exc.current_spend),
                )
                await self._bus.ack(sub.stream, sub.group, message_id)
                return
            except Exception as exc:
                log.warning(
                    "consumer_handler_error",
                    stream=sub.stream,
                    group=sub.group,
                    event_type=event.event_type,
                    event_id=event.event_id,
                    message_id=message_id,
                    attempt=attempt,
                    max_retries=max_retries,
                    error=str(exc),
                )
                if attempt >= max_retries:
                    await self._bus.dead_letter(
                        event=event,
                        source_stream=sub.stream,
                        group=sub.group,
                        error=str(exc),
                        attempts=max_retries,
                    )
                    if isinstance(event, RequestCompletedEvent) and sub.group == CG_SESSION_WRITER:
                        from personal_agent.events.session_write_waiter import (
                            release_session_write_wait,
                        )

                        release_session_write_wait(event.session_id)
                    await self._bus.ack(sub.stream, sub.group, message_id)
                    return
                await asyncio.sleep(0.05 * attempt)
