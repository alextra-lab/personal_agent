"""Tests for telemetry aggregation field correctness (FRE-1108).

Verifies that aggregations query fields that exist in the target indices
and use correct field names (no .keyword suffix for explicitly-mapped keyword fields).
"""

from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.telemetry.queries import TelemetryQueries


class TestAggregationFieldValidity:
    """Verify aggregations use correct field names."""

    @pytest.mark.asyncio
    async def test_get_task_patterns_trace_id_without_keyword_suffix(self) -> None:
        """Aggregation should count trace_id without .keyword suffix."""
        mock_client = AsyncMock()
        mock_client.search = AsyncMock(
            return_value={
                "aggregations": {
                    "total": {"value": 2899},
                    "completed": {"doc_count": 1500},
                    "avg_duration_ms": {"value": 450.5},
                    "avg_cpu": {"value": 25.3},
                    "avg_memory": {"value": 48.7},
                    "top_tools": {"buckets": [{"key": "tool1"}]},
                    "hours": {"buckets": []},
                }
            }
        )

        queries = TelemetryQueries(es_client=mock_client)
        result = await queries.get_task_patterns(days=7)

        # Verify the aggregation query does NOT use trace_id.keyword
        call_args = mock_client.search.call_args
        aggs = call_args.kwargs.get("aggs", {})

        # Should use "trace_id" not "trace_id.keyword"
        total_agg = aggs.get("total", {})
        assert "value_count" in total_agg
        assert total_agg["value_count"]["field"] == "trace_id"

        # Verify result
        assert result.total_tasks == 2899

    @pytest.mark.asyncio
    async def test_get_task_patterns_outcome_without_keyword_suffix(self) -> None:
        """Aggregation should filter outcome without .keyword suffix."""
        mock_client = AsyncMock()
        mock_client.search = AsyncMock(
            return_value={
                "aggregations": {
                    "total": {"value": 100},
                    "completed": {"doc_count": 85},
                    "avg_duration_ms": {"value": 400.0},
                    "avg_cpu": {"value": 20.0},
                    "avg_memory": {"value": 40.0},
                    "top_tools": {"buckets": []},
                    "hours": {"buckets": []},
                }
            }
        )

        queries = TelemetryQueries(es_client=mock_client)
        await queries.get_task_patterns(days=7)

        call_args = mock_client.search.call_args
        aggs = call_args.kwargs.get("aggs", {})

        # Should use "outcome" not "outcome.keyword"
        completed_agg = aggs.get("completed", {})
        assert "filter" in completed_agg
        term_filter = completed_agg["filter"]["term"]
        assert "outcome" in term_filter
        assert "outcome.keyword" not in term_filter

    @pytest.mark.asyncio
    async def test_get_delegation_pattern_buckets_without_keyword_suffix(self) -> None:
        """Aggregation should query event without .keyword suffix."""
        mock_client = AsyncMock()
        mock_client.search = AsyncMock(
            return_value={
                "aggregations": {
                    "total": {"value": 200},
                    "successes": {"value": 150},
                    "rounds_histogram": {"buckets": []},
                    "missing_context_terms": {"buckets": []},
                }
            }
        )

        queries = TelemetryQueries(es_client=mock_client)
        await queries.get_delegation_pattern_buckets(days=7)

        call_args = mock_client.search.call_args
        query = call_args.kwargs.get("query", {})
        filters = query.get("bool", {}).get("filter", [])

        # Find the event filter
        event_filter = None
        for f in filters:
            if "term" in f and "event" in f["term"]:
                event_filter = f
                break

        assert event_filter is not None
        # Should use "event" not "event.keyword"
        assert "event" in event_filter["term"]
        assert "event.keyword" not in event_filter["term"]

    @pytest.mark.asyncio
    async def test_get_delegation_pattern_task_id_without_keyword_suffix(self) -> None:
        """Aggregation should count task_id without .keyword suffix."""
        mock_client = AsyncMock()
        mock_client.search = AsyncMock(
            return_value={
                "aggregations": {
                    "total": {"value": 500},
                    "successes": {"value": 400},
                    "rounds_histogram": {"buckets": []},
                    "missing_context_terms": {"buckets": []},
                }
            }
        )

        queries = TelemetryQueries(es_client=mock_client)
        await queries.get_delegation_pattern_buckets(days=7)

        call_args = mock_client.search.call_args
        aggs = call_args.kwargs.get("aggs", {})

        # Should use "task_id" not "task_id.keyword"
        total_agg = aggs.get("total", {})
        assert "value_count" in total_agg
        assert total_agg["value_count"]["field"] == "task_id"

    @pytest.mark.asyncio
    async def test_get_error_patterns_without_keyword_suffix(self) -> None:
        """Aggregation should query error pattern fields without .keyword suffix."""
        mock_client = AsyncMock()
        mock_client.search = AsyncMock(
            return_value={
                "aggregations": {
                    "error_patterns": {
                        "buckets": [
                            {
                                "key": {
                                    "source_component": "test_comp",
                                    "event": "test_event",
                                    "error_type_normalised": "TestError",
                                    "level": "ERROR",
                                },
                                "doc_count": 50,
                                "first_seen": {"value_as_string": "2026-08-01T00:00:00Z"},
                                "last_seen": {"value_as_string": "2026-08-06T00:00:00Z"},
                                "sample_trace_ids": {"buckets": [{"key": "trace1"}]},
                                "sample_messages": {"buckets": [{"key": "msg1"}]},
                            }
                        ]
                    }
                }
            }
        )

        queries = TelemetryQueries(es_client=mock_client)
        clusters = await queries.get_error_patterns(window_hours=24, min_occurrences=10)

        call_args = mock_client.search.call_args
        aggs = call_args.kwargs.get("aggs", {})

        # Check the composite aggregation sources
        composite_agg = aggs.get("error_patterns", {}).get("composite", {})
        sources = composite_agg.get("sources", [])

        # Verify field names (should NOT have .keyword suffix)
        source_component_source = next((s for s in sources if "source_component" in s), None)
        assert source_component_source is not None
        assert source_component_source["source_component"]["terms"]["field"] == "source_component"

        event_source = next((s for s in sources if "event" in s), None)
        assert event_source is not None
        assert event_source["event"]["terms"]["field"] == "event"

        level_source = next((s for s in sources if "level" in s), None)
        assert level_source is not None
        assert level_source["level"]["terms"]["field"] == "level"

        # Verify cluster was parsed correctly
        assert len(clusters) > 0
        assert clusters[0].sample_trace_ids == ("trace1",)


class TestFieldValidatorGuard:
    """Verify guard against non-existent fields in aggregations."""

    @pytest.mark.asyncio
    async def test_field_validator_detects_missing_field(self) -> None:
        """Field validator should raise error for non-existent fields."""
        from personal_agent.telemetry.field_validator import FieldValidator

        mock_client = AsyncMock()

        # Mock ES field capabilities response
        mock_client.field_caps = AsyncMock(
            return_value={
                "fields": {
                    "trace_id": {"keyword": {"searchable": True}},
                    "outcome": {"keyword": {"searchable": True}},
                    # Note: 'nonexistent_field' is NOT in the response
                }
            }
        )

        validator = FieldValidator(es_client=mock_client)

        # Valid field should not raise
        await validator.validate_field("trace_id", index_pattern="agent-logs-*")

        # Invalid field should raise
        with pytest.raises(ValueError, match="Field 'nonexistent_field' not found"):
            await validator.validate_field("nonexistent_field", index_pattern="agent-logs-*")

    @pytest.mark.asyncio
    async def test_field_validator_caches_results(self) -> None:
        """Field validator should cache field capabilities to avoid repeated queries."""
        from personal_agent.telemetry.field_validator import FieldValidator

        mock_client = AsyncMock()
        mock_client.field_caps = AsyncMock(
            return_value={
                "fields": {
                    "trace_id": {"keyword": {"searchable": True}},
                }
            }
        )

        validator = FieldValidator(es_client=mock_client)

        # First call
        await validator.validate_field("trace_id", index_pattern="agent-logs-*")

        # Second call should use cache (field_caps not called again)
        await validator.validate_field("trace_id", index_pattern="agent-logs-*")

        # field_caps should only be called once due to caching
        assert mock_client.field_caps.call_count == 1
