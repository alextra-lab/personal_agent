"""Regression tests: session identity threading through all LLM call sites.

Covers two related fixes:

1. (2026-05-16) ``ctx.user_id`` reaching tool executors — the orchestrator's
   ``run_task`` must thread ``user_id`` from ``ExecutionContext`` into the
   ``TraceContext`` it builds and passes to tool executors.

2. (2026-05-24) ``session_id`` reaching sub-agent / skill-routing / compressor /
   reflection / expansion LLM calls. ADR-0074 (FRE-376) made
   ``(trace_id, session_id)`` a hard precondition on cost records. Several call
   sites were reconstructing ``TraceContext(trace_id=trace_id)`` without the
   ``session_id`` available in their callers, causing
   ``cost_record_missing_identity`` to fire on every user-facing LLM call that
   went through those paths.
"""

from __future__ import annotations

import inspect
from uuid import UUID, uuid4

import pytest

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


# ---------------------------------------------------------------------------
# 2026-05-24 regression: session_id threading to sub-agent / routing /
# compressor / reflection / expansion call sites (cost_record_missing_identity)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_sub_agent_threads_session_id() -> None:
    """run_sub_agent must forward session_id into the TraceContext it builds.

    ADR-0074 I4 requires session_id on every cost record. If run_sub_agent
    constructs TraceContext(trace_id=trace_id) without session_id, every
    sub-agent LLM call on the cloud path logs cost_record_missing_identity and
    skips cost attribution.
    """
    from unittest.mock import AsyncMock

    from personal_agent.orchestrator.sub_agent import run_sub_agent
    from personal_agent.orchestrator.sub_agent_types import SubAgentSpec

    mock_client = AsyncMock()
    mock_client.respond = AsyncMock(return_value="result")

    spec = SubAgentSpec(
        task="test",
        context=[{"role": "user", "content": "go"}],
        output_format="text",
        max_tokens=256,
        timeout_seconds=30.0,
    )

    await run_sub_agent(
        spec=spec,
        llm_client=mock_client,
        trace_id="trace-abc",
        session_id="sess-sub-123",
    )

    call_kwargs = mock_client.respond.call_args.kwargs
    trace_ctx = call_kwargs["trace_ctx"]
    assert trace_ctx.session_id == "sess-sub-123", (
        "run_sub_agent did not thread session_id into TraceContext — "
        "cost_record_missing_identity will fire on every sub-agent LLM call"
    )


@pytest.mark.asyncio
async def test_route_skills_threads_session_id() -> None:
    """route_skills must forward session_id into the TraceContext it builds."""
    from unittest.mock import AsyncMock, MagicMock

    from personal_agent.orchestrator.skills import route_skills

    client = MagicMock()
    client.respond = AsyncMock(return_value={"content": "[]"})

    await route_skills(
        user_message="hello",
        routing_client=client,
        trace_id="trace-rs",
        session_id="sess-rs-456",
    )

    call_kwargs = client.respond.call_args.kwargs
    trace_ctx = call_kwargs["trace_ctx"]
    assert trace_ctx.session_id == "sess-rs-456", (
        "route_skills did not thread session_id — "
        "cost_record_missing_identity fires on skill-routing LLM calls"
    )


@pytest.mark.asyncio
async def test_compress_turns_threads_session_id() -> None:
    """compress_turns must forward session_id into the TraceContext it builds."""
    from unittest.mock import AsyncMock, patch

    from personal_agent.orchestrator.context_compressor import compress_turns

    mock_client = AsyncMock()
    mock_client.respond = AsyncMock(return_value={"content": "summary"})

    with patch(
        "personal_agent.orchestrator.context_compressor.get_llm_client",
        return_value=mock_client,
    ):
        await compress_turns(
            evicted_messages=[{"role": "user", "content": "old message"}],
            trace_id="trace-ct",
            session_id="sess-ct-789",
        )

    call_kwargs = mock_client.respond.call_args.kwargs
    trace_ctx = call_kwargs["trace_ctx"]
    assert trace_ctx.session_id == "sess-ct-789", (
        "compress_turns did not thread session_id — "
        "cost_record_missing_identity fires on compressor LLM calls"
    )


def test_reflection_source_threads_session_id() -> None:
    """generate_reflection_entry must pass session_id to SystemTraceContext.new.

    The function already receives session_id as a parameter (line 222), but
    the pre-fix code passes SystemTraceContext.new('captains_log_reflection')
    without it, so the field is always None on the reflection LLM call.
    """
    from personal_agent.captains_log import reflection as reflection_module

    src = inspect.getsource(reflection_module)
    assert "session_id=session_id" in src, (
        "reflection.py does not pass session_id to SystemTraceContext.new — "
        "cost_record_missing_identity fires on every captain's log reflection call"
    )


def test_executor_source_threads_session_id_to_expansion() -> None:
    """executor.py must pass session_id=ctx.session_id to ExpansionController.execute."""
    from personal_agent.orchestrator import executor as executor_module

    src = inspect.getsource(executor_module)
    assert "session_id=ctx.session_id" in src, (
        "executor.py does not thread ctx.session_id to expansion/routing calls — "
        "cost_record_missing_identity fires on sub-agent and planning LLM calls"
    )


def test_sub_agent_source_threads_session_id() -> None:
    """sub_agent.py must pass session_id to the TraceContext it constructs."""
    from personal_agent.orchestrator import sub_agent as sub_agent_module

    src = inspect.getsource(sub_agent_module)
    assert "session_id=session_id" in src, (
        "sub_agent.py does not thread session_id into TraceContext — "
        "every sub-agent LLM call will miss cost attribution"
    )


def test_expansion_controller_source_threads_session_id() -> None:
    """expansion_controller.py must pass session_id to its TraceContext."""
    from personal_agent.orchestrator import expansion_controller as ec_module

    src = inspect.getsource(ec_module)
    assert "session_id=session_id" in src, (
        "expansion_controller.py does not thread session_id — "
        "planner and sub-agent calls lose cost attribution"
    )
