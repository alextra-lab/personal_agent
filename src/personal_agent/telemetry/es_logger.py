"""Elasticsearch logger for structured events."""

from datetime import datetime
from typing import Any
from uuid import UUID

from elasticsearch import AsyncElasticsearch

from personal_agent.telemetry import get_logger

log = get_logger(__name__)


class ElasticsearchLogger:
    """Async Elasticsearch logger for structured events.

    Usage:
        es_logger = ElasticsearchLogger("http://localhost:9200")
        await es_logger.connect()
        await es_logger.log_event("task_started", {"task_id": "123"})
    """

    def __init__(self, es_url: str = "http://localhost:9200", index_prefix: str = "agent-logs"):  # noqa: D107
        """Initialize Elasticsearch logger with connection URL and index prefix."""
        self.es_url = es_url
        self.index_prefix = index_prefix
        self.client: AsyncElasticsearch | None = None

    async def connect(self) -> bool:
        """Connect to Elasticsearch.

        Returns:
            True if connected successfully
        """
        try:
            # Configure connection pool and timeouts to prevent connection exhaustion
            self.client = AsyncElasticsearch(
                [self.es_url],
                request_timeout=30,  # Allow slower local ES under heavy concurrent writes
                max_retries=2,  # Retry failed requests twice
                retry_on_timeout=True,
                # Connection pooling
                connections_per_node=20,  # Allow more concurrent connections
            )
            info = await self.client.info()
            log.info("elasticsearch_connected", version=info["version"]["number"])
            return True
        except Exception as e:
            log.error("elasticsearch_connection_failed", error=str(e))
            return False

    async def disconnect(self) -> None:
        """Close Elasticsearch connection."""
        if self.client:
            await self.client.close()
            self.client = None

    def _get_index_name(self) -> str:
        """Get index name with date suffix (daily rotation)."""
        date_str = datetime.utcnow().strftime("%Y.%m.%d")
        return f"{self.index_prefix}-{date_str}"

    async def index_document(
        self,
        index_name: str,
        document: dict[str, Any],
        *,
        id: str | None = None,
    ) -> str | None:
        """Index a document into a named index (e.g. Captain's Log indices).

        When id is provided, indexing is idempotent: repeated index calls
        overwrite the same document (used for backfill replay).

        Args:
            index_name: Full index name (e.g. 'agent-captains-captures-2026-02-22').
            document: Document to index (must be JSON-serializable).
            id: Optional document ID for idempotent upsert (e.g. trace_id, entry_id).

        Returns:
            Document ID if successful, None if failed or not connected.
        """
        if not self.client:
            log.warning("elasticsearch_not_connected", index=index_name)
            return None
        try:
            kwargs: dict[str, Any] = {"index": index_name, "document": document}
            if id is not None:
                kwargs["id"] = id
            result = await self.client.index(**kwargs)
            return str(result["_id"])
        except Exception as e:
            log.warning("elasticsearch_index_failed", index=index_name, error=str(e))
            return None

    async def log_event(
        self,
        event_type: str,
        data: dict[str, Any],
        trace_id: UUID | str | None = None,
        span_id: str | None = None,
    ) -> str | None:
        """Log a structured event to Elasticsearch.

        Args:
            event_type: Type of event (e.g., 'task_started', 'tool_executed')
            data: Event data (will be indexed)
            trace_id: Optional trace ID for correlation
            span_id: Optional span ID

        Returns:
            Document ID if successful, None if failed
        """
        if not self.client:
            log.warning("elasticsearch_not_connected", event=event_type)
            return None

        doc = {
            "@timestamp": datetime.utcnow().isoformat(),
            "event_type": event_type,
            "trace_id": str(trace_id) if trace_id else None,
            "span_id": span_id,
            **data,
        }

        try:
            result = await self.client.index(index=self._get_index_name(), document=doc)
            return str(result["_id"])
        except Exception as e:
            log.error("elasticsearch_log_failed", event=event_type, error=str(e))
            return None

    async def log_batch(self, events: list[tuple[str, dict[str, Any], UUID | None]]) -> int:
        """Log multiple events efficiently.

        Args:
            events: List of (event_type, data, trace_id) tuples

        Returns:
            Number of events logged successfully
        """
        if not self.client:
            return 0

        from elasticsearch.helpers import async_bulk

        index_name = self._get_index_name()
        actions = [
            {
                "_index": index_name,
                "_source": {
                    "@timestamp": datetime.utcnow().isoformat(),
                    "event_type": event_type,
                    "trace_id": str(trace_id) if trace_id else None,
                    **data,
                },
            }
            for event_type, data, trace_id in events
        ]

        try:
            success, _ = await async_bulk(self.client, actions)
            return success
        except Exception as e:
            log.error("elasticsearch_bulk_failed", error=str(e))
            return 0

    async def search_events(
        self,
        event_type: str | None = None,
        trace_id: UUID | str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        query_text: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Search events with filters.

        Args:
            event_type: Filter by event type
            trace_id: Filter by trace ID
            start_time: Start of time range
            end_time: End of time range
            query_text: Full-text search query
            limit: Maximum results

        Returns:
            List of matching events
        """
        if not self.client:
            return []

        must_clauses: list[dict[str, Any]] = []

        if event_type:
            must_clauses.append({"term": {"event_type": event_type}})
        if trace_id:
            must_clauses.append({"term": {"trace_id": str(trace_id)}})
        if start_time or end_time:
            range_clause: dict[str, dict[str, dict[str, str]]] = {"range": {"@timestamp": {}}}
            if start_time:
                range_clause["range"]["@timestamp"]["gte"] = start_time.isoformat()
            if end_time:
                range_clause["range"]["@timestamp"]["lte"] = end_time.isoformat()
            must_clauses.append(range_clause)
        if query_text:
            must_clauses.append({"query_string": {"query": query_text}})

        query = {"bool": {"must": must_clauses}} if must_clauses else {"match_all": {}}

        try:
            result = await self.client.search(
                index=f"{self.index_prefix}-*",
                query=query,
                size=limit,
                sort=[{"@timestamp": "desc"}],
            )
            return [hit["_source"] for hit in result["hits"]["hits"]]
        except Exception as e:
            log.error("elasticsearch_search_failed", error=str(e))
            return []

    async def index_latency_breakdown(
        self,
        trace_id: str,
        breakdown: list[dict[str, Any]],
        session_id: str | None = None,
    ) -> str | None:
        """Index a request-to-reply latency breakdown for dashboarding.

        Call this after a request completes so Kibana can aggregate by phase
        (entry_to_task, init, planning, llm_call, etc.) and show total
        request-to-reply duration over time.

        Args:
            trace_id: Trace ID for the completed request.
            breakdown: Result of get_request_latency_breakdown(trace_id) from
                telemetry.metrics (list of phase dicts with phase, duration_ms,
                start_time, end_time, description).
            session_id: Optional session ID for filtering.

        Returns:
            Document ID if successful, None otherwise.
        """
        if not self.client:
            log.warning("elasticsearch_not_connected", event="request_latency_breakdown")
            return None
        if not breakdown:
            return None

        total_row = next(
            (r for r in breakdown if r.get("phase") == "total_request_to_reply"),
            None,
        )
        total_duration_ms = total_row.get("duration_ms") if total_row else None

        phases_payload: list[dict[str, Any]] = []
        for row in breakdown:
            phase = row.get("phase")
            if phase and phase != "total_request_to_reply":
                dur = row.get("duration_ms")
                phases_payload.append(
                    {
                        "phase": phase,
                        "duration_ms": float(dur) if dur is not None else None,
                        "start_time": row.get("start_time"),
                        "end_time": row.get("end_time"),
                        "description": (row.get("description") or "")[:500],
                    }
                )

        doc: dict[str, Any] = {
            "@timestamp": datetime.utcnow().isoformat(),
            "event_type": "request_latency_breakdown",
            "trace_id": trace_id,
            "session_id": session_id,
            "total_duration_ms": total_duration_ms,
            "phases": phases_payload,
        }

        index_name = self._get_index_name()
        try:
            result = await self.client.index(
                index=index_name,
                document=doc,
                id=trace_id,
            )
            doc_id = str(result["_id"])

            # Index one flat doc per phase so Kibana can aggregate without nested agg
            ts = datetime.utcnow().isoformat()
            for row in phases_payload:
                phase_name = row.get("phase")
                dur = row.get("duration_ms")
                if phase_name is None:
                    continue
                flat_doc: dict[str, Any] = {
                    "@timestamp": ts,
                    "event_type": "request_latency_phase",
                    "trace_id": trace_id,
                    "session_id": session_id,
                    "phase": phase_name,
                    "duration_ms": dur,
                }
                flat_id = f"{trace_id}_{phase_name}"
                try:
                    await self.client.index(
                        index=index_name,
                        document=flat_doc,
                        id=flat_id,
                    )
                except Exception as flat_e:
                    log.warning(
                        "elasticsearch_index_failed",
                        index=index_name,
                        event="request_latency_phase",
                        phase=phase_name,
                        error=str(flat_e),
                    )

            return doc_id
        except Exception as e:
            log.warning(
                "elasticsearch_index_failed",
                index=index_name,
                event="request_latency_breakdown",
                error=str(e),
            )
            return None
