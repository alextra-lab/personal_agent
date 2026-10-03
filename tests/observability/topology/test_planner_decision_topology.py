"""FRE-1512 (ADR-0154 D6) — a decline reads as a decline in the derived views.

Covers the topology label rule (AC-4), the seam's terminal label, the single turn-level ES
projection while a first-token clock is active, and the new ES document fields.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from personal_agent.events.models import TopologyEnteredEvent, TurnCompletedEvent
from personal_agent.governance.models import Mode
from personal_agent.observability import first_token
from personal_agent.observability.route_trace.assembler import assemble_route_trace
from personal_agent.observability.route_trace.types import RouteTraceRow
from personal_agent.observability.topology import seam as seam_mod
from personal_agent.observability.topology.es_projection import build_topology_doc
from personal_agent.observability.topology.seam import observe_topology, topology_label
from personal_agent.orchestrator.types import PlannerInputChars, PlannerRunRecord
from personal_agent.request_gateway.types import Complexity, DecompositionStrategy, TaskType

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clear_clock() -> object:
    first_token.clear_first_token_clock()
    yield
    first_token.clear_first_token_clock()


def _run(decision: str, **overrides: object) -> PlannerRunRecord:
    base: dict[str, object] = dict(
        decision=decision,
        failure_reason="timeout" if decision == "failed" else None,
        deployment="qwen-local",
        mode="planner",
        reasoning_chars=0,
        duration_ms=420.0,
        prompt_tokens=900,
        completion_tokens=40,
        input_chars=PlannerInputChars(system=2439, history=100, digest=0, message=20),
    )
    base.update(overrides)
    return PlannerRunRecord(**base)  # type: ignore[arg-type]


def _gateway(
    strategy: DecompositionStrategy, reason: str = "planner_asked", budget: int = 3
) -> SimpleNamespace:
    return SimpleNamespace(
        intent=SimpleNamespace(
            task_type=TaskType.CONVERSATIONAL, complexity=Complexity.SIMPLE, confidence=0.9
        ),
        decomposition=SimpleNamespace(strategy=strategy, reason=reason),
        degraded_stages=(),
        governance=SimpleNamespace(mode=Mode.NORMAL, expansion_budget=budget),
    )


def _ctx(**overrides: object) -> SimpleNamespace:
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
        planner_run=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _fake_ledger() -> AsyncMock:
    ledger = AsyncMock()
    ledger.fetch_authoritative_cost = AsyncMock(return_value=(0.1, 10, 5))
    ledger.write = AsyncMock()
    return ledger


def _wire(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, AsyncMock, list[tuple[object, str]]]:
    ledger, bus = _fake_ledger(), AsyncMock()
    projected: list[tuple[object, str]] = []
    monkeypatch.setattr(seam_mod, "get_route_trace_ledger", lambda: ledger)
    monkeypatch.setattr(seam_mod, "get_event_bus", lambda: bus)
    monkeypatch.setattr(
        seam_mod,
        "project_route_trace_to_es",
        lambda row, *, topology: projected.append((row, topology)),
    )
    return ledger, bus, projected


# --- the label rule --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("strategy", "reason", "decision", "expected"),
    [
        ("hybrid", "planner_asked", "declined", "primary"),
        ("hybrid", "planner_asked", "failed", "primary"),
        ("hybrid", "planner_asked", "expanded", "hybrid_fanout"),
        ("hybrid", "planner_asked", None, "hybrid_fanout"),
        # A decision only matters on a planner_asked turn.
        ("hybrid", "complex_analysis", "declined", "hybrid_fanout"),
        ("decompose", "planner_asked", "failed", "primary"),
        ("single", "conversational_always_single", None, "primary"),
        ("delegate", "explicit_delegation", None, "delegate"),
        (None, None, None, "primary"),
    ],
)
async def test_topology_label_rule(
    strategy: str | None, reason: str | None, decision: str | None, expected: str
) -> None:
    assert topology_label(strategy, reason, decision) == expected


# --- the seam --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("decision", "terminal"),
    [("declined", "primary"), ("failed", "primary"), ("expanded", "hybrid_fanout")],
)
async def test_seam_uses_terminal_topology_for_row_event_and_projection(
    monkeypatch: pytest.MonkeyPatch, decision: str, terminal: str
) -> None:
    ledger, bus, projected = _wire(monkeypatch)
    ctx = _ctx(gateway_output=_gateway(DecompositionStrategy.HYBRID))

    async with observe_topology(ctx):
        ctx.planner_run = _run(decision)  # the planner decides mid-turn

    events = [c.args[1] for c in bus.publish.await_args_list]
    entered = next(e for e in events if isinstance(e, TopologyEnteredEvent))
    completed = next(e for e in events if isinstance(e, TurnCompletedEvent))
    assert entered.topology == "hybrid_fanout"  # the gateway's intent at entry
    assert completed.topology == terminal
    assert ctx.topology == terminal
    assert [t for _, t in projected] == [terminal]

    # The row carries the planner record, so a later re-projection derives the same label.
    row = ledger.write.call_args.args[0]
    assert row.planner_decision == decision
    assert (
        topology_label(row.decomposition_strategy, row.decomposition_reason, row.planner_decision)
        == terminal
    )


@pytest.mark.parametrize("strategy", list(DecompositionStrategy))
async def test_row_label_matches_seam_label_for_every_strategy(
    monkeypatch: pytest.MonkeyPatch, strategy: DecompositionStrategy
) -> None:
    ledger, _bus, projected = _wire(monkeypatch)
    ctx = _ctx(gateway_output=_gateway(strategy, reason="planner_asked"))

    async with observe_topology(ctx):
        pass

    row = ledger.write.call_args.args[0]
    derived = topology_label(
        row.decomposition_strategy, row.decomposition_reason, row.planner_decision
    )
    assert [t for _, t in projected] == [derived]


async def test_seam_skips_turn_level_projection_while_first_token_clock_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app projects the turn document once, after the first-token update (no ES race)."""
    from personal_agent.orchestrator.sub_agent_types import SubAgentResult

    _ledger, _bus, projected = _wire(monkeypatch)
    sub = SubAgentResult(
        task_id=uuid4(),
        spec_task="x",
        summary="s",
        full_output="o",
        tools_used=[],
        token_count=1,
        duration_ms=1.0,
        success=True,
        cost_usd=0.0,
    )
    ctx = _ctx(sub_agent_results=[sub])
    first_token.start_first_token_clock()

    async with observe_topology(ctx):
        pass

    rows = [row for row, _ in projected]
    assert [r.task_id for r in rows] == [sub.task_id]  # segment only


async def test_seam_projects_turn_level_row_without_a_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ledger, _bus, projected = _wire(monkeypatch)
    async with observe_topology(_ctx()):
        pass
    assert [row.task_id for row, _ in projected] == [None]


# --- the ES document -------------------------------------------------------------------


def _row_from_ctx(ctx: SimpleNamespace) -> RouteTraceRow:
    return assemble_route_trace(
        ctx,
        authoritative_cost_usd=0.1,
        input_tokens=10,
        output_tokens=5,
        store_preview=False,
        preview_chars=0,
        created_at=datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc),
    )


async def test_topology_doc_carries_every_new_field_with_its_declared_type() -> None:
    ctx = _ctx(
        gateway_output=_gateway(DecompositionStrategy.HYBRID),
        planner_run=_run("failed"),
        planner_gate_reason=None,
        conversation_history_chars=1234,
        synthesis_appended=False,
        expansion_budget=2,
    )
    row = RouteTraceRow(**{**_row_from_ctx(ctx).__dict__, "first_token_ms": 1534.5})

    doc = build_topology_doc(row, topology="primary")

    assert doc["planner_decision"] == "failed"
    assert doc["planner_failure_reason"] == "timeout"
    assert doc["planner_deployment"] == "qwen-local"
    assert doc["planner_mode"] == "planner"
    assert doc["planner_reasoning_chars"] == 0 and isinstance(doc["planner_reasoning_chars"], int)
    assert doc["planner_duration_ms"] == 420.0 and isinstance(doc["planner_duration_ms"], float)
    assert doc["planner_prompt_tokens"] == 900
    assert doc["planner_completion_tokens"] == 40
    assert doc["planner_input_chars"] == {
        "system": 2439,
        "history": 100,
        "digest": 0,
        "message": 20,
    }
    assert doc["conversation_history_chars"] == 1234
    assert doc["expansion_budget"] == 2
    assert doc["synthesis_appended"] is False
    assert doc["first_token_ms"] == 1534.5 and isinstance(doc["first_token_ms"], float)
    assert "planner_gate_reason" not in doc  # None fields are omitted


async def test_topology_doc_omits_planner_fields_when_the_planner_did_not_run() -> None:
    row = _row_from_ctx(_ctx())
    doc = build_topology_doc(row, topology="primary")
    for name in (
        "planner_decision",
        "planner_failure_reason",
        "planner_deployment",
        "planner_input_chars",
        "first_token_ms",
        "conversation_history_chars",
    ):
        assert name not in doc
    assert doc["synthesis_appended"] is False  # always present on a turn-level row


# --- AC-5, offline half: the template declares what the projection emits ---------------


async def test_template_declares_every_projected_field_with_a_matching_type() -> None:
    import json
    from pathlib import Path

    template = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "docker"
            / "elasticsearch"
            / "topology-index-template.json"
        ).read_text()
    )
    properties = template["template"]["mappings"]["properties"]
    assert template["template"]["mappings"]["dynamic"] is False

    ctx = _ctx(
        gateway_output=_gateway(DecompositionStrategy.HYBRID),
        planner_run=_run("failed"),
        planner_gate_reason="planner_mode_absent",
        conversation_history_chars=10,
        expansion_budget=1,
    )
    row = RouteTraceRow(**{**_row_from_ctx(ctx).__dict__, "first_token_ms": 1.5})
    doc = build_topology_doc(row, topology="primary")

    compatible = {
        str: {"keyword"},
        bool: {"boolean"},
        int: {"integer", "long"},
        float: {"float", "double"},
    }
    for name, value in doc.items():
        assert name in properties, f"{name} is projected but not declared in the template"
        if name == "@timestamp":
            continue
        if isinstance(value, dict):
            assert set(value) == set(properties[name]["properties"])
            for sub, sub_value in value.items():
                assert properties[name]["properties"][sub]["type"] in compatible[type(sub_value)]
        else:
            assert properties[name]["type"] in compatible[type(value)], name
