"""Tests for the ADR-0088 observe_topology emission seam (FRE-513).

The seam is the mandatory boundary every topology passes through: on enter it publishes
``turn.topology_entered`` (best-effort bus), on exit it writes the durable route-trace row
directly (bus-independent, D8) and publishes ``turn.completed``. Failures in either sink
must never break the wrapped turn; ``CancelledError`` must still propagate.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from personal_agent.observability.topology import seam as seam_mod
from personal_agent.observability.topology.seam import observe_topology

from personal_agent.events.models import (
    TopologyEnteredEvent,
    TurnCompletedEvent,
)

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
