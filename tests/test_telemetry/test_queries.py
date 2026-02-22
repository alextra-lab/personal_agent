"""Tests for telemetry Elasticsearch analytics queries."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from personal_agent.telemetry.queries import TelemetryQueries


@pytest.mark.asyncio
class TestTelemetryQueries:
    """Test TelemetryQueries behavior with mocked ES responses."""

    async def test_get_resource_percentiles_returns_expected_keys(self) -> None:
        """Resource percentile query maps ES response into pXX dict."""
        mock_client = AsyncMock()
        mock_client.search.return_value = {
            "aggregations": {
                "percentiles": {
                    "values": {
                        "50.0": 10.0,
                        "75.0": 15.0,
                        "90.0": 20.0,
                        "95.0": 24.0,
                        "99.0": 30.0,
                    }
                }
            }
        }
        queries = TelemetryQueries(es_client=mock_client)

        result = await queries.get_resource_percentiles(metric="cpu", days=7)

        assert result == {
            "p50": 10.0,
            "p75": 15.0,
            "p90": 20.0,
            "p95": 24.0,
            "p99": 30.0,
        }

    async def test_get_mode_transitions_parses_hits(self) -> None:
        """Mode transition hits are converted into typed transition records."""
        mock_client = AsyncMock()
        mock_client.search.return_value = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "@timestamp": "2026-02-22T10:00:00+00:00",
                            "from_mode": "normal",
                            "to_mode": "alert",
                            "reason": "cpu high",
                            "trace_id": "trace-1",
                        }
                    }
                ]
            }
        }
        queries = TelemetryQueries(es_client=mock_client)

        transitions = await queries.get_mode_transitions(days=3)

        assert len(transitions) == 1
        assert transitions[0].from_mode == "normal"
        assert transitions[0].to_mode == "alert"
        assert transitions[0].trace_id == "trace-1"

    async def test_get_consolidation_triggers_parses_optional_metrics(self) -> None:
        """Consolidation trigger events parse available resource fields."""
        mock_client = AsyncMock()
        mock_client.search.return_value = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "@timestamp": datetime.now(timezone.utc).isoformat(),
                            "trace_id": "trace-2",
                            "cpu_load": 22.5,
                            "memory_used": 40.2,
                            "idle_time": 600,
                        }
                    }
                ]
            }
        }
        queries = TelemetryQueries(es_client=mock_client)

        events = await queries.get_consolidation_triggers(days=7)

        assert len(events) == 1
        assert events[0].trace_id == "trace-2"
        assert events[0].cpu_percent == 22.5
        assert events[0].memory_percent == 40.2
        assert events[0].idle_seconds == 600.0

    async def test_get_task_patterns_aggregates_report(self) -> None:
        """Task pattern report computes rates and top tool list."""
        mock_client = AsyncMock()
        mock_client.search.return_value = {
            "aggregations": {
                "total": {"value": 10},
                "completed": {"doc_count": 8},
                "avg_duration_ms": {"value": 2500.0},
                "avg_cpu": {"value": 14.2},
                "avg_memory": {"value": 33.6},
                "top_tools": {
                    "buckets": [
                        {"key": "ReadFile", "doc_count": 6},
                        {"key": "rg", "doc_count": 4},
                    ]
                },
                "hours": {
                    "buckets": [
                        {"key": 9, "doc_count": 3},
                        {"key": 10, "doc_count": 5},
                    ]
                },
            }
        }
        queries = TelemetryQueries(es_client=mock_client)

        report = await queries.get_task_patterns(days=7)

        assert report.total_tasks == 10
        assert report.completed_tasks == 8
        assert report.success_rate == 0.8
        assert report.avg_duration_ms == 2500.0
        assert report.most_used_tools == ["ReadFile", "rg"]
        assert report.hourly_distribution == {9: 3, 10: 5}
        assert report.avg_cpu_percent == 14.2
        assert report.avg_memory_percent == 33.6

    async def test_get_event_count_returns_total_hits(self) -> None:
        """Event-count query returns total hit count."""
        mock_client = AsyncMock()
        mock_client.search.return_value = {
            "hits": {
                "total": {"value": 17},
            }
        }
        queries = TelemetryQueries(es_client=mock_client)

        count = await queries.get_event_count(event_type="entity_extraction_failed", days=7)

        assert count == 17

    async def test_get_daily_event_counts_maps_histogram_buckets(self) -> None:
        """Daily event counts map date histogram buckets to YYYY-MM-DD keys."""
        mock_client = AsyncMock()
        mock_client.search.return_value = {
            "aggregations": {
                "daily": {
                    "buckets": [
                        {"key_as_string": "2026-02-20T00:00:00.000Z", "doc_count": 2},
                        {"key_as_string": "2026-02-21T00:00:00.000Z", "doc_count": 5},
                    ]
                }
            }
        }
        queries = TelemetryQueries(es_client=mock_client)

        daily_counts = await queries.get_daily_event_counts(
            event_type="entity_extraction_started",
            days=7,
        )

        assert daily_counts == {"2026-02-20": 2, "2026-02-21": 5}
