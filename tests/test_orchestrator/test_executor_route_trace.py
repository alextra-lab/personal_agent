"""Tests that ``execute_task`` is wrapped by the ADR-0088 observe_topology seam (FRE-513).

The seam owns the single durable route-trace write at the turn terminal (replacing the
interim ``_write_route_trace`` site). These tests verify the wiring: running a turn through
``execute_task`` resolves ``ctx.topology`` and fires exactly one durable ledger write on
every terminal path (success, handled ``Exception``, ``asyncio.CancelledError``), and a
failing ledger never breaks the turn. The seam's own enter/exit/event mechanics are
covered in ``tests/observability/topology/test_seam.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from personal_agent.governance.models import Mode
from personal_agent.observability.topology import seam as seam_mod
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


def _patch_seam_ledger(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Patch the seam's durable ledger with an AsyncMock and return it."""
    ledger = AsyncMock()
    ledger.fetch_authoritative_cost = AsyncMock(return_value=(0.0, 0, 0))
    ledger.write = AsyncMock()
    monkeypatch.setattr(seam_mod, "get_route_trace_ledger", lambda: ledger)
    monkeypatch.setattr(seam_mod, "get_event_bus", lambda: AsyncMock())
    return ledger


async def test_seam_writes_route_trace_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = _patch_seam_ledger(monkeypatch)

    async def _complete(ctx, sm, tc):  # type: ignore[no-untyped-def]
        ctx.final_reply = "done"
        return TaskState.COMPLETED

    monkeypatch.setattr(executor_mod, "step_init", _complete)

    ctx = _ctx()
    result = await executor_mod.execute_task(ctx, SessionManager())

    assert result.state == TaskState.COMPLETED
    assert ctx.topology == "primary"
    ledger.write.assert_awaited_once()


async def test_seam_writes_route_trace_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = _patch_seam_ledger(monkeypatch)

    async def _boom(ctx, sm, tc):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    monkeypatch.setattr(executor_mod, "step_init", _boom)

    ctx = _ctx()
    result = await executor_mod.execute_task(ctx, SessionManager())

    # Fatal error is caught, turn marked FAILED, and the seam still writes the row.
    assert result.state == TaskState.FAILED
    ledger.write.assert_awaited_once()


async def test_seam_writes_route_trace_on_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = _patch_seam_ledger(monkeypatch)

    async def _cancel(ctx, sm, tc):  # type: ignore[no-untyped-def]
        raise asyncio.CancelledError()

    monkeypatch.setattr(executor_mod, "step_init", _cancel)

    ctx = _ctx()
    with pytest.raises(asyncio.CancelledError):
        await executor_mod.execute_task(ctx, SessionManager())

    # The seam's finally still attempted the durable write before cancellation propagated.
    ledger.write.assert_awaited_once()


async def test_seam_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = _patch_seam_ledger(monkeypatch)
    ledger.write = AsyncMock(side_effect=RuntimeError("db down"))

    async def _complete(ctx, sm, tc):  # type: ignore[no-untyped-def]
        ctx.final_reply = "done"
        return TaskState.COMPLETED

    monkeypatch.setattr(executor_mod, "step_init", _complete)

    ctx = _ctx()
    # A raising ledger must be swallowed by the seam — the turn still completes.
    result = await executor_mod.execute_task(ctx, SessionManager())
    assert result.state == TaskState.COMPLETED
