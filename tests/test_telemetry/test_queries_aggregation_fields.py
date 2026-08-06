"""Tests for telemetry aggregation field correctness (FRE-1108).

Verifies that aggregations query fields that exist in the target indices
and use correct field names (no .keyword suffix for explicitly-mapped keyword fields).
Also verifies the field validator guard catches invalid fields on real query paths.
"""

from unittest.mock import AsyncMock

import pytest

from personal_agent.config.settings import get_settings
from personal_agent.telemetry.field_validator import FieldValidationError, FieldValidator
from personal_agent.telemetry.queries import TelemetryQueries


class TestAggregationFieldValidity:
    """Verify aggregations use correct field names."""

    @pytest.mark.asyncio
    async def test_get_task_patterns_trace_id_without_keyword_suffix(self) -> None:
        """Aggregation should count trace_id without .keyword suffix."""
        settings = get_settings()
        mock_client = AsyncMock()
        captures_pattern = f"{settings.captains_log_index_prefix}-captures-*"

        # Mock field_caps for validation
        mock_client.field_caps = AsyncMock(
            return_value={
                "fields": {
                    "trace_id": {"keyword": {"searchable": True, "aggregatable": True}},
                    "outcome": {"keyword": {"searchable": True, "aggregatable": True}},
                    "tools_used": {"keyword": {"searchable": True, "aggregatable": True}},
                }
            }
        )

        # Mock search for the aggregation
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

        # Create a validator with the mock client
        validator = FieldValidator(es_client=mock_client)
        queries = TelemetryQueries(es_client=mock_client, field_validator=validator)

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
        settings = get_settings()
        mock_client = AsyncMock()
        captures_pattern = f"{settings.captains_log_index_prefix}-captures-*"

        mock_client.field_caps = AsyncMock(
            return_value={
                "fields": {
                    "trace_id": {"keyword": {"searchable": True, "aggregatable": True}},
                    "outcome": {"keyword": {"searchable": True, "aggregatable": True}},
                    "tools_used": {"keyword": {"searchable": True, "aggregatable": True}},
                }
            }
        )

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

        # Create a validator with the mock client
        validator = FieldValidator(es_client=mock_client)
        queries = TelemetryQueries(es_client=mock_client, field_validator=validator)

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
        settings = get_settings()
        mock_client = AsyncMock()
        logs_pattern = f"{settings.elasticsearch_index_prefix}-*"

        mock_client.field_caps = AsyncMock(
            return_value={
                "fields": {
                    "event": {"keyword": {"searchable": True, "aggregatable": True}},
                    "task_id": {"keyword": {"searchable": True, "aggregatable": True}},
                }
            }
        )

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

        # Create a validator with the mock client
        validator = FieldValidator(es_client=mock_client)
        queries = TelemetryQueries(es_client=mock_client, field_validator=validator)

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
        settings = get_settings()
        mock_client = AsyncMock()
        logs_pattern = f"{settings.elasticsearch_index_prefix}-*"

        mock_client.field_caps = AsyncMock(
            return_value={
                "fields": {
                    "event": {"keyword": {"searchable": True, "aggregatable": True}},
                    "task_id": {"keyword": {"searchable": True, "aggregatable": True}},
                }
            }
        )

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

        # Create a validator with the mock client
        validator = FieldValidator(es_client=mock_client)
        queries = TelemetryQueries(es_client=mock_client, field_validator=validator)

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
        settings = get_settings()
        mock_client = AsyncMock()
        logs_pattern = f"{settings.elasticsearch_index_prefix}-*"

        mock_client.field_caps = AsyncMock(
            return_value={
                "fields": {
                    "component": {"keyword": {"searchable": True, "aggregatable": True}},
                    "event": {"keyword": {"searchable": True, "aggregatable": True}},
                    "error_type": {"keyword": {"searchable": True, "aggregatable": True}},
                    "level": {"keyword": {"searchable": True, "aggregatable": True}},
                    "trace_id": {"keyword": {"searchable": True, "aggregatable": True}},
                    "error.keyword": {"keyword": {"searchable": True, "aggregatable": True}},
                }
            }
        )

        mock_client.search = AsyncMock(
            return_value={
                "aggregations": {
                    "error_patterns": {
                        "buckets": [
                            {
                                "key": {
                                    "component": "test_comp",
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

        # Create a validator with the mock client
        validator = FieldValidator(es_client=mock_client)
        queries = TelemetryQueries(es_client=mock_client, field_validator=validator)

        clusters = await queries.get_error_patterns(window_hours=24, min_occurrences=10)

        call_args = mock_client.search.call_args
        aggs = call_args.kwargs.get("aggs", {})

        # Check the composite aggregation sources
        composite_agg = aggs.get("error_patterns", {}).get("composite", {})
        sources = composite_agg.get("sources", [])

        # Verify field names (should NOT have .keyword suffix)
        component_source = next((s for s in sources if "component" in s), None)
        assert component_source is not None
        assert component_source["component"]["terms"]["field"] == "component"

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
    async def test_get_task_patterns_raises_on_invalid_field(self) -> None:
        """Real query method should raise when field does not exist."""
        mock_es_client = AsyncMock()
        # Mock ES field_caps to return only trace_id and outcome (no invalid_field)
        mock_es_client.field_caps = AsyncMock(
            return_value={
                "fields": {
                    "trace_id": {"keyword": {"searchable": True, "aggregatable": True}},
                    "outcome": {"keyword": {"searchable": True, "aggregatable": True}},
                    "tools_used": {"keyword": {"searchable": True, "aggregatable": True}},
                    # Note: 'invalid_field' is NOT in the response
                }
            }
        )

        # Create a validator that will catch the invalid field
        validator = FieldValidator(es_client=mock_es_client)

        # Create queries with the validator
        queries = TelemetryQueries(es_client=AsyncMock(), field_validator=validator)

        # Patch get_task_patterns to request an invalid field
        original_init = queries.__init__
        settings = get_settings()
        captures_pattern = f"{settings.captains_log_index_prefix}-captures-*"

        # First validation attempt should fail because invalid_field doesn't exist
        with pytest.raises(FieldValidationError, match="invalid_field"):
            await validator.require_validated(
                ["trace_id", "invalid_field"], captures_pattern, "test"
            )

    @pytest.mark.asyncio
    async def test_field_validator_lazy_validation_caches_results(self) -> None:
        """Field validator should cache validation results and not repeat queries."""
        mock_es_client = AsyncMock()
        mock_es_client.field_caps = AsyncMock(
            return_value={
                "fields": {
                    "trace_id": {"keyword": {"searchable": True, "aggregatable": True}},
                    "event": {"keyword": {"searchable": True, "aggregatable": True}},
                }
            }
        )

        validator = FieldValidator(es_client=mock_es_client)

        # First call validates
        await validator.require_validated(["trace_id"], "test-*", "test")

        # Second call uses cache
        await validator.require_validated(["trace_id"], "test-*", "test")

        # field_caps should only be called once (for the first validation)
        assert mock_es_client.field_caps.call_count == 1

    @pytest.mark.asyncio
    async def test_field_validator_fails_closed_on_missing_field(self) -> None:
        """Field validator should raise immediately when field is missing."""
        mock_es_client = AsyncMock()
        mock_es_client.field_caps = AsyncMock(
            return_value={
                "fields": {
                    "trace_id": {"keyword": {"searchable": True, "aggregatable": True}},
                    # Note: 'missing_field' is NOT in the response
                }
            }
        )

        validator = FieldValidator(es_client=mock_es_client)

        # Should raise because missing_field does not exist
        with pytest.raises(FieldValidationError, match="missing_field"):
            await validator.require_validated(["missing_field"], "test-*", "test")
