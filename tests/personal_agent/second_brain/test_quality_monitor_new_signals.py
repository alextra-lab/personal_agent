"""Tests for FRE-374: redundant-relationship and empty-description quality signals."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.second_brain.quality_monitor import (
    Anomaly,
    ConsolidationQualityMonitor,
    GraphHealthReport,
)


def _make_monitor() -> tuple[ConsolidationQualityMonitor, MagicMock]:
    mock_service = MagicMock()
    mock_service.connected = True
    mock_service.driver = MagicMock()
    mock_session = AsyncMock()
    mock_service.driver.session = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    mock_queries = MagicMock()
    monitor = ConsolidationQualityMonitor(mock_service, mock_queries)
    return monitor, mock_service


class TestGraphHealthReportNewFields:
    def test_new_fields_exist_with_given_values(self) -> None:
        report = GraphHealthReport(
            total_nodes=100,
            conversation_nodes=10,
            entity_nodes=90,
            relationship_count=50,
            relationship_density=0.55,
            orphaned_entities=5,
            orphaned_entity_rate=0.055,
            clustered_entity_rate=0.8,
            max_temporal_gap_hours=24.0,
            empty_description_entity_count=15,
            redundant_relationship_pairs=10,
        )
        assert report.empty_description_entity_count == 15
        assert report.redundant_relationship_pairs == 10

    def test_new_fields_default_to_zero(self) -> None:
        report = GraphHealthReport(
            total_nodes=0,
            conversation_nodes=0,
            entity_nodes=0,
            relationship_count=0,
            relationship_density=0.0,
            orphaned_entities=0,
            orphaned_entity_rate=0.0,
            clustered_entity_rate=0.0,
            max_temporal_gap_hours=0.0,
        )
        assert report.empty_description_entity_count == 0
        assert report.redundant_relationship_pairs == 0


class TestNewAnomalyTypes:
    def _base_mocks(self) -> tuple[MagicMock, MagicMock]:
        mock_quality = MagicMock()
        mock_quality.conversations = 40  # FRE-620: non-zero so ratio isn't insufficient_data
        mock_quality.entities_per_conversation_ratio = 3.0  # inside recalibrated (2.0, 5.0) band
        mock_quality.duplicate_rate = 0.0
        mock_quality.extraction_failure_rate = 0.0
        mock_health = MagicMock()
        mock_health.relationship_density = 1.5
        mock_health.entity_nodes = 100
        mock_health.relationship_count = 150
        mock_health.empty_description_entity_count = 0
        mock_health.redundant_relationship_pairs = 0
        mock_health.relationship_bearing_pairs = 200  # FRE-620: rate denominator
        return mock_quality, mock_health

    @pytest.mark.asyncio
    async def test_empty_description_rate_never_produces_an_anomaly(self) -> None:
        """FRE-620: empty-description rate is demoted to dashboard/info — never an anomaly,
        regardless of magnitude (was: fired at >10%, promotable).
        """
        monitor, _ = _make_monitor()
        mock_quality, mock_health = self._base_mocks()
        mock_health.empty_description_entity_count = 90  # 90% of 100 — would have fired before

        with (
            patch.object(
                monitor, "check_entity_extraction_quality", AsyncMock(return_value=mock_quality)
            ),
            patch.object(monitor, "check_graph_health", AsyncMock(return_value=mock_health)),
            patch.object(monitor, "_queries") as mq,
        ):
            mq.get_daily_event_counts = AsyncMock(return_value=[])
            anomalies = await monitor.detect_anomalies(days=7)

        assert "empty_description_rate_high" not in [a.anomaly_type for a in anomalies]
        assert not any("empty_description" in a.anomaly_type for a in anomalies)

    @pytest.mark.asyncio
    async def test_redundant_relationship_pairs_high_fires_above_rate_threshold(self) -> None:
        """FRE-620: redundant-pairs is now a rate (>10%), not an absolute count."""
        monitor, _ = _make_monitor()
        mock_quality, mock_health = self._base_mocks()
        mock_health.redundant_relationship_pairs = 30
        mock_health.relationship_bearing_pairs = 200  # 15% > 10% target

        with (
            patch.object(
                monitor, "check_entity_extraction_quality", AsyncMock(return_value=mock_quality)
            ),
            patch.object(monitor, "check_graph_health", AsyncMock(return_value=mock_health)),
            patch.object(monitor, "_queries") as mq,
        ):
            mq.get_daily_event_counts = AsyncMock(return_value=[])
            anomalies = await monitor.detect_anomalies(days=7)

        assert "redundant_relationship_pairs_high" in [a.anomaly_type for a in anomalies]

    @pytest.mark.asyncio
    async def test_redundant_relationship_pairs_high_does_not_fire_below_rate_threshold(
        self,
    ) -> None:
        monitor, _ = _make_monitor()
        mock_quality, mock_health = self._base_mocks()
        mock_health.redundant_relationship_pairs = 624
        mock_health.relationship_bearing_pairs = 11265  # live snapshot: 5.54%, below 10% target

        with (
            patch.object(
                monitor, "check_entity_extraction_quality", AsyncMock(return_value=mock_quality)
            ),
            patch.object(monitor, "check_graph_health", AsyncMock(return_value=mock_health)),
            patch.object(monitor, "_queries") as mq,
        ):
            mq.get_daily_event_counts = AsyncMock(return_value=[])
            anomalies = await monitor.detect_anomalies(days=7)

        assert "redundant_relationship_pairs_high" not in [a.anomaly_type for a in anomalies]

    @pytest.mark.asyncio
    async def test_redundant_relationship_pairs_absolute_count_alone_no_longer_fires(self) -> None:
        """FRE-620: the old abs-50 threshold must not still gate the anomaly."""
        monitor, _ = _make_monitor()
        mock_quality, mock_health = self._base_mocks()
        mock_health.redundant_relationship_pairs = 237  # old prod baseline, well past abs-50
        mock_health.relationship_bearing_pairs = 5000  # rate = 4.74%, below 10% target

        with (
            patch.object(
                monitor, "check_entity_extraction_quality", AsyncMock(return_value=mock_quality)
            ),
            patch.object(monitor, "check_graph_health", AsyncMock(return_value=mock_health)),
            patch.object(monitor, "_queries") as mq,
        ):
            mq.get_daily_event_counts = AsyncMock(return_value=[])
            anomalies = await monitor.detect_anomalies(days=7)

        assert "redundant_relationship_pairs_high" not in [a.anomaly_type for a in anomalies]
