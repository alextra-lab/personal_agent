"""Tests for proactive insights engine (FRE-24)."""

from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.insights.engine import CostAnomaly, Insight, InsightsEngine
from personal_agent.telemetry.queries import TaskPatternReport


@pytest.mark.asyncio
class TestInsightsEngine:
    """Validate cross-data insights generation and proposal conversion."""

    async def test_analyze_patterns_returns_correlation_trend_and_anomaly(self) -> None:
        """Pattern analysis combines telemetry trends and cost anomalies."""
        telemetry_queries = AsyncMock()
        telemetry_queries.get_task_patterns.return_value = TaskPatternReport(
            total_tasks=20,
            completed_tasks=14,
            success_rate=0.70,
            avg_duration_ms=1900.0,
            most_used_tools=["ReadFile", "rg"],
            hourly_distribution={3: 8, 9: 4, 10: 4, 11: 4},
            avg_cpu_percent=38.0,
            avg_memory_percent=72.0,
        )
        telemetry_queries.get_resource_percentiles.side_effect = [
            {"p50": 22.0, "p75": 48.0, "p90": 66.0, "p95": 75.0, "p99": 88.0},
            {"p50": 45.0, "p75": 62.0, "p90": 78.0, "p95": 85.0, "p99": 91.0},
        ]
        telemetry_queries.get_mode_transitions.return_value = [object()] * 12

        engine = InsightsEngine(telemetry_queries=telemetry_queries)
        engine.detect_cost_anomalies = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                CostAnomaly(
                    anomaly_type="daily_cost_spike",
                    message="Cost spike: $2.50 today vs $0.50 avg.",
                    observed_cost_usd=2.5,
                    baseline_cost_usd=0.5,
                    ratio=5.0,
                    confidence=0.8,
                )
            ]
        )

        insights = await engine.analyze_patterns(days=7)
        insight_types = {insight.insight_type for insight in insights}

        assert "correlation" in insight_types
        assert "trend" in insight_types
        assert "optimization" in insight_types
        assert "anomaly" in insight_types

    async def test_detect_cost_anomalies_flags_daily_spike(self) -> None:
        """Daily cost anomaly is emitted when latest cost breaches threshold."""
        engine = InsightsEngine(telemetry_queries=AsyncMock())
        engine._get_daily_costs = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "2026-02-18": 0.40,
                "2026-02-19": 0.45,
                "2026-02-20": 0.50,
                "2026-02-21": 0.55,
                "2026-02-22": 2.50,
            }
        )

        anomalies = await engine.detect_cost_anomalies(days=14)

        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == "daily_cost_spike"
        assert anomalies[0].observed_cost_usd == 2.50
        assert anomalies[0].baseline_cost_usd > 0.0

    async def test_create_captain_log_proposals_filters_non_actionable(self) -> None:
        """Proposal generation keeps only actionable high-confidence insights."""
        engine = InsightsEngine(telemetry_queries=AsyncMock())
        insights = [
            Insight(
                insight_type="anomaly",
                title="Cost spike detected",
                summary="Cost spike: $2.50 today vs $0.50 avg.",
                confidence=0.80,
                evidence={"observed_cost_usd": 2.5, "baseline_cost_usd": 0.5},
                actionable=True,
            ),
            Insight(
                insight_type="trend",
                title="Usage shifted to 3AM",
                summary="Most activity now concentrated at 03:00 UTC.",
                confidence=0.42,
                evidence={"peak_hour_utc": 3},
                actionable=True,
            ),
            Insight(
                insight_type="correlation",
                title="Low confidence correlation",
                summary="Weak signal for memory-related failures.",
                confidence=0.70,
                evidence={"memory_p90_percent": 68.0},
                actionable=False,
            ),
        ]

        proposals = await engine.create_captain_log_proposals(insights)

        assert len(proposals) == 1
        proposal = proposals[0]
        assert proposal.type.value == "config_proposal"
        assert proposal.proposed_change is not None
        assert proposal.metrics_structured is not None

    async def test_analyze_patterns_indexes_insights_to_es(self) -> None:
        """Generated insights are sent to the insights index writer."""
        telemetry_queries = AsyncMock()
        telemetry_queries.get_task_patterns.return_value = TaskPatternReport(
            total_tasks=12,
            completed_tasks=9,
            success_rate=0.75,
            avg_duration_ms=2000.0,
            most_used_tools=["ReadFile"],
            hourly_distribution={3: 4, 10: 2, 11: 2, 12: 2, 13: 2},
            avg_cpu_percent=45.0,
            avg_memory_percent=73.0,
        )
        telemetry_queries.get_resource_percentiles.side_effect = [
            {"p50": 20.0, "p75": 40.0, "p90": 70.0, "p95": 80.0, "p99": 90.0},
            {"p50": 30.0, "p75": 60.0, "p90": 75.0, "p95": 85.0, "p99": 95.0},
        ]
        telemetry_queries.get_mode_transitions.return_value = [object()] * 10
        engine = InsightsEngine(telemetry_queries=telemetry_queries)
        engine.detect_cost_anomalies = AsyncMock(return_value=[])  # type: ignore[method-assign]

        with patch("personal_agent.insights.engine.schedule_es_index") as mock_schedule:
            insights = await engine.analyze_patterns(days=7)
            assert len(insights) > 0
            assert mock_schedule.call_count >= 1
