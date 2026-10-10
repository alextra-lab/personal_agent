"""Tests for the ADR-0088 observe_topology emission seam (FRE-513).

The seam is the mandatory boundary every topology passes through: on enter it publishes
``turn.topology_entered`` (best-effort bus), on exit it writes the durable route-trace row
directly (bus-independent, D8) and publishes ``turn.completed``. Failures in either sink
must never break the wrapped turn; ``CancelledError`` must still propagate.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from personal_agent.events.models import (
    TopologyEnteredEvent,
    TurnCompletedEvent,
)
from personal_agent.observability.first_token import (
    clear_first_token_clock,
    request_elapsed_ms,
    start_first_token_clock,
)
from personal_agent.observability.topology import seam as seam_mod
from personal_agent.observability.topology.seam import observe_topology
from personal_agent.orchestrator.sub_agent_types import SubAgentResult


def _sub_result(**overrides: object) -> SubAgentResult:
    """A minimal SubAgentResult for segment-row tests (task_id is a UUID)."""
    base: dict[str, object] = dict(
        task_id=uuid4(),
        spec_task="x",
        summary="s",
        full_output="full output",
        tools_used=["web_search"],
        token_count=20,
        duration_ms=5.0,
        success=True,
        cost_usd=0.02,
    )
    base.update(overrides)
    return SubAgentResult(**base)  # type: ignore[arg-type]


pytestmark = pytest.mark.asyncio


def _ctx(**overrides: object) -> SimpleNamespace:
    """Minimal duck-typed execution context the assembler reads defensively."""
    base: dict[str, object] = dict(
        trace_id=str(uuid4()),
        session_id=str(uuid4()),
        gateway_output=None,
        messages=[],
        steps=[],
        sub_agent_results=None,
        expansion_phase_results=[],
        topology=None,
        turn_cost_usd=0.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _fake_ledger() -> AsyncMock:
    ledger = AsyncMock()
    ledger.fetch_authoritative_cost = AsyncMock(return_value=(0.42, 1200, 800))
    ledger.write = AsyncMock()
    return ledger


def _patch(monkeypatch: pytest.MonkeyPatch, ledger: object, bus: object) -> None:
    monkeypatch.setattr(seam_mod, "get_route_trace_ledger", lambda: ledger)
    monkeypatch.setattr(seam_mod, "get_event_bus", lambda: bus)


async def test_seam_publishes_enter_writes_row_and_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _fake_ledger()
    bus = AsyncMock()
    _patch(monkeypatch, ledger, bus)
    ctx = _ctx()

    async with observe_topology(ctx):
        pass

    # ctx.topology resolved (no gateway_output → primary)
    assert ctx.topology == "primary"
    # exactly one durable row written
    ledger.write.assert_awaited_once()
    row = ledger.write.call_args.args[0]
    assert str(row.trace_id) == ctx.trace_id
    assert row.cost_authoritative_usd == pytest.approx(0.42)
    # enter + complete published
    published = [c.args[1] for c in bus.publish.await_args_list]
    assert any(isinstance(e, TopologyEnteredEvent) for e in published)
    assert any(isinstance(e, TurnCompletedEvent) for e in published)


async def test_seam_resolves_hybrid_topology(monkeypatch: pytest.MonkeyPatch) -> None:
    from personal_agent.request_gateway.types import DecompositionStrategy

    ledger = _fake_ledger()
    bus = AsyncMock()
    _patch(monkeypatch, ledger, bus)
    gw = SimpleNamespace(decomposition=SimpleNamespace(strategy=DecompositionStrategy.HYBRID))
    ctx = _ctx(gateway_output=gw)

    async with observe_topology(ctx):
        pass

    assert ctx.topology == "hybrid_fanout"
    entered = next(
        c.args[1]
        for c in bus.publish.await_args_list
        if isinstance(c.args[1], TopologyEnteredEvent)
    )
    assert entered.topology == "hybrid_fanout"


async def test_seam_swallows_ledger_and_bus_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _fake_ledger()
    ledger.write = AsyncMock(side_effect=RuntimeError("db down"))
    bus = AsyncMock()
    bus.publish = AsyncMock(side_effect=RuntimeError("redis down"))
    _patch(monkeypatch, ledger, bus)
    ctx = _ctx()

    # Must not raise despite both sinks failing.
    async with observe_topology(ctx):
        pass


async def test_seam_writes_row_under_noop_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    from personal_agent.events.bus import NoOpBus

    ledger = _fake_ledger()
    _patch(monkeypatch, ledger, NoOpBus())
    ctx = _ctx()

    async with observe_topology(ctx):
        pass

    # Durable write survives a no-op bus (ADR-0088 D8).
    ledger.write.assert_awaited_once()


async def test_seam_writes_segment_row_per_sub_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-517: one turn-level row + one segment row per sub-agent are written."""
    ledger = _fake_ledger()
    bus = AsyncMock()
    _patch(monkeypatch, ledger, bus)
    sub_a, sub_b = _sub_result(), _sub_result()
    ctx = _ctx(sub_agent_results=[sub_a, sub_b])

    async with observe_topology(ctx):
        pass

    # 1 turn-level + 2 segments
    assert ledger.write.await_count == 3
    written = [c.args[0] for c in ledger.write.await_args_list]
    assert written[0].task_id is None  # turn-level first
    segment_task_ids = {r.task_id for r in written[1:]}
    assert segment_task_ids == {sub_a.task_id, sub_b.task_id}
    assert all(r.model_role == "sub_agent" for r in written[1:])


async def test_seam_segment_write_failure_does_not_corrupt_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing segment write is isolated: the turn still completes with the real cost."""
    ledger = _fake_ledger()

    calls: list[object] = []

    async def _write(row: object) -> None:
        calls.append(row)
        if getattr(row, "task_id", None) is not None:
            raise RuntimeError("segment db blip")

    ledger.write = AsyncMock(side_effect=_write)
    bus = AsyncMock()
    _patch(monkeypatch, ledger, bus)
    ctx = _ctx(sub_agent_results=[_sub_result(), _sub_result()])

    async with observe_topology(ctx):
        pass

    # Both segments were attempted despite the first failing; turn_completed cost intact.
    assert len(calls) == 3
    completed = next(
        c.args[1] for c in bus.publish.await_args_list if isinstance(c.args[1], TurnCompletedEvent)
    )
    assert completed.cost_authoritative_usd == pytest.approx(0.42)


async def test_seam_reraises_cancelled_after_writing_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _fake_ledger()
    bus = AsyncMock()
    _patch(monkeypatch, ledger, bus)
    ctx = _ctx()

    with pytest.raises(asyncio.CancelledError):
        async with observe_topology(ctx):
            raise asyncio.CancelledError()

    # The durable row is still attempted on cancellation.
    ledger.write.assert_awaited_once()


async def test_seam_projects_turn_level_and_segments_to_es(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-548: the seam projects the turn-level row + each segment row to ES."""
    ledger = _fake_ledger()
    bus = AsyncMock()
    _patch(monkeypatch, ledger, bus)
    projected: list[tuple[object, str]] = []
    monkeypatch.setattr(
        seam_mod,
        "project_route_trace_to_es",
        lambda row, *, topology: projected.append((row, topology)),
    )
    sub_a, sub_b = _sub_result(), _sub_result()
    ctx = _ctx(sub_agent_results=[sub_a, sub_b])

    async with observe_topology(ctx):
        pass

    # 1 turn-level + 2 segments, each with the resolved topology threaded through.
    assert len(projected) == 3
    assert all(topology == "primary" for _, topology in projected)
    rows = [row for row, _ in projected]
    assert rows[0].task_id is None  # turn-level first
    assert {r.task_id for r in rows[1:]} == {sub_a.task_id, sub_b.task_id}


async def test_seam_projection_failure_does_not_break_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising projector must not break the turn (defence-in-depth over its own guard)."""
    ledger = _fake_ledger()
    bus = AsyncMock()
    _patch(monkeypatch, ledger, bus)

    def _boom(row: object, *, topology: str) -> None:
        raise RuntimeError("es projection blew up")

    monkeypatch.setattr(seam_mod, "project_route_trace_to_es", _boom)
    ctx = _ctx(sub_agent_results=[_sub_result()])

    # Must not raise despite the projector failing on both the turn-level and segment rows.
    async with observe_topology(ctx):
        pass


async def test_seam_row_carries_the_time_since_request_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-1568: ``route_traces.latency_total_ms`` is request receipt to the row write."""
    ledger = _fake_ledger()
    _patch(monkeypatch, ledger, AsyncMock())
    clear_first_token_clock()
    start_first_token_clock(time.monotonic() - 2.5)
    try:
        async with observe_topology(_ctx()):
            pass
    finally:
        clear_first_token_clock()

    row = ledger.write.call_args.args[0]
    assert row.latency_total_ms is not None
    assert 2500.0 <= row.latency_total_ms < 60_000.0


async def test_seam_row_latency_is_none_when_no_request_clock_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One column, one meaning: a turn with no receipt clock gets None, not a second source."""
    ledger = _fake_ledger()
    _patch(monkeypatch, ledger, AsyncMock())
    clear_first_token_clock()

    async with observe_topology(_ctx(turn_started_monotonic=time.monotonic() - 9.0)):
        pass

    assert ledger.write.call_args.args[0].latency_total_ms is None


def test_request_elapsed_ms_reads_the_running_clock_and_none_otherwise() -> None:
    clear_first_token_clock()
    assert request_elapsed_ms() is None
    start_first_token_clock(time.monotonic() - 1.0)
    try:
        elapsed = request_elapsed_ms()
    finally:
        clear_first_token_clock()
    assert elapsed is not None and elapsed >= 1000.0
    assert request_elapsed_ms() is None
