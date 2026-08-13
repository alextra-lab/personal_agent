"""Root-span coverage for the joinability probe scheduler runner (ADR-0129 D3, FRE-1069)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests._helpers.log_capture import capture_log_records


class TestRunScheduledProbeRootSpan:
    """AC-1/AC-2: the probe opens exactly one root span, and every log record it
    emits carries that span's identity and ``kind``.
    """

    @pytest.mark.asyncio
    async def test_opens_exactly_one_root_span_with_full_log_coverage(self) -> None:
        from personal_agent.observability.joinability.scheduler_runner import (
            run_scheduled_probe,
        )

        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")

        with (
            patch(
                "personal_agent.observability.joinability.scheduler_runner._open_pg_pool",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "personal_agent.observability.joinability.scheduler_runner._open_neo4j_driver",
                return_value=None,
            ),
            patch(
                "personal_agent.observability.joinability.scheduler_runner._open_redis",
                new=AsyncMock(return_value=None),
            ),
            capture_log_records() as records,
        ):
            # es_client=None: pg_pool=None makes _pick_session return None too, so
            # this reaches the cheapest "skipped" path with no substrate needed.
            result = await run_scheduled_probe(es_client=None, tracer=tracer)

        assert result is not None
        assert result.outcome == "skipped"

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.parent is None
        assert span.attributes is not None
        assert span.attributes["personal_agent.kind"] == "system:joinability_probe"

        expected_trace_id = format(span.context.trace_id, "032x")
        expected_span_id = format(span.context.span_id, "016x")
        assert records, "expected at least one log record during the probe"
        for record in records:
            assert record.get("trace_id") == expected_trace_id
            assert record.get("span_id") == expected_span_id
            assert record.get("kind") == "system:joinability_probe"
