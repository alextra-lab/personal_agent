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
                request_timeout=10,  # 10s timeout for requests
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

    async def disconnect(self):
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
            return result["_id"]
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
            return result["_id"]
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

        must_clauses = []

        if event_type:
            must_clauses.append({"term": {"event_type": event_type}})
        if trace_id:
            must_clauses.append({"term": {"trace_id": str(trace_id)}})
        if start_time or end_time:
            range_clause = {"range": {"@timestamp": {}}}
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
