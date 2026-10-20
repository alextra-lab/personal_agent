"""FRE-1634 — the transport phases of a HYBRID turn name the work the model does.

The expansion planner call is announced as ``planning``. The primary call that writes over
the helper reports is announced as ``synthesis`` with the detail ``helpers``, never as
``planning``. A turn with no expansion keeps the labels it had (ADR-0123 §1-§2).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

import personal_agent.orchestrator.executor as ex
import personal_agent.transport.agui.transport as transport_mod
from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.sub_agent_types import SubAgentResult, SubAgentSpec
from personal_agent.orchestrator.types import ExecutionContext, TaskState
from personal_agent.request_gateway.types import (
    AssembledContext,
    Complexity,
    DecompositionResult,
    DecompositionStrategy,
    GatewayOutput,
    GovernanceContext,
    IntentResult,
    TaskType,
)
from personal_agent.telemetry.trace import TraceContext
from personal_agent.transport.events import Phase, PhaseEndEvent, PhaseStartEvent

QUERY = "Compare the weather in two cities and summarise"
SESSION = str(uuid4())


class Ev(NamedTuple):
    """One phase event as the transport received it."""

    kind: str  # "start" or "end"
    phase: str
    detail: str | None
    phase_id: str
    parent_id: str | None


Seen = list[Ev]


def _plan_json(tasks: int) -> str:
    return json.dumps(
        {
            "strategy": "HYBRID",
            "tasks": [
                {"name": f"task_{i}", "goal": f"Goal {i}", "type": "general"} for i in range(tasks)
            ],
        }
    )


def _planner(response: Any) -> MagicMock:
    client = MagicMock()
    client.respond = AsyncMock(side_effect=response)
    client.provider = None
    client.model_key = "planner-test"
    client.model_def = None
    return client


def _sub_result(spec: SubAgentSpec, **_: Any) -> SubAgentResult:
    return SubAgentResult(
        task_id=uuid4(),
        spec_task=spec.task,
        summary="done",
        full_output="done",
        tools_used=[],
        token_count=1,
        duration_ms=1,
        success=True,
        cost_usd=0.0,
    )


def _gateway(*, reason: str) -> GatewayOutput:
    return GatewayOutput(
        intent=IntentResult(
            task_type=TaskType.ANALYSIS, complexity=Complexity.SIMPLE, confidence=0.9, signals=[]
        ),
        governance=GovernanceContext(
            mode=Mode.NORMAL, expansion_permitted=True, expansion_budget=3
        ),
        decomposition=DecompositionResult(strategy=DecompositionStrategy.HYBRID, reason=reason),
        context=AssembledContext(
            messages=[{"role": "user", "content": QUERY}],
            memory_context=None,
            tool_definitions=None,
        ),
        session_id=SESSION,
        trace_id="t-fre1634",
    )


def _session() -> MagicMock:
    session = MagicMock()
    session.messages = []
    session.add_message = AsyncMock()
    session.get_messages = AsyncMock(return_value=[])
    manager = MagicMock()
    manager.get_session = MagicMock(return_value=session)
    manager.add_message = AsyncMock()
    manager.get_messages = AsyncMock(return_value=[])
    return manager


def _primary() -> MagicMock:
    client = MagicMock()
    client.respond = AsyncMock(
        return_value={
            "content": "Here is the answer.",
            "tool_calls": [],
            "response_id": None,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )
    client.model_configs = {}
    return client


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> Seen:
    """Record every phase event the transport receives."""
    events: Seen = []

    async def _capture(event: Any, session_id: str) -> None:
        if isinstance(event, PhaseStartEvent):
            events.append(
                Ev("start", event.phase.value, event.detail, event.phase_id, event.parent_id)
            )
        elif isinstance(event, PhaseEndEvent):
            events.append(Ev("end", event.phase.value, None, event.phase_id, event.parent_id))

    monkeypatch.setattr(transport_mod, "_push_event", _capture)

    async def _no_progress(_ctx: ExecutionContext) -> None:
        return None

    monkeypatch.setattr(ex, "_report_turn_progress", _no_progress)
    monkeypatch.setattr(
        "personal_agent.orchestrator.expansion_controller.run_sub_agent",
        AsyncMock(side_effect=_sub_result),
    )
    return events


async def _run_turn(
    monkeypatch: pytest.MonkeyPatch, *, planner: MagicMock, reason: str
) -> tuple[ExecutionContext, TaskState]:
    """Run ``step_init`` (planner and helpers), then the primary call over their reports."""
    primary = _primary()
    monkeypatch.setattr(
        "personal_agent.llm_client.factory.get_llm_client",
        lambda role_name=None, mode=None: planner if mode == "planner" else primary,
    )
    ctx = ExecutionContext(
        session_id=SESSION,
        trace_id="t-fre1634",
        user_message=QUERY,
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        gateway_output=_gateway(reason=reason),
    )
    trace_ctx = TraceContext(trace_id="t-fre1634", session_id=SESSION)
    manager = _session()
    state = await ex.step_init(ctx, manager, trace_ctx)
    assert state == TaskState.LLM_CALL
    with patch(
        "personal_agent.orchestrator.executor.get_default_registry",
        return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
    ):
        state = await ex.step_llm_call(ctx, manager, trace_ctx)
    return ctx, state


def _starts(seen: Seen) -> list[tuple[str, str | None]]:
    return [(e.phase, e.detail) for e in seen if e.kind == "start"]


def _pos(seen: Seen, kind: str, phase: str, nth: int = 0) -> int:
    return [i for i, e in enumerate(seen) if e.kind == kind and e.phase == phase][nth]


def _plan_response(tasks: int) -> Any:
    return lambda **_: {"content": _plan_json(tasks), "cost_usd": 0.0}


class TestAC1ExpansionTurnPhases:
    """AC-1: planning, then expansion with its sub-agents, then synthesis."""

    @pytest.mark.asyncio
    async def test_phase_order_is_planning_expansion_sub_agents_synthesis(
        self, seen: Seen, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, _ = await _run_turn(
            monkeypatch, planner=_planner(_plan_response(2)), reason="analysis_moderate_hybrid"
        )

        assert ctx.synthesis_appended is True
        assert [e.phase for e in seen if e.kind == "start"] == [
            "planning",
            "expansion",
            "sub_agent",
            "sub_agent",
            "synthesis",
        ]
        assert _starts(seen)[-1] == ("synthesis", "helpers")

    @pytest.mark.asyncio
    async def test_the_intervals_are_ordered_and_every_start_has_its_end(
        self, seen: Seen, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _run_turn(
            monkeypatch, planner=_planner(_plan_response(2)), reason="analysis_moderate_hybrid"
        )

        start_ids = [e.phase_id for e in seen if e.kind == "start"]
        end_ids = [e.phase_id for e in seen if e.kind == "end"]
        assert sorted(start_ids) == sorted(end_ids)
        assert len(set(start_ids)) == len(start_ids)

        # planning ends before expansion starts; synthesis starts after expansion ends.
        assert _pos(seen, "end", "planning") < _pos(seen, "start", "expansion")
        assert _pos(seen, "end", "expansion") < _pos(seen, "start", "synthesis")
        # Each sub-agent is a child of the expansion and ends before it does.
        expansion_id = seen[_pos(seen, "start", "expansion")].phase_id
        children = [e for e in seen if e.kind == "start" and e.phase == "sub_agent"]
        assert len(children) == 2
        assert {c.parent_id for c in children} == {expansion_id}
        for child in children:
            end = next(
                i for i, e in enumerate(seen) if e.kind == "end" and e.phase_id == child.phase_id
            )
            assert end < _pos(seen, "end", "expansion")

    @pytest.mark.asyncio
    async def test_no_planning_phase_starts_after_the_expansion_ends(
        self, seen: Seen, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _run_turn(
            monkeypatch, planner=_planner(_plan_response(2)), reason="analysis_moderate_hybrid"
        )

        after = seen[_pos(seen, "end", "expansion") + 1 :]
        assert [e for e in after if e.kind == "start" and e.phase == "planning"] == []

    @pytest.mark.asyncio
    async def test_a_planner_timeout_still_pairs_the_planning_phase(
        self, seen: Seen, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fallback plan runs after a timeout. The planning wait is still announced and closed."""
        await _run_turn(
            monkeypatch, planner=_planner(asyncio.TimeoutError()), reason="analysis_moderate_hybrid"
        )

        assert [e.phase for e in seen if e.kind == "start"].count("planning") == 1
        assert _pos(seen, "end", "planning") < _pos(seen, "start", "expansion")
        assert _starts(seen)[-1] == ("synthesis", "helpers")

    @pytest.mark.asyncio
    async def test_when_every_worker_raises_the_primary_call_is_still_synthesis(
        self, seen: Seen, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No worker report exists, so no synthesis message is appended and there is no detail."""
        monkeypatch.setattr(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            AsyncMock(side_effect=RuntimeError("worker down")),
        )

        ctx, _ = await _run_turn(
            monkeypatch, planner=_planner(_plan_response(2)), reason="analysis_moderate_hybrid"
        )

        assert ctx.synthesis_appended is False
        assert ctx.sub_agents_dispatched == 2
        assert _starts(seen)[-1] == ("synthesis", None)
        after = seen[_pos(seen, "end", "expansion") + 1 :]
        assert [e for e in after if e.kind == "start" and e.phase == "planning"] == []


class TestPlannerAskedDecline:
    """A decline runs no helper, so the primary call is still the planning inference."""

    @pytest.mark.asyncio
    async def test_a_decline_keeps_the_primary_call_labelled_planning(
        self, seen: Seen, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        planner = _planner(lambda **_: {"content": json.dumps({"strategy": "SINGLE", "tasks": []})})

        ctx, _ = await _run_turn(monkeypatch, planner=planner, reason="planner_asked")

        assert ctx.synthesis_appended is False
        assert _starts(seen) == [("planning", None), ("planning", None)]


def test_the_phase_names_are_unchanged() -> None:
    """The ticket adds no phase name: the PWA renders the names it already knows."""
    assert {p.value for p in Phase} == {
        "planning",
        "synthesis",
        "artifact_build",
        "expansion",
        "sub_agent",
        "waiting_for_choice",
    }
