# FRE-157: Event Bus Phase 1 — Implementation Plan

**Status**: Ready for implementation
**ADR**: ADR-0041 (Approved)
**Scope**: Redis Docker service + EventBus protocol + RedisStreamBus + event models + consumer runner + first migration (`request.captured` -> consolidator) + feature flag

---

## 1. Implementation Order (Dependency Graph)

```
Step 1: docker-compose.yml (Redis service)           — no code deps
Step 2: pyproject.toml (redis dependency)             — no code deps
Step 3: config/settings.py (event_bus_* fields)       — no code deps
Step 4: events/models.py (frozen Pydantic events)     — depends on Step 3
Step 5: events/bus.py (EventBus protocol + NoOpBus)   — depends on Step 4
Step 6: events/redis_backend.py (RedisStreamBus)      — depends on Step 5
Step 7: events/consumer.py (ConsumerRunner)            — depends on Step 5, 6
Step 8: events/__init__.py (public API)                — depends on Steps 4-7
Step 9: telemetry/events.py (event bus constants)      — no code deps
Step 10: brainstem/scheduler.py (on_request_captured)  — depends on Step 4
Step 11: orchestrator/executor.py (publish event)      — depends on Step 5, 8
Step 12: service/app.py (lifecycle wiring)             — depends on Steps 6-8, 10
Step 13: .env.example (documentation)                  — no code deps
Step 14: Tests                                         — depends on all above
```

Steps 1-3 and 9 and 13 are independent and can be done in parallel. Steps 4-8 are sequential. Steps 10-12 are the wiring layer. Step 14 is tests.

---

## 2. Detailed File-by-File Implementation

### Step 1: `docker-compose.yml` — Add Redis service

**Location**: `/Users/Alex/Dev/personal_agent/docker-compose.yml`

Add a `redis` service before the `volumes:` section (after the `searxng` service, around line 105). Add `redis_data` to the volumes section.

```yaml
  # Redis 7 for event bus (ADR-0041)
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped
```

Add to `volumes:` section:
```yaml
  redis_data:
```

**Pattern match**: Follows the same structure as postgres, elasticsearch, neo4j services (healthcheck, volumes, ports).

---

### Step 2: `pyproject.toml` — Add redis dependency

**Location**: `/Users/Alex/Dev/personal_agent/pyproject.toml`

Add to `dependencies` list (around line 54, after the `litellm` entry):

```toml
  "redis[hiredis]>=5.0.0",  # Redis Streams event bus (ADR-0041)
```

The `hiredis` extra provides the C parser for ~10x faster protocol parsing.

---

### Step 3: `src/personal_agent/config/settings.py` — Add event bus config fields

**Location**: `/Users/Alex/Dev/personal_agent/src/personal_agent/config/settings.py`

Add six flat fields to `AppConfig` class. Place them in the "Feature flags" section (after `enable_memory_graph` around line 404), grouped with a comment block.

```python
    # Event Bus (ADR-0041)
    event_bus_enabled: bool = Field(
        default=False,
        description="Enable Redis Streams event bus for async inter-component communication",
    )
    event_bus_redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis URL for event bus streams",
    )
    event_bus_consumer_poll_interval_ms: int = Field(
        default=100,
        ge=10,
        le=5000,
        description="Consumer XREADGROUP poll interval in milliseconds",
    )
    event_bus_max_retries: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Max delivery attempts before routing to dead-letter stream",
    )
    event_bus_dead_letter_stream: str = Field(
        default="stream:dead_letter",
        description="Stream name for dead-letter events",
    )
    event_bus_ack_timeout_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Timeout before pending entries are claimable by another consumer",
    )
```

**Design note**: Fields are flat on `AppConfig` (no nested sub-model), matching the existing pattern. The ADR shows a nested `EventBusSettings` model but the actual config system uses flat fields with the `AGENT_` env prefix. Env vars map to `AGENT_EVENT_BUS_ENABLED`, `AGENT_EVENT_BUS_REDIS_URL`, etc.

---

### Step 4: `src/personal_agent/events/models.py` — Event models (frozen Pydantic)

**Location**: `/Users/Alex/Dev/personal_agent/src/personal_agent/events/models.py`

This file defines the base `Event` model and the `RequestCapturedEvent` for Phase 1. The models are frozen (immutable) and use discriminated unions via `event_type: Literal[...]`.

```python
"""Event models for the event bus (ADR-0041).

All events are frozen Pydantic models. Events carry identifiers and metadata,
not large payloads. Consumers fetch full data from source systems if needed.

Uses discriminated union via event_type field for type-safe deserialization.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class EventBase(BaseModel):
    """Base class for all event bus events.

    All events carry a unique event_id, timestamp, and trace_id for
    correlation with the originating request.

    Attributes:
        event_id: Unique identifier for this event instance.
        timestamp: UTC timestamp when the event was created.
        trace_id: Trace identifier from the originating request.
        event_type: Discriminator field (overridden by subclasses).
    """

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    trace_id: str
    event_type: str  # Overridden by subclasses with Literal[...]


class RequestCapturedEvent(EventBase):
    """Published after a task capture is written to disk.

    Signals that a completed request has been captured and is available
    for consolidation processing.

    Attributes:
        event_type: Discriminator literal "request.captured".
        session_id: Session the request belongs to.
        capture_trace_id: Trace ID of the captured request (same as trace_id).
    """

    event_type: Literal["request.captured"] = "request.captured"
    session_id: str
    capture_trace_id: str


# Discriminated union of all event types.
# Extend this union as new event types are added in later phases.
Event = Annotated[
    Union[RequestCapturedEvent],
    Field(discriminator="event_type"),
]

# Stream name constants
STREAM_REQUEST_CAPTURED = "stream:request.captured"

# Consumer group constants
CG_CONSOLIDATOR = "cg:consolidator"
```

**Design decisions**:
- `EventBase` uses `ConfigDict(frozen=True)` -- immutable after creation, consistent with project conventions.
- `event_id` auto-generates a UUID. `timestamp` auto-generates UTC now. The caller only needs to provide `trace_id` and type-specific fields.
- The `Event` discriminated union type starts with just `RequestCapturedEvent`. As Phase 2/3 add events, they extend this union.
- Stream and consumer group names are string constants here (single source of truth).

---

### Step 5: `src/personal_agent/events/bus.py` — EventBus protocol + NoOpBus + singleton

**Location**: `/Users/Alex/Dev/personal_agent/src/personal_agent/events/bus.py`

```python
"""EventBus protocol and NoOp fallback (ADR-0041).

Defines the abstract EventBus interface and a NoOpBus that silently
discards events. When event_bus_enabled is False, get_event_bus()
returns the NoOpBus so publishing code requires no conditional checks.

The global bus instance is set during application lifespan (service/app.py)
and accessed via get_event_bus() from any component.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

import structlog

from personal_agent.events.models import EventBase

log = structlog.get_logger(__name__)

# Type alias for event handler callbacks
EventHandler = Callable[[EventBase, dict[str, Any]], Awaitable[None]]


@runtime_checkable
class EventBus(Protocol):
    """Abstract event bus interface.

    Publishers call publish() to append an event to a named stream.
    Consumers call subscribe() to register a handler for a stream+group.

    Implementations:
    - RedisStreamBus: Production backend using Redis Streams.
    - NoOpBus: Silent discard when event bus is disabled.
    """

    async def publish(self, stream: str, event: EventBase) -> str:
        """Publish an event to a named stream.

        Args:
            stream: Stream name (e.g., "stream:request.captured").
            event: Event instance to publish.

        Returns:
            Message ID assigned by the backend (or empty string for NoOp).
        """
        ...

    async def subscribe(
        self,
        stream: str,
        group: str,
        consumer: str,
        handler: EventHandler,
    ) -> None:
        """Register a handler for a stream and consumer group.

        The handler is called for each event delivered to this consumer.
        Acknowledgment and retry logic are handled by the implementation.

        Args:
            stream: Stream name to consume from.
            group: Consumer group name (e.g., "cg:consolidator").
            consumer: Consumer name within the group (e.g., hostname).
            handler: Async callback invoked with (event, raw_data).
        """
        ...

    async def connect(self) -> None:
        """Initialize connections and create stream infrastructure.

        Called once during application startup.
        """
        ...

    async def disconnect(self) -> None:
        """Gracefully close connections and stop consumers.

        Called once during application shutdown.
        """
        ...


class NoOpBus:
    """Silent event bus that discards all events.

    Used when event_bus_enabled is False. All methods are no-ops
    so publishing code does not need conditional checks.
    """

    async def publish(self, stream: str, event: EventBase) -> str:
        """Discard event silently.

        Args:
            stream: Ignored.
            event: Ignored.

        Returns:
            Empty string (no message ID).
        """
        return ""

    async def subscribe(
        self,
        stream: str,
        group: str,
        consumer: str,
        handler: EventHandler,
    ) -> None:
        """No-op subscribe.

        Args:
            stream: Ignored.
            group: Ignored.
            consumer: Ignored.
            handler: Ignored.
        """

    async def connect(self) -> None:
        """No-op connect."""

    async def disconnect(self) -> None:
        """No-op disconnect."""


# ---------------------------------------------------------------------------
# Process-global singleton (same pattern as metrics_daemon.py)
# ---------------------------------------------------------------------------

_global_event_bus: EventBus | NoOpBus = NoOpBus()


def set_global_event_bus(bus: EventBus | NoOpBus) -> None:
    """Set the process-global event bus instance.

    Called from service/app.py lifespan on startup.

    Args:
        bus: EventBus implementation or NoOpBus.
    """
    global _global_event_bus
    _global_event_bus = bus


def get_event_bus() -> EventBus | NoOpBus:
    """Get the process-global event bus instance.

    Returns NoOpBus if no bus has been set (feature disabled or
    called before lifespan initialization).

    Returns:
        Current event bus instance.
    """
    return _global_event_bus
```

**Design decisions**:
- `@runtime_checkable` on the Protocol enables `isinstance()` checks in tests.
- Singleton follows the exact `set_global_metrics_daemon` / `get_global_metrics_daemon` pattern from `brainstem/sensors/metrics_daemon.py`.
- Default singleton is `NoOpBus()` -- safe to call `get_event_bus().publish(...)` from anywhere even before lifespan initialization.
- `EventHandler` type alias keeps handler signatures consistent.
- `connect()` and `disconnect()` are part of the Protocol for lifecycle management.

---

### Step 6: `src/personal_agent/events/redis_backend.py` — RedisStreamBus

**Location**: `/Users/Alex/Dev/personal_agent/src/personal_agent/events/redis_backend.py`

```python
"""Redis Streams backend for the EventBus protocol (ADR-0041).

Wraps redis.asyncio to implement durable event publishing, consumer groups
with explicit acknowledgment, and dead-letter routing after max retries.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from personal_agent.config.settings import get_settings
from personal_agent.events.bus import EventHandler
from personal_agent.events.models import EventBase

log = structlog.get_logger(__name__)


class RedisStreamBus:
    """Redis Streams implementation of EventBus.

    Manages:
    - Redis connection lifecycle
    - Stream creation and consumer group initialization
    - Event publishing (XADD)
    - Consumer registration (stored for ConsumerRunner to iterate)
    - Dead-letter routing after max_retries failed deliveries

    Attributes:
        redis: Async Redis client instance.
        subscriptions: Registered consumer subscriptions.
    """

    def __init__(self, redis_url: str | None = None) -> None:
        """Initialize RedisStreamBus.

        Args:
            redis_url: Redis connection URL. Defaults to settings value.
        """
        settings = get_settings()
        self._redis_url = redis_url or settings.event_bus_redis_url
        self._max_retries = settings.event_bus_max_retries
        self._dead_letter_stream = settings.event_bus_dead_letter_stream
        self.redis: Redis | None = None
        self.subscriptions: list[_Subscription] = []

    async def connect(self) -> None:
        """Connect to Redis and verify connectivity.

        Raises:
            ConnectionError: If Redis is unreachable.
        """
        self.redis = Redis.from_url(
            self._redis_url,
            decode_responses=True,
            socket_connect_timeout=5.0,
        )
        # Verify connection
        await self.redis.ping()
        log.info("event_bus_redis_connected", redis_url=self._redis_url)

    async def disconnect(self) -> None:
        """Close the Redis connection."""
        if self.redis:
            await self.redis.aclose()
            self.redis = None
            log.info("event_bus_redis_disconnected")

    async def publish(self, stream: str, event: EventBase) -> str:
        """Publish an event to a Redis stream via XADD.

        Serializes the event to a flat dict and appends to the stream.
        Uses approximate MAXLEN trimming to prevent unbounded growth.

        Args:
            stream: Stream name (e.g., "stream:request.captured").
            event: Event instance to publish.

        Returns:
            Redis message ID (e.g., "1680000000000-0").

        Raises:
            RuntimeError: If Redis is not connected.
        """
        if not self.redis:
            raise RuntimeError("RedisStreamBus not connected")

        # Serialize event to flat string dict for Redis
        data = _event_to_redis_dict(event)
        msg_id: str = await self.redis.xadd(
            stream,
            data,
            maxlen=10000,  # Approximate trim to prevent unbounded growth
            approximate=True,
        )
        log.debug(
            "event_published",
            stream=stream,
            event_type=event.event_type,
            event_id=event.event_id,
            msg_id=msg_id,
            trace_id=event.trace_id,
        )
        return msg_id

    async def subscribe(
        self,
        stream: str,
        group: str,
        consumer: str,
        handler: EventHandler,
    ) -> None:
        """Register a consumer subscription.

        Creates the consumer group if it does not exist. The actual
        consumption loop is driven by ConsumerRunner (events/consumer.py).

        Args:
            stream: Stream name to consume from.
            group: Consumer group name.
            consumer: Consumer name within the group.
            handler: Async callback for each event.
        """
        if not self.redis:
            raise RuntimeError("RedisStreamBus not connected")

        # Create consumer group (idempotent -- ignore BUSYGROUP error)
        try:
            await self.redis.xgroup_create(
                stream, group, id="0", mkstream=True
            )
            log.info(
                "event_bus_consumer_group_created",
                stream=stream,
                group=group,
            )
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

        sub = _Subscription(
            stream=stream,
            group=group,
            consumer=consumer,
            handler=handler,
        )
        self.subscriptions.append(sub)
        log.info(
            "event_bus_subscribed",
            stream=stream,
            group=group,
            consumer=consumer,
        )

    async def ack(self, stream: str, group: str, msg_id: str) -> None:
        """Acknowledge a message as successfully processed.

        Args:
            stream: Stream name.
            group: Consumer group name.
            msg_id: Redis message ID to acknowledge.
        """
        if not self.redis:
            raise RuntimeError("RedisStreamBus not connected")
        await self.redis.xack(stream, group, msg_id)

    async def dead_letter(
        self,
        original_stream: str,
        group: str,
        msg_id: str,
        data: dict[str, str],
        error: str,
        attempts: int,
    ) -> None:
        """Route a failed message to the dead-letter stream.

        Args:
            original_stream: Stream the message originated from.
            group: Consumer group that failed to process.
            msg_id: Original Redis message ID.
            data: Original message data.
            error: Error description from the last failure.
            attempts: Total delivery attempts.
        """
        if not self.redis:
            raise RuntimeError("RedisStreamBus not connected")

        dl_data = {
            "original_stream": original_stream,
            "original_msg_id": msg_id,
            "consumer_group": group,
            "error": str(error)[:500],  # Truncate long errors
            "attempts": str(attempts),
            **data,
        }
        await self.redis.xadd(
            self._dead_letter_stream,
            dl_data,
            maxlen=1000,
            approximate=True,
        )
        log.warning(
            "event_bus_dead_letter",
            original_stream=original_stream,
            msg_id=msg_id,
            group=group,
            error=error,
            attempts=attempts,
        )

    @property
    def max_retries(self) -> int:
        """Maximum delivery attempts before dead-lettering."""
        return self._max_retries


class _Subscription:
    """Internal subscription record.

    Attributes:
        stream: Stream name.
        group: Consumer group name.
        consumer: Consumer name.
        handler: Event handler callback.
    """

    __slots__ = ("stream", "group", "consumer", "handler")

    def __init__(
        self,
        stream: str,
        group: str,
        consumer: str,
        handler: EventHandler,
    ) -> None:
        self.stream = stream
        self.group = group
        self.consumer = consumer
        self.handler = handler


def _event_to_redis_dict(event: EventBase) -> dict[str, str]:
    """Serialize an event to a flat string dict for XADD.

    Redis stream entries are flat string->string maps. We serialize the
    event as JSON under a single "payload" key plus top-level "event_type"
    and "event_id" for fast filtering without deserialization.

    Args:
        event: Event to serialize.

    Returns:
        Dict suitable for redis XADD.
    """
    import orjson

    return {
        "event_type": event.event_type,
        "event_id": event.event_id,
        "trace_id": event.trace_id,
        "payload": orjson.dumps(
            event.model_dump(mode="json")
        ).decode(),
    }


def _redis_dict_to_event(data: dict[str, str]) -> EventBase:
    """Deserialize a Redis stream entry back to an Event.

    Args:
        data: Dict from XREADGROUP.

    Returns:
        Deserialized Event instance.

    Raises:
        ValueError: If event_type is unknown.
    """
    import orjson

    from personal_agent.events.models import RequestCapturedEvent

    payload = orjson.loads(data["payload"])
    event_type = data.get("event_type", payload.get("event_type"))

    if event_type == "request.captured":
        return RequestCapturedEvent(**payload)

    raise ValueError(f"Unknown event_type: {event_type}")


def get_default_consumer_name() -> str:
    """Generate a default consumer name from hostname + PID.

    Returns:
        Consumer name string (e.g., "macbook-12345").
    """
    import os

    hostname = socket.gethostname().split(".")[0]
    return f"{hostname}-{os.getpid()}"
```

**Design decisions**:
- Serialization uses `orjson` (already a project dependency) for speed. Events are stored as a `payload` JSON string plus top-level `event_type`, `event_id`, `trace_id` keys for filtering.
- `MAXLEN` approximate trimming prevents unbounded stream growth.
- `_Subscription` is a lightweight internal record; `ConsumerRunner` iterates `bus.subscriptions` to drive consumption loops.
- `_redis_dict_to_event` dispatches on `event_type` -- simple switch, extended as new event types are added.
- Dead-letter data includes the original stream, group, error, and attempt count for debugging.

---

### Step 7: `src/personal_agent/events/consumer.py` — ConsumerRunner

**Location**: `/Users/Alex/Dev/personal_agent/src/personal_agent/events/consumer.py`

```python
"""Consumer runner for Redis Streams event bus (ADR-0041).

Drives async consumption loops for all registered subscriptions.
Each subscription gets its own asyncio task that polls via XREADGROUP,
dispatches to the handler, acknowledges on success, and routes to
dead-letter after max_retries failures.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from personal_agent.config.settings import get_settings
from personal_agent.events.redis_backend import (
    RedisStreamBus,
    _redis_dict_to_event,
)

log = structlog.get_logger(__name__)


class ConsumerRunner:
    """Manages async consumer loops for a RedisStreamBus.

    Usage:
        runner = ConsumerRunner(bus)
        await runner.start()   # Launches one task per subscription
        # ... application runs ...
        await runner.stop()    # Cancels all consumer tasks

    Attributes:
        bus: The RedisStreamBus with registered subscriptions.
        tasks: Running asyncio tasks for each consumer loop.
    """

    def __init__(self, bus: RedisStreamBus) -> None:
        """Initialize the consumer runner.

        Args:
            bus: RedisStreamBus with subscriptions already registered.
        """
        self.bus = bus
        self.tasks: list[asyncio.Task[None]] = []
        settings = get_settings()
        self._poll_interval_ms = settings.event_bus_consumer_poll_interval_ms
        self._running = False

    async def start(self) -> None:
        """Start consumer loops for all registered subscriptions.

        Creates one asyncio task per subscription. Each task runs
        _consume_loop() until stop() is called.
        """
        if self._running:
            log.warning("consumer_runner_already_running")
            return

        self._running = True
        for sub in self.bus.subscriptions:
            task = asyncio.create_task(
                self._consume_loop(sub),
                name=f"consumer:{sub.group}:{sub.stream}",
            )
            self.tasks.append(task)
            log.info(
                "consumer_loop_started",
                stream=sub.stream,
                group=sub.group,
                consumer=sub.consumer,
            )

    async def stop(self) -> None:
        """Stop all consumer loops and wait for graceful shutdown."""
        self._running = False
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        log.info("consumer_runner_stopped")

    async def _consume_loop(self, sub: Any) -> None:
        """Poll loop for a single subscription.

        Reads from the stream via XREADGROUP, dispatches to handler,
        acknowledges on success, retries or dead-letters on failure.

        Args:
            sub: _Subscription instance with stream, group, consumer, handler.
        """
        redis = self.bus.redis
        if not redis:
            log.error("consumer_loop_no_redis", stream=sub.stream, group=sub.group)
            return

        poll_interval_s = self._poll_interval_ms / 1000.0

        while self._running:
            try:
                # XREADGROUP: read new messages for this consumer
                results = await redis.xreadgroup(
                    groupname=sub.group,
                    consumername=sub.consumer,
                    streams={sub.stream: ">"},
                    count=10,
                    block=int(self._poll_interval_ms),
                )

                if not results:
                    continue

                for stream_name, messages in results:
                    for msg_id, data in messages:
                        await self._process_message(sub, msg_id, data)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(
                    "consumer_loop_error",
                    stream=sub.stream,
                    group=sub.group,
                    error=str(e),
                    exc_info=True,
                )
                # Back off on unexpected errors
                await asyncio.sleep(poll_interval_s * 10)

    async def _process_message(
        self,
        sub: Any,
        msg_id: str,
        data: dict[str, str],
    ) -> None:
        """Process a single message: deserialize, dispatch, ack or dead-letter.

        Args:
            sub: Subscription record.
            msg_id: Redis message ID.
            data: Raw message data from XREADGROUP.
        """
        try:
            event = _redis_dict_to_event(data)
            await sub.handler(event, data)
            await self.bus.ack(sub.stream, sub.group, msg_id)

            log.debug(
                "event_processed",
                stream=sub.stream,
                group=sub.group,
                event_type=event.event_type,
                msg_id=msg_id,
                trace_id=event.trace_id,
            )

        except Exception as e:
            # Check delivery count from the pending entries list
            attempts = await self._get_delivery_count(sub, msg_id)

            if attempts >= self.bus.max_retries:
                await self.bus.dead_letter(
                    original_stream=sub.stream,
                    group=sub.group,
                    msg_id=msg_id,
                    data=data,
                    error=str(e),
                    attempts=attempts,
                )
                # Ack to remove from PEL after dead-lettering
                await self.bus.ack(sub.stream, sub.group, msg_id)
            else:
                log.warning(
                    "event_processing_failed",
                    stream=sub.stream,
                    group=sub.group,
                    msg_id=msg_id,
                    error=str(e),
                    attempt=attempts,
                    max_retries=self.bus.max_retries,
                )

    async def _get_delivery_count(self, sub: Any, msg_id: str) -> int:
        """Get the delivery count for a pending message.

        Uses XPENDING with message ID range to get delivery count
        from the pending entries list.

        Args:
            sub: Subscription record.
            msg_id: Redis message ID.

        Returns:
            Number of times this message has been delivered.
        """
        redis = self.bus.redis
        if not redis:
            return 1

        try:
            # XPENDING <stream> <group> <min> <max> <count>
            pending = await redis.xpending_range(
                sub.stream,
                sub.group,
                min=msg_id,
                max=msg_id,
                count=1,
            )
            if pending:
                # Each entry: {message_id, consumer, time_since_delivered, times_delivered}
                return int(pending[0].get("times_delivered", 1))
        except Exception:
            pass
        return 1
```

**Design decisions**:
- `XREADGROUP` with `block` uses the configured poll interval -- Redis blocks server-side rather than busy-polling.
- `count=10` processes up to 10 messages per poll to batch work.
- Delivery count comes from Redis PEL (pending entries list) via `xpending_range` -- no application-side counter needed.
- After `max_retries`, the message is dead-lettered AND acknowledged (removed from PEL) to prevent infinite retry.
- Error back-off on unexpected loop errors (10x poll interval).

---

### Step 8: `src/personal_agent/events/__init__.py` — Public API

**Location**: `/Users/Alex/Dev/personal_agent/src/personal_agent/events/__init__.py`

```python
"""Event bus module for async inter-component communication (ADR-0041).

Public API:
    get_event_bus() -> EventBus | NoOpBus
    set_global_event_bus(bus) -> None

Event types:
    RequestCapturedEvent

Stream constants:
    STREAM_REQUEST_CAPTURED
    CG_CONSOLIDATOR
"""

from personal_agent.events.bus import (
    EventBus,
    EventHandler,
    NoOpBus,
    get_event_bus,
    set_global_event_bus,
)
from personal_agent.events.models import (
    CG_CONSOLIDATOR,
    STREAM_REQUEST_CAPTURED,
    EventBase,
    RequestCapturedEvent,
)

__all__ = [
    "EventBus",
    "EventHandler",
    "EventBase",
    "NoOpBus",
    "RequestCapturedEvent",
    "STREAM_REQUEST_CAPTURED",
    "CG_CONSOLIDATOR",
    "get_event_bus",
    "set_global_event_bus",
]
```

---

### Step 9: `src/personal_agent/telemetry/events.py` — Add event bus telemetry constants

**Location**: `/Users/Alex/Dev/personal_agent/src/personal_agent/telemetry/events.py`

Add at the end of the file (after line 93):

```python
# Event Bus events (ADR-0041)
EVENT_BUS_PUBLISHED = "event_bus_published"
EVENT_BUS_CONSUMED = "event_bus_consumed"
EVENT_BUS_DEAD_LETTER = "event_bus_dead_letter"
EVENT_BUS_CONSUMER_STARTED = "event_bus_consumer_started"
EVENT_BUS_CONSUMER_STOPPED = "event_bus_consumer_stopped"
```

Also add these to `src/personal_agent/telemetry/__init__.py`'s imports and `__all__`.

---

### Step 10: `src/personal_agent/brainstem/scheduler.py` — Add `on_request_captured()` method

**Location**: `/Users/Alex/Dev/personal_agent/src/personal_agent/brainstem/scheduler.py`

Add a new public method `on_request_captured()` to `BrainstemScheduler`. This is the event handler that the consumer will call. It respects all existing resource gates.

Add this method after `record_request()` (after line 198):

```python
    async def on_request_captured(self, trace_id: str, session_id: str) -> None:
        """Handle a request.captured event from the event bus.

        This is the event-driven alternative to the polling-based
        _monitoring_loop. Respects all existing consolidation gates:
        - enable_second_brain feature flag
        - _should_consolidate() resource checks (active requests,
          min interval, idle time, CPU, memory)

        Called by the cg:consolidator consumer registered in service/app.py.

        Args:
            trace_id: Trace ID of the captured request.
            session_id: Session ID of the captured request.
        """
        if not settings.enable_second_brain:
            log.debug(
                "on_request_captured_second_brain_disabled",
                trace_id=trace_id,
            )
            return

        if await self._should_consolidate():
            log.info(
                "consolidation_triggered_by_event",
                trace_id=trace_id,
                session_id=session_id,
            )
            await self._trigger_consolidation()
        else:
            log.debug(
                "consolidation_skipped_by_event_gates",
                trace_id=trace_id,
                session_id=session_id,
            )
```

**Design note**: This method reuses `_should_consolidate()` which checks active requests, min interval, idle time, CPU, and memory. The existing polling loop in `_monitoring_loop` remains as a fallback -- both paths converge on the same gate checks and `_trigger_consolidation()` call.

---

### Step 11: `src/personal_agent/orchestrator/executor.py` — Publish `request.captured` event

**Location**: `/Users/Alex/Dev/personal_agent/src/personal_agent/orchestrator/executor.py`

After `write_capture(capture)` at line 630, inside the same `try` block, add the event publish call. The publish should be fire-and-forget (background task) to avoid adding latency to the user response.

Insert after line 630 (`write_capture(capture)`):

```python
                # Publish request.captured event to event bus (ADR-0041)
                try:
                    from personal_agent.events.bus import get_event_bus
                    from personal_agent.events.models import (
                        STREAM_REQUEST_CAPTURED,
                        RequestCapturedEvent,
                    )

                    bus = get_event_bus()
                    event = RequestCapturedEvent(
                        trace_id=ctx.trace_id,
                        session_id=ctx.session_id,
                        capture_trace_id=ctx.trace_id,
                    )
                    # Fire-and-forget: don't block user response on event publish
                    from personal_agent.captains_log.background import run_in_background

                    async def _publish_event() -> None:
                        await bus.publish(STREAM_REQUEST_CAPTURED, event)

                    run_in_background(_publish_event())
                except Exception as pub_err:
                    log.warning(
                        "event_bus_publish_failed",
                        trace_id=ctx.trace_id,
                        error=str(pub_err),
                    )
```

**Design decisions**:
- Uses lazy imports (inside the `if state == TaskState.COMPLETED:` block) to match the existing pattern in executor.py (e.g., `from personal_agent.captains_log.capture import TaskCapture, write_capture` at line 600).
- Wrapped in `run_in_background()` to avoid blocking user response, matching the Captain's Log reflection pattern at line 645.
- Wrapped in its own `try/except` so a publish failure never breaks task completion.
- When event bus is disabled, `get_event_bus()` returns `NoOpBus` which silently discards -- the publish call is effectively free.

---

### Step 12: `src/personal_agent/service/app.py` — Wire event bus lifecycle

**Location**: `/Users/Alex/Dev/personal_agent/src/personal_agent/service/app.py`

Three changes to the `lifespan()` function:

**12a. Add import at top of file** (around line 12):

```python
from personal_agent.events.bus import NoOpBus, set_global_event_bus
```

**12b. Add global variable** (around line 38):

```python
event_bus: "RedisStreamBus | NoOpBus | None" = None
consumer_runner: "ConsumerRunner | None" = None
```

**12c. Add event bus initialization in lifespan(), after metrics daemon setup but before scheduler creation** (insert before line 222, after `set_global_metrics_daemon(metrics_daemon)` at line 188).

This placement ensures the event bus is available before the scheduler starts, since the scheduler's `on_request_captured()` method will be registered as a consumer handler.

```python
    # --- Event Bus (ADR-0041) ---
    if settings.event_bus_enabled:
        try:
            from personal_agent.events.consumer import ConsumerRunner
            from personal_agent.events.models import (
                CG_CONSOLIDATOR,
                STREAM_REQUEST_CAPTURED,
            )
            from personal_agent.events.redis_backend import (
                RedisStreamBus,
                get_default_consumer_name,
            )

            event_bus = RedisStreamBus()
            await event_bus.connect()
            set_global_event_bus(event_bus)
            log.info("event_bus_initialized", redis_url=settings.event_bus_redis_url)

        except Exception as e:
            log.warning(
                "event_bus_init_failed",
                error=str(e),
                remedy="Redis may not be running. Event bus disabled, falling back to polling.",
            )
            event_bus = None
            set_global_event_bus(NoOpBus())
    else:
        set_global_event_bus(NoOpBus())
```

**12d. After scheduler creation (after `await scheduler.start()` around line 248), register the consolidator consumer and start the runner:**

```python
    # Register event bus consumers (after scheduler is created)
    if settings.event_bus_enabled and event_bus is not None and scheduler is not None:
        try:
            from personal_agent.events.consumer import ConsumerRunner
            from personal_agent.events.models import (
                CG_CONSOLIDATOR,
                STREAM_REQUEST_CAPTURED,
                EventBase,
            )
            from personal_agent.events.redis_backend import get_default_consumer_name

            consumer_name = get_default_consumer_name()

            # Create handler that delegates to scheduler
            _scheduler_ref = scheduler  # capture for closure

            async def _on_request_captured(event: EventBase, raw: dict) -> None:
                from personal_agent.events.models import RequestCapturedEvent

                if isinstance(event, RequestCapturedEvent):
                    await _scheduler_ref.on_request_captured(
                        trace_id=event.trace_id,
                        session_id=event.session_id,
                    )

            await event_bus.subscribe(
                stream=STREAM_REQUEST_CAPTURED,
                group=CG_CONSOLIDATOR,
                consumer=consumer_name,
                handler=_on_request_captured,
            )

            consumer_runner = ConsumerRunner(event_bus)
            await consumer_runner.start()
            log.info("event_bus_consumers_started")

        except Exception as e:
            log.warning(
                "event_bus_consumer_setup_failed",
                error=str(e),
                exc_info=True,
            )
```

**12e. Add shutdown code (in the shutdown section, before `if scheduler:` around line 264):**

```python
    # Stop event bus consumers and disconnect
    if consumer_runner:
        await consumer_runner.stop()
    if event_bus and hasattr(event_bus, "disconnect"):
        await event_bus.disconnect()
        set_global_event_bus(NoOpBus())
```

**12f. Add `event_bus` and `consumer_runner` to the `global` declaration in lifespan** (line 123):

```python
    global es_handler, memory_service, scheduler, metrics_daemon, mcp_adapter, event_bus, consumer_runner
```

---

### Step 13: `.env.example` — Add event bus config section

**Location**: `/Users/Alex/Dev/personal_agent/.env.example`

Add a new section after the "CAPTAIN'S LOG -> LINEAR FEEDBACK LOOP" section (around line 318):

```
# =============================================================================
# EVENT BUS (ADR-0041)
# =============================================================================
# Enable Redis Streams event bus for async inter-component communication.
# When disabled, all event publishing is silently discarded (NoOpBus).
# Scheduler polling remains as fallback regardless of this setting.
# Requires: Redis running (docker compose service "redis").
# Default: false
# AGENT_EVENT_BUS_ENABLED=false

# Redis connection URL for event bus
# Default: redis://localhost:6379/0
# AGENT_EVENT_BUS_REDIS_URL=redis://localhost:6379/0

# Consumer poll interval in milliseconds (XREADGROUP block time)
# Default: 100
# AGENT_EVENT_BUS_CONSUMER_POLL_INTERVAL_MS=100

# Max delivery attempts before dead-lettering
# Default: 3
# AGENT_EVENT_BUS_MAX_RETRIES=3

# Dead-letter stream name
# Default: stream:dead_letter
# AGENT_EVENT_BUS_DEAD_LETTER_STREAM=stream:dead_letter

# Timeout before pending entries are claimable by another consumer
# Default: 300
# AGENT_EVENT_BUS_ACK_TIMEOUT_SECONDS=300
```

---

## 3. Test Strategy

### Test Files to Create

All tests use `pytest` + `pytest-asyncio`. No real Redis required for unit tests -- use either `NoOpBus` or an in-memory stub.

#### `tests/personal_agent/events/__init__.py`

Empty init file for the test package.

#### `tests/personal_agent/events/test_models.py`

```
Test cases:
1. RequestCapturedEvent is frozen (assignment raises TypeError)
2. RequestCapturedEvent.event_type is "request.captured"
3. EventBase generates unique event_id on each instantiation
4. EventBase generates timestamp in UTC
5. RequestCapturedEvent serializes/deserializes via model_dump/model_validate
6. Discriminated union Event resolves RequestCapturedEvent correctly
```

#### `tests/personal_agent/events/test_bus.py`

```
Test cases:
1. NoOpBus.publish() returns empty string
2. NoOpBus.subscribe() is a no-op (no error)
3. NoOpBus.connect() and disconnect() are no-ops
4. get_event_bus() returns NoOpBus by default (before set_global_event_bus)
5. set_global_event_bus() / get_event_bus() round-trip
6. EventBus Protocol is runtime_checkable -- NoOpBus is an instance
7. EventBus Protocol is runtime_checkable -- RedisStreamBus is an instance
```

#### `tests/personal_agent/events/test_redis_backend.py`

```
Test cases (use unittest.mock.AsyncMock for redis client):
1. _event_to_redis_dict produces expected keys (event_type, event_id, trace_id, payload)
2. _redis_dict_to_event round-trips a RequestCapturedEvent
3. _redis_dict_to_event raises ValueError for unknown event_type
4. RedisStreamBus.publish() calls redis.xadd with correct args
5. RedisStreamBus.subscribe() calls xgroup_create and appends subscription
6. RedisStreamBus.subscribe() ignores BUSYGROUP ResponseError
7. RedisStreamBus.ack() calls redis.xack
8. RedisStreamBus.dead_letter() calls redis.xadd on dead_letter_stream
9. get_default_consumer_name() returns hostname-pid format
```

#### `tests/personal_agent/events/test_consumer.py`

```
Test cases (mock RedisStreamBus internals):
1. ConsumerRunner.start() creates one task per subscription
2. ConsumerRunner.stop() cancels all tasks
3. _process_message calls handler, then ack on success
4. _process_message calls dead_letter after max_retries failures
5. _process_message logs warning on failure below max_retries
6. _consume_loop backs off on unexpected errors
```

#### Test for scheduler integration

Add to a new `tests/personal_agent/brainstem/test_scheduler_events.py`:

```
Test cases:
1. on_request_captured() calls _trigger_consolidation when _should_consolidate returns True
2. on_request_captured() skips when enable_second_brain is False
3. on_request_captured() skips when _should_consolidate returns False
```

### Integration Test (marked `@pytest.mark.integration`)

Optional, requires running Redis:
1. Publish a RequestCapturedEvent to a real Redis stream
2. Consume it with a ConsumerRunner
3. Verify handler was called with correct event data
4. Verify XACK removed the message from PEL

---

## 4. Verification Steps

After implementation, verify the following:

### 4a. Unit tests pass
```bash
uv run pytest tests/personal_agent/events/ -v
uv run pytest tests/personal_agent/brainstem/test_scheduler_events.py -v
```

### 4b. Type checking
```bash
uv run mypy src/personal_agent/events/
```

### 4c. Linting
```bash
uv run ruff check src/personal_agent/events/
uv run ruff format --check src/personal_agent/events/
```

### 4d. Infrastructure
```bash
# Start Redis alongside existing services
docker compose up -d redis
docker compose exec redis redis-cli ping  # Expect: PONG
```

### 4e. Feature flag off (default)
Start the service with `AGENT_EVENT_BUS_ENABLED=false` (default). Verify:
- No Redis connection attempted
- `get_event_bus()` returns `NoOpBus`
- Service starts normally, scheduler polling works as before

### 4f. Feature flag on
Start with `AGENT_EVENT_BUS_ENABLED=true`. Verify:
- Redis connection established (log: `event_bus_redis_connected`)
- Consumer group created (log: `event_bus_consumer_group_created`)
- Consumer loop started (log: `consumer_loop_started`)
- Send a request, observe:
  - `event_published` log after capture write
  - `event_processed` log in consumer
  - `consolidation_triggered_by_event` or `consolidation_skipped_by_event_gates` log
- Scheduler polling still runs (fallback)

### 4g. Graceful degradation
Start with `AGENT_EVENT_BUS_ENABLED=true` but Redis not running. Verify:
- `event_bus_init_failed` warning logged
- Service starts normally with NoOpBus fallback
- Scheduler polling works as before

---

## 5. Risk Mitigations

| Risk | Mitigation |
|------|-----------|
| Redis unavailable at startup | Catch connection error, fall back to NoOpBus, log warning. Polling continues. |
| Redis goes down during operation | Publish errors caught in executor try/except block. Consumer loop backs off. No user-facing impact. |
| Event handler raises exception | ConsumerRunner catches, checks retry count, dead-letters after max_retries. |
| Circular imports | All imports in executor.py and app.py are lazy (inside function body), matching existing patterns. |
| Publish adds latency to response | Publish is fire-and-forget via `run_in_background()`, same pattern as Captain's Log reflection. |
| Double-triggering (event + poll) | `_should_consolidate()` checks `min_consolidation_interval_seconds` (1 hour default), preventing double-fire. Both paths converge on same gate. |

---

## 6. Files Summary

### New Files (5 source + 1 test package + 4 test files = 10)

| File | Purpose |
|------|---------|
| `src/personal_agent/events/__init__.py` | Package init, public API exports |
| `src/personal_agent/events/models.py` | Frozen Pydantic event models |
| `src/personal_agent/events/bus.py` | EventBus protocol, NoOpBus, singleton |
| `src/personal_agent/events/redis_backend.py` | RedisStreamBus implementation |
| `src/personal_agent/events/consumer.py` | ConsumerRunner async loops |
| `tests/personal_agent/events/__init__.py` | Test package init |
| `tests/personal_agent/events/test_models.py` | Event model tests |
| `tests/personal_agent/events/test_bus.py` | Protocol + NoOpBus tests |
| `tests/personal_agent/events/test_redis_backend.py` | Redis backend tests (mocked) |
| `tests/personal_agent/events/test_consumer.py` | Consumer runner tests (mocked) |

### Modified Files (7)

| File | Change |
|------|--------|
| `docker-compose.yml` | Add `redis:7-alpine` service + `redis_data` volume |
| `pyproject.toml` | Add `redis[hiredis]>=5.0.0` dependency |
| `src/personal_agent/config/settings.py` | Add 6 `event_bus_*` fields to AppConfig |
| `src/personal_agent/orchestrator/executor.py` | Publish `request.captured` event after `write_capture()` |
| `src/personal_agent/brainstem/scheduler.py` | Add `on_request_captured()` public method |
| `src/personal_agent/service/app.py` | Wire event bus lifecycle (init, consumer start, shutdown) |
| `.env.example` | Add event bus configuration section |
| `src/personal_agent/telemetry/events.py` | Add event bus telemetry constants |
| `src/personal_agent/telemetry/__init__.py` | Export new event constants |

