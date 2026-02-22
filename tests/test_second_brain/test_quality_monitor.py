"""Tests for consolidation quality monitor (FRE-23)."""

from unittest.mock import AsyncMock

import pytest

from personal_agent.second_brain.quality_monitor import ConsolidationQualityMonitor


@pytest.mark.asyncio
class TestConsolidationQualityMonitor:
    """Validate quality metric calculations and anomaly detection."""

    async def test_check_entity_extraction_quality_calculates_targets(self) -> None:
        """Entity quality report includes ratio, duplicate, and failure rates."""
        telemetry_queries = AsyncMock()
        telemetry_queries.get_event_count.side_effect = [100, 2]
        monitor = ConsolidationQualityMonitor(telemetry_queries=telemetry_queries)
        monitor._run_scalar_query = AsyncMock(side_effect=[20, 30, 3])  # type: ignore[method-assign]
        monitor._run_list_query = AsyncMock(return_value=[4, 6, 8, 10])  # type: ignore[method-assign]

        report = await monitor.check_entity_extraction_quality(days=7)

        assert report.conversations == 20
        assert report.entities == 30
        assert report.entities_per_conversation_ratio == 1.5
        assert report.duplicate_rate == 0.1
        assert report.extraction_failure_rate == 0.02
        assert report.entity_name_length_distribution["p50"] == 8.0

    async def test_check_graph_health_calculates_density_and_gaps(self) -> None:
        """Graph health report computes density and temporal gap metrics."""
        monitor = ConsolidationQualityMonitor(telemetry_queries=AsyncMock())
        monitor._run_scalar_query = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                50,  # total nodes
                20,  # entity nodes
                30,  # conversation nodes
                40,  # relationship count
                2,  # orphaned entities
                0.6,  # clustered ratio
            ]
        )
        monitor._run_list_query = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                "2026-02-20T00:00:00+00:00",
                "2026-02-20T12:00:00+00:00",
                "2026-02-22T00:00:00+00:00",
            ]
        )

        report = await monitor.check_graph_health()

        assert report.relationship_density == 2.0
        assert report.orphaned_entity_rate == 0.1
        assert report.clustered_entity_rate == 0.6
        assert report.max_temporal_gap_hours == 36.0

    async def test_detect_anomalies_flags_out_of_range_and_spikes(self) -> None:
        """Anomaly detector flags threshold breaches and spike patterns."""
        telemetry_queries = AsyncMock()
        telemetry_queries.get_daily_event_counts.return_value = {
            "2026-02-19": 3,
            "2026-02-20": 2,
            "2026-02-21": 4,
            "2026-02-22": 30,
        }
        monitor = ConsolidationQualityMonitor(telemetry_queries=telemetry_queries)
        monitor.check_entity_extraction_quality = AsyncMock(  # type: ignore[method-assign]
            return_value=type(
                "QualityReportStub",
                (),
                {
                    "entities_per_conversation_ratio": 3.4,
                    "duplicate_rate": 0.08,
                    "extraction_failure_rate": 0.03,
                },
            )()
        )
        monitor.check_graph_health = AsyncMock(  # type: ignore[method-assign]
            return_value=type(
                "GraphHealthReportStub",
                (),
                {
                    "relationship_density": 0.2,
                    "entity_nodes": 10,
                    "relationship_count": 0,
                },
            )()
        )

        anomalies = await monitor.detect_anomalies(days=7)
        anomaly_types = {item.anomaly_type for item in anomalies}

        assert "entity_conversation_ratio_out_of_range" in anomaly_types
        assert "relationship_density_out_of_range" in anomaly_types
        assert "duplicate_rate_high" in anomaly_types
        assert "extraction_failure_rate_high" in anomaly_types
        assert "entity_extraction_spike" in anomaly_types
