"""End-to-end tests for FRE-32 quality monitor scheduler wiring."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.brainstem.scheduler import BrainstemScheduler
from personal_agent.second_brain.quality_monitor import ConsolidationQualityMonitor
from personal_agent.telemetry.queries import TelemetryQueries


class _InMemoryESClient:
    """Minimal async Elasticsearch client for telemetry query tests."""

    def __init__(self) -> None:
        self.documents: list[dict[str, Any]] = []

    async def search(
        self,
        index: str,
        query: dict[str, Any],
        size: int = 0,
        aggs: dict[str, Any] | None = None,
        sort: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        _ = (index, size, sort)
        filtered = self._apply_filters(query)

        if aggs and "daily" in aggs:
            day_counts: dict[str, int] = defaultdict(int)
            for doc in filtered:
                day_key = str(doc["@timestamp"]).split("T")[0]
                day_counts[day_key] += 1
            buckets = [
                {"key_as_string": f"{day}T00:00:00.000Z", "doc_count": count}
                for day, count in sorted(day_counts.items())
            ]
            return {
                "hits": {"total": {"value": len(filtered)}},
                "aggregations": {"daily": {"buckets": buckets}},
            }

        return {"hits": {"total": {"value": len(filtered)}}}

    async def close(self) -> None:
        """No-op close to mirror AsyncElasticsearch API."""

    def _apply_filters(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        filters = query.get("bool", {}).get("filter", [])
        docs = list(self.documents)

        for current in filters:
            if "term" in current:
                field, expected = next(iter(current["term"].items()))
                docs = [doc for doc in docs if doc.get(field) == expected]
            elif "range" in current:
                field, bounds = next(iter(current["range"].items()))
                gte = datetime.fromisoformat(str(bounds["gte"]).replace("Z", "+00:00"))
                lte = datetime.fromisoformat(str(bounds["lte"]).replace("Z", "+00:00"))

                def in_window(doc: dict[str, Any]) -> bool:
                    raw = str(doc.get(field, "")).replace("Z", "+00:00")
                    if not raw:
                        return False
                    timestamp = datetime.fromisoformat(raw)
                    return gte <= timestamp <= lte

                docs = [doc for doc in docs if in_window(doc)]

        return docs


class _InMemoryEventLogger:
    """Capture monitor log events into in-memory ES-like storage."""

    def __init__(self, client: _InMemoryESClient) -> None:
        self._client = client

    def info(self, event_type: str, **kwargs: Any) -> None:
        self._client.documents.append(
            {
                "@timestamp": datetime.now(timezone.utc).isoformat(),
                "event_type": event_type,
                **kwargs,
            }
        )

    def warning(self, event_type: str, **kwargs: Any) -> None:
        self.info(event_type, **kwargs)


@pytest.mark.asyncio
async def test_scheduler_quality_monitor_events_are_queryable() -> None:
    """Validate scheduler -> monitor -> telemetry query event flow."""
    fake_es_client = _InMemoryESClient()
    telemetry_queries = TelemetryQueries(es_client=fake_es_client)
    monitor = ConsolidationQualityMonitor(telemetry_queries=telemetry_queries)

    # check_entity_extraction_quality() scalar queries:
    # conversations, entities, duplicates
    # check_graph_health() scalar queries:
    # total_nodes, entity_nodes, conversation_nodes, relationships, orphaned, clustered_ratio
    monitor._run_scalar_query = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            10,
            100,
            20,  # entity report pass 1
            140,
            100,
            40,
            0,
            20,
            0.1,  # graph report pass 1
            10,
            100,
            20,  # entity report pass 2 (inside detect_anomalies)
            140,
            100,
            40,
            0,
            20,
            0.1,  # graph report pass 2 (inside detect_anomalies)
        ]
    )
    monitor._run_list_query = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            [3, 4, 5, 6, 7, 8],  # entity name lengths
            [
                (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ],  # conversation timestamps
            [3, 4, 5, 6, 7, 8],  # entity name lengths (detect_anomalies pass)
            [
                (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ],  # conversation timestamps (detect_anomalies pass)
        ]
    )
    telemetry_queries.get_event_count = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda event_type, days: 100 if event_type == "entity_extraction_started" else 10
    )
    telemetry_queries.get_daily_event_counts = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "2026-02-19": 2,
            "2026-02-20": 3,
            "2026-02-21": 2,
            "2026-02-22": 30,
        }
    )

    scheduler = BrainstemScheduler(quality_monitor=monitor)
    event_logger = _InMemoryEventLogger(fake_es_client)

    with patch("personal_agent.second_brain.quality_monitor.log", event_logger):
        await scheduler._run_quality_monitoring()

    verification_queries = TelemetryQueries(es_client=fake_es_client)
    assert await verification_queries.get_event_count("quality_monitor_entity_report", days=1) >= 1
    assert await verification_queries.get_event_count("quality_monitor_graph_report", days=1) >= 1
    assert await verification_queries.get_event_count("quality_monitor_anomalies_detected", days=1) >= 1

    daily_counts = await verification_queries.get_daily_event_counts(
        "quality_monitor_anomalies_detected",
        days=1,
    )
    assert sum(daily_counts.values()) >= 1
