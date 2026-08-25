"""FRE-1295: entity extraction must mint a fresh trace per capture, not reuse the
enclosing batch tick's trace id.

Consolidation opens one root span for the whole tick (FRE-1069/ADR-0129 D3).
Before this fix, ``SystemTraceContext.new("entity_extraction", session_id=...)``
read that span's trace id on every capture, so N captures/sessions processed in
one tick all wrote the SAME trace id onto their ``budget_reservations`` rows —
breaking ADR-0074 §8c session-level joinability (measured live: one trace id
carrying 3 different sessions' reservations). The fix opens a genuine nested
root span per capture so each one mints its own fresh trace id.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import orjson
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from personal_agent.second_brain.entity_extraction import extract_entities_and_relationships
from personal_agent.telemetry.spans import close_root_span, open_root_span
from tests._helpers.log_capture import capture_log_records

_MODEL_JSON: dict[str, Any] = {
    "summary": "User reviewed a healthcheck.",
    "entities": [],
    "relationships": [],
    "stances": [],
    "claims": [],
}
_USER_MSG = "Run a healthcheck on the stack."


def _mock_cloud_client() -> AsyncMock:
    client = AsyncMock()
    client.respond = AsyncMock(return_value={"content": orjson.dumps(_MODEL_JSON).decode("utf-8")})
    return client


@pytest.mark.asyncio
class TestChildTraceIsolation:
    async def test_child_trace_differs_from_enclosing_tick_and_between_captures(self) -> None:
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")

        mock_model_def = SimpleNamespace(
            provider="openai", id="gpt-5.4-mini", temperature=0.0, reasoning_effort=None
        )
        with (
            patch("personal_agent.second_brain.entity_extraction.load_model_config") as mock_cfg,
            patch(
                "personal_agent.second_brain.entity_extraction.resolve_role_model_key",
                return_value="gpt-5.4-mini",
            ),
            patch("personal_agent.llm_client.factory.get_llm_client_for_key") as mock_get_client,
        ):
            mock_cfg.return_value.models = {"gpt-5.4-mini": mock_model_def}
            mock_client = _mock_cloud_client()
            mock_get_client.return_value = mock_client

            span, token, cv_tokens = open_root_span("consolidation", tracer=tracer)
            try:
                enclosing_trace_id = format(span.context.trace_id, "032x")

                await extract_entities_and_relationships(
                    _USER_MSG, "reply one", session_id="session-a", tracer=tracer
                )
                first_ctx = mock_client.respond.call_args.kwargs["trace_ctx"]

                await extract_entities_and_relationships(
                    _USER_MSG, "reply two", session_id="session-b", tracer=tracer
                )
                second_ctx = mock_client.respond.call_args.kwargs["trace_ctx"]
            finally:
                close_root_span(span, token, cv_tokens)

        # AC-1: neither capture's cost-record trace id is the tick's shared trace,
        # and the two captures (different sessions) don't collide with each other.
        assert first_ctx.trace_id != enclosing_trace_id
        assert second_ctx.trace_id != enclosing_trace_id
        assert first_ctx.trace_id != second_ctx.trace_id
        assert first_ctx.session_id == "session-a"
        assert second_ctx.session_id == "session-b"

    async def test_child_span_is_exported_as_its_own_root_not_a_child_of_the_tick(self) -> None:
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")

        mock_model_def = SimpleNamespace(
            provider="openai", id="gpt-5.4-mini", temperature=0.0, reasoning_effort=None
        )
        with (
            patch("personal_agent.second_brain.entity_extraction.load_model_config") as mock_cfg,
            patch(
                "personal_agent.second_brain.entity_extraction.resolve_role_model_key",
                return_value="gpt-5.4-mini",
            ),
            patch("personal_agent.llm_client.factory.get_llm_client_for_key") as mock_get_client,
        ):
            mock_cfg.return_value.models = {"gpt-5.4-mini": mock_model_def}
            mock_get_client.return_value = _mock_cloud_client()

            span, token, cv_tokens = open_root_span("consolidation", tracer=tracer)
            try:
                await extract_entities_and_relationships(
                    _USER_MSG, "reply", session_id="session-a", tracer=tracer
                )
            finally:
                close_root_span(span, token, cv_tokens)

        spans = exporter.get_finished_spans()
        names = [s.name for s in spans]
        assert "consolidation" in names
        assert "entity_extraction" in names
        tick_span = next(s for s in spans if s.name == "consolidation")
        child_span = next(s for s in spans if s.name == "entity_extraction")
        # A genuine root: no OTel parent link, and its own distinct trace id.
        assert child_span.parent is None
        assert child_span.context.trace_id != tick_span.context.trace_id

    async def test_parent_trace_id_is_recoverable_from_child_trace_logs(self) -> None:
        """AC-2: from a child trace id, the owning tick is recoverable via logs."""
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")

        mock_model_def = SimpleNamespace(
            provider="openai", id="gpt-5.4-mini", temperature=0.0, reasoning_effort=None
        )
        with (
            patch("personal_agent.second_brain.entity_extraction.load_model_config") as mock_cfg,
            patch(
                "personal_agent.second_brain.entity_extraction.resolve_role_model_key",
                return_value="gpt-5.4-mini",
            ),
            patch("personal_agent.llm_client.factory.get_llm_client_for_key") as mock_get_client,
        ):
            mock_cfg.return_value.models = {"gpt-5.4-mini": mock_model_def}
            mock_get_client.return_value = _mock_cloud_client()

            span, token, cv_tokens = open_root_span("consolidation", tracer=tracer)
            try:
                enclosing_trace_id = format(span.context.trace_id, "032x")
                with capture_log_records() as records:
                    await extract_entities_and_relationships(
                        _USER_MSG, "reply", session_id="session-a", tracer=tracer
                    )
            finally:
                close_root_span(span, token, cv_tokens)

        linkage_records = [r for r in records if r.get("event") == "batch_child_trace_opened"]
        assert linkage_records, "expected a batch_child_trace_opened log record"
        record = linkage_records[0]
        assert record["parent_trace_id"] == enclosing_trace_id
        assert record["trace_id"] != enclosing_trace_id
        assert record["session_id"] == "session-a"
