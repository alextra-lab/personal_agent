"""Elasticsearch logger for structured events."""

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch

else:
    AsyncElasticsearch = Any  # noqa: A001

from personal_agent.telemetry import get_logger
from personal_agent.telemetry.redaction import redact_mapping

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
            from elasticsearch import AsyncElasticsearch as ESClient

            # Configure connection pool and timeouts to prevent connection exhaustion
            self.client = ESClient(
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

    def current_index_name(self) -> str:
        """Return the index name for *now*, with month suffix (FRE-1036).

        Public because delivery is queued (FRE-1055): the handler resolves the
        destination when the record is emitted, not when it is finally written,
        so a backlog draining across a month boundary still lands in the month
        the events belong to.

        Returns:
            Index name, e.g. ``agent-logs-2026-08``.
        """
        month_str = datetime.utcnow().strftime("%Y-%m")
        return f"{self.index_prefix}-{month_str}"

    async def _index_agent_log(
        self,
        document: dict[str, Any],
        *,
        id: str | None = None,
        index: str | None = None,
    ) -> str | None:
        """Index one document into the agent-logs family, redacting it first.

        The single write chokepoint for ``agent-logs-*`` (FRE-1068). Every
        caller writing to this family must route through here: the audit found
        five independent write paths, four of which bypassed ``log_event`` and
        so bypassed any guarantee stated on it. ``redact_mapping`` governs what
        is stored, which the index template cannot — the template controls
        searchability, while ``_source`` retains whatever was submitted.

        ``index_document`` is deliberately *not* routed here: it writes the
        Captain's Log named indices, a different family under a different
        template and retention policy.

        A structural test (``test_no_agent_logs_write_bypasses_the_chokepoint``)
        fails if a new write path reaches this index without passing through.

        Args:
            document: Document to index; redacted in place of the original.
            id: Optional document ID for idempotent upsert.
            index: Destination index; defaults to :meth:`current_index_name`.
                Passed explicitly by the queued delivery path (FRE-1055), which
                resolves the destination at emission time. It stays inside this
                seam — every write still redacts — so it does not widen the
                FRE-1068 chokepoint, only the choice of month within the family.

        Returns:
            Document ID if the write succeeded, None if no client is attached.

        Raises:
            Exception: Propagated from the Elasticsearch client. Write failures
                are deliberately not swallowed here — each calling path already
                logs them with its own event name and context, and catching
                them centrally would erase that attribution.
        """
        if not self.client:
            return None
        kwargs: dict[str, Any] = {
            "index": index if index is not None else self.current_index_name(),
            "document": redact_mapping(document),
        }
        if id is not None:
            kwargs["id"] = id
        result = await self.client.index(**kwargs)
        return str(result["_id"])

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

    async def update_by_query(
        self,
        index_pattern: str,
        query: dict[str, Any],
        script_source: str,
        params: dict[str, Any],
    ) -> int:
        """Partial-update matching documents via a Painless script.

        Best-effort: returns 0 (never raises) when not connected or on any
        client error, mirroring :meth:`index_document`'s failure handling.

        Args:
            index_pattern: Index or index pattern to search (e.g. ``agent-insights-*``).
            query: ES query DSL dict selecting the documents to update.
            script_source: Painless script source (e.g.
                ``"ctx._source.linear_issue_id = params.linear_issue_id"``).
            params: Script parameters.

        Returns:
            Number of documents updated, or 0 if not connected or on failure.
        """
        if not self.client:
            log.warning("elasticsearch_not_connected", index=index_pattern)
            return 0
        try:
            result = await self.client.update_by_query(
                index=index_pattern,
                query=query,
                script={"source": script_source, "params": params, "lang": "painless"},
                conflicts="proceed",
            )
            return int(result.get("updated", 0))
        except Exception as e:
            log.warning("elasticsearch_update_by_query_failed", index=index_pattern, error=str(e))
            return 0

    async def log_event(
        self,
        event_type: str,
        data: dict[str, Any],
        trace_id: UUID | str | None = None,
        span_id: str | None = None,
        index: str | None = None,
        timestamp: str | None = None,
    ) -> str | None:
        """Log a structured event to Elasticsearch.

        Args:
            event_type: Type of event (e.g., 'task_started', 'tool_executed')
            data: Event data (will be indexed)
            trace_id: Optional trace ID for correlation
            span_id: Optional span ID
            index: Destination index; defaults to the current month's. The
                queued delivery path (FRE-1055) passes the index it resolved at
                emission time so a drained backlog is not misfiled.
            timestamp: ``@timestamp`` for the document; defaults to now. Passed
                by the queued delivery path for the same reason as ``index``:
                stamping at write time would date a drained backlog to the
                drain rather than to when the events happened.

        Returns:
            Document ID if successful, None if failed
        """
        if not self.client:
            log.warning(
                "elasticsearch_not_connected",
                event=event_type,
                trace_id=str(trace_id) if trace_id else None,
            )
            return None

        doc = {
            "@timestamp": timestamp if timestamp is not None else datetime.utcnow().isoformat(),
            "event_type": event_type,
            "trace_id": str(trace_id) if trace_id else None,
            "span_id": span_id,
            **data,
        }

        try:
            return await self._index_agent_log(doc, index=index)
        except Exception as e:
            log.error(
                "elasticsearch_log_failed",
                event=event_type,
                error=str(e),
                trace_id=str(trace_id) if trace_id else None,
            )
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

        index_name = self.current_index_name()
        actions = [
            {
                "_index": index_name,
                # FRE-1068: the bulk path writes agent-logs directly rather
                # than through _index_agent_log, so it redacts here.
                "_source": redact_mapping(
                    {
                        "@timestamp": datetime.utcnow().isoformat(),
                        "event_type": event_type,
                        "trace_id": str(trace_id) if trace_id else None,
                        **data,
                    }
                ),
            }
            for event_type, data, trace_id in events
        ]

        try:
            success, _ = await async_bulk(self.client, actions)
            return success
        except Exception as e:
            log.error(  # trace-allow: batch-level sink failure, no single trace_id for the bulk
                "elasticsearch_bulk_failed", error=str(e)
            )
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
            log.error(
                "elasticsearch_search_failed",
                error=str(e),
                trace_id=str(trace_id) if trace_id else None,
            )
            return []

    async def index_request_trace(
        self,
        trace_id: str,
        timer: object,
        session_id: str | None = None,
        user_id: UUID | str | None = None,
    ) -> str | None:
        """Index request trace summary and per-step documents for Kibana.

        One request_trace document (summary) and one request_trace_step per
        completed span. Document IDs are idempotent: trace_{trace_id},
        trace_{trace_id}_step_{sequence}.

        Args:
            trace_id: Trace ID for the completed request.
            timer: RequestTimer after request completion (to_trace_summary
                and spans used).
            session_id: Optional session ID for filtering.
            user_id: Authenticated user UUID for the request (ADR-0107 D5).
                This dict-based indexing path bypasses structlog entirely, so
                it does not inherit ``structlog.contextvars`` bindings and
                needs ``user_id`` threaded explicitly, same as ``session_id``.

        Returns:
            Document ID of the request_trace doc if successful, None otherwise.
        """
        if not self.client:
            return None

        from personal_agent.telemetry.request_timer import RequestTimer as RT

        if not isinstance(timer, RT):
            log.warning("index_request_trace_invalid_timer", trace_id=trace_id)
            return None

        summary = timer.to_trace_summary()
        breakdown = timer.to_breakdown()
        return await self.index_request_trace_from_snapshot(
            trace_id=trace_id,
            trace_summary=summary,
            trace_breakdown=breakdown,
            session_id=session_id,
            user_id=user_id,
        )

    async def index_request_trace_from_snapshot(
        self,
        trace_id: str,
        trace_summary: dict[str, Any],
        trace_breakdown: list[dict[str, Any]],
        session_id: str | None = None,
        user_id: UUID | str | None = None,
    ) -> str | None:
        """Index request trace from a timer snapshot (Redis event consumer path).

        Same documents as ``index_request_trace`` — idempotent IDs
        ``trace_{trace_id}`` and ``trace_{trace_id}_step_{sequence}``.

        Args:
            trace_id: Request trace identifier.
            trace_summary: Dict from ``RequestTimer.to_trace_summary()``.
            trace_breakdown: List from ``RequestTimer.to_breakdown()``.
            session_id: Optional session ID for filtering.
            user_id: Authenticated user UUID for the request (ADR-0107 D5),
                threaded from ``RequestCompletedEvent.user_id``.

        Returns:
            Document ID of the request_trace summary doc if successful, else None.
        """
        if not self.client:
            return None

        total_duration_ms = trace_summary.get("total_duration_ms")
        if total_duration_ms is None:
            total_duration_ms = 0.0
        ts = datetime.utcnow().isoformat()
        index_name = self.current_index_name()
        user_id_str = str(user_id) if user_id else None

        trace_doc: dict[str, Any] = {
            "@timestamp": ts,
            "event_type": "request_trace",
            "trace_id": trace_id,
            "session_id": session_id,
            "user_id": user_id_str,
            "total_duration_ms": total_duration_ms,
            "total_steps": trace_summary.get("total_steps", 0),
            "phases_summary": trace_summary.get("phases_summary") or {},
        }

        try:
            written = await self._index_agent_log(trace_doc, id=f"trace_{trace_id}")
            if written is None:
                return None
            doc_id = written

            for entry in trace_breakdown:
                if entry.get("phase") == "total":
                    continue
                step_doc: dict[str, Any] = {
                    "@timestamp": ts,
                    "event_type": "request_trace_step",
                    "trace_id": trace_id,
                    "session_id": session_id,
                    "user_id": user_id_str,
                    "sequence": entry.get("sequence", 0),
                    "phase": entry.get("phase", ""),
                    "name": entry.get("name", ""),
                    "offset_ms": entry.get("offset_ms", 0.0),
                    "duration_ms": entry.get("duration_ms", 0.0),
                    "total_duration_ms": total_duration_ms,
                }
                meta = entry.get("metadata")
                if meta and isinstance(meta, dict):
                    for k, v in meta.items():
                        if k not in step_doc and v is not None:
                            step_doc[k] = v
                step_id = f"trace_{trace_id}_step_{entry.get('sequence', 0)}"
                try:
                    await self._index_agent_log(step_doc, id=step_id)
                except Exception as step_e:
                    log.warning(
                        "elasticsearch_index_failed",
                        index=index_name,
                        event="request_trace_step",
                        sequence=entry.get("sequence"),
                        error=str(step_e),
                        trace_id=trace_id,
                    )

            return doc_id
        except Exception as e:
            log.warning(
                "elasticsearch_index_failed",
                index=index_name,
                event="request_trace",
                error=str(e),
                trace_id=trace_id,
            )
            return None

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
            log.warning(
                "elasticsearch_not_connected",
                event="request_latency_breakdown",
                trace_id=trace_id,
            )
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

        index_name = self.current_index_name()
        try:
            written = await self._index_agent_log(doc, id=trace_id)
            if written is None:
                return None
            doc_id = written

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
                    await self._index_agent_log(flat_doc, id=flat_id)
                except Exception as flat_e:
                    log.warning(
                        "elasticsearch_index_failed",
                        index=index_name,
                        event="request_latency_phase",
                        phase=phase_name,
                        error=str(flat_e),
                        trace_id=trace_id,
                    )

            return doc_id
        except Exception as e:
            log.warning(
                "elasticsearch_index_failed",
                index=index_name,
                event="request_latency_breakdown",
                error=str(e),
                trace_id=trace_id,
            )
            return None
