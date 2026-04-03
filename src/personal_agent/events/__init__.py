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
    STREAM_REQUEST_CAPTURED,
    EventBase,
    RequestCapturedEvent,
)

__all__ = [
    "CG_CONSOLIDATOR",
    "EventBase",
    "EventBus",
    "EventHandler",
    "NoOpBus",
    "RequestCapturedEvent",
    "STREAM_REQUEST_CAPTURED",
    "get_event_bus",
    "set_global_event_bus",
]
