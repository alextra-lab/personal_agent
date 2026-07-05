"""Tests for consolidation quality monitor (FRE-23)."""

from unittest.mock import AsyncMock

import pytest

from personal_agent.second_brain.quality_monitor import (
    ENTITY_RATIO_TARGET,
    ConsolidationQualityMonitor,
    _detect_spike,
)


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

    async def test_check_entity_extraction_quality_queries_turn_not_conversation(self) -> None:
        """FRE-620: the :Conversation label was renamed to :Turn; queries must follow."""
        telemetry_queries = AsyncMock()
        telemetry_queries.get_event_count.side_effect = [100, 2]
        monitor = ConsolidationQualityMonitor(telemetry_queries=telemetry_queries)
        monitor._run_scalar_query = AsyncMock(side_effect=[20, 30, 3])  # type: ignore[method-assign]
        monitor._run_list_query = AsyncMock(return_value=[4, 6, 8, 10])  # type: ignore[method-assign]

        await monitor.check_entity_extraction_quality(days=7)

        turn_count_query = monitor._run_scalar_query.call_args_list[0].args[0]
        assert "Turn" in turn_count_query
        assert "Conversation" not in turn_count_query

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
                3,  # FRE-374: empty_description_entity_count
                5,  # FRE-374: redundant_relationship_pairs
                20,  # FRE-620: relationship_bearing_pairs
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

    async def test_check_graph_health_queries_turn_not_conversation(self) -> None:
        """FRE-620: conversation_nodes count and freshness timestamps must query :Turn."""
        monitor = ConsolidationQualityMonitor(telemetry_queries=AsyncMock())
        monitor._run_scalar_query = AsyncMock(  # type: ignore[method-assign]
            side_effect=[50, 20, 30, 40, 2, 0.6, 3, 5, 100]
        )
        monitor._run_list_query = AsyncMock(  # type: ignore[method-assign]
            return_value=["2026-02-20T00:00:00+00:00"]
        )

        await monitor.check_graph_health()

        conversation_nodes_query = monitor._run_scalar_query.call_args_list[2].args[0]
        assert "Turn" in conversation_nodes_query
        assert "Conversation" not in conversation_nodes_query

        timestamps_query = monitor._run_list_query.call_args_list[0].args[0]
        assert "Turn" in timestamps_query
        assert "Conversation" not in timestamps_query

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
        monitor._memory_service.connected = True
        monitor.check_entity_extraction_quality = AsyncMock(  # type: ignore[method-assign]
            return_value=type(
                "QualityReportStub",
                (),
                {
                    "conversations": 20,
                    "entities_per_conversation_ratio": 7.0,  # FRE-620: outside recalibrated (2.0, 5.0) band
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
                    "empty_description_entity_count": 0,  # FRE-374
                    "redundant_relationship_pairs": 0,  # FRE-374
                    "relationship_bearing_pairs": 0,  # FRE-620
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

    def _quality_stub(self, conversations: int, ratio: float = 3.4) -> object:
        return type(
            "QualityReportStub",
            (),
            {
                "conversations": conversations,
                "entities_per_conversation_ratio": ratio,
                "duplicate_rate": 0.0,
                "extraction_failure_rate": 0.0,
            },
        )()

    def _graph_stub(
        self, entity_nodes: int, density: float = 2.0, relationship_count: int = 20
    ) -> object:
        return type(
            "GraphHealthReportStub",
            (),
            {
                "relationship_density": density,
                "entity_nodes": entity_nodes,
                "relationship_count": relationship_count,
                "empty_description_entity_count": 0,
                "redundant_relationship_pairs": 0,
                "relationship_bearing_pairs": 0,
            },
        )()

    async def test_detect_anomalies_disconnected_yields_insufficient_data_not_high_severity(
        self,
    ) -> None:
        """FRE-620: a disconnected driver must never produce a high-severity anomaly."""
        telemetry_queries = AsyncMock()
        telemetry_queries.get_daily_event_counts.return_value = {}
        monitor = ConsolidationQualityMonitor(telemetry_queries=telemetry_queries)
        monitor._memory_service.connected = False
        monitor.check_entity_extraction_quality = AsyncMock(  # type: ignore[method-assign]
            return_value=self._quality_stub(conversations=20)
        )
        monitor.check_graph_health = AsyncMock(  # type: ignore[method-assign]
            return_value=self._graph_stub(entity_nodes=10)
        )

        anomalies = await monitor.detect_anomalies(days=7)
        anomaly_types = {a.anomaly_type for a in anomalies}

        assert "insufficient_data" in anomaly_types
        assert "entity_conversation_ratio_out_of_range" not in anomaly_types
        assert "relationship_density_out_of_range" not in anomaly_types
        assert all(a.severity != "high" for a in anomalies)

    async def test_detect_anomalies_zero_conversations_skips_only_ratio(self) -> None:
        """FRE-620: zero turns should skip the ratio check but still evaluate density."""
        telemetry_queries = AsyncMock()
        telemetry_queries.get_daily_event_counts.return_value = {}
        monitor = ConsolidationQualityMonitor(telemetry_queries=telemetry_queries)
        monitor._memory_service.connected = True
        monitor.check_entity_extraction_quality = AsyncMock(  # type: ignore[method-assign]
            return_value=self._quality_stub(conversations=0)
        )
        monitor.check_graph_health = AsyncMock(  # type: ignore[method-assign]
            return_value=self._graph_stub(entity_nodes=10, density=0.1)  # out of (1.0, 3.0) range
        )

        anomalies = await monitor.detect_anomalies(days=7)
        anomaly_types = {a.anomaly_type for a in anomalies}

        assert "insufficient_data" in anomaly_types
        assert "entity_conversation_ratio_out_of_range" not in anomaly_types
        assert "relationship_density_out_of_range" in anomaly_types  # entity_nodes fine, still runs

    async def test_detect_anomalies_zero_entities_skips_only_density(self) -> None:
        """FRE-620: zero entities should skip the density check but still evaluate ratio."""
        telemetry_queries = AsyncMock()
        telemetry_queries.get_daily_event_counts.return_value = {}
        monitor = ConsolidationQualityMonitor(telemetry_queries=telemetry_queries)
        monitor._memory_service.connected = True
        monitor.check_entity_extraction_quality = AsyncMock(  # type: ignore[method-assign]
            return_value=self._quality_stub(conversations=20, ratio=0.1)  # out of (2.0, 5.0) range
        )
        monitor.check_graph_health = AsyncMock(  # type: ignore[method-assign]
            return_value=self._graph_stub(entity_nodes=0)
        )

        anomalies = await monitor.detect_anomalies(days=7)
        anomaly_types = {a.anomaly_type for a in anomalies}

        assert "insufficient_data" in anomaly_types
        assert "relationship_density_out_of_range" not in anomaly_types
        assert "entity_conversation_ratio_out_of_range" in anomaly_types  # conversations fine

    async def test_detect_anomalies_no_insufficient_data_when_connected_and_populated(self) -> None:
        """FRE-620: healthy connected graph with data never emits insufficient_data."""
        telemetry_queries = AsyncMock()
        telemetry_queries.get_daily_event_counts.return_value = {}
        monitor = ConsolidationQualityMonitor(telemetry_queries=telemetry_queries)
        monitor._memory_service.connected = True
        monitor.check_entity_extraction_quality = AsyncMock(  # type: ignore[method-assign]
            return_value=self._quality_stub(conversations=20, ratio=3.4)
        )
        monitor.check_graph_health = AsyncMock(  # type: ignore[method-assign]
            return_value=self._graph_stub(entity_nodes=10, density=2.0)
        )

        anomalies = await monitor.detect_anomalies(days=7)
        anomaly_types = {a.anomaly_type for a in anomalies}

        assert "insufficient_data" not in anomaly_types


class TestRecalibratedRatioBand:
    """FRE-620: entity/turn ratio band re-derived from a validated live 30-day baseline (3.22-3.56)."""

    def test_ratio_band_covers_validated_baseline_with_headroom(self) -> None:
        low, high = ENTITY_RATIO_TARGET
        assert low == 2.0
        assert high == 5.0
        # Validated live baseline (2026-06-06 -> 2026-07-05): 3.22 - 3.56.
        assert low < 3.22 and 3.56 < high


class TestSpikeDetectionFloor:
    """FRE-620: a min_absolute_spike floor prevents trivial deltas on a near-zero baseline firing."""

    def test_small_delta_on_near_zero_baseline_does_not_fire(self) -> None:
        # Mirrors real 32-day data: mostly-zero baseline with an occasional single-digit day.
        daily_counts = {
            "2026-06-15": 0,
            "2026-06-16": 0,
            "2026-06-17": 1,
            "2026-06-18": 0,
            "2026-06-19": 0,
            "2026-06-20": 0,
            "2026-06-21": 6,
        }
        assert _detect_spike(daily_counts) is None

    def test_large_delta_still_fires(self) -> None:
        # Mirrors the real 2026-06-13 batch day (50 vs a low baseline).
        daily_counts = {
            "2026-06-07": 0,
            "2026-06-08": 0,
            "2026-06-09": 0,
            "2026-06-10": 2,
            "2026-06-11": 0,
            "2026-06-12": 0,
            "2026-06-13": 50,
        }
        spike = _detect_spike(daily_counts)
        assert spike is not None
        assert spike.anomaly_type == "entity_extraction_spike"
