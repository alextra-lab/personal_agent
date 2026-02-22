"""Elasticsearch analytics queries for adaptive threshold tuning.

This module provides reusable, typed query helpers for telemetry data used by
the threshold optimizer (FRE-11).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch
else:
    AsyncElasticsearch = Any

from personal_agent.config.settings import get_settings
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

CAPTURES_INDEX_PREFIX = "agent-captains-captures"
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
            self._es_client = ESClient([settings.elasticsearch_url], request_timeout=10)
        return self._es_client

    async def disconnect(self) -> None:
        """Close owned Elasticsearch client."""
        if self._client_owned and self._es_client is not None:
            await self._es_client.close()
            self._es_client = None

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
                        {"range": {"@timestamp": {"gte": start.isoformat(), "lte": now.isoformat()}}},
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
                        {"range": {"@timestamp": {"gte": start.isoformat(), "lte": now.isoformat()}}},
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
            index=f"{CAPTURES_INDEX_PREFIX}-*",
            query={
                "bool": {
                    "filter": [
                        {"range": {"timestamp": {"gte": start.isoformat(), "lte": now.isoformat()}}},
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
