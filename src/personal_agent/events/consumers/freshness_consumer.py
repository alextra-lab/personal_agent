"""Batch consumer for memory.accessed events (FRE-164 / ADR-0042 Step 4).

Reads ``MemoryAccessedEvent`` instances from ``stream:memory.accessed``,
accumulates them over a configurable batch window, deduplicates entity
accesses within the window, and executes a single Cypher UNWIND transaction
per flush to update Neo4j access metadata.

Design notes:
- Events are ACKed on buffer insertion (best-effort; matches ADR-0042 §Decision 3).
- Neo4j writes are async to the consumer runner loop — failures are logged but
  do not block new events.
- ``access_count`` is an increment, not an absolute set; replay double-counts
  (acceptable per ADR-0042 idempotency note).
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime
from typing import Any

from personal_agent.events.models import EventBase, MemoryAccessedEvent
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

_ENTITY_UPDATE_CYPHER = """
UNWIND $updates AS update
MATCH (e:Entity {name: update.entity_id})
SET e.last_accessed_at    = datetime(update.last_accessed_at),
    e.access_count        = COALESCE(e.access_count, 0) + update.access_increment,
    e.last_access_context = update.access_context,
    e.first_accessed_at   = COALESCE(e.first_accessed_at, datetime(update.last_accessed_at))
RETURN count(e) AS updated
"""


class FreshnessConsumer:
    """Batched Neo4j writer for memory access metadata.

    Usage::

        consumer = FreshnessConsumer(batch_window_seconds=5.0, batch_max_events=50)
        await consumer.start()
        # register consumer.handle with the event bus
        # ... service runs ...
        await consumer.stop()  # drains remaining buffer

    Args:
        driver: Optional Neo4j async driver. When ``None``, resolved lazily
            from the global ``memory_service`` at first flush.
        batch_window_seconds: Maximum seconds between automatic flushes.
        batch_max_events: Maximum buffered events before an early flush.
    """

    def __init__(
        self,
        driver: Any | None = None,
        batch_window_seconds: float = 5.0,
        batch_max_events: int = 50,
    ) -> None:
        """Initialise consumer with optional driver and batch configuration."""
        self._driver = driver
        self._batch_window_seconds = batch_window_seconds
        self._batch_max_events = batch_max_events
        self._buffer: list[MemoryAccessedEvent] = []
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        """Start the background flush loop.

        Must be called before the consumer receives events.
        """
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop(), name="freshness-consumer-flush")
        log.info(
            "freshness_consumer_started",
            batch_window_seconds=self._batch_window_seconds,
            batch_max_events=self._batch_max_events,
        )

    async def stop(self) -> None:
        """Stop the flush loop and drain any buffered events.

        Safe to call even if ``start()`` was never called.
        """
        self._running = False
        if self._flush_task is not None:
            self._flush_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None
        await self._flush()  # drain remaining buffer
        log.info("freshness_consumer_stopped")

    async def handle(self, event: EventBase) -> None:
        """Accept a single event from the consumer runner.

        Appends ``MemoryAccessedEvent`` to the internal buffer.  Events are
        considered handled (ACKed by the runner) once this method returns.

        Args:
            event: Incoming event from the bus. Non-``MemoryAccessedEvent``
                types are silently ignored.
        """
        if not isinstance(event, MemoryAccessedEvent):
            return

        async with self._lock:
            self._buffer.append(event)
            should_flush_now = len(self._buffer) >= self._batch_max_events

        if should_flush_now:
            log.debug(
                "freshness_early_flush_triggered",
                buffer_size=self._batch_max_events,
            )
            await self._flush()

    # -- Internal ---------------------------------------------------------

    async def _flush_loop(self) -> None:
        """Timer-driven flush loop — wakes every ``batch_window_seconds``."""
        while self._running:
            try:
                await asyncio.sleep(self._batch_window_seconds)
                await self._flush()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning(
                    "freshness_flush_loop_error",
                    error=str(exc),
                    exc_info=True,
                )

    async def _flush(self) -> None:
        """Drain the buffer and persist to Neo4j.

        Swaps the buffer under a lock so new events can be accepted while the
        Cypher transaction is in-flight.
        """
        async with self._lock:
            if not self._buffer:
                return
            batch = self._buffer[:]
            self._buffer.clear()

        try:
            await self._write_batch(batch)
        except Exception as exc:
            log.error(
                "freshness_batch_write_failed",
                batch_size=len(batch),
                error=str(exc),
                exc_info=True,
            )

    async def _write_batch(self, events: list[MemoryAccessedEvent]) -> None:
        """Deduplicate and write entity access updates in a single Cypher transaction.

        Collapse rules within a batch:
        - ``access_increment`` accumulates the total access count for the entity.
        - ``last_accessed_at`` is the maximum ``created_at`` across events touching
          the entity.
        - ``access_context`` is taken from the event with the latest ``created_at``.

        Args:
            events: Non-empty list of events to process.

        Raises:
            Exception: Propagated from Neo4j on write failure (caller logs it).
        """
        entity_updates: dict[str, dict[str, Any]] = {}
        for event in events:
            for entity_id in event.entity_ids:
                if not entity_id:
                    continue
                if entity_id not in entity_updates:
                    entity_updates[entity_id] = {
                        "entity_id": entity_id,
                        "last_accessed_at": event.created_at,
                        "access_increment": 0,
                        "access_context": event.access_context.value,
                    }
                rec = entity_updates[entity_id]
                rec["access_increment"] += 1
                if event.created_at > rec["last_accessed_at"]:
                    rec["last_accessed_at"] = event.created_at
                    rec["access_context"] = event.access_context.value

        if not entity_updates:
            return

        driver = self._resolve_driver()
        if driver is None:
            log.warning(
                "freshness_consumer_no_driver",
                entity_count=len(entity_updates),
            )
            return

        updates = [
            {
                **rec,
                "last_accessed_at": rec["last_accessed_at"].isoformat()
                if isinstance(rec["last_accessed_at"], datetime)
                else str(rec["last_accessed_at"]),
            }
            for rec in entity_updates.values()
        ]

        async with driver.session() as db_session:
            result = await db_session.run(_ENTITY_UPDATE_CYPHER, updates=updates)
            record = await result.single()
            updated_count: int = record["updated"] if record else 0
            log.info(
                "freshness_batch_written",
                events_in_batch=len(events),
                entities_deduplicated=len(updates),
                entities_updated_in_neo4j=updated_count,
            )

    def _resolve_driver(self) -> Any | None:
        """Return a Neo4j driver, falling back to the global memory_service.

        Returns:
            A connected Neo4j async driver or ``None`` if unavailable.
        """
        if self._driver is not None:
            return self._driver
        try:
            from personal_agent.service.app import memory_service as _ms

            if _ms is not None and _ms.driver is not None:
                return _ms.driver
        except (ImportError, AttributeError):
            pass
        return None
