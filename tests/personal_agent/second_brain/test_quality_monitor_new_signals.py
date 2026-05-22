"""Tests for FRE-374: redundant-relationship and empty-description quality signals."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from personal_agent.second_brain.quality_monitor import (
    ConsolidationQualityMonitor,
    GraphHealthReport,
    Anomaly,
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
            total_nodes=100, conversation_nodes=10, entity_nodes=90,
            relationship_count=50, relationship_density=0.55,
            orphaned_entities=5, orphaned_entity_rate=0.055,
            clustered_entity_rate=0.8, max_temporal_gap_hours=24.0,
            empty_description_entity_count=15, redundant_relationship_pairs=10,
        )
        assert report.empty_description_entity_count == 15
        assert report.redundant_relationship_pairs == 10

    def test_new_fields_default_to_zero(self) -> None:
        report = GraphHealthReport(
            total_nodes=0, conversation_nodes=0, entity_nodes=0,
            relationship_count=0, relationship_density=0.0,
            orphaned_entities=0, orphaned_entity_rate=0.0,
            clustered_entity_rate=0.0, max_temporal_gap_hours=0.0,
        )
        assert report.empty_description_entity_count == 0
        assert report.redundant_relationship_pairs == 0


class TestNewAnomalyTypes:

    def _base_mocks(self) -> tuple[MagicMock, MagicMock]:
        mock_quality = MagicMock()
        mock_quality.entities_per_conversation_ratio = 1.0
        mock_quality.duplicate_rate = 0.0
        mock_quality.extraction_failure_rate = 0.0
        mock_health = MagicMock()
        mock_health.relationship_density = 1.5
        mock_health.entity_nodes = 100
        mock_health.relationship_count = 150
        mock_health.empty_description_entity_count = 0
        mock_health.redundant_relationship_pairs = 0
        return mock_quality, mock_health

    @pytest.mark.asyncio
    async def test_empty_description_rate_high_fires_above_threshold(self) -> None:
        monitor, _ = _make_monitor()
        mock_quality, mock_health = self._base_mocks()
        mock_health.empty_description_entity_count = 20  # 20% of 100

        with (
            patch.object(monitor, "check_entity_extraction_quality", AsyncMock(return_value=mock_quality)),
            patch.object(monitor, "check_graph_health", AsyncMock(return_value=mock_health)),
            patch.object(monitor, "_queries") as mq,
        ):
            mq.get_daily_event_counts = AsyncMock(return_value=[])
            anomalies = await monitor.detect_anomalies(days=7)

        assert "empty_description_rate_high" in [a.anomaly_type for a in anomalies]

    @pytest.mark.asyncio
    async def test_empty_description_rate_high_does_not_fire_below_threshold(self) -> None:
        monitor, _ = _make_monitor()
        mock_quality, mock_health = self._base_mocks()
        mock_health.empty_description_entity_count = 5  # 5% — below 10%

        with (
            patch.object(monitor, "check_entity_extraction_quality", AsyncMock(return_value=mock_quality)),
            patch.object(monitor, "check_graph_health", AsyncMock(return_value=mock_health)),
            patch.object(monitor, "_queries") as mq,
        ):
            mq.get_daily_event_counts = AsyncMock(return_value=[])
            anomalies = await monitor.detect_anomalies(days=7)

        assert "empty_description_rate_high" not in [a.anomaly_type for a in anomalies]

    @pytest.mark.asyncio
    async def test_redundant_relationship_pairs_high_fires_above_threshold(self) -> None:
        monitor, _ = _make_monitor()
        mock_quality, mock_health = self._base_mocks()
        mock_health.redundant_relationship_pairs = 237  # prod baseline

        with (
            patch.object(monitor, "check_entity_extraction_quality", AsyncMock(return_value=mock_quality)),
            patch.object(monitor, "check_graph_health", AsyncMock(return_value=mock_health)),
            patch.object(monitor, "_queries") as mq,
        ):
            mq.get_daily_event_counts = AsyncMock(return_value=[])
            anomalies = await monitor.detect_anomalies(days=7)

        assert "redundant_relationship_pairs_high" in [a.anomaly_type for a in anomalies]

    @pytest.mark.asyncio
    async def test_redundant_relationship_pairs_high_does_not_fire_below_threshold(self) -> None:
        monitor, _ = _make_monitor()
        mock_quality, mock_health = self._base_mocks()
        mock_health.redundant_relationship_pairs = 30  # below 50

        with (
            patch.object(monitor, "check_entity_extraction_quality", AsyncMock(return_value=mock_quality)),
            patch.object(monitor, "check_graph_health", AsyncMock(return_value=mock_health)),
            patch.object(monitor, "_queries") as mq,
        ):
            mq.get_daily_event_counts = AsyncMock(return_value=[])
            anomalies = await monitor.detect_anomalies(days=7)

        assert "redundant_relationship_pairs_high" not in [a.anomaly_type for a in anomalies]
