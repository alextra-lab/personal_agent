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


# ---------------------------------------------------------------------------
# FRE-557 — projector-health counters
# ---------------------------------------------------------------------------


def _capture_health(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Patch schedule_es_index in the projector and capture (index, doc, doc_id)."""
    calls: list[dict[str, Any]] = []

    def _fake(index_name, document, es_handler=None, doc_id=None):  # type: ignore[no-untyped-def]
        calls.append({"index": index_name, "doc": document, "doc_id": doc_id})

    monkeypatch.setattr(projector_mod, "schedule_es_index", _fake)
    return calls


async def test_handle_counts_events_per_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch)
    proj = TurnObservationProjector()

    await proj.handle(TopologyEnteredEvent(trace_id="t-1", session_id="s-1", topology="decompose"))
    for _ in range(2):
        await proj.handle(
            ModelCallCompletedEvent(
                trace_id="t-1", session_id="s-1", cost_usd=0.01, input_tokens=1, output_tokens=1
            )
        )

    obs = proj._by_trace["t-1"]
    assert obs.events_received == 3
    assert obs.model_calls_received == 2


async def test_turn_completed_emits_health_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch)
    calls = _capture_health(monkeypatch)
    proj = TurnObservationProjector()

    await proj.handle(TopologyEnteredEvent(trace_id="t-1", session_id="s-1", topology="primary"))
    for _ in range(2):
        await proj.handle(
            ModelCallCompletedEvent(
                trace_id="t-1", session_id="s-1", cost_usd=0.01, input_tokens=1, output_tokens=1
            )
        )
    await proj.handle(
        TurnCompletedEvent(
            trace_id="t-1", session_id="s-1", topology="primary", cost_authoritative_usd=0.05
        )
    )

    assert len(calls) == 1
    assert calls[0]["index"].startswith("agent-monitors-projector-health-")
    assert calls[0]["doc_id"] == "t-1"
    doc = calls[0]["doc"]
    assert doc["trace_id"] == "t-1" and doc["session_id"] == "s-1"
    assert doc["model_calls_received"] == 2
    assert doc["events_received"] == 4  # topology + 2 model calls + completed
    # The projector's bus-accumulated live cost is captured BEFORE the authoritative overwrite.
    assert doc["projector_live_cost_usd"] == pytest.approx(0.02)
    assert doc["cost_authoritative_usd"] == pytest.approx(0.05)
    assert doc["cost_delta_usd"] == pytest.approx(-0.03)
    assert doc["observation_complete"] is True
    assert "T" in doc["@timestamp"]


async def test_health_emit_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch)

    def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("es down")

    monkeypatch.setattr(projector_mod, "schedule_es_index", _boom)
    proj = TurnObservationProjector()

    await proj.handle(TopologyEnteredEvent(trace_id="t-1", session_id="s-1", topology="primary"))
    # A failing health emit must not break the consumer or skip eviction.
    await proj.handle(
        TurnCompletedEvent(
            trace_id="t-1", session_id="s-1", topology="primary", cost_authoritative_usd=0.0
        )
    )
    assert "t-1" not in proj._by_trace


async def test_completion_without_prior_events_flags_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(monkeypatch)
    calls = _capture_health(monkeypatch)
    proj = TurnObservationProjector()

    # Only the completion event arrives — the projector never saw this trace's earlier events.
    await proj.handle(
        TurnCompletedEvent(
            trace_id="t-99", session_id="s-1", topology="primary", cost_authoritative_usd=0.4
        )
    )

    doc = calls[0]["doc"]
    assert doc["observation_complete"] is False
    assert doc["model_calls_received"] == 0


async def test_global_counter_counts_unknown_events(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch)
    proj = TurnObservationProjector()

    from personal_agent.events.models import EventBase

    class _UnknownEvent(EventBase):
        event_type: str = "unknown_test_event"
        source_component: str = "test"
        trace_id: str = "t-1"
        session_id: str = "s-1"

    await proj.handle(_UnknownEvent())

    assert proj._events_received_total == 1
    assert "t-1" not in proj._by_trace  # unknown events create no per-trace observation


async def test_rolling_counter_emits_every_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    import structlog

    _capture(monkeypatch)
    monkeypatch.setattr(projector_mod, "_ROLLING_EMIT_EVERY", 3)
    proj = TurnObservationProjector()

    with structlog.testing.capture_logs() as logs:
        for _ in range(3):
            await proj.handle(
                TopologyEnteredEvent(trace_id="t-1", session_id="s-1", topology="primary")
            )

    rolling = [e for e in logs if e.get("event") == "projector_events_rolling"]
    assert len(rolling) == 1
    assert rolling[0]["events_total"] == 3


async def test_rolling_counter_time_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    import structlog

    _capture(monkeypatch)
    # Count threshold high so only the time heartbeat can fire; zero seconds so any elapsed
    # time triggers it deterministically. (Do NOT set _last_rolling_emit to a literal like
    # 0.0 — time.monotonic()'s reference is arbitrary, so on a freshly-booted runner the
    # delta can be < the threshold and the heartbeat would never fire.)
    monkeypatch.setattr(projector_mod, "_ROLLING_EMIT_EVERY", 10_000)
    monkeypatch.setattr(projector_mod, "_ROLLING_EMIT_SECONDS", 0.0)
    proj = TurnObservationProjector()

    with structlog.testing.capture_logs() as logs:
        await proj.handle(
            TopologyEnteredEvent(trace_id="t-1", session_id="s-1", topology="primary")
        )

    assert any(e.get("event") == "projector_events_rolling" for e in logs)


# ---------------------------------------------------------------------------
# FRE-568 — session-aggregate: session cost + context occupancy (ADR-0092 §D2/§D3/§D4)
# ---------------------------------------------------------------------------


async def test_session_cost_accumulates_across_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two completed traces for the same session sum into session_cost_usd (D2)."""
    emitted = _capture(monkeypatch)
    proj = TurnObservationProjector()

    await proj.handle(
        TurnCompletedEvent(
            trace_id="t-1", session_id="s-1", topology="primary", cost_authoritative_usd=0.5
        )
    )
    await proj.handle(
        TurnCompletedEvent(
            trace_id="t-2", session_id="s-1", topology="primary", cost_authoritative_usd=0.3
        )
    )

    assert emitted[-1]["session_cost_usd"] == pytest.approx(0.8)


async def test_session_cost_set_not_added_on_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Completing the same trace_id twice overwrites (set, never +=) — no double-count (D2)."""
    emitted = _capture(monkeypatch)
    proj = TurnObservationProjector()

    await proj.handle(
        TurnCompletedEvent(
            trace_id="t-1", session_id="s-1", topology="primary", cost_authoritative_usd=0.5
        )
    )
    await proj.handle(
        TurnCompletedEvent(
            trace_id="t-1", session_id="s-1", topology="primary", cost_authoritative_usd=0.5
        )
    )

    assert emitted[-1]["session_cost_usd"] == pytest.approx(0.5)


async def test_session_context_carries_across_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """context_tokens from an earlier turn is still surfaced as session_context_tokens
    on a subsequent event that does not update context_tokens (D3).
    """
    emitted = _capture(monkeypatch)
    proj = TurnObservationProjector()

    await proj.handle(
        TurnProgressEvent(
            trace_id="t-1",
            session_id="s-1",
            tool_iteration=1,
            tool_iteration_max=25,
            context_tokens=8000,
            context_max=40000,
        )
    )
    # A new trace completes — no progress event for it; context_tokens from t-1 must carry.
    await proj.handle(
        TurnCompletedEvent(
            trace_id="t-1", session_id="s-1", topology="primary", cost_authoritative_usd=0.1
        )
    )
    await proj.handle(TopologyEnteredEvent(trace_id="t-2", session_id="s-1", topology="primary"))

    assert emitted[-1]["session_context_tokens"] == 8000


async def test_hydration_restores_session_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    """On first touch the hydration source populates historical costs (D4)."""
    emitted = _capture(monkeypatch)

    async def _source(session_id: str) -> dict[str, float]:
        return {"t-hist-1": 0.5, "t-hist-2": 0.3}

    proj = TurnObservationProjector(hydration_source=_source)

    # First event for s-1 triggers hydration.
    await proj.handle(TopologyEnteredEvent(trace_id="t-new", session_id="s-1", topology="primary"))
    assert emitted[-1]["session_cost_usd"] == pytest.approx(0.8)

    # A new live completion is added on top.
    await proj.handle(
        TurnCompletedEvent(
            trace_id="t-new", session_id="s-1", topology="primary", cost_authoritative_usd=0.2
        )
    )
    assert emitted[-1]["session_cost_usd"] == pytest.approx(1.0)


async def test_hydration_no_double_count_with_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live completion for a trace already in the hydrated set overwrites (live wins) (D4)."""
    emitted = _capture(monkeypatch)

    async def _source(session_id: str) -> dict[str, float]:
        # t-1 hydrated as 0.5 (partial — captured before the turn fully closed)
        return {"t-1": 0.5}

    proj = TurnObservationProjector(hydration_source=_source)

    # Trigger hydration.
    await proj.handle(TopologyEnteredEvent(trace_id="t-1", session_id="s-1", topology="primary"))
    # Live completion for the same trace at the authoritative (final) value.
    await proj.handle(
        TurnCompletedEvent(
            trace_id="t-1", session_id="s-1", topology="primary", cost_authoritative_usd=0.7
        )
    )

    assert emitted[-1]["session_cost_usd"] == pytest.approx(0.7)


async def test_hydration_best_effort_source_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing hydration source must not propagate — projector continues carry-only (D4)."""
    emitted = _capture(monkeypatch)

    async def _boom(session_id: str) -> dict[str, float]:
        raise RuntimeError("db down")

    proj = TurnObservationProjector(hydration_source=_boom)

    await proj.handle(TopologyEnteredEvent(trace_id="t-1", session_id="s-1", topology="primary"))

    assert emitted[-1]["session_cost_usd"] == pytest.approx(0.0)


async def test_eviction_of_active_trace_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    import structlog

    _capture(monkeypatch)
    monkeypatch.setattr(projector_mod, "_MAX_TRACKED_TRACES", 2)
    proj = TurnObservationProjector()

    # t-1 becomes active (a model call → events_received > 0), then two more traces evict it.
    await proj.handle(
        ModelCallCompletedEvent(
            trace_id="t-1", session_id="s-1", cost_usd=0.01, input_tokens=1, output_tokens=1
        )
    )
    with structlog.testing.capture_logs() as logs:
        await proj.handle(
            TopologyEnteredEvent(trace_id="t-2", session_id="s-1", topology="primary")
        )
        await proj.handle(
            TopologyEnteredEvent(trace_id="t-3", session_id="s-1", topology="primary")
        )

    assert any(e.get("event") == "projector_evicted_active_trace" for e in logs)
