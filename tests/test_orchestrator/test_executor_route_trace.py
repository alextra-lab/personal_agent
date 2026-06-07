"""Tests for the route-trace emit site in ``execute_task`` (FRE-452 / ADR-0088 D6).

Verifies the single ``finally``-guarded durable write fires on every terminal path
(success, handled ``Exception``, ``asyncio.CancelledError``) and that the writer itself is
best-effort — a failing ledger never breaks the turn.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from personal_agent.governance.models import Mode
from personal_agent.orchestrator import executor as executor_mod
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.session import SessionManager
from personal_agent.orchestrator.types import ExecutionContext, TaskState
from personal_agent.telemetry.trace import TraceContext

pytestmark = pytest.mark.asyncio


def _ctx() -> ExecutionContext:
    """Build a minimal eval-mode execution context (suppresses capture side effects)."""
    sm = SessionManager()
    session_id = sm.create_session(Mode.NORMAL, Channel.CHAT)
    trace_ctx = TraceContext.new_trace()
    return ExecutionContext(
        session_id=session_id,
        trace_id=trace_ctx.trace_id,
        user_message="hello",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        eval_mode=True,
    )


async def test_writes_route_trace_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    write = AsyncMock()
    monkeypatch.setattr(executor_mod, "_write_route_trace", write)

    async def _complete(ctx, sm, tc):  # type: ignore[no-untyped-def]
        ctx.final_reply = "done"
        return TaskState.COMPLETED

    monkeypatch.setattr(executor_mod, "step_init", _complete)

    ctx = _ctx()
    result = await executor_mod.execute_task(ctx, SessionManager())

    assert result.state == TaskState.COMPLETED
    write.assert_awaited_once()


async def test_writes_route_trace_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    write = AsyncMock()
    monkeypatch.setattr(executor_mod, "_write_route_trace", write)

    async def _boom(ctx, sm, tc):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    monkeypatch.setattr(executor_mod, "step_init", _boom)

    ctx = _ctx()
    result = await executor_mod.execute_task(ctx, SessionManager())

    # Fatal error is caught, turn marked FAILED, and the row is still written.
    assert result.state == TaskState.FAILED
    write.assert_awaited_once()


async def test_writes_route_trace_on_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    write = AsyncMock()
    monkeypatch.setattr(executor_mod, "_write_route_trace", write)

    async def _cancel(ctx, sm, tc):  # type: ignore[no-untyped-def]
        raise asyncio.CancelledError()

    monkeypatch.setattr(executor_mod, "step_init", _cancel)

    ctx = _ctx()
    with pytest.raises(asyncio.CancelledError):
        await executor_mod.execute_task(ctx, SessionManager())

    # The finally still attempted the durable write before cancellation propagated.
    write.assert_awaited_once()


async def test_write_route_trace_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    failing_ledger = SimpleNamespace(
        fetch_authoritative_cost=AsyncMock(return_value=(0.0, 0, 0)),
        write=AsyncMock(side_effect=RuntimeError("db down")),
    )
    monkeypatch.setattr(
        "personal_agent.observability.route_trace.get_route_trace_ledger",
        lambda: failing_ledger,
    )

    # A raising ledger must be swallowed — no exception escapes _write_route_trace.
    await executor_mod._write_route_trace(_ctx())
