"""Proactive insights engine for cross-data pattern detection (FRE-24)."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import mean, pstdev

from personal_agent.captains_log.es_indexer import schedule_es_index
from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    Metric,
    ProposedChange,
)
from personal_agent.llm_client.cost_tracker import CostTrackerService
from personal_agent.memory.service import MemoryService
from personal_agent.telemetry import TaskPatternReport, TelemetryQueries, get_logger

log = get_logger(__name__)

DEFAULT_ACTIONABLE_CONFIDENCE = 0.55
INSIGHTS_INDEX_PREFIX = "agent-insights"


@dataclass(frozen=True)
class Insight:
    """A generated insight from cross-data analysis."""

    insight_type: str
    title: str
    summary: str
    confidence: float
    evidence: dict[str, float | int | str]
    actionable: bool = True


@dataclass(frozen=True)
class CostAnomaly:
    """Represents a detected abnormal cost pattern."""

    anomaly_type: str
    message: str
    observed_cost_usd: float
    baseline_cost_usd: float
    ratio: float
    confidence: float


@dataclass(frozen=True)
class Improvement:
    """Actionable improvement recommendation derived from insights."""

    category: str
    recommendation: str
    rationale: str
    priority: str
    confidence: float
    evidence: dict[str, float | int | str]


class InsightsEngine:
    """Generates insights from telemetry, memory graph, and cost data."""

    def __init__(
        self,
        telemetry_queries: TelemetryQueries | None = None,
        memory_service: MemoryService | None = None,
        cost_tracker: CostTrackerService | None = None,
    ) -> None:
        """Initialize dependencies used for cross-data analysis.

        Args:
            telemetry_queries: Elasticsearch telemetry query adapter.
            memory_service: Neo4j memory graph service.
            cost_tracker: PostgreSQL-backed API cost tracker.
        """
        self._queries = telemetry_queries or TelemetryQueries()
        self._memory = memory_service or MemoryService()
        self._cost_tracker = cost_tracker or CostTrackerService()

    async def analyze_patterns(self, days: int = 7) -> list[Insight]:
        """Find patterns across telemetry, graph memory, and cost data.

        Args:
            days: Lookback window for telemetry and graph trend analysis.

        Returns:
            List of generated insights across correlation/trend/optimization/anomaly types.
        """
        task_patterns = await self._queries.get_task_patterns(days=days)
        cpu_percentiles = await self._queries.get_resource_percentiles("cpu", days=days)
        memory_percentiles = await self._queries.get_resource_percentiles("memory", days=days)
        transitions = await self._queries.get_mode_transitions(days=days)

        insights: list[Insight] = []
        insights.extend(
            self._build_resource_insights(
                task_patterns=task_patterns,
                cpu_percentiles=cpu_percentiles,
                memory_percentiles=memory_percentiles,
                transition_count=len(transitions),
            )
        )
        insights.extend(await self._build_graph_trend_insights(days=days))
        insights.extend(self._build_usage_trend_insights(task_patterns))

        for anomaly in await self.detect_cost_anomalies(days=max(14, days)):
            insights.append(
                Insight(
                    insight_type="anomaly",
                    title="Cost spike detected",
                    summary=anomaly.message,
                    confidence=anomaly.confidence,
                    evidence={
                        "observed_cost_usd": round(anomaly.observed_cost_usd, 4),
                        "baseline_cost_usd": round(anomaly.baseline_cost_usd, 4),
                        "ratio": round(anomaly.ratio, 3),
                    },
                    actionable=True,
                )
            )

        self._index_insights(insights=insights, days=days)
        log.info("insights_generated", days=days, count=len(insights))
        return insights

    async def detect_cost_anomalies(self, days: int = 14) -> list[CostAnomaly]:
        """Detect unusual spending patterns from PostgreSQL cost history.

        Args:
            days: Number of days to inspect for anomaly detection.

        Returns:
            Cost anomalies where current daily spend significantly exceeds baseline.
        """
        daily_costs = await self._get_daily_costs(days=days)
        if len(daily_costs) < 3:
            return []

        ordered_days = sorted(daily_costs)
        values = [daily_costs[day] for day in ordered_days]
        latest_day = ordered_days[-1]
        latest_value = values[-1]
        baseline = values[:-1]
        baseline_mean = float(mean(baseline))
        baseline_std = float(pstdev(baseline)) if len(baseline) > 1 else 0.0
        dynamic_threshold = baseline_mean + (3 * baseline_std)
        floor_threshold = baseline_mean * 2.0
        threshold = max(dynamic_threshold, floor_threshold, 0.25)

        if latest_value <= threshold:
            return []

        ratio = latest_value / baseline_mean if baseline_mean > 0 else float("inf")
        anomaly = CostAnomaly(
            anomaly_type="daily_cost_spike",
            message=(
                f"Cost spike: ${latest_value:.2f} on {latest_day} vs "
                f"${baseline_mean:.2f} average baseline."
            ),
            observed_cost_usd=latest_value,
            baseline_cost_usd=baseline_mean,
            ratio=ratio,
            confidence=0.75 if ratio >= 2.5 else 0.6,
        )
        log.warning(
            "insights_cost_anomaly_detected",
            anomaly_type=anomaly.anomaly_type,
            observed_cost_usd=round(anomaly.observed_cost_usd, 4),
            baseline_cost_usd=round(anomaly.baseline_cost_usd, 4),
            ratio=round(anomaly.ratio, 3),
        )
        return [anomaly]

    async def suggest_improvements(self, days: int = 7) -> list[Improvement]:
        """Generate prioritized improvement suggestions from insight signals.

        Args:
            days: Lookback window used for pattern analysis.

        Returns:
            Prioritized recommendations with rationale and evidence.
        """
        insights = await self.analyze_patterns(days=days)
        improvements: list[Improvement] = []

        for insight in insights:
            if insight.insight_type == "correlation":
                improvements.append(
                    Improvement(
                        category="resource_optimization",
                        recommendation=(
                            "Shift heavy operations away from high-memory windows or "
                            "increase consolidation spacing during those periods."
                        ),
                        rationale=insight.summary,
                        priority="high",
                        confidence=insight.confidence,
                        evidence=insight.evidence,
                    )
                )
            elif insight.insight_type == "optimization":
                improvements.append(
                    Improvement(
                        category="scheduling",
                        recommendation=(
                            "Schedule second-brain consolidation near peak effectiveness "
                            "hours inferred from successful task periods."
                        ),
                        rationale=insight.summary,
                        priority="medium",
                        confidence=insight.confidence,
                        evidence=insight.evidence,
                    )
                )
            elif insight.insight_type == "anomaly":
                improvements.append(
                    Improvement(
                        category="cost_control",
                        recommendation=(
                            "Review model usage for high-cost traces and enforce temporary "
                            "budget-aware routing on expensive workflows."
                        ),
                        rationale=insight.summary,
                        priority="high",
                        confidence=insight.confidence,
                        evidence=insight.evidence,
                    )
                )

        log.info("insights_improvements_generated", days=days, count=len(improvements))
        return improvements

    async def create_captain_log_proposals(self, insights: list[Insight]) -> list[CaptainLogEntry]:
        """Convert actionable insights into Captain's Log proposal entries.

        Args:
            insights: Insights to convert into proposal-ready entries.

        Returns:
            Captain's Log entries (config_proposal) for review workflow ingestion.
        """
        proposals: list[CaptainLogEntry] = []
        for insight in insights:
            if not insight.actionable or insight.confidence < DEFAULT_ACTIONABLE_CONFIDENCE:
                continue

            supporting_metrics = [
                f"{metric_name}: {metric_value}"
                for metric_name, metric_value in insight.evidence.items()
            ]
            metrics_structured = [
                Metric(
                    name=str(metric_name),
                    value=_metric_value(metric_value),
                    unit=_metric_unit(metric_name),
                )
                for metric_name, metric_value in insight.evidence.items()
            ]
            proposed_change = ProposedChange(
                what=f"Address insight pattern: {insight.title}",
                why=insight.summary,
                how=(
                    "Run targeted mitigation experiment for 7 days, monitor impact metrics, "
                    "and apply change if confidence improves."
                ),
            )
            proposals.append(
                CaptainLogEntry(
                    entry_id="",
                    type=CaptainLogEntryType.CONFIG_PROPOSAL,
                    title=f"Insight proposal: {insight.title}",
                    rationale=(
                        f"Generated by InsightsEngine from cross-data analysis "
                        f"(type={insight.insight_type}, confidence={insight.confidence:.0%})."
                    ),
                    proposed_change=proposed_change,
                    supporting_metrics=supporting_metrics,
                    metrics_structured=metrics_structured,
                    impact_assessment=(
                        "Expected to reduce failure/cost risk while preserving normal "
                        "throughput and memory quality."
                    ),
                )
            )

        log.info(
            "insights_captains_log_proposals_created",
            input_count=len(insights),
            count=len(proposals),
        )
        return proposals

    def _build_resource_insights(
        self,
        task_patterns: TaskPatternReport,
        cpu_percentiles: dict[str, float],
        memory_percentiles: dict[str, float],
        transition_count: int,
    ) -> list[Insight]:
        """Generate correlation and optimization insights from telemetry aggregates."""
        insights: list[Insight] = []
        memory_p90 = float(memory_percentiles.get("p90", 0.0))
        cpu_p90 = float(cpu_percentiles.get("p90", 0.0))

        if memory_p90 >= 70.0 and task_patterns.success_rate <= 0.85:
            insights.append(
                Insight(
                    insight_type="correlation",
                    title="Higher failure risk when memory is elevated",
                    summary=(
                        f"Task success rate is {task_patterns.success_rate:.0%} while memory p90 "
                        f"is {memory_p90:.1f}%, indicating a likely memory-pressure correlation."
                    ),
                    confidence=0.68,
                    evidence={
                        "task_success_rate": round(task_patterns.success_rate, 4),
                        "memory_p90_percent": round(memory_p90, 2),
                        "tasks": task_patterns.total_tasks,
                    },
                    actionable=True,
                )
            )

        if transition_count >= 10 and cpu_p90 >= 60.0:
            insights.append(
                Insight(
                    insight_type="optimization",
                    title="Frequent mode transitions under high CPU",
                    summary=(
                        f"{transition_count} mode transitions observed with CPU p90 at {cpu_p90:.1f}%. "
                        "Consolidation timing may need retuning."
                    ),
                    confidence=0.62,
                    evidence={
                        "mode_transitions": transition_count,
                        "cpu_p90_percent": round(cpu_p90, 2),
                    },
                    actionable=True,
                )
            )

        return insights

    def _build_usage_trend_insights(self, task_patterns: TaskPatternReport) -> list[Insight]:
        """Generate trend insights from task usage distribution."""
        if not task_patterns.hourly_distribution:
            return []

        peak_hour, peak_count = max(
            task_patterns.hourly_distribution.items(),
            key=lambda item: item[1],
        )
        if task_patterns.total_tasks <= 0:
            return []
        concentration = peak_count / task_patterns.total_tasks
        if concentration < 0.2:
            return []

        return [
            Insight(
                insight_type="trend",
                title="Task volume concentrated in a narrow time window",
                summary=(
                    f"Peak task activity occurs around {peak_hour:02d}:00 UTC with "
                    f"{concentration:.0%} of tasks in that hour."
                ),
                confidence=0.58,
                evidence={
                    "peak_hour_utc": peak_hour,
                    "peak_hour_task_count": peak_count,
                    "total_tasks": task_patterns.total_tasks,
                },
                actionable=False,
            )
        ]

    async def _build_graph_trend_insights(self, days: int) -> list[Insight]:
        """Generate graph-based trend insights from Neo4j entity activity."""
        top_entities = await self._get_top_entities(days=days)
        if not top_entities:
            return []

        lead_name, lead_count = top_entities[0]
        total_mentions = sum(count for _, count in top_entities)
        if total_mentions <= 0:
            return []

        share = lead_count / total_mentions
        return [
            Insight(
                insight_type="trend",
                title="Entity mention concentration detected",
                summary=(
                    f"Entity '{lead_name}' accounts for {share:.0%} of recent top-entity mentions, "
                    "indicating a strong recurring topic."
                ),
                confidence=0.57,
                evidence={
                    "entity": lead_name,
                    "entity_mentions": lead_count,
                    "top_entity_total_mentions": total_mentions,
                },
                actionable=False,
            )
        ]

    async def _get_top_entities(self, days: int) -> list[tuple[str, int]]:
        """Fetch top mentioned entities from the memory graph."""
        if not self._memory.connected or self._memory.driver is None:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        query = """
            MATCH (e:Entity)
            WHERE e.last_seen IS NOT NULL
              AND datetime(e.last_seen) >= datetime($cutoff_iso)
            RETURN e.name AS name, COALESCE(e.mention_count, 0) AS mentions
            ORDER BY mentions DESC
            LIMIT 5
        """
        try:
            entities: list[tuple[str, int]] = []
            async with self._memory.driver.session() as session:
                result = await session.run(query, cutoff_iso=cutoff.isoformat())
                async for record in result:
                    name = str(record.get("name") or "").strip()
                    if not name:
                        continue
                    mentions = int(record.get("mentions", 0) or 0)
                    entities.append((name, mentions))
            return entities
        except Exception as exc:
            log.warning("insights_graph_query_failed", error=str(exc))
            return []

    async def _get_daily_costs(self, days: int) -> dict[str, float]:
        """Return daily API cost totals from PostgreSQL for anomaly detection."""
        connected_here = False
        if self._cost_tracker.pool is None:
            await self._cost_tracker.connect()
            connected_here = self._cost_tracker.pool is not None
        if self._cost_tracker.pool is None:
            return {}

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        query = """
            SELECT date_trunc('day', timestamp) AS day_bucket, SUM(cost_usd) AS total_cost
            FROM api_costs
            WHERE timestamp >= $1
            GROUP BY day_bucket
            ORDER BY day_bucket ASC
        """
        try:
            async with self._cost_tracker.pool.acquire() as conn:
                rows = await conn.fetch(query, cutoff)
            costs: dict[str, float] = {}
            for row in rows:
                day_bucket = row.get("day_bucket")
                if not day_bucket:
                    continue
                day_key = day_bucket.date().isoformat()
                costs[day_key] = float(row.get("total_cost", 0.0) or 0.0)
            return costs
        except Exception as exc:
            log.warning("insights_cost_query_failed", error=str(exc))
            return {}
        finally:
            if connected_here:
                await self._cost_tracker.disconnect()

    def _index_insights(self, insights: list[Insight], days: int) -> None:
        """Index generated insights into `agent-insights-*` for dashboarding."""
        if not insights:
            return
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        index_name = f"{INSIGHTS_INDEX_PREFIX}-{date_str}"
        for insight in insights:
            document = {
                "timestamp": now.isoformat(),
                "record_type": "insight",
                "insight_type": insight.insight_type,
                "title": insight.title,
                "summary": insight.summary,
                "confidence": insight.confidence,
                "actionable": insight.actionable,
                "evidence": insight.evidence,
                "analysis_window_days": days,
            }
            schedule_es_index(index_name, document)


def _metric_value(raw: float | int | str) -> float | int | str:
    """Normalize evidence value for Metric model."""
    if isinstance(raw, (float, int, str)):
        return raw
    return str(raw)


def _metric_unit(metric_name: str) -> str | None:
    """Infer basic metric unit from metric key conventions."""
    lower_name = metric_name.lower()
    if lower_name.endswith("_percent"):
        return "%"
    if lower_name.endswith("_usd"):
        return "usd"
    if lower_name.endswith("_seconds"):
        return "s"
    return None
