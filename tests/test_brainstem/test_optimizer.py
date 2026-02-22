"""Tests for adaptive threshold optimizer."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from personal_agent.brainstem.optimizer import ThresholdOptimizer
from personal_agent.telemetry.queries import (
    ConsolidationEvent,
    ModeTransition,
    TaskPatternReport,
)


@pytest.mark.asyncio
class TestThresholdOptimizer:
    """Test threshold optimization heuristics."""

    async def test_analyze_resource_patterns_returns_compound_result(self) -> None:
        """Analyze method combines percentile, transition, and task reports."""
        queries = AsyncMock()
        queries.get_resource_percentiles.side_effect = [
            {"p50": 10.0, "p75": 14.0, "p90": 18.0, "p95": 22.0, "p99": 30.0},
            {"p50": 20.0, "p75": 30.0, "p90": 40.0, "p95": 48.0, "p99": 60.0},
        ]
        queries.get_mode_transitions.return_value = []
        queries.get_consolidation_triggers.return_value = []
        queries.get_task_patterns.return_value = TaskPatternReport(
            total_tasks=12,
            completed_tasks=11,
            success_rate=11 / 12,
            avg_duration_ms=2200.0,
            most_used_tools=["ReadFile"],
            hourly_distribution={10: 4},
            avg_cpu_percent=18.0,
            avg_memory_percent=35.0,
        )
        optimizer = ThresholdOptimizer(telemetry_queries=queries)

        analysis = await optimizer.analyze_resource_patterns(days=7)

        assert analysis.days == 7
        assert analysis.cpu_percentiles["p90"] == 18.0
        assert analysis.memory_percentiles["p95"] == 48.0
        assert analysis.task_patterns.total_tasks == 12

    async def test_detect_false_positives_flags_low_resource_events(self) -> None:
        """Low-resource consolidations without nearby risky transitions are flagged."""
        now = datetime.now(timezone.utc)
        queries = AsyncMock()
        queries.get_consolidation_triggers.return_value = [
            ConsolidationEvent(
                timestamp=now,
                cpu_percent=15.0,
                memory_percent=20.0,
                idle_seconds=600.0,
            )
        ]
        queries.get_mode_transitions.return_value = [
            ModeTransition(
                timestamp=now,
                from_mode="normal",
                to_mode="normal",
                reason="steady state",
            )
        ]
        optimizer = ThresholdOptimizer(telemetry_queries=queries)

        report = await optimizer.detect_false_positives()

        assert report.total_consolidations == 1
        assert report.suspected_false_positives == 1
        assert report.false_positive_rate == 1.0
        assert len(report.evidence) == 1

    async def test_propose_threshold_adjustment_for_cpu(self) -> None:
        """CPU threshold proposal uses observed percentiles and includes payload."""
        queries = AsyncMock()
        queries.get_resource_percentiles.side_effect = [
            {"p50": 10.0, "p75": 20.0, "p90": 30.0, "p95": 35.0, "p99": 45.0},
            {"p50": 20.0, "p75": 30.0, "p90": 40.0, "p95": 45.0, "p99": 55.0},
        ]
        queries.get_mode_transitions.return_value = []
        queries.get_consolidation_triggers.return_value = []
        queries.get_task_patterns.return_value = TaskPatternReport(
            total_tasks=30,
            completed_tasks=29,
            success_rate=29 / 30,
            avg_duration_ms=1800.0,
            most_used_tools=["ReadFile", "rg"],
            hourly_distribution={9: 10, 10: 12},
            avg_cpu_percent=16.0,
            avg_memory_percent=32.0,
        )
        optimizer = ThresholdOptimizer(telemetry_queries=queries)

        proposal = await optimizer.propose_threshold_adjustment("cpu_threshold")

        assert proposal.metric == "cpu_threshold"
        assert proposal.proposed_value > 0
        assert proposal.captains_log_payload["type"] == "config_proposal"
        assert "proposed_change" in proposal.captains_log_payload

    async def test_run_ab_test_returns_recommendation(self) -> None:
        """A/B test compares baseline and projected false-positive rates."""
        queries = AsyncMock()
        queries.get_resource_percentiles.side_effect = [
            {"p50": 10.0, "p75": 20.0, "p90": 30.0, "p95": 35.0, "p99": 45.0},
            {"p50": 20.0, "p75": 30.0, "p90": 40.0, "p95": 45.0, "p99": 55.0},
            {"p50": 10.0, "p75": 20.0, "p90": 30.0, "p95": 35.0, "p99": 45.0},
            {"p50": 20.0, "p75": 30.0, "p90": 40.0, "p95": 45.0, "p99": 55.0},
        ]
        queries.get_mode_transitions.return_value = []
        queries.get_consolidation_triggers.return_value = []
        queries.get_task_patterns.return_value = TaskPatternReport(
            total_tasks=20,
            completed_tasks=20,
            success_rate=1.0,
            avg_duration_ms=1600.0,
            most_used_tools=["ReadFile"],
            hourly_distribution={11: 5},
            avg_cpu_percent=14.0,
            avg_memory_percent=30.0,
        )
        optimizer = ThresholdOptimizer(telemetry_queries=queries)
        proposal = await optimizer.propose_threshold_adjustment("cpu_threshold")

        result = await optimizer.run_ab_test(proposal)

        assert result.metric == "cpu_threshold"
        assert result.baseline_value == proposal.current_value
        assert result.candidate_value == proposal.proposed_value
        assert isinstance(result.recommended, bool)
