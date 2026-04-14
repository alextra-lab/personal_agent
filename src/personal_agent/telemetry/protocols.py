"""ADR-0049 Phase 1: Protocol interfaces for telemetry sinks and metrics.

Defines structural contracts for trace event emission (Elasticsearch) and
metrics collection. Consumers depend on these protocols rather than
ElasticsearchLogger or MetricsDaemon directly.

See: docs/architecture_decisions/ADR-0049.md
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID


class TraceSinkProtocol(Protocol):
    """Protocol for structured trace event emission.

    Structural contract for ElasticsearchLogger (and future) trace backends.
    All structured events emitted during request processing flow through
    implementations of this protocol.

    Key invariants:
        - ``log_event`` is best-effort and never raises to callers.
        - ``search_events`` returns an empty list (not an exception) when
          no events match or the backend is unavailable.
        - ``index_document`` is idempotent when called with the same ``doc_id``.
    """

    async def log_event(
        self,
        event_type: str,
        data: Mapping[str, Any],
        trace_id: UUID | str | None,
        span_id: str | None,
    ) -> str | None:
        """Log a structured event to the trace store.

        Args:
            event_type: Semantic event type (e.g. ``"task_started"``).
            data: Event payload — must be JSON-serialisable.
            trace_id: Trace identifier for request correlation (optional).
            span_id: Span identifier for nested operations (optional).

        Returns:
            Backend-assigned document identifier if successful, None on failure.
        """
        ...

    async def search_events(
        self,
        event_type: str | None,
        trace_id: UUID | str | None,
        start_time: datetime | None,
        end_time: datetime | None,
        query_text: str | None,
        limit: int,
    ) -> Sequence[Mapping[str, Any]]:
        """Search stored events with optional filters.

        Args:
            event_type: Filter by semantic event type (None = any type).
            trace_id: Filter by trace identifier (None = any trace).
            start_time: Inclusive lower bound on event timestamp (None = no bound).
            end_time: Inclusive upper bound on event timestamp (None = no bound).
            query_text: Full-text filter applied across all event fields.
            limit: Maximum number of results to return.

        Returns:
            Sequence of event mappings ordered by timestamp descending.
        """
        ...

    async def index_document(
        self,
        index_name: str,
        document: Mapping[str, Any],
        *,
        id: str | None,
    ) -> str | None:
        """Index a document into a named index (idempotent when id is provided).

        Args:
            index_name: Target index name (e.g. ``"agent-captains-captures-2026-04-14"``).
            document: JSON-serialisable document payload.
            id: Optional document identifier. When provided, repeated calls
                overwrite the same document (used for backfill replay).

        Returns:
            Backend-assigned document identifier if successful, None on failure.
        """
        ...


class MetricsCollectorProtocol(Protocol):
    """Protocol for metrics aggregation and recording.

    Structural contract for any metrics backend (in-memory buffer, Prometheus,
    StatsD, etc.). All numeric signals from brainstem sensors and request
    timing flow through this protocol.

    Key invariants:
        - ``record`` is synchronous and must not block the event loop.
        - ``tags`` values are opaque strings; implementations must not interpret them.
    """

    def record(
        self,
        metric: str,
        value: float,
        tags: Mapping[str, str],
    ) -> None:
        """Record a single metric observation.

        Args:
            metric: Metric name in dot-separated namespace format
                (e.g. ``"request.latency_ms"``).
            value: Numeric value for this observation.
            tags: Arbitrary string key-value pairs for dimensional aggregation
                (e.g. ``{"mode": "work", "tool": "read_file"}``).
        """
        ...
