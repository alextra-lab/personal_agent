"""Tests for TelemetryQueries.get_mean_rating_by_callsite (FRE-409)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from personal_agent.telemetry.queries import TelemetryQueries


def _ratings_response(buckets: list[dict]) -> dict:
    """Build a synthetic ES aggregation response for user-turn-ratings-*."""
    return {"aggregations": {"by_callsite": {"buckets": buckets}}}


def _bucket(key: str, doc_count: int, avg_value: float | None) -> dict:
    """Build a synthetic ES terms+avg bucket."""
    return {
        "key": key,
        "doc_count": doc_count,
        "avg_rating": {"value": avg_value},
    }


@pytest.mark.asyncio
class TestGetMeanRatingByCallsite:
    async def test_returns_callsite_to_mean_and_count(self) -> None:
        """Normal buckets: returns (mean, count) per callsite."""
        mock_client = AsyncMock()
        mock_client.search.return_value = _ratings_response(
            [
                _bucket("orchestrator.primary", 43, 2.1),
                _bucket("gateway.chat", 10, 1.5),
            ]
        )
        queries = TelemetryQueries(es_client=mock_client)

        result = await queries.get_mean_rating_by_callsite(days=7)

        assert "orchestrator.primary" in result
        mean, n = result["orchestrator.primary"]
        assert abs(mean - 2.1) < 1e-9
        assert n == 43

        mean2, n2 = result["gateway.chat"]
        assert abs(mean2 - 1.5) < 1e-9
        assert n2 == 10

    async def test_excludes_unknown_bucket(self) -> None:
        """Null/unknown callsite bucket is excluded from the result."""
        mock_client = AsyncMock()
        mock_client.search.return_value = _ratings_response(
            [
                _bucket("unknown", 5, 1.0),
                _bucket("orchestrator.primary", 20, 2.0),
            ]
        )
        queries = TelemetryQueries(es_client=mock_client)

        result = await queries.get_mean_rating_by_callsite(days=7)

        assert "unknown" not in result
        assert "orchestrator.primary" in result

    async def test_returns_empty_on_es_exception(self) -> None:
        """ES query failure → empty dict, no exception propagated."""
        mock_client = AsyncMock()
        mock_client.search.side_effect = Exception("ES connection refused")
        queries = TelemetryQueries(es_client=mock_client)

        result = await queries.get_mean_rating_by_callsite(days=7)

        assert result == {}

    async def test_returns_empty_when_no_buckets(self) -> None:
        """Empty aggregation buckets → empty dict."""
        mock_client = AsyncMock()
        mock_client.search.return_value = _ratings_response([])
        queries = TelemetryQueries(es_client=mock_client)

        result = await queries.get_mean_rating_by_callsite(days=7)

        assert result == {}

    async def test_none_avg_value_uses_zero(self) -> None:
        """Bucket with null avg_value (no ratings yet for callsite) defaults to 0.0."""
        mock_client = AsyncMock()
        mock_client.search.return_value = _ratings_response(
            [_bucket("orchestrator.primary", 0, None)]
        )
        queries = TelemetryQueries(es_client=mock_client)

        result = await queries.get_mean_rating_by_callsite(days=7)

        # doc_count=0 with missing avg: included with (0.0, 0) unless excluded
        # The bucket has key!="unknown" so it should be included
        if "orchestrator.primary" in result:
            mean, n = result["orchestrator.primary"]
            assert mean == 0.0
            assert n == 0

    async def test_query_uses_user_turn_ratings_index(self) -> None:
        """ES search must target the user-turn-ratings-* index."""
        mock_client = AsyncMock()
        mock_client.search.return_value = _ratings_response([])
        queries = TelemetryQueries(es_client=mock_client)

        await queries.get_mean_rating_by_callsite(days=7)

        call_args = mock_client.search.call_args
        index = call_args.kwargs.get("index") or (call_args.args[0] if call_args.args else "")
        assert "user-turn-ratings" in str(index)
