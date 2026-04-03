"""Event models for the Redis Streams event bus (ADR-0041).

All event models are frozen Pydantic models with a ``event_type`` literal
discriminator — consistent with the project's discriminated-union coding
standard.  Events carry identifiers and metadata, not large payloads;
consumers fetch full data from the source system when needed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Stream and consumer-group constants
# ---------------------------------------------------------------------------

STREAM_REQUEST_CAPTURED = "stream:request.captured"
"""Stream for request-captured events (Phase 1)."""

CG_CONSOLIDATOR = "cg:consolidator"
"""Consumer group: brainstem consolidator."""


# ---------------------------------------------------------------------------
# Base model
# ---------------------------------------------------------------------------


class EventBase(BaseModel):
    """Base class for all event bus events.

    Attributes:
        event_id: Unique identifier for this event instance.
        event_type: Literal discriminator — set by each concrete subclass.
        created_at: UTC timestamp when the event was created.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    event_type: str  # overridden as Literal in subclasses
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Phase 1 events
# ---------------------------------------------------------------------------


class RequestCapturedEvent(EventBase):
    """Published after a task capture is written to disk.

    The consolidator consumer listens for this event to trigger
    near-real-time consolidation instead of relying on polling.

    Attributes:
        trace_id: Request trace identifier.
        session_id: Session that originated the request.
    """

    event_type: Literal["request.captured"] = "request.captured"
    trace_id: str
    session_id: str
