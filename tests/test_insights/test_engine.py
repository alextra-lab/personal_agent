"""Tests for proactive insights engine (FRE-24)."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.captains_log.models import ChangeCategory, ChangeScope
from personal_agent.insights.engine import (
    CostAnomaly,
    Insight,
    InsightsEngine,
    _category_for_insight_type,
    _cost_fingerprint,
    _pattern_fingerprint,
    _scope_for_insight_type,
    _severity_for_cost_ratio,
)
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
        telemetry_queries.get_delegation_pattern_buckets.return_value = {
            "total": 0,
            "successes": 0,
            "rounds_needed_values": [],
            "missing_context_terms": [],
        }

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
        telemetry_queries.get_delegation_pattern_buckets.return_value = {
            "total": 0,
            "successes": 0,
            "rounds_needed_values": [],
            "missing_context_terms": [],
        }
        engine = InsightsEngine(telemetry_queries=telemetry_queries)
        engine.detect_cost_anomalies = AsyncMock(return_value=[])  # type: ignore[method-assign]

        with patch("personal_agent.insights.engine.schedule_es_index") as mock_schedule:
            insights = await engine.analyze_patterns(days=7)
            assert len(insights) > 0
            assert mock_schedule.call_count >= 1


class TestInsightsEngineHelpers:
    """ADR-0057 helpers: fingerprints, category/scope mapping, severity."""

    def test_pattern_fingerprint_stable_across_equivalent_titles(self) -> None:
        """Digit-normalised titles produce the same fingerprint."""
        fp1 = _pattern_fingerprint("anomaly", "", "Cost spike: $4.12 on 2026-04-19")
        fp2 = _pattern_fingerprint("anomaly", "", "Cost spike: $5.23 on 2026-04-20")
        assert fp1 == fp2, "digits in title must be normalised to # for dedup"
        assert len(fp1) == 16

    def test_pattern_fingerprint_distinguishes_insight_types(self) -> None:
        """Different insight_type values → different fingerprints."""
        fp1 = _pattern_fingerprint("anomaly", "", "same title")
        fp2 = _pattern_fingerprint("correlation", "", "same title")
        assert fp1 != fp2

    def test_pattern_fingerprint_includes_pattern_kind(self) -> None:
        """Different pattern_kind values → different fingerprints."""
        fp1 = _pattern_fingerprint("delegation", "delegation_success_rate", "Low success")
        fp2 = _pattern_fingerprint("delegation", "delegation_rounds", "Low success")
        assert fp1 != fp2

    def test_cost_fingerprint_keyed_on_anomaly_date(self) -> None:
        """Different observation dates → different fingerprints."""
        fp1 = _cost_fingerprint("daily_cost_spike", "2026-04-19")
        fp2 = _cost_fingerprint("daily_cost_spike", "2026-04-20")
        assert fp1 != fp2
        assert len(fp1) == 16

    def test_severity_thresholds(self) -> None:
        """Severity classification matches ADR-0057 §D5 thresholds."""
        assert _severity_for_cost_ratio(1.8) == "low"
        assert _severity_for_cost_ratio(2.4999) == "low"
        assert _severity_for_cost_ratio(2.5) == "medium"
        assert _severity_for_cost_ratio(3.9) == "medium"
        assert _severity_for_cost_ratio(4.0) == "high"
        assert _severity_for_cost_ratio(12.0) == "high"

    def test_category_for_insight_type(self) -> None:
        """insight_type → ChangeCategory mapping per ADR-0057 §D7."""
        assert _category_for_insight_type("correlation") == ChangeCategory.PERFORMANCE
        assert _category_for_insight_type("optimization") == ChangeCategory.PERFORMANCE
        assert _category_for_insight_type("trend") == ChangeCategory.OBSERVABILITY
        assert _category_for_insight_type("anomaly") == ChangeCategory.COST
        assert _category_for_insight_type("graph_staleness") == ChangeCategory.KNOWLEDGE_QUALITY
        assert (
            _category_for_insight_type("graph_staleness_trend") == ChangeCategory.KNOWLEDGE_QUALITY
        )
        assert _category_for_insight_type("feedback_summary") == ChangeCategory.OBSERVABILITY
        assert _category_for_insight_type("feedback_category") == ChangeCategory.OBSERVABILITY
        assert _category_for_insight_type("delegation") == ChangeCategory.RELIABILITY
        assert _category_for_insight_type("unknown_future_type") == ChangeCategory.OBSERVABILITY

    def test_scope_for_insight_type(self) -> None:
        """insight_type → ChangeScope mapping per ADR-0057 §D7."""
        assert _scope_for_insight_type("correlation") == ChangeScope.CROSS_CUTTING
        assert _scope_for_insight_type("optimization") == ChangeScope.BRAINSTEM
        assert _scope_for_insight_type("trend") == ChangeScope.CROSS_CUTTING
        assert _scope_for_insight_type("anomaly") == ChangeScope.LLM_CLIENT
        assert _scope_for_insight_type("graph_staleness") == ChangeScope.SECOND_BRAIN
        assert _scope_for_insight_type("graph_staleness_trend") == ChangeScope.SECOND_BRAIN
        assert _scope_for_insight_type("feedback_summary") == ChangeScope.CAPTAINS_LOG
        assert _scope_for_insight_type("feedback_category") == ChangeScope.CAPTAINS_LOG
        assert _scope_for_insight_type("delegation") == ChangeScope.ORCHESTRATOR
        assert _scope_for_insight_type("unknown_future_type") == ChangeScope.CROSS_CUTTING


@pytest.mark.asyncio
class TestCreateCaptainLogProposalsFingerprinted:
    """ADR-0057: proposals carry fingerprint/category/scope/seen_count=1."""

    async def test_anomaly_insight_gets_cost_fingerprint_and_cost_category(self) -> None:
        """Anomaly insights use cost fingerprint keyed on today's date."""
        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        engine = InsightsEngine()
        insight = Insight(
            insight_type="anomaly",
            title="Cost spike detected",
            summary="Cost spike today",
            confidence=0.75,
            evidence={"observed_cost_usd": 4.12, "ratio": 3.22},
            actionable=True,
        )
        proposals = await engine.create_captain_log_proposals([insight])
        assert len(proposals) == 1
        pc = proposals[0].proposed_change
        assert pc is not None
        assert pc.fingerprint == _cost_fingerprint("daily_cost_spike", today_iso)
        assert pc.category == ChangeCategory.COST
        assert pc.scope == ChangeScope.LLM_CLIENT
        assert pc.seen_count == 1
        assert pc.first_seen is not None

    async def test_correlation_insight_gets_pattern_fingerprint(self) -> None:
        """Correlation insights use pattern fingerprint keyed on insight_type + title."""
        engine = InsightsEngine()
        insight = Insight(
            insight_type="correlation",
            title="Higher failure risk when memory is elevated",
            summary="Task success rate 70% while memory p90 78%",
            confidence=0.68,
            evidence={"memory_p90_percent": 78.0},
            actionable=True,
        )
        proposals = await engine.create_captain_log_proposals([insight])
        assert len(proposals) == 1
        pc = proposals[0].proposed_change
        assert pc is not None
        assert pc.fingerprint == _pattern_fingerprint("correlation", "", insight.title)
        assert pc.category == ChangeCategory.PERFORMANCE
        assert pc.scope == ChangeScope.CROSS_CUTTING

    async def test_below_confidence_threshold_dropped(self) -> None:
        """Insights below DEFAULT_ACTIONABLE_CONFIDENCE (0.55) are not proposed."""
        engine = InsightsEngine()
        insight = Insight(
            insight_type="correlation",
            title="Low confidence insight",
            summary="",
            confidence=0.40,
            evidence={},
            actionable=True,
        )
        proposals = await engine.create_captain_log_proposals([insight])
        assert proposals == []

    async def test_delegation_insight_carries_pattern_kind(self) -> None:
        """Delegation insights encode pattern_kind in their fingerprint."""
        engine = InsightsEngine()
        insight = Insight(
            insight_type="delegation",
            pattern_kind="delegation_success_rate",
            title="Low success rate for ClaudeCode delegations (42%)",
            summary="13/31 successful over 30d",
            confidence=0.78,
            evidence={"success_rate": 0.42, "total_delegations": 31},
            actionable=True,
        )
        proposals = await engine.create_captain_log_proposals([insight])
        assert len(proposals) == 1
        pc = proposals[0].proposed_change
        assert pc is not None
        assert pc.category == ChangeCategory.RELIABILITY
        assert pc.scope == ChangeScope.ORCHESTRATOR
        assert pc.fingerprint == _pattern_fingerprint(
            "delegation", "delegation_success_rate", insight.title
        )
