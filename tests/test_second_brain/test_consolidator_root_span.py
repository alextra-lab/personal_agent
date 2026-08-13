"""Root-span coverage for consolidate_recent_captures (ADR-0129 D3, FRE-1069)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from personal_agent.captains_log.capture import TaskCapture
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

    @pytest.mark.asyncio
    async def test_run_level_span_identity_does_not_clobber_per_capture_identity(self) -> None:
        """Regression (found in self-review): _add_span_context unconditionally
        overwrites event_dict["trace_id"] from the active span on every log call —
        it does not defer to an already-bound contextvar or an explicit kwarg. Before
        this ticket, no span was active during consolidation, so per-capture log
        lines correctly carried the capture's OWN trace_id via
        structlog.contextvars.bound_contextvars(). Opening a run-level root span here
        means every per-capture log line's "trace_id" field is now the RUN's
        identity, not the capture's — so the capture's own identity must survive
        under a distinct field ("capture_trace_id") for per-capture correlation
        (e.g. tracing one failed capture back to the user turn it came from) to keep
        working at all.
        """
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")

        capture = TaskCapture(
            trace_id="original-capture-trace-id",
            timestamp=datetime.now(timezone.utc),
            user_message="hello",
            assistant_response="hi",
            session_id="session-a",
            tools_used=[],
            duration_ms=100,
            outcome="completed",
            user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        )

        memory_service = MagicMock()
        memory_service.connected = True
        memory_service.turn_exists = AsyncMock(return_value=False)
        consolidator = SecondBrainConsolidator(memory_service=memory_service, tracer=tracer)

        with (
            patch(
                "personal_agent.second_brain.consolidator.read_captures",
                return_value=[capture],
            ),
            patch.object(consolidator, "_process_capture", AsyncMock(return_value={})),
            capture_log_records() as records,
        ):
            await consolidator.consolidate_recent_captures(days=7)

        span = exporter.get_finished_spans()[0]
        run_trace_id = format(span.context.trace_id, "032x")

        capture_log = next(
            r for r in records if r.get("event") == "consolidation_processing_capture"
        )
        # The run's own span identity is still present (AC-1/AC-2) ...
        assert capture_log["trace_id"] == run_trace_id
        # ... and the capture's own identity survives under its own field, distinct
        # from — and NOT overwritten by — the run's.
        assert capture_log["capture_trace_id"] == "original-capture-trace-id"
        assert capture_log["capture_trace_id"] != capture_log["trace_id"]
