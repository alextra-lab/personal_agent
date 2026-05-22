"""Test helpers for ADR-0074 / FRE-376 Phase 4 trace context.

Internal APIs are non-optional on :class:`TraceContext`. Tests that exercise
those APIs need a real (system-tagged) context to pass; constructing one
inline at every call site is noisy. Use :func:`make_test_ctx` instead.
"""

from __future__ import annotations

from personal_agent.telemetry.trace import SystemTraceContext, TraceContext


def make_test_ctx(source: str = "test") -> TraceContext:
    """Mint a :class:`TraceContext` suitable for tests.

    The returned context is tagged ``kind="system:<source>"`` so that any
    telemetry emitted during the test is clearly distinguishable from
    organic traffic in shared substrates.

    Args:
        source: Sub-source tag, joined into ``kind`` as ``"system:test"``
            by default. Pass a more specific value (e.g. ``"test_bash"``)
            when you want to filter for a single test in ES.

    Returns:
        A fresh :class:`TraceContext` with a generated trace_id.
    """
    return SystemTraceContext.new(source)
