"""Redis Streams event bus backend (ADR-0041).

Wraps ``redis.asyncio`` to provide durable publish/subscribe with consumer
groups, explicit acknowledgment, and dead-letter routing.
"""

from __future__ import annotations

import redis.asyncio as aioredis

from personal_agent.config.settings import get_settings
from personal_agent.events.bus import EventHandler
from personal_agent.events.models import EventBase
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


class Subscription:
    """Metadata for a single consumer-group subscription.

    Attributes:
        stream: Redis stream name.
        group: Consumer group name.
        consumer_name: Unique consumer within the group.
        handler: Async callback for each event.
    """

    __slots__ = ("stream", "group", "consumer_name", "handler")

    def __init__(
        self,
        stream: str,
        group: str,
        consumer_name: str,
        handler: EventHandler,
    ) -> None:
        """Initialize subscription metadata.

        Args:
            stream: Redis stream name.
            group: Consumer group name.
            consumer_name: Unique consumer within the group.
            handler: Async callback for each event.
        """
        self.stream = stream
        self.group = group
        self.consumer_name = consumer_name
        self.handler = handler


class RedisStreamBus:
    """Event bus backed by Redis Streams.

    Lifecycle:
    1. Instantiate with ``await RedisStreamBus.connect(redis_url)``.
    2. Register subscriptions via ``subscribe()``.
    3. Hand subscriptions to ``ConsumerRunner`` for the read loop.
    4. Call ``close()`` on shutdown.
    """

    def __init__(self, client: aioredis.Redis) -> None:
        """Initialize with an existing Redis client.

        Args:
            client: Connected ``redis.asyncio.Redis`` instance.
        """
        self._client = client
        self._subscriptions: list[Subscription] = []

    # -- Factory ----------------------------------------------------------

    @classmethod
    async def connect(cls, redis_url: str | None = None) -> RedisStreamBus:
        """Create a connected RedisStreamBus.

        Args:
            redis_url: Redis connection URL.  Falls back to
                ``settings.event_bus_redis_url``.

        Returns:
            Connected bus instance.

        Raises:
            redis.ConnectionError: If Redis is unreachable.
        """
        url = redis_url or get_settings().event_bus_redis_url
        client: aioredis.Redis = aioredis.from_url(  # type: ignore[no-untyped-call]
            url, decode_responses=True
        )
        # Verify connectivity
        await client.ping()  # type: ignore[misc]
        log.info("redis_stream_bus_connected", redis_url=url)
        return cls(client)

    # -- Publish ----------------------------------------------------------

    async def publish(self, stream: str, event: EventBase) -> None:
        """Publish an event to a Redis stream via XADD.

        Args:
            stream: Target stream name.
            event: Event to publish.
        """
        payload = event.model_dump(mode="json")
        # Flatten nested dict: Redis streams store flat field-value pairs.
        # We serialize the whole event as a single JSON field for simplicity
        # and to preserve type fidelity on the consumer side.
        import orjson

        data = {"data": orjson.dumps(payload).decode()}
        message_id = await self._client.xadd(stream, data)  # type: ignore[arg-type]
        log.debug(
            "event_published",
            stream=stream,
            event_type=event.event_type,
            event_id=event.event_id,
            message_id=message_id,
        )

    # -- Subscribe --------------------------------------------------------

    async def subscribe(
        self,
        stream: str,
        group: str,
        consumer_name: str,
        handler: EventHandler,
    ) -> None:
        """Register a consumer-group subscription.

        Creates the consumer group if it doesn't exist (idempotent).

        Args:
            stream: Stream name.
            group: Consumer group name.
            consumer_name: Unique consumer within the group.
            handler: Async callback for each event.
        """
        # Ensure stream and consumer group exist (MKSTREAM).
        try:
            await self._client.xgroup_create(stream, group, id="0", mkstream=True)
            log.info(
                "consumer_group_created",
                stream=stream,
                group=group,
            )
        except aioredis.ResponseError as exc:
            if "BUSYGROUP" in str(exc):
                # Group already exists — expected on restart.
                log.debug("consumer_group_exists", stream=stream, group=group)
            else:
                raise

        sub = Subscription(
            stream=stream,
            group=group,
            consumer_name=consumer_name,
            handler=handler,
        )
        self._subscriptions.append(sub)
        log.info(
            "subscription_registered",
            stream=stream,
            group=group,
            consumer_name=consumer_name,
        )

    # -- Acknowledge / Dead-letter ----------------------------------------

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        """Acknowledge a message (XACK).

        Args:
            stream: Stream name.
            group: Consumer group name.
            message_id: Redis message ID to acknowledge.
        """
        await self._client.xack(stream, group, message_id)

    async def dead_letter(
        self,
        event: EventBase,
        source_stream: str,
        group: str,
        error: str,
        attempts: int,
    ) -> None:
        """Route a failed event to the dead-letter stream.

        Args:
            event: The event that failed processing.
            source_stream: Original stream the event came from.
            group: Consumer group that failed.
            error: Error message from the last attempt.
            attempts: Total delivery attempts.
        """
        import orjson

        settings = get_settings()
        payload = {
            "data": orjson.dumps(event.model_dump(mode="json")).decode(),
            "source_stream": source_stream,
            "consumer_group": group,
            "error": str(error)[:500],  # Truncate to avoid huge payloads
            "attempts": str(attempts),
        }
        await self._client.xadd(
            settings.event_bus_dead_letter_stream,
            payload,  # type: ignore[arg-type]
        )
        log.warning(
            "event_dead_lettered",
            event_type=event.event_type,
            event_id=event.event_id,
            source_stream=source_stream,
            group=group,
            error=error,
            attempts=attempts,
        )

    # -- Accessors --------------------------------------------------------

    @property
    def subscriptions(self) -> list[Subscription]:
        """Return registered subscriptions."""
        return list(self._subscriptions)

    @property
    def client(self) -> aioredis.Redis:
        """Return the underlying Redis client."""
        return self._client

    # -- Lifecycle --------------------------------------------------------

    async def close(self) -> None:
        """Close the Redis connection."""
        await self._client.aclose()
        log.info("redis_stream_bus_closed")
