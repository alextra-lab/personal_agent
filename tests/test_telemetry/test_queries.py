"""Tests for telemetry Elasticsearch analytics queries."""

import asyncio
import time
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
        # FRE-1108: Mock field_caps for lazy validation
        mock_client.field_caps = AsyncMock(
            return_value={
                "fields": {
                    "trace_id": {"keyword": {"searchable": True}},
                    "outcome": {"keyword": {"searchable": True}},
                    "tools_used": {"keyword": {"searchable": True}},
                }
            }
        )
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

    async def test_get_trace_events_builds_exact_term_query(self) -> None:
        """FRE-1034: trace_id is keyword-mapped — the query must be an exact term match."""
        mock_client = AsyncMock()
        mock_client.search.return_value = {"hits": {"hits": []}}
        queries = TelemetryQueries(es_client=mock_client)

        await queries.get_trace_events("trace-abc")

        _, kwargs = mock_client.search.call_args
        assert kwargs["query"] == {"term": {"trace_id": "trace-abc"}}
        assert kwargs["index"] == f"{queries._logs_index_prefix}-*"
        assert kwargs["sort"] == [{"@timestamp": "asc"}]

    async def test_get_trace_events_translates_es_field_names(self) -> None:
        """FRE-1034: event_type/@timestamp must be translated to event/timestamp.

        ElasticsearchHandler stores the structlog `event` key as `event_type` and
        stamps `@timestamp` instead of the original `timestamp` field — every
        downstream consumer of trace events (_summarize_telemetry,
        _extract_failure_excerpt, build_prompt_manifest) reads `event`/`timestamp`,
        so an untranslated ES hit would silently look like an event-less entry.
        """
        mock_client = AsyncMock()
        mock_client.search.return_value = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "@timestamp": "2026-07-28T16:35:34.034593",
                            "event_type": "tool_executed",
                            "trace_id": "trace-abc",
                            "tool": "read_file",
                            "duration_ms": 12.5,
                            "success": True,
                        }
                    }
                ]
            }
        }
        queries = TelemetryQueries(es_client=mock_client)

        events = await queries.get_trace_events("trace-abc")

        assert len(events) == 1
        entry = events[0]
        assert entry["event"] == "tool_executed"
        assert entry["timestamp"] == "2026-07-28T16:35:34.034593"
        assert "event_type" not in entry
        assert "@timestamp" not in entry
        # Custom fields pass through unchanged.
        assert entry["tool"] == "read_file"
        assert entry["duration_ms"] == 12.5
        assert entry["success"] is True

    async def test_get_trace_events_propagates_es_errors(self) -> None:
        """FRE-1034: this method makes no fallback decision — callers decide."""
        mock_client = AsyncMock()
        mock_client.search.side_effect = RuntimeError("es unreachable")
        queries = TelemetryQueries(es_client=mock_client)

        with pytest.raises(RuntimeError, match="es unreachable"):
            await queries.get_trace_events("trace-abc")

    async def test_get_trace_events_concurrent_calls_do_not_scale_linearly(self) -> None:
        """FRE-1034: concurrent ES fetches must not serialize like the GIL-bound scan.

        Master measured the threaded file scan scaling linearly with concurrency
        (1/2/4/8 concurrent -> 0.113s/0.352s/0.706s/1.544s — the pure-Python
        `line_filter not in line` check holds the GIL, so threads serialize) versus
        Elasticsearch over the same range (0.037s/0.036s/0.124s/0.190s — network I/O
        releases the GIL, so queries genuinely overlap). This test reproduces that
        distinction deterministically with a simulated network delay: if
        get_trace_events serialized the way the thread scan does, 8 concurrent calls
        would take ~8x a single call; real async I/O keeps it close to flat.
        """
        simulated_latency = 0.05

        async def fake_search(*args: object, **kwargs: object) -> dict:
            await asyncio.sleep(simulated_latency)
            return {"hits": {"hits": []}}

        mock_client = AsyncMock()
        mock_client.search = fake_search
        queries = TelemetryQueries(es_client=mock_client)

        async def run_concurrent(n: int) -> float:
            start = time.monotonic()
            await asyncio.gather(*(queries.get_trace_events(f"trace-{i}") for i in range(n)))
            return time.monotonic() - start

        single_call_duration = await run_concurrent(1)
        eight_concurrent_duration = await run_concurrent(8)

        # Serialized (GIL-bound), 8 concurrent would take ~8x a single call.
        # Genuinely concurrent I/O keeps it within a small multiple regardless of N.
        assert eight_concurrent_duration < single_call_duration * 3, (
            f"8 concurrent calls took {eight_concurrent_duration:.3f}s vs a single call's "
            f"{single_call_duration:.3f}s — scaling looks linear/serialized, not concurrent"
        )
