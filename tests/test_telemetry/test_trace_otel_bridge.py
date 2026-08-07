"""Tests for the TraceContext ↔ OpenTelemetry context bridge (ADR-0129 D1 / FRE-1065).

The bridge makes ``TraceContext`` *read* its trace identity from the active OTel
span instead of minting its own, while retaining the five identity fields that
are load-bearing outside telemetry (``user_id``, ``session_id``, ``kind``,
``eval_mode``, ``authenticated``).

Provider isolation: every test builds its **own** :class:`TracerProvider` with an
:class:`InMemorySpanExporter` and takes a tracer from it directly.
``trace.set_tracer_provider()`` is never called, so no process-global state is
mutated and these tests neither depend on nor disturb ``configure_tracing()``.
That works because ``start_as_current_span`` publishes the span into the ambient
*context* regardless of which provider minted the tracer — and the ambient
context is the only thing the bridge reads.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer

from personal_agent.telemetry.trace import SystemTraceContext, TraceContext

_HEX32 = 32
_HEX16 = 16


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    """An in-memory span exporter collecting finished spans (AC-6's instrument)."""
    return InMemorySpanExporter()


@pytest.fixture
def tracer(exporter: InMemorySpanExporter) -> Tracer:
    """A tracer bound to a local provider — never registered globally."""
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("fre-1065-bridge-test")


def _trace_hex(span: otel_trace.Span) -> str:
    """Render a span's trace id the way the bridge and FRE-1064's processor do."""
    return format(span.get_span_context().trace_id, "032x")


def _span_hex(span: otel_trace.Span) -> str:
    """Render a span's own span id as 16 lowercase hex chars."""
    return format(span.get_span_context().span_id, "016x")


class TestReadsRatherThanMints:
    """AC-6 — identity is read from the active span, not minted alongside it."""

    def test_new_trace_adopts_the_active_span_trace_id(self, tracer: Tracer) -> None:
        """Inside a span, new_trace() reports that span's trace id."""
        with tracer.start_as_current_span("root") as span:
            ctx = TraceContext.new_trace()

            assert ctx.trace_id == _trace_hex(span)

    def test_span_id_matches_the_active_span(self, tracer: Tracer) -> None:
        """Inside a span, span_id reports that span's id."""
        with tracer.start_as_current_span("root") as span:
            ctx = TraceContext.new_trace()

            assert ctx.span_id == _span_hex(span)

    def test_exported_span_carries_the_ids_the_context_reported(
        self, tracer: Tracer, exporter: InMemorySpanExporter
    ) -> None:
        """The ids are asserted against the *exported* span, not just the live context.

        This is the discriminator AC-6 asks for: a bridge that minted its own id
        alongside the span's would disagree with what actually reached the
        exporter, which is the divergence the bridge exists to end.
        """
        with tracer.start_as_current_span("root"):
            ctx = TraceContext.new_trace()
            captured = (ctx.trace_id, ctx.span_id)

        (finished,) = exporter.get_finished_spans()
        assert captured == (
            format(finished.context.trace_id, "032x"),
            format(finished.context.span_id, "016x"),
        )

    def test_system_context_also_adopts_the_active_span(self, tracer: Tracer) -> None:
        """SystemTraceContext.new() reads the ambient span too.

        Roughly twenty of its call sites sit inside served HTTP handlers, which
        now run under FRE-1064's root span. Minting there would guarantee the
        context disagreed with its own log records.
        """
        with tracer.start_as_current_span("root") as span:
            ctx = SystemTraceContext.new("scheduler")

            assert ctx.trace_id == _trace_hex(span)


class TestSpanIdIsGuardedToTheSameTrace:
    """The span_id property never pairs ids drawn from two different traces."""

    def test_directly_constructed_context_reports_no_span_id(self, tracer: Tracer) -> None:
        """A hand-built context inside a foreign span must not borrow its span id.

        TraceContext is constructed directly in eleven places in src/, so this is
        a real shape, not a hypothetical one.
        """
        with tracer.start_as_current_span("unrelated"):
            ctx = TraceContext(trace_id=uuid.uuid4().hex)

            assert ctx.span_id is None

    def test_span_id_is_none_after_the_originating_span_closes(self, tracer: Tracer) -> None:
        """Once the span ends, trace_id is retained but span_id goes away."""
        with tracer.start_as_current_span("root") as span:
            ctx = TraceContext.new_trace()
            adopted = _trace_hex(span)

        assert ctx.trace_id == adopted
        assert ctx.span_id is None

    def test_nested_span_tracks_the_current_span_within_one_trace(self, tracer: Tracer) -> None:
        """In a child span of the same trace, span_id follows the child; trace_id holds."""
        with tracer.start_as_current_span("root") as root:
            ctx = TraceContext.new_trace()

            with tracer.start_as_current_span("child") as child:
                assert ctx.trace_id == _trace_hex(root) == _trace_hex(child)
                assert ctx.span_id == _span_hex(child)
                assert ctx.span_id != _span_hex(root)

    def test_span_id_is_none_inside_an_unrelated_trace(self, tracer: Tracer) -> None:
        """A context carried into a different trace reports no span id."""
        with tracer.start_as_current_span("first") as first:
            ctx = TraceContext.new_trace()

        with tracer.start_as_current_span("second") as second:
            assert _trace_hex(second) != _trace_hex(first)
            assert ctx.span_id is None

    @pytest.mark.asyncio
    async def test_async_task_started_outside_the_span_reports_no_span_id(
        self, tracer: Tracer
    ) -> None:
        """OTel context propagation is respected across an asyncio task boundary."""
        with tracer.start_as_current_span("root"):
            ctx = TraceContext.new_trace()

        async def read_span_id() -> str | None:
            return ctx.span_id

        assert await asyncio.create_task(read_span_id()) is None


class TestFallbackWhenNoSpanIsActive:
    """D-f — an invalid span context is never adopted."""

    def test_new_trace_mints_when_no_span_is_active(self) -> None:
        """With no span, new_trace() mints a valid id rather than the all-zero one."""
        ctx = TraceContext.new_trace()

        assert ctx.trace_id != "0" * _HEX32
        assert int(ctx.trace_id, 16) != 0

    def test_system_context_mints_when_no_span_is_active(self) -> None:
        """Same guard on the system factory — the nil id would collide on every row."""
        ctx = SystemTraceContext.new("scheduler")

        assert ctx.trace_id != "0" * _HEX32
        assert int(ctx.trace_id, 16) != 0

    def test_minted_ids_stay_unique(self) -> None:
        """Distinct traces still get distinct ids when nothing is active."""
        assert TraceContext.new_trace().trace_id != TraceContext.new_trace().trace_id
        assert SystemTraceContext.new("a").trace_id != SystemTraceContext.new("b").trace_id

    def test_span_id_is_none_with_no_active_span(self) -> None:
        """No span, no span id — ADR-0129 D8 drops sentinels rather than inventing one."""
        assert TraceContext.new_trace().span_id is None


class TestIdentityFormat:
    """D-c — read and minted ids are indistinguishable in shape, and UUID-coercible."""

    def test_minted_id_is_32_lowercase_hex(self) -> None:
        """A minted trace id matches the OTel rendering FRE-1064's processor emits."""
        trace_id = TraceContext.new_trace().trace_id

        assert len(trace_id) == _HEX32
        assert trace_id == trace_id.lower()
        assert int(trace_id, 16) >= 0

    def test_read_id_is_32_lowercase_hex(self, tracer: Tracer) -> None:
        """An adopted trace id has the same shape as a minted one."""
        with tracer.start_as_current_span("root"):
            trace_id = TraceContext.new_trace().trace_id

        assert len(trace_id) == _HEX32
        assert trace_id == trace_id.lower()

    def test_span_id_is_16_lowercase_hex(self, tracer: Tracer) -> None:
        """Span ids render as 16 hex chars, matching the structlog processor."""
        with tracer.start_as_current_span("root"):
            span_id = TraceContext.new_trace().span_id

        assert span_id is not None
        assert len(span_id) == _HEX16
        assert span_id == span_id.lower()

    def test_trace_ids_remain_uuid_coercible(self, tracer: Tracer) -> None:
        """Both forms parse as UUIDs — trace_id is a Postgres UUID column in 8+ tables."""
        minted = TraceContext.new_trace().trace_id
        with tracer.start_as_current_span("root"):
            adopted = TraceContext.new_trace().trace_id

        assert uuid.UUID(minted)
        assert uuid.UUID(adopted)


class TestIdentityFieldsSurviveTheBridge:
    """AC-1…AC-5 — the five fields the bridge must not disturb, at context altitude."""

    def test_new_trace_retains_user_session_and_authenticated(self, tracer: Tracer) -> None:
        """AC-1 / AC-2 / AC-3 — the fields driving scoping and visibility survive adoption."""
        user_id = uuid.uuid4()
        with tracer.start_as_current_span("root"):
            ctx = TraceContext.new_trace(user_id=user_id, session_id="sess-1", authenticated=True)

        assert ctx.user_id == user_id
        assert ctx.session_id == "sess-1"
        assert ctx.authenticated is True

    def test_authenticated_false_is_not_widened(self, tracer: Tracer) -> None:
        """AC-1 — the unauthenticated half must stay unauthenticated.

        Widening here would be a data-access regression, not merely a failing
        test: 'group'-visibility memory is revealed on this field.
        """
        with tracer.start_as_current_span("root"):
            ctx = TraceContext.new_trace()

        assert ctx.authenticated is False

    def test_eval_mode_survives_adoption(self, tracer: Tracer) -> None:
        """AC-4 — eval_mode still reaches the FRE-375 substrate guard."""
        with tracer.start_as_current_span("root"):
            ctx = TraceContext(trace_id=uuid.uuid4().hex, eval_mode=True)
            child, _ = ctx.new_span()

        assert ctx.eval_mode is True
        assert child.eval_mode is True

    def test_kind_still_separates_system_from_organic(self, tracer: Tracer) -> None:
        """AC-5 — adopting a shared span must not blur the organic/scheduled split."""
        with tracer.start_as_current_span("root"):
            system_ctx = SystemTraceContext.new("scheduler")
            user_ctx = TraceContext.new_trace()

        assert system_ctx.kind == "system:scheduler"
        assert system_ctx.is_system is True
        assert user_ctx.kind == "user"
        assert user_ctx.is_system is False

    def test_shared_trace_id_still_distinguishes_kind(self, tracer: Tracer) -> None:
        """AC-5's real test: same trace id, still separable by kind.

        Under the bridge two contexts minted in one request share a trace id, so
        kind — not the id — is what keeps scheduled work distinguishable.
        """
        with tracer.start_as_current_span("root"):
            system_ctx = SystemTraceContext.new("monitor")
            user_ctx = TraceContext.new_trace()

        assert system_ctx.trace_id == user_ctx.trace_id
        assert system_ctx.is_system != user_ctx.is_system

    def test_all_five_fields_survive_new_span(self, tracer: Tracer) -> None:
        """Child spans carry the full identity tuple through unchanged."""
        user_id = uuid.uuid4()
        with tracer.start_as_current_span("root"):
            parent = TraceContext.new_trace(
                user_id=user_id, session_id="sess-2", authenticated=True
            )
            child, _ = parent.new_span()

        assert (child.user_id, child.session_id, child.authenticated) == (
            user_id,
            "sess-2",
            True,
        )
        assert (child.kind, child.eval_mode) == (parent.kind, parent.eval_mode)
        assert child.trace_id == parent.trace_id
