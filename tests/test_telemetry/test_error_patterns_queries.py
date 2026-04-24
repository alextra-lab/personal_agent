"""Tests for TelemetryQueries.get_error_events and get_error_patterns (ADR-0056 §step 3).

RED phase: methods don't exist yet.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.events.models import ErrorPatternCluster
from personal_agent.telemetry.queries import TelemetryQueries


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_composite_agg_response(
    buckets: list[dict],
) -> dict:
    """Build a mock ES composite aggregation response."""
    return {
        "aggregations": {
            "error_patterns": {
                "buckets": buckets,
                "after_key": None,
            }
        }
    }


def _make_bucket(
    component: str = "tools.fetch_url",
    event_name: str = "fetch_url_timeout",
    error_type: str = "TimeoutError",
    level: str = "ERROR",
    doc_count: int = 10,
    first_seen: str = "2026-04-20T00:00:00Z",
    last_seen: str = "2026-04-24T00:00:00Z",
    trace_ids: list[str] | None = None,
    messages: list[str] | None = None,
) -> dict:
    return {
        "key": {
            "source_component": component,
            "event": event_name,
            "error_type_normalised": error_type,
            "level": level,
        },
        "doc_count": doc_count,
        "first_seen": {"value_as_string": first_seen},
        "last_seen": {"value_as_string": last_seen},
        "sample_trace_ids": {"buckets": [{"key": t} for t in (trace_ids or ["tid-1"])]},
        "sample_messages": {"buckets": [{"key": m} for m in (messages or ["Read timeout"])]},
    }


@pytest.mark.asyncio
class TestGetErrorPatterns:
    """Tests for TelemetryQueries.get_error_patterns."""

    async def test_happy_path_returns_error_pattern_clusters(self) -> None:
        """A bucket above min_occurrences becomes an ErrorPatternCluster."""
        mock_client = AsyncMock()
        mock_client.search.return_value = _make_composite_agg_response(
            [_make_bucket(doc_count=12)]
        )
        queries = TelemetryQueries(es_client=mock_client)

        clusters = await queries.get_error_patterns(window_hours=24, min_occurrences=5)

        assert len(clusters) == 1
        cluster = clusters[0]
        assert isinstance(cluster, ErrorPatternCluster)
        assert cluster.component == "tools.fetch_url"
        assert cluster.event_name == "fetch_url_timeout"
        assert cluster.error_type == "TimeoutError"
        assert cluster.level == "ERROR"
        assert cluster.occurrences == 12
        assert cluster.window_hours == 24
        assert "tid-1" in cluster.sample_trace_ids

    async def test_buckets_below_min_occurrences_are_excluded(self) -> None:
        """Buckets with doc_count < min_occurrences are not returned."""
        mock_client = AsyncMock()
        mock_client.search.return_value = _make_composite_agg_response(
            [_make_bucket(doc_count=3)]
        )
        queries = TelemetryQueries(es_client=mock_client)

        clusters = await queries.get_error_patterns(window_hours=24, min_occurrences=5)

        assert clusters == []

    async def test_out_of_scope_component_is_filtered(self) -> None:
        """Components in the out-of-scope list are dropped per ADR-0056 D1."""
        mock_client = AsyncMock()
        mock_client.search.return_value = _make_composite_agg_response(
            [_make_bucket(component="elastic_transport.network", doc_count=50)]
        )
        queries = TelemetryQueries(es_client=mock_client)

        clusters = await queries.get_error_patterns(window_hours=24, min_occurrences=5)

        assert clusters == []

    async def test_elasticsearch_component_is_filtered(self) -> None:
        """'elasticsearch.*' components are excluded from patterns."""
        mock_client = AsyncMock()
        mock_client.search.return_value = _make_composite_agg_response(
            [_make_bucket(component="elasticsearch.client", doc_count=20)]
        )
        queries = TelemetryQueries(es_client=mock_client)

        clusters = await queries.get_error_patterns(window_hours=24, min_occurrences=5)

        assert clusters == []

    async def test_httpx_component_is_filtered(self) -> None:
        """'httpx.*' components are excluded (third-party library noise)."""
        mock_client = AsyncMock()
        mock_client.search.return_value = _make_composite_agg_response(
            [_make_bucket(component="httpx._client", doc_count=20)]
        )
        queries = TelemetryQueries(es_client=mock_client)

        clusters = await queries.get_error_patterns(window_hours=24, min_occurrences=5)

        assert clusters == []

    async def test_empty_aggregation_returns_empty_list(self) -> None:
        """An empty ES aggregation bucket list returns an empty cluster list."""
        mock_client = AsyncMock()
        mock_client.search.return_value = _make_composite_agg_response([])
        queries = TelemetryQueries(es_client=mock_client)

        clusters = await queries.get_error_patterns(window_hours=24, min_occurrences=5)

        assert clusters == []

    async def test_fingerprint_is_deterministic(self) -> None:
        """Same (component, event_name, error_type) always produces the same fingerprint."""
        mock_client = AsyncMock()
        mock_client.search.return_value = _make_composite_agg_response(
            [_make_bucket(doc_count=10)]
        )
        queries = TelemetryQueries(es_client=mock_client)

        clusters1 = await queries.get_error_patterns(window_hours=24, min_occurrences=5)
        clusters2 = await queries.get_error_patterns(window_hours=24, min_occurrences=5)

        assert clusters1[0].fingerprint == clusters2[0].fingerprint

    async def test_fingerprint_is_16_hex_chars(self) -> None:
        """Fingerprint is exactly 16 hex characters (sha256[:16])."""
        mock_client = AsyncMock()
        mock_client.search.return_value = _make_composite_agg_response(
            [_make_bucket(doc_count=10)]
        )
        queries = TelemetryQueries(es_client=mock_client)

        clusters = await queries.get_error_patterns(window_hours=24, min_occurrences=5)

        assert len(clusters[0].fingerprint) == 16
        assert all(c in "0123456789abcdef" for c in clusters[0].fingerprint)

    async def test_sample_trace_ids_capped_at_five(self) -> None:
        """Cluster sample_trace_ids is capped at 5 even if ES returns more."""
        many_traces = [f"tid-{i}" for i in range(10)]
        mock_client = AsyncMock()
        mock_client.search.return_value = _make_composite_agg_response(
            [_make_bucket(doc_count=10, trace_ids=many_traces)]
        )
        queries = TelemetryQueries(es_client=mock_client)

        clusters = await queries.get_error_patterns(window_hours=24, min_occurrences=5)

        assert len(clusters[0].sample_trace_ids) <= 5

    async def test_sample_messages_capped_at_three(self) -> None:
        """Cluster sample_messages is capped at 3."""
        mock_client = AsyncMock()
        mock_client.search.return_value = _make_composite_agg_response(
            [_make_bucket(doc_count=10, messages=["m1", "m2", "m3", "m4", "m5"])]
        )
        queries = TelemetryQueries(es_client=mock_client)

        clusters = await queries.get_error_patterns(window_hours=24, min_occurrences=5)

        assert len(clusters[0].sample_messages) <= 3

    async def test_multiple_valid_buckets_all_returned(self) -> None:
        """Multiple above-threshold buckets all become clusters."""
        mock_client = AsyncMock()
        mock_client.search.return_value = _make_composite_agg_response(
            [
                _make_bucket(component="tools.fetch_url", doc_count=10),
                _make_bucket(component="llm_client.main", event_name="model_call_error", doc_count=7),
            ]
        )
        queries = TelemetryQueries(es_client=mock_client)

        clusters = await queries.get_error_patterns(window_hours=24, min_occurrences=5)

        assert len(clusters) == 2
        components = {c.component for c in clusters}
        assert "tools.fetch_url" in components
        assert "llm_client.main" in components


@pytest.mark.asyncio
class TestGetErrorEvents:
    """Tests for TelemetryQueries.get_error_events."""

    async def test_get_error_events_returns_raw_hits(self) -> None:
        """get_error_events returns list of _source dicts from ES hits."""
        mock_client = AsyncMock()
        mock_client.search.return_value = {
            "hits": {
                "hits": [
                    {"_source": {"event": "fetch_url_timeout", "level": "ERROR"}}
                ]
            }
        }
        queries = TelemetryQueries(es_client=mock_client)

        events = await queries.get_error_events(days=1)

        assert len(events) == 1
        assert events[0]["event"] == "fetch_url_timeout"

    async def test_get_error_events_empty_returns_empty_list(self) -> None:
        """Empty hits list → empty result."""
        mock_client = AsyncMock()
        mock_client.search.return_value = {"hits": {"hits": []}}
        queries = TelemetryQueries(es_client=mock_client)

        events = await queries.get_error_events(days=1)

        assert events == []
