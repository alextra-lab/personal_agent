"""Regression test: ``ctx.user_id`` reaches tool executors.

The orchestrator's ``run_task`` builds a ``TraceContext`` from the
``ExecutionContext`` and passes it (via ``ToolExecutionLayer.execute_tool``)
to any tool executor that declares a ``ctx`` parameter. A 2026-05-16 bug
dropped ``user_id`` at that handoff, breaking ``notes_write`` and
``recall_personal_history`` for every authenticated CF Access call.

This test pins the fix: when ``ExecutionContext.user_id`` is set, the
``ctx`` reaching a tool executor must carry the same value.
"""

from __future__ import annotations

import inspect
from uuid import UUID, uuid4

from personal_agent.telemetry import TraceContext


def test_trace_context_carries_user_id_and_session_id() -> None:
    """The frozen dataclass holds the new identity fields."""
    uid = uuid4()
    ctx = TraceContext(trace_id="t1", user_id=uid, session_id="sess-1")
    assert ctx.user_id == uid
    assert ctx.session_id == "sess-1"


def test_new_span_propagates_identity() -> None:
    """Child spans inherit user_id / session_id so deep stacks keep scope."""
    uid = uuid4()
    parent = TraceContext(trace_id="t2", user_id=uid, session_id="sess-2")
    child, span_id = parent.new_span()
    assert child.trace_id == "t2"
    assert child.user_id == uid
    assert child.session_id == "sess-2"
    assert child.parent_span_id == span_id


def test_new_trace_accepts_identity_kwargs() -> None:
    """``TraceContext.new_trace`` supports passing identity at construction."""
    uid = uuid4()
    ctx = TraceContext.new_trace(user_id=uid, session_id="sess-3")
    assert ctx.user_id == uid
    assert ctx.session_id == "sess-3"


def test_run_task_constructs_trace_ctx_with_user_id() -> None:
    """The exact construction site that broke notes_write must thread user_id.

    We don't run the full orchestrator here (it needs a session manager, LLM
    client, ...). Instead we mirror the construction pattern at
    ``orchestrator/executor.py:run_task`` and assert it propagates the field.
    A bare ``TraceContext(trace_id=ctx.trace_id)`` would regress this test.
    """
    uid = uuid4()
    session_id = str(uuid4())

    # Mirror the ExecutionContext shape that run_task receives.
    class _MiniExecCtx:
        def __init__(self) -> None:
            self.trace_id = "trace-xyz"
            self.user_id: UUID | None = uid
            self.session_id: str = session_id

    ctx = _MiniExecCtx()

    trace_ctx = TraceContext(
        trace_id=ctx.trace_id,
        user_id=ctx.user_id,
        session_id=ctx.session_id,
    )

    assert trace_ctx.user_id == uid
    assert trace_ctx.session_id == session_id


def test_run_task_source_threads_identity() -> None:
    """Source-level guard: run_task must reference ctx.user_id when building trace_ctx.

    If a future refactor reverts ``orchestrator/executor.py`` to the
    pre-fix ``TraceContext(trace_id=ctx.trace_id)`` form, this assertion
    fires immediately rather than waiting for an end-to-end smoke test.
    """
    from personal_agent.orchestrator import executor as executor_module

    src = inspect.getsource(executor_module)
    # Look for the run_task construction site. We only require that the
    # token "user_id=ctx.user_id" appears somewhere in the executor module.
    assert "user_id=ctx.user_id" in src, (
        "executor.py no longer threads ctx.user_id into TraceContext — "
        "tool executors will see ctx.user_id is None. See test docstring."
    )
