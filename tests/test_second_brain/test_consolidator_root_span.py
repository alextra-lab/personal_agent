"""Root-span coverage for consolidate_recent_captures (ADR-0129 D3, FRE-1069)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from personal_agent.second_brain.consolidator import SecondBrainConsolidator
from tests._helpers.log_capture import capture_log_records


class TestConsolidateRecentCapturesRootSpan:
    """AC-1/AC-2: consolidation opens exactly one root span, and every log record
    it emits carries that span's identity and ``kind``.
    """

    @pytest.mark.asyncio
    async def test_opens_exactly_one_root_span_with_full_log_coverage(self) -> None:
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")

        memory_service = MagicMock()
        memory_service.connected = True
        consolidator = SecondBrainConsolidator(memory_service=memory_service, tracer=tracer)

        with (
            patch(
                "personal_agent.second_brain.consolidator.read_captures",
                return_value=[],
            ),
            capture_log_records() as records,
        ):
            # No captures: cheapest path — logs "no_captures_to_consolidate" and
            # returns, without needing any other substrate mocked.
            result = await consolidator.consolidate_recent_captures(days=7)

        assert result["captures_processed"] == 0

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.parent is None
        assert span.attributes is not None
        assert span.attributes["personal_agent.kind"] == "system:consolidation"

        expected_trace_id = format(span.context.trace_id, "032x")
        expected_span_id = format(span.context.span_id, "016x")
        assert records, "expected at least one log record during consolidation"
        for record in records:
            assert record.get("trace_id") == expected_trace_id
            assert record.get("span_id") == expected_span_id
            assert record.get("kind") == "system:consolidation"
