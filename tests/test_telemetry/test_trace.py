"""Tests for TraceContext."""

import uuid

import pytest

from personal_agent.telemetry.trace import TraceContext


class TestTraceContext:
    """Test TraceContext functionality."""

    def test_new_trace_creates_unique_trace_id(self) -> None:
        """Test that new_trace creates a context with unique trace_id."""
        ctx1 = TraceContext.new_trace()
        ctx2 = TraceContext.new_trace()

        assert ctx1.trace_id != ctx2.trace_id
        assert ctx1.parent_span_id is None
        assert ctx2.parent_span_id is None
        # Verify it's a valid UUID
        uuid.UUID(ctx1.trace_id)
        uuid.UUID(ctx2.trace_id)

    def test_new_trace_has_no_parent_span(self) -> None:
        """Test that new trace has no parent span."""
        ctx = TraceContext.new_trace()
        assert ctx.parent_span_id is None

    def test_new_span_creates_child_context(self) -> None:
        """Test that new_span creates a child context with same trace_id."""
        parent = TraceContext.new_trace()
        child_ctx, span_id = parent.new_span()

        assert child_ctx.trace_id == parent.trace_id
        assert child_ctx.parent_span_id == span_id
        assert span_id != parent.trace_id
        # Verify span_id is a valid UUID
        uuid.UUID(span_id)

    def test_new_span_preserves_trace_id(self) -> None:
        """Test that new_span preserves trace_id across spans."""
        root = TraceContext.new_trace()
        child1_ctx, span1_id = root.new_span()
        child2_ctx, span2_id = child1_ctx.new_span()

        assert root.trace_id == child1_ctx.trace_id == child2_ctx.trace_id
        assert child1_ctx.parent_span_id == span1_id
        assert child2_ctx.parent_span_id == span2_id
        assert span1_id != span2_id

    def test_trace_context_is_immutable(self) -> None:
        """Test that TraceContext is immutable (frozen dataclass)."""
        from dataclasses import FrozenInstanceError

        ctx = TraceContext.new_trace()

        with pytest.raises(FrozenInstanceError):
            ctx.trace_id = "new-id"  # type: ignore[misc]

        with pytest.raises(FrozenInstanceError):
            ctx.parent_span_id = "new-parent"  # type: ignore[misc]

    def test_trace_context_equality(self) -> None:
        """Test TraceContext equality comparison."""
        ctx1 = TraceContext(trace_id="test-123", parent_span_id=None)
        ctx2 = TraceContext(trace_id="test-123", parent_span_id=None)
        ctx3 = TraceContext(trace_id="test-456", parent_span_id=None)

        assert ctx1 == ctx2
        assert ctx1 != ctx3

    def test_trace_context_with_parent_span(self) -> None:
        """Test TraceContext with parent_span_id set."""
        ctx = TraceContext(trace_id="test-123", parent_span_id="parent-456")
        assert ctx.trace_id == "test-123"
        assert ctx.parent_span_id == "parent-456"
