"""Tests for the ADR-0088 live turn-observation projector (FRE-513).

The projector is the sole emitter of ``turn_status`` (ADR-0076 sink): it consumes the
``stream:turn.observed`` events, maintains a per-trace :class:`TurnObservation`, and emits
a full-state STATE_DELTA. The live cost meter climbs from ``turn.model_call_completed``
events (topology-independent) and reconciles to the authoritative sum at ``turn.completed``.
"""

from __future__ import annotations

from typing import Any

import pytest

from personal_agent.events.models import (
    ModelCallCompletedEvent,
    SubAgentProgressEvent,
    TopologyEnteredEvent,
    TurnCompletedEvent,
    TurnDegradedEvent,
    TurnProgressEvent,
)
from personal_agent.observability.topology import projector as projector_mod
from personal_agent.observability.topology.projector import TurnObservationProjector

pytestmark = pytest.mark.asyncio


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Patch emit_turn_status and capture each emitted value payload."""
    emitted: list[dict[str, Any]] = []

    async def _fake_emit(*, session_id: str, value: dict[str, Any]) -> None:
        emitted.append({"session_id": session_id, **value})

    monkeypatch.setattr(projector_mod, "emit_turn_status", _fake_emit)
    return emitted


async def test_topology_entered_emits_topology(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted = _capture(monkeypatch)
    proj = TurnObservationProjector()

    await proj.handle(
        TopologyEnteredEvent(trace_id="t-1", session_id="s-1", topology="hybrid_fanout")
    )

    assert emitted[-1]["topology"] == "hybrid_fanout"
    assert emitted[-1]["trace_id"] == "t-1"
    assert emitted[-1]["session_id"] == "s-1"


async def test_cost_meter_climbs_across_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted = _capture(monkeypatch)
    proj = TurnObservationProjector()

    await proj.handle(TopologyEnteredEvent(trace_id="t-1", session_id="s-1", topology="decompose"))
    await proj.handle(
        ModelCallCompletedEvent(
            trace_id="t-1",
            session_id="s-1",
            cost_usd=0.01,
            input_tokens=100,
            output_tokens=50,
            model_role="primary",
        )
    )
    await proj.handle(
        ModelCallCompletedEvent(
            trace_id="t-1",
            session_id="s-1",
            cost_usd=0.02,
            input_tokens=200,
            output_tokens=80,
            model_role="sub_agent",
        )
    )

    # The live meter accumulates across (topology-independent) model calls.
    assert emitted[-1]["turn_cost_usd"] == pytest.approx(0.03)


async def test_progress_updates_tool_iteration_and_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted = _capture(monkeypatch)
    proj = TurnObservationProjector()

    await proj.handle(
        TurnProgressEvent(
            trace_id="t-1",
            session_id="s-1",
            tool_iteration=3,
            tool_iteration_max=25,
            context_tokens=8000,
            context_max=40000,
            topology="primary",
        )
    )

    assert emitted[-1]["tool_iteration"] == 3
    assert emitted[-1]["tool_iteration_max"] == 25
    assert emitted[-1]["context_tokens"] == 8000
    assert emitted[-1]["context_max"] == 40000


async def test_sub_agent_progress_climbs_aggregate(monkeypatch: pytest.MonkeyPatch) -> None:
    """FRE-553: the surfaced meter is primary + Σ sub-agent (numerator and max)."""
    emitted = _capture(monkeypatch)
    proj = TurnObservationProjector()

    # Primary reports a baseline tick (1/25), then two concurrent sub-agents progress.
    await proj.handle(
        TurnProgressEvent(
            trace_id="t-1",
            session_id="s-1",
            tool_iteration=1,
            tool_iteration_max=25,
            context_tokens=0,
            context_max=0,
            topology="decompose",
        )
    )
    await proj.handle(
        SubAgentProgressEvent(
            trace_id="t-1", session_id="s-1", task_id="a", iteration=3, iteration_max=10
        )
    )
    await proj.handle(
        SubAgentProgressEvent(
            trace_id="t-1", session_id="s-1", task_id="b", iteration=2, iteration_max=10
        )
    )

    # primary 1 + a:3 + b:2 = 6 ; max 25 + 10 + 10 = 45
    assert emitted[-1]["tool_iteration"] == 6
    assert emitted[-1]["tool_iteration_max"] == 45


async def test_concurrent_sub_agents_do_not_clobber(monkeypatch: pytest.MonkeyPatch) -> None:
    """FRE-553: interleaved per-task ticks sum, never collapse to one task's value."""
    emitted = _capture(monkeypatch)
    proj = TurnObservationProjector()

    await proj.handle(
        SubAgentProgressEvent(
            trace_id="t-1", session_id="s-1", task_id="a", iteration=1, iteration_max=10
        )
    )
    await proj.handle(
        SubAgentProgressEvent(
            trace_id="t-1", session_id="s-1", task_id="b", iteration=1, iteration_max=10
        )
    )
    await proj.handle(
        SubAgentProgressEvent(
            trace_id="t-1", session_id="s-1", task_id="a", iteration=4, iteration_max=10
        )
    )

    # latest a:4 + latest b:1 = 5 (no clobber — never just 4 or just 1)
    assert emitted[-1]["tool_iteration"] == 5


async def test_reordered_tick_is_non_regressing(monkeypatch: pytest.MonkeyPatch) -> None:
    """FRE-553: a stale/reordered lower tick must not drop the surfaced count (max-wins)."""
    emitted = _capture(monkeypatch)
    proj = TurnObservationProjector()

    await proj.handle(
        SubAgentProgressEvent(
            trace_id="t-1", session_id="s-1", task_id="a", iteration=3, iteration_max=10
        )
    )
    await proj.handle(
        SubAgentProgressEvent(
            trace_id="t-1", session_id="s-1", task_id="a", iteration=1, iteration_max=10
        )
    )

    assert emitted[-1]["tool_iteration"] == 3


async def test_non_decomposed_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    """FRE-553: with no sub-agent events the surfaced meter equals the primary values."""
    emitted = _capture(monkeypatch)
    proj = TurnObservationProjector()

    await proj.handle(
        TurnProgressEvent(
            trace_id="t-1",
            session_id="s-1",
            tool_iteration=7,
            tool_iteration_max=25,
            context_tokens=0,
            context_max=0,
        )
    )

    assert emitted[-1]["tool_iteration"] == 7
    assert emitted[-1]["tool_iteration_max"] == 25


async def test_degraded_raises_visible_state(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted = _capture(monkeypatch)
    proj = TurnObservationProjector()

    await proj.handle(
        TurnDegradedEvent(
            trace_id="t-1",
            session_id="s-1",
            where="decompose",
            reason="planner_schema_fail",
            severity="critical",
        )
    )

    assert emitted[-1]["degraded"] is True
    assert any("planner_schema_fail" in d for d in emitted[-1]["degradations"])


async def test_completed_reconciles_cost_and_evicts(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted = _capture(monkeypatch)
    proj = TurnObservationProjector()

    await proj.handle(
        TopologyEnteredEvent(trace_id="t-1", session_id="s-1", topology="hybrid_fanout")
    )
    await proj.handle(
        ModelCallCompletedEvent(
            trace_id="t-1",
            session_id="s-1",
            cost_usd=0.50,
            input_tokens=100,
            output_tokens=50,
        )
    )
    # Authoritative sum (0.90) wins over the live-accumulated 0.50 at completion.
    await proj.handle(
        TurnCompletedEvent(
            trace_id="t-1",
            session_id="s-1",
            topology="hybrid_fanout",
            cost_authoritative_usd=0.90,
        )
    )

    assert emitted[-1]["turn_cost_usd"] == pytest.approx(0.90)
    # Per-trace state is evicted after completion.
    assert "t-1" not in proj._by_trace


async def test_emit_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(*, session_id: str, value: dict[str, Any]) -> None:
        raise RuntimeError("transport down")

    monkeypatch.setattr(projector_mod, "emit_turn_status", _boom)
    proj = TurnObservationProjector()

    # A failing transport must not propagate out of the consumer handler.
    await proj.handle(TopologyEnteredEvent(trace_id="t-1", session_id="s-1", topology="primary"))
