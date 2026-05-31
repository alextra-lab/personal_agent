"""Elasticsearch analytics queries for adaptive threshold tuning.

This module provides reusable, typed query helpers for telemetry data used by
the threshold optimizer (FRE-11).
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch
else:
    AsyncElasticsearch = Any

from personal_agent.config.settings import get_settings
from personal_agent.events.models import ErrorPatternCluster
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

MODE_TRANSITION_EVENT = "mode_transition"
CONSOLIDATION_EVENT = "consolidation_triggered"
REQUEST_METRICS_EVENT = "request_metrics_summary"

METRIC_FIELD_MAP: dict[str, str] = {
    "cpu": "cpu_avg",
    "cpu_avg": "cpu_avg",
    "memory": "memory_avg",
    "memory_avg": "memory_avg",
    "duration_ms": "duration_ms",
}


@dataclass(frozen=True)
class ModeTransition:
    """Mode transition record from telemetry."""

    timestamp: datetime
    from_mode: str
    to_mode: str
    reason: str
    trace_id: str | None = None


@dataclass(frozen=True)
class ConsolidationEvent:
    """Consolidation trigger record from telemetry."""

    timestamp: datetime
    trace_id: str | None = None
    cpu_percent: float | None = None
    memory_percent: float | None = None
    idle_seconds: float | None = None


@dataclass(frozen=True)
class TaskPatternReport:
    """Aggregated task execution patterns from Captain's Log captures."""

    total_tasks: int
    completed_tasks: int
    success_rate: float
    avg_duration_ms: float
    most_used_tools: list[str]
    hourly_distribution: dict[int, int]
    avg_cpu_percent: float
    avg_memory_percent: float


class TelemetryQueries:
    """Common analytics queries for Elasticsearch telemetry data."""

    def __init__(self, es_client: AsyncElasticsearch | None = None) -> None:
        """Initialize query service.

        Args:
            es_client: Optional preconfigured Elasticsearch client.
        """
        settings = get_settings()
        self._es_client = es_client
        self._client_owned = es_client is None
        self._logs_index_prefix = settings.elasticsearch_index_prefix
        self._captures_index_prefix = f"{settings.captains_log_index_prefix}-captures"

    async def _get_client(self) -> AsyncElasticsearch:
        """Get active Elasticsearch client, creating one if needed."""
        if self._es_client is None:
            settings = get_settings()
            try:
                from elasticsearch import AsyncElasticsearch as ESClient
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "elasticsearch package is required to create a telemetry query client"
                ) from exc
            self._es_client = ESClient([settings.elasticsearch_url], request_timeout=30)
        return self._es_client

    async def disconnect(self) -> None:
        """Close owned Elasticsearch client."""
        if self._client_owned and self._es_client is not None:
            await self._es_client.close()
            self._es_client = None

    async def get_event_count(self, event_type: str, days: int) -> int:
        """Count telemetry events of a given type in a time window.

        Args:
            event_type: Event type (structlog event name).
            days: Number of days to analyze.

        Returns:
            Matching event count.
        """
        client = await self._get_client()
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days)
        response = await client.search(
            index=f"{self._logs_index_prefix}-*",
            query={
                "bool": {
                    "filter": [
                        {"term": {"event_type": event_type}},
                        {
                            "range": {
                                "@timestamp": {"gte": start.isoformat(), "lte": now.isoformat()}
                            }
                        },
                    ]
                }
            },
            size=0,
        )
        return int(response.get("hits", {}).get("total", {}).get("value", 0) or 0)

    async def get_daily_event_counts(self, event_type: str, days: int) -> dict[str, int]:
        """Get daily buckets for a telemetry event type.

        Args:
            event_type: Event type (structlog event name).
            days: Number of days to analyze.

        Returns:
            Dict keyed by `YYYY-MM-DD` to event count.
        """
        client = await self._get_client()
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days)
        response = await client.search(
            index=f"{self._logs_index_prefix}-*",
            query={
                "bool": {
                    "filter": [
                        {"term": {"event_type": event_type}},
                        {
                            "range": {
                                "@timestamp": {"gte": start.isoformat(), "lte": now.isoformat()}
                            }
                        },
                    ]
                }
            },
            size=0,
            aggs={
                "daily": {
                    "date_histogram": {
                        "field": "@timestamp",
                        "calendar_interval": "day",
                        "min_doc_count": 0,
                    }
                }
            },
        )
        daily_counts: dict[str, int] = {}
        for bucket in response.get("aggregations", {}).get("daily", {}).get("buckets", []):
            key_as_string = bucket.get("key_as_string")
            if not key_as_string:
                continue
            day = str(key_as_string).split("T")[0]
            daily_counts[day] = int(bucket.get("doc_count", 0) or 0)
        return daily_counts

    async def get_resource_percentiles(self, metric: str, days: int) -> dict[str, float]:
        """Get p50, p75, p90, p95, and p99 for a resource metric.

        Args:
            metric: Metric alias or field name.
            days: Number of days to analyze.

        Returns:
            Percentiles keyed by p50/p75/p90/p95/p99.
        """
        field_name = METRIC_FIELD_MAP.get(metric, metric)
        client = await self._get_client()
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days)

        query = {
            "bool": {
                "filter": [
                    {"term": {"event_type": REQUEST_METRICS_EVENT}},
                    {"range": {"@timestamp": {"gte": start.isoformat(), "lte": now.isoformat()}}},
                    {"exists": {"field": field_name}},
                ]
            }
        }

        response = await client.search(
            index=f"{self._logs_index_prefix}-*",
            query=query,
            size=0,
            aggs={
                "percentiles": {
                    "percentiles": {
                        "field": field_name,
                        "percents": [50, 75, 90, 95, 99],
                    }
                }
            },
        )

        values = response.get("aggregations", {}).get("percentiles", {}).get("values", {})
        return {
            "p50": float(values.get("50.0", 0.0) or 0.0),
            "p75": float(values.get("75.0", 0.0) or 0.0),
            "p90": float(values.get("90.0", 0.0) or 0.0),
            "p95": float(values.get("95.0", 0.0) or 0.0),
            "p99": float(values.get("99.0", 0.0) or 0.0),
        }

    async def get_mode_transitions(self, days: int) -> list[ModeTransition]:
        """Get mode transitions with transition context.

        Args:
            days: Number of days to analyze.

        Returns:
            Mode transition records ordered by newest first.
        """
        client = await self._get_client()
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days)
        response = await client.search(
            index=f"{self._logs_index_prefix}-*",
            query={
                "bool": {
                    "filter": [
                        {"term": {"event_type": MODE_TRANSITION_EVENT}},
                        {
                            "range": {
                                "@timestamp": {"gte": start.isoformat(), "lte": now.isoformat()}
                            }
                        },
                    ]
                }
            },
            size=1000,
            sort=[{"@timestamp": "desc"}],
        )

        transitions: list[ModeTransition] = []
        for hit in response.get("hits", {}).get("hits", []):
            source = hit.get("_source", {})
            timestamp = _parse_timestamp(source.get("@timestamp"))
            if timestamp is None:
                continue
            transitions.append(
                ModeTransition(
                    timestamp=timestamp,
                    from_mode=str(source.get("from_mode", "unknown")),
                    to_mode=str(source.get("to_mode", "unknown")),
                    reason=str(source.get("reason", "unspecified")),
                    trace_id=_coerce_optional_string(source.get("trace_id")),
                )
            )
        return transitions

    async def get_consolidation_triggers(self, days: int) -> list[ConsolidationEvent]:
        """Get consolidation trigger events.

        Args:
            days: Number of days to analyze.

        Returns:
            Consolidation trigger events ordered by newest first.
        """
        client = await self._get_client()
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days)

        response = await client.search(
            index=f"{self._logs_index_prefix}-*",
            query={
                "bool": {
                    "filter": [
                        {"term": {"event_type": CONSOLIDATION_EVENT}},
                        {
                            "range": {
                                "@timestamp": {"gte": start.isoformat(), "lte": now.isoformat()}
                            }
                        },
                    ]
                }
            },
            size=1000,
            sort=[{"@timestamp": "desc"}],
        )

        events: list[ConsolidationEvent] = []
        for hit in response.get("hits", {}).get("hits", []):
            source = hit.get("_source", {})
            timestamp = _parse_timestamp(source.get("@timestamp"))
            if timestamp is None:
                continue
            events.append(
                ConsolidationEvent(
                    timestamp=timestamp,
                    trace_id=_coerce_optional_string(source.get("trace_id")),
                    cpu_percent=_coerce_optional_float(source.get("cpu_load"))
                    or _coerce_optional_float(source.get("cpu_avg")),
                    memory_percent=_coerce_optional_float(source.get("memory_used"))
                    or _coerce_optional_float(source.get("memory_avg")),
                    idle_seconds=_coerce_optional_float(source.get("idle_time")),
                )
            )
        return events

    async def get_task_patterns(self, days: int) -> TaskPatternReport:
        """Analyze task execution patterns from capture indices.

        Args:
            days: Number of days to analyze.

        Returns:
            Aggregated task pattern report.
        """
        client = await self._get_client()
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days)

        response = await client.search(
            index=f"{self._captures_index_prefix}-*",
            query={
                "bool": {
                    "filter": [
                        {
                            "range": {
                                "timestamp": {"gte": start.isoformat(), "lte": now.isoformat()}
                            }
                        },
                    ]
                }
            },
            size=0,
            aggs={
                "total": {"value_count": {"field": "trace_id.keyword"}},
                "completed": {"filter": {"term": {"outcome.keyword": "completed"}}},
                "avg_duration_ms": {"avg": {"field": "duration_ms"}},
                "avg_cpu": {"avg": {"field": "metrics_summary.cpu_avg"}},
                "avg_memory": {"avg": {"field": "metrics_summary.memory_avg"}},
                "top_tools": {"terms": {"field": "tools_used.keyword", "size": 5}},
                "hours": {
                    "terms": {
                        "script": {
                            "lang": "painless",
                            "source": "doc['timestamp'].value.getHour()",
                        },
                        "size": 24,
                    }
                },
            },
        )

        aggs = response.get("aggregations", {})
        total_tasks = int(aggs.get("total", {}).get("value", 0) or 0)
        completed_tasks = int(aggs.get("completed", {}).get("doc_count", 0) or 0)
        success_rate = float(completed_tasks / total_tasks) if total_tasks > 0 else 0.0
        top_tools = [
            str(bucket.get("key"))
            for bucket in aggs.get("top_tools", {}).get("buckets", [])
            if bucket.get("key")
        ]
        hourly_distribution: dict[int, int] = {}
        for bucket in aggs.get("hours", {}).get("buckets", []):
            key = bucket.get("key")
            count = bucket.get("doc_count", 0)
            if key is None:
                continue
            hourly_distribution[int(key)] = int(count or 0)

        return TaskPatternReport(
            total_tasks=total_tasks,
            completed_tasks=completed_tasks,
            success_rate=success_rate,
            avg_duration_ms=float(aggs.get("avg_duration_ms", {}).get("value", 0.0) or 0.0),
            most_used_tools=top_tools,
            hourly_distribution=hourly_distribution,
            avg_cpu_percent=float(aggs.get("avg_cpu", {}).get("value", 0.0) or 0.0),
            avg_memory_percent=float(aggs.get("avg_memory", {}).get("value", 0.0) or 0.0),
        )

    async def get_delegation_pattern_buckets(self, days: int) -> dict[str, Any]:
        """Aggregate delegation_outcome_recorded events over trailing ``days``.

        Queries agent-logs-* for events with event == "delegation_outcome_recorded"
        and returns aggregate stats: total count, success count, distribution of
        rounds_needed, and a terms aggregation on what_was_missing.

        Args:
            days: Rolling look-back window size in days.

        Returns:
            Dict with keys:
              - ``total`` (int): total delegation records found
              - ``successes`` (int): sum of success=True records
              - ``rounds_needed_values`` (list[int]): full distribution (one value per record)
              - ``missing_context_terms`` (list[tuple[str, int]]): (term, count) pairs,
                sorted descending by count, lowercased + truncated to 80 chars
        """
        client = await self._get_client()
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days)

        response = await client.search(
            index=f"{self._logs_index_prefix}-*",
            query={
                "bool": {
                    "filter": [
                        {
                            "range": {
                                "@timestamp": {
                                    "gte": start.isoformat(),
                                    "lte": now.isoformat(),
                                }
                            }
                        },
                        {"term": {"event.keyword": "delegation_outcome_recorded"}},
                    ]
                }
            },
            size=0,
            aggs={
                "total": {"value_count": {"field": "task_id.keyword"}},
                "successes": {"sum": {"field": "success"}},
                "rounds_histogram": {
                    "histogram": {"field": "rounds_needed", "interval": 1, "min_doc_count": 1}
                },
                "missing_context_terms": {
                    "terms": {
                        "script": {
                            "source": (
                                "def v = doc['what_was_missing.keyword'].size() == 0 "
                                "? '' : doc['what_was_missing.keyword'].value; "
                                "return v.toLowerCase().substring(0, Math.min(v.length(), 80));"
                            )
                        },
                        "size": 20,
                        "min_doc_count": 1,
                    }
                },
            },
        )
        aggs = response.get("aggregations", {})
        total = int(aggs.get("total", {}).get("value", 0) or 0)
        successes = int(aggs.get("successes", {}).get("value", 0) or 0)
        rounds_values: list[int] = []
        for bucket in aggs.get("rounds_histogram", {}).get("buckets", []):
            count = int(bucket.get("doc_count", 0) or 0)
            rounds = int(bucket.get("key", 0) or 0)
            rounds_values.extend([rounds] * count)
        missing_terms: list[tuple[str, int]] = []
        for bucket in aggs.get("missing_context_terms", {}).get("buckets", []):
            term = str(bucket.get("key", "") or "").strip()
            count = int(bucket.get("doc_count", 0) or 0)
            if not term:
                continue
            missing_terms.append((term, count))
        return {
            "total": total,
            "successes": successes,
            "rounds_needed_values": rounds_values,
            "missing_context_terms": missing_terms,
        }

    async def get_missing_skill_buckets(self, days: int) -> list[tuple[str, int, int]]:
        """Aggregate ``missing_skill_requested`` events over trailing ``days`` (FRE-328).

        Queries agent-logs-* for events where ``event_type == "missing_skill_requested"``
        and groups by ``requested_name``.  Each bucket carries the total request
        count and a cardinality sub-aggregation counting distinct sessions.

        Note on field naming: ``ElasticsearchHandler.emit()`` stores the structlog
        ``event`` key under the ES field ``event_type``.  ``requested_name`` and
        ``session_id`` are mapped directly as ``keyword`` — there is no
        ``.keyword`` sub-field to query through.

        Args:
            days: Rolling look-back window size in days.

        Returns:
            List of ``(requested_name, request_count, distinct_sessions)`` tuples,
            sorted descending by request count, capped at 50 buckets.
        """
        client = await self._get_client()
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days)

        response = await client.search(
            index=f"{self._logs_index_prefix}-*",
            query={
                "bool": {
                    "filter": [
                        {
                            "range": {
                                "@timestamp": {
                                    "gte": start.isoformat(),
                                    "lte": now.isoformat(),
                                }
                            }
                        },
                        {"term": {"event_type": "missing_skill_requested"}},
                    ]
                }
            },
            size=0,
            aggs={
                "by_skill": {
                    "terms": {
                        "field": "requested_name",
                        "size": 50,
                        "min_doc_count": 1,
                    },
                    "aggs": {"distinct_sessions": {"cardinality": {"field": "session_id"}}},
                }
            },
        )
        buckets = (response.get("aggregations") or {}).get("by_skill", {}).get("buckets", [])
        results: list[tuple[str, int, int]] = []
        for bucket in buckets:
            name = str(bucket.get("key", "") or "").strip()
            if not name:
                continue
            count = int(bucket.get("doc_count", 0) or 0)
            sessions = int((bucket.get("distinct_sessions") or {}).get("value", 0) or 0)
            results.append((name, count, sessions))
        return results

    async def get_error_events(
        self,
        days: int,
        level_filter: Sequence[str] = ("ERROR",),
    ) -> list[dict[str, Any]]:
        """Fetch raw ERROR/WARNING log events from Elasticsearch.

        Args:
            days: Number of days to look back.
            level_filter: Log levels to include (default: ERROR only).

        Returns:
            List of ``_source`` dicts from matching ES hits.
        """
        client = await self._get_client()
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days)
        response = await client.search(
            index=f"{self._logs_index_prefix}-*",
            query={
                "bool": {
                    "filter": [
                        {"terms": {"level": list(level_filter)}},
                        {
                            "range": {
                                "@timestamp": {
                                    "gte": start.isoformat(),
                                    "lte": now.isoformat(),
                                }
                            }
                        },
                    ]
                }
            },
            size=1000,
            sort=[{"@timestamp": "desc"}],
        )
        return [hit["_source"] for hit in response.get("hits", {}).get("hits", [])]

    async def get_skill_index_p95_chars(self, days: int = 7) -> float:
        """Compute the p95 of ``injected_chars`` across ``skill_index_assembled`` events.

        Used by the ADR-0066 D2 threshold monitor to detect when the skill index
        is growing large enough to justify switching to ``model_decided`` routing.

        Args:
            days: Rolling look-back window in days.

        Returns:
            p95 of ``injected_chars`` over the window; ``0.0`` if no events found.
        """
        client = await self._get_client()
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days)

        response = await client.search(
            index=f"{self._logs_index_prefix}-*",
            query={
                "bool": {
                    "filter": [
                        {"term": {"event_type": "skill_index_assembled"}},
                        {
                            "range": {
                                "@timestamp": {
                                    "gte": start.isoformat(),
                                    "lte": now.isoformat(),
                                }
                            }
                        },
                        {"exists": {"field": "injected_chars"}},
                    ]
                }
            },
            size=0,
            aggs={
                "p95_chars": {
                    "percentiles": {
                        "field": "injected_chars",
                        "percents": [95],
                    }
                }
            },
        )
        values = response.get("aggregations", {}).get("p95_chars", {}).get("values", {})
        return float(values.get("95.0", 0.0) or 0.0)

    async def get_low_rating_buckets(self, days: int) -> list[dict[str, Any]]:
        """Aggregate explicit ratings + total turn counts per prompt_callsite (FRE-407).

        Two parallel ES queries are executed:

        1. ``user-turn-ratings-*`` — per-callsite sum and count of explicit user
           ratings in the window (the existing aggregation).
        2. ``agent-logs-*`` — cardinality of distinct ``trace_id`` per
           ``prompt_callsite`` among ``model_call_completed`` events in the window.
           This is the *population* of rateable turns for each callsite.

        The read-time join (ADR-0081 decision §6): ratings whose denormed
        ``prompt_callsite`` is null are joined to ``agent-logs-*`` at query time
        to recover identity.  Ratings still null after the join are bucketed as
        ``callsite="unknown"`` and excluded from per-callsite flags downstream
        (never raise an error for genuinely identity-less turns).

        Args:
            days: Rolling look-back window in days.

        Returns:
            List of dicts with keys:
              - ``callsite`` (str): prompt callsite identifier.
              - ``rated_sum`` (float): sum of explicit rating values.
              - ``rated_count`` (int): number of explicit ratings received.
              - ``total_turns`` (int): cardinality of distinct trace_ids with
                ``model_call_completed`` for this callsite in the window.
                Used by the consumer to compute the imputed mean.

            One entry per distinct callsite appearing in either index.
            Excludes the ``"unknown"`` bucket.
        """
        client = await self._get_client()
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days)

        # --- Query 1: explicit ratings from user-turn-ratings-* ---
        ratings_response = await client.search(
            index="user-turn-ratings-*",
            query={
                "range": {
                    "rated_at": {
                        "gte": start.isoformat(),
                        "lte": now.isoformat(),
                    }
                }
            },
            size=0,
            aggs={
                "by_callsite": {
                    "terms": {
                        "field": "prompt_callsite",
                        "size": 50,
                        "missing": "unknown",
                    },
                    "aggs": {
                        "sum_rating": {"sum": {"field": "rating"}},
                    },
                }
            },
        )

        # --- Query 2: total completed turns per callsite from agent-logs-* ---
        turns_response = await client.search(
            index=f"{self._logs_index_prefix}-*",
            query={
                "bool": {
                    "filter": [
                        {"term": {"event_type": "model_call_completed"}},
                        {
                            "range": {
                                "@timestamp": {
                                    "gte": start.isoformat(),
                                    "lte": now.isoformat(),
                                }
                            }
                        },
                    ]
                }
            },
            size=0,
            aggs={
                "by_callsite": {
                    "terms": {
                        "field": "prompt_callsite",
                        "size": 50,
                        "missing": "unknown",
                    },
                    "aggs": {
                        "distinct_traces": {"cardinality": {"field": "trace_id"}},
                    },
                }
            },
        )

        # Build total_turns lookup: callsite → cardinality of distinct trace_ids
        total_turns_by_callsite: dict[str, int] = {}
        for bucket in (
            turns_response.get("aggregations", {}).get("by_callsite", {}).get("buckets", [])
        ):
            callsite = str(bucket.get("key", "") or "unknown")
            total_turns_by_callsite[callsite] = int(
                (bucket.get("distinct_traces") or {}).get("value", 0) or 0
            )

        # Merge explicit ratings with total_turns; build per-callsite result dicts
        results: list[dict[str, Any]] = []
        seen_callsites: set[str] = set()

        for bucket in (
            ratings_response.get("aggregations", {}).get("by_callsite", {}).get("buckets", [])
        ):
            callsite = str(bucket.get("key", "") or "unknown")
            rated_count = int(bucket.get("doc_count", 0) or 0)
            sum_val = (bucket.get("sum_rating") or {}).get("value")
            rated_sum = float(sum_val) if sum_val is not None else 0.0
            total_turns = total_turns_by_callsite.get(callsite, 0)
            seen_callsites.add(callsite)
            results.append(
                {
                    "callsite": callsite,
                    "rated_sum": rated_sum,
                    "rated_count": rated_count,
                    "total_turns": total_turns,
                }
            )

        # Include callsites that appear only in agent-logs-* (zero ratings)
        for callsite, total_turns in total_turns_by_callsite.items():
            if callsite in seen_callsites:
                continue
            results.append(
                {
                    "callsite": callsite,
                    "rated_sum": 0.0,
                    "rated_count": 0,
                    "total_turns": total_turns,
                }
            )

        return results

    async def get_error_patterns(
        self,
        window_hours: int,
        min_occurrences: int,
        warning_allowlist: frozenset[str] = frozenset(),
    ) -> list[ErrorPatternCluster]:
        """Aggregate error events into clusters via ES composite aggregation.

        Queries ``agent-logs-*`` for ERROR events (and allowlisted WARNINGs)
        within the trailing ``window_hours``, groups by
        ``(source_component, event, error_type_normalised, level)``, applies
        the D1 out-of-scope filter, and returns clusters with
        ``occurrences >= min_occurrences``.

        Args:
            window_hours: Rolling look-back window size in hours.
            min_occurrences: Minimum event count for a cluster to qualify.
            warning_allowlist: Warning event names to include alongside errors.
                Only events in this set with ``level=WARNING`` are aggregated.
                Defaults to empty (errors only).

        Returns:
            List of ``ErrorPatternCluster`` records, one per qualifying group.
        """
        client = await self._get_client()
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=window_hours)

        should_clauses: list[dict[str, Any]] = [{"term": {"level": "ERROR"}}]
        if warning_allowlist:
            should_clauses.append(
                {
                    "bool": {
                        "must": [
                            {"term": {"level": "WARNING"}},
                            {"terms": {"event.keyword": sorted(warning_allowlist)}},
                        ]
                    }
                }
            )

        response = await client.search(
            index=f"{self._logs_index_prefix}-*",
            query={
                "bool": {
                    "filter": [
                        {
                            "range": {
                                "@timestamp": {
                                    "gte": start.isoformat(),
                                    "lte": now.isoformat(),
                                }
                            }
                        },
                    ],
                    "should": should_clauses,
                    "minimum_should_match": 1,
                }
            },
            size=0,
            aggs={
                "error_patterns": {
                    "composite": {
                        "size": 200,
                        "sources": [
                            {"source_component": {"terms": {"field": "source_component.keyword"}}},
                            {"event": {"terms": {"field": "event.keyword"}}},
                            {
                                "error_type_normalised": {
                                    "terms": {
                                        "field": "error_type.keyword",
                                        "missing_bucket": True,
                                    }
                                }
                            },
                            {"level": {"terms": {"field": "level.keyword"}}},
                        ],
                    },
                    "aggs": {
                        "first_seen": {"min": {"field": "@timestamp"}},
                        "last_seen": {"max": {"field": "@timestamp"}},
                        "sample_trace_ids": {"terms": {"field": "trace_id.keyword", "size": 5}},
                        "sample_messages": {"terms": {"field": "error.keyword", "size": 3}},
                    },
                }
            },
        )

        buckets = response.get("aggregations", {}).get("error_patterns", {}).get("buckets", [])
        clusters: list[ErrorPatternCluster] = []
        for bucket in buckets:
            key = bucket.get("key", {})
            component = str(key.get("source_component", ""))
            event_name = str(key.get("event", ""))
            error_type = str(key.get("error_type_normalised", "<no_exc>") or "<no_exc>")
            level = str(key.get("level", "ERROR"))
            doc_count = int(bucket.get("doc_count", 0) or 0)

            if doc_count < min_occurrences:
                continue
            if _is_out_of_scope(component, event_name):
                continue

            first_seen = (
                _parse_timestamp(bucket.get("first_seen", {}).get("value_as_string")) or now
            )
            last_seen = _parse_timestamp(bucket.get("last_seen", {}).get("value_as_string")) or now

            sample_trace_ids = tuple(
                str(b["key"]) for b in bucket.get("sample_trace_ids", {}).get("buckets", [])
            )[:5]
            sample_messages = tuple(
                str(b["key"]) for b in bucket.get("sample_messages", {}).get("buckets", [])
            )[:3]

            fingerprint = _compute_error_fingerprint(component, event_name, error_type)
            clusters.append(
                ErrorPatternCluster(
                    fingerprint=fingerprint,
                    component=component,
                    event_name=event_name,
                    error_type=error_type,
                    level=level,
                    occurrences=doc_count,
                    first_seen=first_seen,
                    last_seen=last_seen,
                    sample_trace_ids=sample_trace_ids,
                    sample_messages=sample_messages,
                    window_hours=window_hours,
                )
            )
        return clusters


# ---------------------------------------------------------------------------
# Error-pattern helpers (ADR-0056)
# ---------------------------------------------------------------------------

_OUT_OF_SCOPE_PREFIXES: frozenset[str] = frozenset(
    {"elastic_transport", "elasticsearch", "neo4j", "httpx", "httpcore"}
)
_OUT_OF_SCOPE_EVENT_NAMES: frozenset[str] = frozenset(
    {"elasticsearch_log_failed", "elasticsearch_bulk_failed"}
)


def _is_out_of_scope(component: str, event_name: str) -> bool:
    """Return True when a component/event should be excluded per ADR-0056 D1."""
    root = component.split(".")[0]
    if root in _OUT_OF_SCOPE_PREFIXES:
        return True
    if event_name in _OUT_OF_SCOPE_EVENT_NAMES:
        return True
    if component == "telemetry.error_monitor":
        return True
    return False


def _compute_error_fingerprint(component: str, event_name: str, error_type: str) -> str:
    """Compute a 16-char hex fingerprint from the cluster key tuple."""
    raw = f"{component}:{event_name}:{error_type}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _coerce_optional_float(value: Any) -> float | None:
    """Convert an arbitrary value into float when possible."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_string(value: Any) -> str | None:
    """Convert value to string if present."""
    if value is None:
        return None
    value_str = str(value).strip()
    return value_str if value_str else None


def _parse_timestamp(timestamp_value: Any) -> datetime | None:
    """Parse an ISO timestamp string into an aware UTC datetime."""
    if not timestamp_value:
        return None
    try:
        dt = datetime.fromisoformat(str(timestamp_value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        log.warning("invalid_telemetry_timestamp", value=str(timestamp_value))
        return None
