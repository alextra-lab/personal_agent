"""Tests for the span-tree primitives (ADR-0129 D3 / FRE-1067).

Provider isolation follows ``test_trace_otel_bridge.py``'s established pattern:
every test builds its own :class:`TracerProvider` with an
:class:`InMemorySpanExporter` and passes a tracer bound to it explicitly —
the process-global provider is never touched.
"""

from __future__ import annotations

import asyncio

import pytest
import structlog
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer
from opentelemetry.trace.status import StatusCode

from personal_agent.telemetry import spans

_HEX16 = 16


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def tracer(exporter: InMemorySpanExporter) -> Tracer:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("fre-1067-spans-test")


def _span_hex(span: otel_trace.Span) -> str:
    return format(span.get_span_context().span_id, "016x")


class TestOpenCloseStepSpan:
    """Step span lifecycle: attach on open, detach + end on close."""

    def test_open_step_span_makes_it_current(self, tracer: Tracer) -> None:
        step_span, token = spans.open_step_span(iteration=0, tracer=tracer)
        try:
            current = otel_trace.get_current_span()
            assert current.get_span_context().span_id == step_span.get_span_context().span_id
        finally:
            spans.close_step_span(step_span, token, tool_count=0)

    def test_close_step_span_restores_prior_context(self, tracer: Tracer) -> None:
        before = otel_trace.get_current_span()
        step_span, token = spans.open_step_span(iteration=0, tracer=tracer)
        spans.close_step_span(step_span, token, tool_count=0)
        after = otel_trace.get_current_span()
        assert after.get_span_context().span_id == before.get_span_context().span_id

    def test_close_step_span_ends_the_span(
        self, tracer: Tracer, exporter: InMemorySpanExporter
    ) -> None:
        step_span, token = spans.open_step_span(iteration=0, tracer=tracer)
        spans.close_step_span(step_span, token, tool_count=3)
        (finished,) = exporter.get_finished_spans()
        assert finished.end_time is not None

    def test_close_step_span_records_tool_count_attribute(
        self, tracer: Tracer, exporter: InMemorySpanExporter
    ) -> None:
        step_span, token = spans.open_step_span(iteration=0, tracer=tracer)
        spans.close_step_span(step_span, token, tool_count=5)
        (finished,) = exporter.get_finished_spans()
        assert finished.attributes is not None
        assert finished.attributes[spans.namespaced("step.tool_count")] == 5

    def test_open_step_span_records_iteration_attribute(
        self, tracer: Tracer, exporter: InMemorySpanExporter
    ) -> None:
        step_span, token = spans.open_step_span(iteration=2, tracer=tracer)
        spans.close_step_span(step_span, token, tool_count=0)
        (finished,) = exporter.get_finished_spans()
        assert finished.attributes is not None
        assert finished.attributes[spans.namespaced("step.iteration")] == 2

    def test_step_span_root_has_no_parent(
        self, tracer: Tracer, exporter: InMemorySpanExporter
    ) -> None:
        step_span, token = spans.open_step_span(iteration=0, tracer=tracer)
        spans.close_step_span(step_span, token, tool_count=0)
        (finished,) = exporter.get_finished_spans()
        assert finished.parent is None


class TestModelCallSpanAndToolCallSpanAutoParent:
    """Model-call and tool-call spans auto-parent off whatever is current —
    no explicit ``context=`` argument, matching AC-2's sibling shape.
    """

    def test_model_call_span_parents_to_open_step_span(
        self, tracer: Tracer, exporter: InMemorySpanExporter
    ) -> None:
        step_span, token = spans.open_step_span(iteration=0, tracer=tracer)
        with spans.model_call_span(
            role="primary", model="qwen3.6", provider="slm_local", tracer=tracer
        ) as call_span:
            pass
        spans.close_step_span(step_span, token, tool_count=0)

        finished = {s.name: s for s in exporter.get_finished_spans()}
        model_span = finished["model_call qwen3.6"]
        assert model_span.parent is not None
        assert model_span.parent.span_id == step_span.get_span_context().span_id
        assert call_span.get_span_context().span_id == model_span.context.span_id

    def test_tool_call_span_parents_to_open_step_span(
        self, tracer: Tracer, exporter: InMemorySpanExporter
    ) -> None:
        step_span, token = spans.open_step_span(iteration=0, tracer=tracer)
        with spans.tool_call_span(tool_name="web_search", tracer=tracer):
            pass
        spans.close_step_span(step_span, token, tool_count=1)

        finished = {s.name: s for s in exporter.get_finished_spans()}
        tool_span = finished["tool_call web_search"]
        assert tool_span.parent is not None
        assert tool_span.parent.span_id == step_span.get_span_context().span_id

    def test_model_call_and_tool_call_are_siblings_not_nested(
        self, tracer: Tracer, exporter: InMemorySpanExporter
    ) -> None:
        """AC-2: model-call and tool-call spans share the step as parent —
        neither is a child of the other.
        """
        step_span, token = spans.open_step_span(iteration=0, tracer=tracer)
        with spans.model_call_span(role="primary", model="m", provider="p", tracer=tracer):
            pass
        with spans.tool_call_span(tool_name="t", tracer=tracer):
            pass
        spans.close_step_span(step_span, token, tool_count=1)

        finished = {s.name: s for s in exporter.get_finished_spans()}
        model_span = finished["model_call m"]
        tool_span = finished["tool_call t"]
        step_id = step_span.get_span_context().span_id
        assert model_span.parent is not None
        assert tool_span.parent is not None
        assert model_span.parent.span_id == step_id
        assert tool_span.parent.span_id == step_id
        assert model_span.context.span_id != tool_span.context.span_id

    def test_model_call_span_sets_gen_ai_request_attributes(
        self, tracer: Tracer, exporter: InMemorySpanExporter
    ) -> None:
        from opentelemetry.semconv._incubating.attributes import (
            gen_ai_attributes as gen_ai,
        )

        with spans.model_call_span(
            role="sub_agent", model="claude-sonnet-5", provider="anthropic", tracer=tracer
        ):
            pass
        (finished,) = exporter.get_finished_spans()
        assert finished.attributes is not None
        assert finished.attributes[gen_ai.GEN_AI_OPERATION_NAME] == "sub_agent"
        assert finished.attributes[gen_ai.GEN_AI_SYSTEM] == "anthropic"
        assert finished.attributes[gen_ai.GEN_AI_REQUEST_MODEL] == "claude-sonnet-5"

    def test_tool_call_span_sets_tool_name_attribute(
        self, tracer: Tracer, exporter: InMemorySpanExporter
    ) -> None:
        with spans.tool_call_span(tool_name="recall_memory", tracer=tracer):
            pass
        (finished,) = exporter.get_finished_spans()
        assert finished.attributes is not None
        assert finished.attributes[spans.namespaced("tool.name")] == "recall_memory"

    def test_tool_call_span_records_exception_and_error_status_on_failure(
        self, tracer: Tracer, exporter: InMemorySpanExporter
    ) -> None:
        with pytest.raises(ValueError):
            with spans.tool_call_span(tool_name="broken_tool", tracer=tracer) as span:
                try:
                    raise ValueError("boom")
                except ValueError as e:
                    span.record_exception(e)
                    span.set_status(otel_trace.Status(StatusCode.ERROR, str(e)))
                    raise

        (finished,) = exporter.get_finished_spans()
        assert finished.status.status_code == StatusCode.ERROR
        assert any(event.name == "exception" for event in finished.events)


class TestNamespacedAttributeKey:
    def test_namespaced_prefixes_with_project_namespace(self) -> None:
        assert spans.namespaced("tool.name") == "personal_agent.tool.name"


class TestSemconvVersionPin:
    def test_assert_semconv_version_pinned_passes_against_installed(self) -> None:
        spans.assert_semconv_version_pinned()  # must not raise

    def test_pinned_constant_matches_installed_package(self) -> None:
        from importlib.metadata import version

        assert spans.SEMCONV_VERSION == version("opentelemetry-semantic-conventions")


class TestOpenCloseRootSpan:
    """Background entrypoint root span lifecycle (ADR-0129 D3, FRE-1069).

    Round-1 codex plan review flagged that a root span opened without an
    explicit empty parent context would silently inherit whatever span is
    already current — these tests assert root-ness holds even under an
    already-active unrelated span, and that closing restores rather than
    blindly clears the ``kind`` contextvar.
    """

    def test_open_root_span_is_root_even_under_an_active_parent(
        self, tracer: Tracer, exporter: InMemorySpanExporter
    ) -> None:
        with tracer.start_as_current_span("unrelated_parent"):
            span, token, cv_tokens = spans.open_root_span("scheduler.lifecycle", tracer=tracer)
            spans.close_root_span(span, token, cv_tokens)

        finished = {s.name: s for s in exporter.get_finished_spans()}
        assert finished["scheduler.lifecycle"].parent is None

    def test_open_root_span_makes_it_current(self, tracer: Tracer) -> None:
        span, token, cv_tokens = spans.open_root_span("consolidation", tracer=tracer)
        try:
            current = otel_trace.get_current_span()
            assert current.get_span_context().span_id == span.get_span_context().span_id
        finally:
            spans.close_root_span(span, token, cv_tokens)

    def test_close_root_span_restores_prior_context(self, tracer: Tracer) -> None:
        before = otel_trace.get_current_span()
        span, token, cv_tokens = spans.open_root_span("consolidation", tracer=tracer)
        spans.close_root_span(span, token, cv_tokens)
        after = otel_trace.get_current_span()
        assert after.get_span_context().span_id == before.get_span_context().span_id

    def test_close_root_span_ends_the_span(
        self, tracer: Tracer, exporter: InMemorySpanExporter
    ) -> None:
        span, token, cv_tokens = spans.open_root_span("consolidation", tracer=tracer)
        spans.close_root_span(span, token, cv_tokens)
        (finished,) = exporter.get_finished_spans()
        assert finished.end_time is not None

    def test_open_root_span_sets_kind_attribute(
        self, tracer: Tracer, exporter: InMemorySpanExporter
    ) -> None:
        span, token, cv_tokens = spans.open_root_span("joinability_probe", tracer=tracer)
        spans.close_root_span(span, token, cv_tokens)
        (finished,) = exporter.get_finished_spans()
        assert finished.attributes is not None
        assert finished.attributes[spans.namespaced("kind")] == "system:joinability_probe"

    def test_open_root_span_binds_kind_contextvar(self, tracer: Tracer) -> None:
        span, token, cv_tokens = spans.open_root_span("slm_health_probe", tracer=tracer)
        try:
            bound = structlog.contextvars.get_contextvars()
            assert bound["kind"] == "system:slm_health_probe"
        finally:
            spans.close_root_span(span, token, cv_tokens)

    def test_close_root_span_restores_a_pre_existing_kind_binding(self, tracer: Tracer) -> None:
        """The reset must restore a prior binding, not just clear to absent."""
        outer_tokens = structlog.contextvars.bind_contextvars(kind="outer")
        try:
            span, token, cv_tokens = spans.open_root_span("cache_erosion_probe", tracer=tracer)
            assert structlog.contextvars.get_contextvars()["kind"] == "system:cache_erosion_probe"
            spans.close_root_span(span, token, cv_tokens)
            assert structlog.contextvars.get_contextvars()["kind"] == "outer"
        finally:
            structlog.contextvars.reset_contextvars(**outer_tokens)

    @pytest.mark.asyncio
    async def test_two_interleaved_tasks_get_independent_root_spans(
        self, tracer: Tracer, exporter: InMemorySpanExporter
    ) -> None:
        """Concurrent asyncio tasks each opening a root span must not cross-contaminate."""

        async def _run(source: str, delay: float) -> str:
            span, token, cv_tokens = spans.open_root_span(source, tracer=tracer)
            try:
                await asyncio.sleep(delay)
                return structlog.contextvars.get_contextvars()["kind"]
            finally:
                spans.close_root_span(span, token, cv_tokens)

        results = await asyncio.gather(
            _run("scheduler.lifecycle", 0.01),
            _run("consolidation", 0.0),
        )

        assert results == ["system:scheduler.lifecycle", "system:consolidation"]
        finished = {s.name: s for s in exporter.get_finished_spans()}
        assert finished["scheduler.lifecycle"].parent is None
        assert finished["consolidation"].parent is None
        assert (
            finished["scheduler.lifecycle"].context.trace_id
            != finished["consolidation"].context.trace_id
        )
