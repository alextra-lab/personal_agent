"""FRE-1295: the session-digest sweep must mint a fresh trace per session, not
reuse the enclosing sweep tick's trace id.

The scheduler's session-summary sweep opens one root span for the whole tick
(FRE-1069/ADR-0129 D3). Before this fix, ``SystemTraceContext.new("session_summary",
session_id=...)`` inside ``_call_model`` read that span's trace id on every session
swept, so N sessions swept in one tick all wrote the SAME trace id onto their
``budget_reservations`` rows — breaking ADR-0074 §8c session-level joinability. The
fix opens a genuine nested root span per session so each one mints its own fresh
trace id.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tests._helpers.log_capture import capture_log_records

from personal_agent.second_brain.session_summary import _call_model
from personal_agent.telemetry.spans import close_root_span, open_root_span


def _mock_cloud_client() -> AsyncMock:
    client = AsyncMock()
    client.respond = AsyncMock(return_value={"content": '{"label": "x", "digest": {}}'})
    return client


@pytest.mark.asyncio
class TestSessionSummaryChildTraceIsolation:
    async def test_child_trace_differs_from_enclosing_tick_and_between_sessions(self) -> None:
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")

        with patch("personal_agent.llm_client.factory.get_llm_client_for_key") as mock_get_client:
            mock_client = _mock_cloud_client()
            mock_get_client.return_value = mock_client

            span, token, cv_tokens = open_root_span("scheduler.session_summary", tracer=tracer)
            try:
                enclosing_trace_id = format(span.context.trace_id, "032x")

                await _call_model(
                    "prompt one",
                    role_name="session_summary",
                    provider="anthropic",
                    session_id="session-a",
                    tracer=tracer,
                )
                first_ctx = mock_client.respond.call_args.kwargs["trace_ctx"]

                await _call_model(
                    "prompt two",
                    role_name="session_summary",
                    provider="anthropic",
                    session_id="session-b",
                    tracer=tracer,
                )
                second_ctx = mock_client.respond.call_args.kwargs["trace_ctx"]
            finally:
                close_root_span(span, token, cv_tokens)

        # AC-1: neither session's cost-record trace id is the tick's shared trace,
        # and the two sessions don't collide with each other.
        assert first_ctx.trace_id != enclosing_trace_id
        assert second_ctx.trace_id != enclosing_trace_id
        assert first_ctx.trace_id != second_ctx.trace_id
        assert first_ctx.session_id == "session-a"
        assert second_ctx.session_id == "session-b"

    async def test_parent_trace_id_is_recoverable_from_child_trace_logs(self) -> None:
        """AC-2: from a child trace id, the owning sweep tick is recoverable via logs."""
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")

        with patch("personal_agent.llm_client.factory.get_llm_client_for_key") as mock_get_client:
            mock_get_client.return_value = _mock_cloud_client()

            span, token, cv_tokens = open_root_span("scheduler.session_summary", tracer=tracer)
            try:
                enclosing_trace_id = format(span.context.trace_id, "032x")
                with capture_log_records() as records:
                    await _call_model(
                        "prompt",
                        role_name="session_summary",
                        provider="anthropic",
                        session_id="session-a",
                        tracer=tracer,
                    )
            finally:
                close_root_span(span, token, cv_tokens)

        linkage_records = [r for r in records if r.get("event") == "batch_child_trace_opened"]
        assert linkage_records, "expected a batch_child_trace_opened log record"
        record = linkage_records[0]
        assert record["parent_trace_id"] == enclosing_trace_id
        assert record["trace_id"] != enclosing_trace_id
        assert record["session_id"] == "session-a"
