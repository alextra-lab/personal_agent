"""Event bus package (ADR-0041 — Redis Streams).

Public API::

    from personal_agent.events import (
        EventBus,
        EventHandler,
        NoOpBus,
        get_event_bus,
        set_global_event_bus,
        EventBase,
        RequestCapturedEvent,
        STREAM_REQUEST_CAPTURED,
        CG_CONSOLIDATOR,
    )
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
    CG_ES_INDEXER,
    CG_FRESHNESS,
    CG_SESSION_WRITER,
    STREAM_MEMORY_ACCESSED,
    STREAM_MEMORY_ENTITIES_UPDATED,
    STREAM_REQUEST_CAPTURED,
    STREAM_REQUEST_COMPLETED,
    EventBase,
    MemoryAccessedEvent,
    MemoryEntitiesUpdatedEvent,
    RequestCapturedEvent,
    RequestCompletedEvent,
    parse_stream_event,
)

__all__ = [
    "CG_CONSOLIDATOR",
    "CG_ES_INDEXER",
    "CG_FRESHNESS",
    "CG_SESSION_WRITER",
    "EventBase",
    "EventBus",
    "EventHandler",
    "MemoryAccessedEvent",
    "MemoryEntitiesUpdatedEvent",
    "NoOpBus",
    "RequestCapturedEvent",
    "RequestCompletedEvent",
    "STREAM_MEMORY_ACCESSED",
    "STREAM_MEMORY_ENTITIES_UPDATED",
    "STREAM_REQUEST_CAPTURED",
    "STREAM_REQUEST_COMPLETED",
    "get_event_bus",
    "parse_stream_event",
    "set_global_event_bus",
]
