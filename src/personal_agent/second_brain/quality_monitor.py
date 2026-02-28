"""Consolidation quality monitoring for memory graph health (FRE-23)."""

from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import mean, pstdev
from typing import Any

from personal_agent.memory.service import MemoryService
from personal_agent.telemetry import TelemetryQueries, get_logger

log = get_logger(__name__)

ENTITY_RATIO_TARGET = (0.5, 2.0)
RELATIONSHIP_DENSITY_TARGET = (1.0, 3.0)
DUPLICATE_RATE_TARGET_MAX = 0.05
EXTRACTION_FAILURE_RATE_TARGET_MAX = 0.01


@dataclass(frozen=True)
class QualityReport:
    """Entity extraction quality metrics."""

    conversations: int
    entities: int
    entities_per_conversation_ratio: float
    entity_name_length_distribution: dict[str, float]
    duplicate_entity_count: int
    duplicate_rate: float
    extraction_started: int
    extraction_failed: int
    extraction_failure_rate: float


@dataclass(frozen=True)
class GraphHealthReport:
    """Knowledge graph structural health metrics."""

    total_nodes: int
    conversation_nodes: int
    entity_nodes: int
    relationship_count: int
    relationship_density: float
    orphaned_entities: int
    orphaned_entity_rate: float
    clustered_entity_rate: float
    max_temporal_gap_hours: float


@dataclass(frozen=True)
class Anomaly:
    """Detected quality anomaly with threshold context."""

    anomaly_type: str
    severity: str
    message: str
    observed_value: float
    expected_range: tuple[float, float] | None = None
    metadata: dict[str, Any] | None = None


class ConsolidationQualityMonitor:
    """Monitors entity extraction quality and graph health trends."""

    def __init__(
        self,
        memory_service: MemoryService | None = None,
        telemetry_queries: TelemetryQueries | None = None,
    ) -> None:
        """Initialize monitor dependencies.

        Args:
            memory_service: Memory graph service (Neo4j).
            telemetry_queries: Telemetry analytics adapter (Elasticsearch).
        """
        self._memory_service = memory_service or MemoryService()
        self._queries = telemetry_queries or TelemetryQueries()

    async def check_entity_extraction_quality(self, days: int = 7) -> QualityReport:
        """Analyze extraction quality against FRE-23 targets.

        Args:
            days: Window for extraction telemetry.

        Returns:
            Entity extraction quality report.
        """
        conversation_count = int(
            await self._run_scalar_query("MATCH (c:Conversation) RETURN count(c) AS value")
        )
        entity_count = int(
            await self._run_scalar_query("MATCH (e:Entity) RETURN count(e) AS value")
        )
        duplicate_count = int(
            await self._run_scalar_query(
                """
                MATCH (e:Entity)
                WITH toLower(trim(e.name)) AS normalized_name, count(*) AS cnt
                WHERE normalized_name <> "" AND cnt > 1
                RETURN COALESCE(sum(cnt - 1), 0) AS value
                """
            )
        )
        name_lengths = await self._run_list_query(
            "MATCH (e:Entity) WHERE e.name IS NOT NULL RETURN size(trim(e.name)) AS value"
        )
        extraction_started = await self._queries.get_event_count("entity_extraction_started", days)
        extraction_failed = await self._queries.get_event_count("entity_extraction_failed", days)

        ratio = float(entity_count) / float(conversation_count) if conversation_count > 0 else 0.0
        duplicate_rate = float(duplicate_count / entity_count) if entity_count > 0 else 0.0
        extraction_failure_rate = (
            float(extraction_failed / extraction_started) if extraction_started > 0 else 0.0
        )

        report = QualityReport(
            conversations=conversation_count,
            entities=entity_count,
            entities_per_conversation_ratio=ratio,
            entity_name_length_distribution=_summarize_lengths(name_lengths),
            duplicate_entity_count=duplicate_count,
            duplicate_rate=duplicate_rate,
            extraction_started=extraction_started,
            extraction_failed=extraction_failed,
            extraction_failure_rate=extraction_failure_rate,
        )
        log.info(
            "quality_monitor_entity_report",
            ratio=round(report.entities_per_conversation_ratio, 4),
            duplicate_rate=round(report.duplicate_rate, 4),
            extraction_failure_rate=round(report.extraction_failure_rate, 4),
            conversations=report.conversations,
            entities=report.entities,
        )
        return report

    async def check_graph_health(self) -> GraphHealthReport:
        """Analyze graph structure and topology quality.

        Returns:
            Graph health report.
        """
        total_nodes = int(await self._run_scalar_query("MATCH (n) RETURN count(n) AS value"))
        entity_nodes = int(
            await self._run_scalar_query("MATCH (e:Entity) RETURN count(e) AS value")
        )
        conversation_nodes = int(
            await self._run_scalar_query("MATCH (c:Conversation) RETURN count(c) AS value")
        )
        relationship_count = int(
            await self._run_scalar_query(
                """
                MATCH ()-[r]->()
                WHERE type(r) IN ["RELATIONSHIP", "DISCUSSES"]
                RETURN count(r) AS value
                """
            )
        )
        orphaned_entities = int(
            await self._run_scalar_query(
                "MATCH (e:Entity) WHERE NOT (e)--() RETURN count(e) AS value"
            )
        )
        clustered_ratio = float(
            await self._run_scalar_query(
                """
                MATCH (e:Entity)
                OPTIONAL MATCH (e)-[:RELATIONSHIP]-(:Entity)
                WITH e, count(*) AS degree
                RETURN COALESCE(avg(CASE WHEN degree >= 2 THEN 1.0 ELSE 0.0 END), 0.0) AS value
                """
            )
        )
        timestamps = await self._run_list_query(
            "MATCH (c:Conversation) WHERE c.timestamp IS NOT NULL RETURN c.timestamp AS value ORDER BY c.timestamp ASC"
        )

        relationship_density = (
            float(relationship_count) / float(entity_nodes) if entity_nodes > 0 else 0.0
        )
        orphaned_rate = float(orphaned_entities / entity_nodes) if entity_nodes > 0 else 0.0
        max_temporal_gap_hours = _max_gap_hours(timestamps)

        report = GraphHealthReport(
            total_nodes=total_nodes,
            conversation_nodes=conversation_nodes,
            entity_nodes=entity_nodes,
            relationship_count=relationship_count,
            relationship_density=relationship_density,
            orphaned_entities=orphaned_entities,
            orphaned_entity_rate=orphaned_rate,
            clustered_entity_rate=clustered_ratio,
            max_temporal_gap_hours=max_temporal_gap_hours,
        )
        log.info(
            "quality_monitor_graph_report",
            relationship_density=round(report.relationship_density, 4),
            orphaned_entity_rate=round(report.orphaned_entity_rate, 4),
            clustered_entity_rate=round(report.clustered_entity_rate, 4),
            max_temporal_gap_hours=round(report.max_temporal_gap_hours, 2),
        )
        return report

    async def detect_anomalies(self, days: int = 7) -> list[Anomaly]:
        """Detect quality anomalies from current metrics and recent trends.

        Args:
            days: Trend window for spike detection.

        Returns:
            List of anomalies, empty if healthy.
        """
        quality = await self.check_entity_extraction_quality(days=days)
        graph = await self.check_graph_health()
        anomalies: list[Anomaly] = []

        anomalies.extend(
            _range_anomaly(
                "entity_conversation_ratio_out_of_range",
                quality.entities_per_conversation_ratio,
                ENTITY_RATIO_TARGET,
                "Entity-to-conversation ratio outside target range.",
            )
        )
        anomalies.extend(
            _range_anomaly(
                "relationship_density_out_of_range",
                graph.relationship_density,
                RELATIONSHIP_DENSITY_TARGET,
                "Relationship density outside target range.",
            )
        )
        if quality.duplicate_rate > DUPLICATE_RATE_TARGET_MAX:
            anomalies.append(
                Anomaly(
                    anomaly_type="duplicate_rate_high",
                    severity="medium",
                    message="Duplicate entity rate exceeds 5% target.",
                    observed_value=quality.duplicate_rate,
                    expected_range=(0.0, DUPLICATE_RATE_TARGET_MAX),
                )
            )
        if quality.extraction_failure_rate > EXTRACTION_FAILURE_RATE_TARGET_MAX:
            anomalies.append(
                Anomaly(
                    anomaly_type="extraction_failure_rate_high",
                    severity="high",
                    message="Entity extraction failure rate exceeds 1% target.",
                    observed_value=quality.extraction_failure_rate,
                    expected_range=(0.0, EXTRACTION_FAILURE_RATE_TARGET_MAX),
                )
            )
        if graph.entity_nodes > 0 and graph.relationship_count == 0:
            anomalies.append(
                Anomaly(
                    anomaly_type="no_relationships_created",
                    severity="high",
                    message="Entity nodes exist but no relationships are present.",
                    observed_value=float(graph.relationship_count),
                    expected_range=(1.0, float("inf")),
                )
            )

        daily_starts = await self._queries.get_daily_event_counts(
            event_type="entity_extraction_started",
            days=days,
        )
        spike = _detect_spike(daily_starts)
        if spike is not None:
            anomalies.append(spike)

        log.info("quality_monitor_anomalies_detected", count=len(anomalies), days=days)
        return anomalies

    async def _run_scalar_query(self, query: str, **params: Any) -> float:
        """Execute scalar Neo4j query and return a numeric value."""
        if not self._memory_service.connected or self._memory_service.driver is None:
            log.warning("quality_monitor_memory_not_connected")
            return 0.0

        async with self._memory_service.driver.session() as session:
            result = await session.run(query, **params)
            record = await result.single()
            if not record:
                return 0.0
            value = record.get("value", 0.0)
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0

    async def _run_list_query(self, query: str, **params: Any) -> list[Any]:
        """Execute Neo4j query and return list of `value` column items."""
        if not self._memory_service.connected or self._memory_service.driver is None:
            return []

        values: list[Any] = []
        async with self._memory_service.driver.session() as session:
            result = await session.run(query, **params)
            async for record in result:
                values.append(record.get("value"))
        return values


def _summarize_lengths(lengths: list[Any]) -> dict[str, float]:
    """Summarize entity-name lengths as numeric distribution statistics."""
    clean = [int(v) for v in lengths if isinstance(v, (int, float)) and int(v) >= 0]
    if not clean:
        return {"min": 0.0, "avg": 0.0, "p50": 0.0, "p90": 0.0, "max": 0.0}

    ordered = sorted(clean)
    return {
        "min": float(ordered[0]),
        "avg": float(mean(ordered)),
        "p50": _percentile(ordered, 0.50),
        "p90": _percentile(ordered, 0.90),
        "max": float(ordered[-1]),
    }


def _percentile(sorted_values: list[int], ratio: float) -> float:
    """Compute percentile from sorted values."""
    if not sorted_values:
        return 0.0
    index = min(max(int(round((len(sorted_values) - 1) * ratio)), 0), len(sorted_values) - 1)
    return float(sorted_values[index])


def _max_gap_hours(timestamps: list[Any]) -> float:
    """Calculate max gap between consecutive conversation timestamps."""
    parsed: list[datetime] = []
    for raw in timestamps:
        dt = _parse_datetime(raw)
        if dt is not None:
            parsed.append(dt)

    if len(parsed) < 2:
        return 0.0

    parsed.sort()
    gaps = [
        (parsed[index] - parsed[index - 1]).total_seconds() / 3600.0
        for index in range(1, len(parsed))
    ]
    return float(max(gaps)) if gaps else 0.0


def _parse_datetime(raw: Any) -> datetime | None:
    """Parse Neo4j/ISO datetime into timezone-aware datetime."""
    if raw is None:
        return None
    if hasattr(raw, "to_native"):
        native = raw.to_native()
        if isinstance(native, datetime):
            return native if native.tzinfo is not None else native.replace(tzinfo=timezone.utc)
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _range_anomaly(
    anomaly_type: str,
    observed: float,
    target: tuple[float, float],
    message: str,
) -> list[Anomaly]:
    """Create low/high anomalies when observed value is out of range."""
    low, high = target
    if low <= observed <= high:
        return []
    severity = "high" if observed < low * 0.5 or observed > high * 1.5 else "medium"
    return [
        Anomaly(
            anomaly_type=anomaly_type,
            severity=severity,
            message=message,
            observed_value=observed,
            expected_range=target,
        )
    ]


def _detect_spike(daily_counts: dict[str, int]) -> Anomaly | None:
    """Detect sudden spike in extraction volume."""
    if len(daily_counts) < 4:
        return None

    ordered_days = sorted(daily_counts.keys())
    ordered_values = [daily_counts[day] for day in ordered_days]
    baseline = ordered_values[:-1]
    latest_value = ordered_values[-1]

    baseline_mean = mean(baseline)
    baseline_std = pstdev(baseline) if len(baseline) > 1 else 0.0
    threshold = baseline_mean + (3 * baseline_std)
    if baseline_mean <= 0.0:
        return None
    if latest_value <= max(threshold, baseline_mean * 2.0):
        return None

    return Anomaly(
        anomaly_type="entity_extraction_spike",
        severity="medium",
        message="Entity extraction volume spiked above expected baseline.",
        observed_value=float(latest_value),
        expected_range=(0.0, float(max(threshold, baseline_mean * 2.0))),
        metadata={
            "latest_day": ordered_days[-1],
            "baseline_mean": round(float(baseline_mean), 2),
            "baseline_std": round(float(baseline_std), 2),
        },
    )
