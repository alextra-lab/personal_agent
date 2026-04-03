"""Event models for the Redis Streams event bus (ADR-0041).

All event models are frozen Pydantic models with a ``event_type`` literal
discriminator — consistent with the project's discriminated-union coding
standard.  Events carry identifiers and metadata, not large payloads;
consumers fetch full data from the source system when needed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Stream and consumer-group constants
# ---------------------------------------------------------------------------

STREAM_REQUEST_CAPTURED = "stream:request.captured"
"""Stream for request-captured events (Phase 1)."""

STREAM_REQUEST_COMPLETED = "stream:request.completed"
"""Stream for request-completed events (Phase 2 — ES + session writer)."""

CG_CONSOLIDATOR = "cg:consolidator"
"""Consumer group: brainstem consolidator."""

CG_ES_INDEXER = "cg:es-indexer"
"""Consumer group: request trace indexing to Elasticsearch."""

CG_SESSION_WRITER = "cg:session-writer"
"""Consumer group: durable assistant message append to Postgres."""


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


class RequestCompletedEvent(EventBase):
    """Published when a chat request finishes (response ready).

    Consumers: ``cg:es-indexer`` (telemetry), ``cg:session-writer`` (DB append).
    Carries a timer snapshot for ES; identifiers only otherwise (ADR-0041).

    Attributes:
        trace_id: Request trace identifier.
        session_id: Session that originated the request.
        assistant_response: Assistant reply text to persist.
        trace_summary: Output of ``RequestTimer.to_trace_summary()`` at publish time.
        trace_breakdown: Output of ``RequestTimer.to_breakdown()`` at publish time.
    """

    event_type: Literal["request.completed"] = "request.completed"
    trace_id: str
    session_id: str
    assistant_response: str
    trace_summary: dict[str, Any]
    trace_breakdown: list[dict[str, Any]]


def parse_stream_event(payload: dict[str, Any]) -> EventBase:
    """Deserialize a stream JSON payload into the correct event subclass.

    ``EventBase.model_validate`` drops subclass fields; this dispatches on
    ``event_type`` so consumers receive full models.

    Args:
        payload: Decoded JSON object from the Redis ``data`` field.

    Returns:
        Concrete event instance.

    Raises:
        ValueError: If ``event_type`` is missing or unknown.
    """
    raw_type = payload.get("event_type")
    if raw_type == "request.captured":
        return RequestCapturedEvent.model_validate(payload)
    if raw_type == "request.completed":
        return RequestCompletedEvent.model_validate(payload)
    raise ValueError(f"unknown event_type: {raw_type!r}")
