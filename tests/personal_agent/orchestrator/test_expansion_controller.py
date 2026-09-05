"""Tests for the expansion controller.

Tests the enforced expansion path: planner → validate → dispatch → synthesize.
Uses mocked LLM client and sub-agent runner.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from personal_agent.governance.models import Mode
from personal_agent.llm_client.types import LLMServerError, ModelRole
from personal_agent.orchestrator.expansion_controller import (
    ExpansionController,
    _validate_plan_json,
)
from personal_agent.orchestrator.sub_agent_types import SubAgentResult

# Force-import _run_dispatch's own lazy imports at module load rather than on
# first call. Several tests below assert fan-out-window arithmetic against a
# real clock; a cold first import inside _run_dispatch (the module has never
# been touched yet when this test file is run in isolation, e.g. via `-k`)
# costs tens to hundreds of ms and would skew those margins. Unused directly —
# imported for the caching side effect only.
from personal_agent.transport.agui.transport import phase_span as _  # noqa: F401


def _make_plan_json(tasks: int = 3) -> str:
    """Create valid plan JSON for testing."""
    plan = {
        "strategy": "HYBRID",
        "tasks": [
            {
                "name": f"task_{i}",
                "goal": f"Goal for task {i}",
                "constraints": [f"constraint_{i}"],
                "expected_output": "text",
            }
            for i in range(tasks)
        ],
    }
    return json.dumps(plan)


def _make_sub_agent_result(
    task_name: str = "task_0",
    success: bool = True,
    summary: str = "Result summary",
    cost_usd: float = 0.0,
    denied_tools: tuple[str, ...] = (),
    refused_tool_attempts: tuple[str, ...] = (),
    stated_tool_gap: str | None = None,
) -> SubAgentResult:
    return SubAgentResult(
        task_id=uuid4(),
        spec_task=task_name,
        summary=summary,
        full_output=summary,
        tools_used=[],
        token_count=50,
        duration_ms=2000,
        success=success,
        error=None if success else "Timeout",
        cost_usd=cost_usd,
        denied_tools=denied_tools,
        refused_tool_attempts=refused_tool_attempts,
        stated_tool_gap=stated_tool_gap,
    )


class TestValidatePlanJson:
    def test_valid_plan(self) -> None:
        plan = _validate_plan_json(_make_plan_json(3))
        assert plan is not None
        assert len(plan.tasks) == 3
        assert plan.strategy == "HYBRID"

    def test_invalid_json(self) -> None:
        assert _validate_plan_json("not json") is None

    def test_missing_tasks(self) -> None:
        assert _validate_plan_json('{"strategy": "HYBRID"}') is None

    def test_empty_tasks(self) -> None:
        assert _validate_plan_json('{"strategy": "HYBRID", "tasks": []}') is None

    def test_task_missing_name(self) -> None:
        bad = '{"strategy": "HYBRID", "tasks": [{"goal": "g"}]}'
        assert _validate_plan_json(bad) is None

    def test_task_missing_goal(self) -> None:
        bad = '{"strategy": "HYBRID", "tasks": [{"name": "n"}]}'
        assert _validate_plan_json(bad) is None

    def test_caps_task_count_hybrid(self) -> None:
        plan = _validate_plan_json(_make_plan_json(10))
        # HYBRID caps at 4+1 = 5
        assert plan is not None
        assert len(plan.tasks) <= 5


class TestPlannerDiscoveryRetired:
    """FRE-884 — ADR-0086's tooled discovery-slice ``mode`` is retired.

    A raw plan carrying the old discovery-slice ``mode`` field is still
    ignored — ``SubAgentMode`` only has PARALLEL_INFERENCE. ``tools`` is a
    SEPARATE, still-live field (FRE-1389): it is now parsed and later
    filtered against the sub-agent tool grant set at dispatch time, not
    dropped at parse time.
    """

    @staticmethod
    def _tooled_plan(tools: list[str]) -> str:
        return json.dumps(
            {
                "strategy": "HYBRID",
                "tasks": [
                    {
                        "name": "discover_flow",
                        "goal": "map the request flow",
                        "mode": "tooled_sequential",
                        "tools": tools,
                    }
                ],
            }
        )

    def test_mode_field_is_still_ignored(self) -> None:
        from personal_agent.orchestrator.expansion_types import SubAgentMode

        plan = _validate_plan_json(self._tooled_plan(["bash", "read"]))
        assert plan is not None
        assert plan.tasks[0].mode == SubAgentMode.PARALLEL_INFERENCE

    def test_tools_field_is_now_parsed(self) -> None:
        """FRE-1389: closes the FRE-884 gap — the planner's tools request now
        reaches PlanTask.tools, where dispatch-time governance filters it.
        """
        plan = _validate_plan_json(self._tooled_plan(["bash", "read"]))
        assert plan is not None
        assert plan.tasks[0].tools == ["bash", "read"]

    def test_non_list_tools_field_is_ignored(self) -> None:
        """A malformed (non-list) tools field must not be iterated char-by-char."""
        raw = json.dumps(
            {
                "strategy": "HYBRID",
                "tasks": [{"name": "t", "goal": "g", "tools": "run_python"}],
            }
        )
        plan = _validate_plan_json(raw)
        assert plan is not None
        assert plan.tasks[0].tools == []

    def test_planner_prompt_never_mentions_tooled_sequential(self) -> None:
        from personal_agent.orchestrator.expansion_controller import (
            _build_planner_system_prompt,
        )

        assert "tooled_sequential" not in _build_planner_system_prompt(["run_python"])
        assert "tooled_sequential" not in _build_planner_system_prompt([])


class TestPlannerPromptToolSurface:
    """FRE-1389: the planner prompt advertises the LIVE sub-agent tool grant."""

    def test_lists_available_tools(self) -> None:
        from personal_agent.orchestrator.expansion_controller import (
            _build_planner_system_prompt,
        )

        prompt = _build_planner_system_prompt(["run_python"])
        assert "run_python" in prompt
        assert '"tools"' in prompt

    def test_empty_surface_tells_planner_to_omit(self) -> None:
        from personal_agent.orchestrator.expansion_controller import (
            _build_planner_system_prompt,
        )

        prompt = _build_planner_system_prompt([])
        assert "no tools are currently available" in prompt

    def test_surface_lookup_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A governance/mode lookup error yields an empty surface, not a crash."""
        from personal_agent.config import GovernanceConfigError
        from personal_agent.orchestrator import expansion_controller as ec

        def _boom() -> None:
            raise GovernanceConfigError("boom")

        monkeypatch.setattr(ec, "get_current_mode", lambda: Mode.NORMAL)
        monkeypatch.setattr(ec, "load_governance_config", _boom)

        assert ec._current_sub_agent_tool_surface("t") == []


class TestExpansionControllerExecute:
    @pytest.fixture
    def controller(self) -> ExpansionController:
        return ExpansionController()

    @pytest.fixture
    def mock_llm(self) -> AsyncMock:
        client = AsyncMock()
        client.respond = AsyncMock(return_value=_make_plan_json(3))
        return client

    @pytest.mark.asyncio
    async def test_successful_expansion(
        self, controller: ExpansionController, mock_llm: AsyncMock
    ) -> None:
        """LLM produces valid plan → sub-agents execute → synthesis."""
        mock_results = [_make_sub_agent_result(f"task_{i}") for i in range(3)]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            result = await controller.execute(
                query="Compare Redis, Memcached, and Hazelcast",
                strategy="HYBRID",
                llm_client=mock_llm,
                trace_id="test-trace",
                messages=[{"role": "user", "content": "Compare Redis, Memcached, and Hazelcast"}],
            )

        assert result.plan is not None
        assert len(result.sub_agent_results) == 3
        assert all(r.success for r in result.sub_agent_results)

    @pytest.mark.asyncio
    async def test_hybrid_emits_start_and_complete_telemetry(
        self,
        controller: ExpansionController,
        mock_llm: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """HYBRID expansion emits hybrid_expansion_start and hybrid_expansion_complete (eval contract)."""
        caplog.set_level("INFO", logger="personal_agent.orchestrator.expansion_controller")
        mock_results = [_make_sub_agent_result(f"task_{i}") for i in range(3)]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            await controller.execute(
                query="Compare Redis, Memcached, and Hazelcast",
                strategy="HYBRID",
                llm_client=mock_llm,
                trace_id="test-trace-hybrid-events",
                messages=[{"role": "user", "content": "Compare Redis, Memcached, and Hazelcast"}],
            )

        assert "hybrid_expansion_start" in caplog.text
        assert "hybrid_expansion_complete" in caplog.text

    @pytest.mark.asyncio
    async def test_fallback_on_invalid_plan(
        self, controller: ExpansionController, mock_llm: AsyncMock
    ) -> None:
        """LLM produces garbage → fallback planner engaged."""
        mock_llm.respond = AsyncMock(return_value="I'll just answer directly...")

        # Fallback planner for "Compare Redis and Memcached" (vs pattern) yields 3 tasks
        # (evaluate_redis, evaluate_memcached, synthesize_recommendation). Supply enough
        # mocks to cover any fallback plan size.
        mock_results = [
            _make_sub_agent_result("evaluate_redis"),
            _make_sub_agent_result("evaluate_memcached"),
            _make_sub_agent_result("synthesize_recommendation"),
        ]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            result = await controller.execute(
                query="Compare Redis and Memcached",
                strategy="HYBRID",
                llm_client=mock_llm,
                trace_id="test-trace",
                messages=[],
            )

        assert result.plan is not None
        assert result.plan.is_fallback is True

    @pytest.mark.asyncio
    async def test_planner_timeout_triggers_fallback(
        self, controller: ExpansionController, mock_llm: AsyncMock
    ) -> None:
        """LLM planner times out → fallback planner engaged."""

        async def slow_respond(*args: Any, **kwargs: Any) -> str:
            await asyncio.sleep(100)
            return _make_plan_json()

        mock_llm.respond = slow_respond

        # Fallback planner for open-ended query yields 2 tasks (research + synthesis).
        # Supply enough mocks to cover both tasks.
        mock_results = [
            _make_sub_agent_result("research_analysis"),
            _make_sub_agent_result("synthesize_recommendation"),
        ]

        # Build a mock settings object with a very short planner timeout
        mock_settings = MagicMock()
        mock_settings.planner_timeout_seconds = 0.01
        mock_settings.worker_timeout_seconds = 45.0

        with (
            patch(
                "personal_agent.orchestrator.expansion_controller.run_sub_agent",
                side_effect=mock_results,
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.get_settings",
                return_value=mock_settings,
            ),
        ):
            result = await controller.execute(
                query="Research scaling approaches",
                strategy="HYBRID",
                llm_client=mock_llm,
                trace_id="test-trace",
                messages=[],
            )

        assert result.plan is not None
        assert result.plan.is_fallback is True

    @pytest.mark.asyncio
    async def test_partial_sub_agent_failure(
        self, controller: ExpansionController, mock_llm: AsyncMock
    ) -> None:
        """Some sub-agents fail → partial results returned."""
        mock_results = [
            _make_sub_agent_result("task_0", success=True),
            _make_sub_agent_result("task_1", success=False),
            _make_sub_agent_result("task_2", success=True),
        ]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            result = await controller.execute(
                query="Compare Redis, Memcached, and Hazelcast",
                strategy="HYBRID",
                llm_client=mock_llm,
                trace_id="test-trace",
                messages=[],
            )

        assert len(result.sub_agent_results) == 3
        assert result.successful_count == 2
        assert result.failed_count == 1


class TestGracefulDegradation:
    @pytest.fixture
    def controller(self) -> ExpansionController:
        return ExpansionController()

    @pytest.fixture
    def mock_llm(self) -> AsyncMock:
        client = AsyncMock()
        client.respond = AsyncMock(return_value=_make_plan_json(3))
        return client

    @pytest.mark.asyncio
    async def test_all_subagents_fail_degraded_response(
        self, controller: ExpansionController, mock_llm: AsyncMock
    ) -> None:
        """All sub-agents fail → degraded=True."""
        mock_results = [_make_sub_agent_result(f"task_{i}", success=False) for i in range(3)]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            result = await controller.execute(
                query="Compare Redis, Memcached, and Hazelcast",
                strategy="HYBRID",
                llm_client=mock_llm,
                trace_id="test-trace",
                messages=[],
            )

        assert result.degraded is True
        assert result.failed_count == 3

    @pytest.mark.asyncio
    async def test_synthesis_context_notes_failures(
        self, controller: ExpansionController, mock_llm: AsyncMock
    ) -> None:
        """Partial failure → synthesis context includes failure notes."""
        mock_results = [
            _make_sub_agent_result("task_0", success=True, summary="Redis is fast"),
            _make_sub_agent_result("task_1", success=False),
            _make_sub_agent_result("task_2", success=True, summary="Hazelcast scales"),
        ]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            result = await controller.execute(
                query="Compare Redis, Memcached, and Hazelcast",
                strategy="HYBRID",
                llm_client=mock_llm,
                trace_id="test-trace",
                messages=[],
            )

        assert "FAILED" in result.synthesis_context
        assert "Redis is fast" in result.synthesis_context
        assert "Hazelcast scales" in result.synthesis_context


class TestExpansionPhaseEvents:
    """ADR-0123 AC-8 (FRE-934): a fan-out is one EXPANSION parent + N SUB_AGENT children.

    Drives ``_run_dispatch`` with a hand-built 3-task plan (bypassing the planner /
    fallback) so the assertion is purely about phase pairing.
    """

    @pytest.fixture
    def controller(self) -> ExpansionController:
        return ExpansionController()

    @staticmethod
    def _capture_phase_events(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
        import personal_agent.transport.agui.transport as transport_mod
        from personal_agent.transport.events import PhaseEndEvent, PhaseStartEvent

        captured: list[Any] = []

        async def _capture(event: Any, session_id: str) -> None:
            if isinstance(event, (PhaseStartEvent, PhaseEndEvent)):
                captured.append(event)

        monkeypatch.setattr(transport_mod, "_push_event", _capture)
        return captured

    @pytest.mark.asyncio
    async def test_three_children_one_parent_parent_ends_last(
        self,
        controller: ExpansionController,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from personal_agent.orchestrator.expansion_controller import ExpansionResult
        from personal_agent.transport.events import Phase, PhaseEndEvent, PhaseStartEvent

        events = self._capture_phase_events(monkeypatch)
        plan = _validate_plan_json(_make_plan_json(3))

        # Distinct per-task duration — phase pairing must hold regardless of how
        # long each child takes (dispatch is sequential, so completion order
        # matches dispatch order: 0, 1, 2).
        delays = {"Goal for task 0": 0.03, "Goal for task 1": 0.01, "Goal for task 2": 0.02}

        async def _delayed(**kwargs: Any) -> SubAgentResult:
            spec = kwargs["spec"]
            await asyncio.sleep(delays.get(spec.task, 0.01))
            return _make_sub_agent_result(spec.task)

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=_delayed,
        ):
            await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="test-trace-ac8",
                messages=[],
                result=ExpansionResult(),
                session_id=str(uuid4()),
            )

        starts = [e for e in events if isinstance(e, PhaseStartEvent)]
        ends = [e for e in events if isinstance(e, PhaseEndEvent)]

        parent_starts = [e for e in starts if e.phase is Phase.EXPANSION]
        child_starts = [e for e in starts if e.phase is Phase.SUB_AGENT]
        parent_ends = [e for e in ends if e.phase is Phase.EXPANSION]
        child_ends = [e for e in ends if e.phase is Phase.SUB_AGENT]

        # One parent, three children.
        assert len(parent_starts) == 1
        assert len(child_starts) == 3
        assert len(parent_ends) == 1
        assert len(child_ends) == 3

        parent_id = parent_starts[0].phase_id
        # Every child is parented to the one EXPANSION phase.
        assert {c.parent_id for c in child_starts} == {parent_id}
        # Children have distinct identities.
        assert len({c.phase_id for c in child_starts}) == 3
        # Each child start pairs with an end of the same phase_id.
        assert {c.phase_id for c in child_starts} == {c.phase_id for c in child_ends}

        # The parent ends only after the last child ends (AC-8).
        parent_end_pos = events.index(parent_ends[0])
        child_end_positions = [events.index(e) for e in child_ends]
        assert parent_end_pos > max(child_end_positions)

    @pytest.mark.asyncio
    async def test_no_session_emits_no_phase_events(
        self,
        controller: ExpansionController,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        events = self._capture_phase_events(monkeypatch)
        plan = _validate_plan_json(_make_plan_json(3))
        mock_results = [_make_sub_agent_result(f"task_{i}") for i in range(3)]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="test-trace-no-session",
                messages=[],
                result=ExpansionResult(),
                session_id=None,
            )

        assert events == []


class TestExpansionResultCost:
    """FRE-501 — ExpansionResult exposes planner + sub-agent cost for the meter."""

    def test_cost_usd_zero_by_default(self) -> None:
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        assert ExpansionResult().cost_usd == 0.0
        assert ExpansionResult().planner_cost_usd == 0.0

    def test_cost_usd_sums_planner_and_subagents(self) -> None:
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        result = ExpansionResult(
            sub_agent_results=[
                _make_sub_agent_result("a", cost_usd=0.1),
                _make_sub_agent_result("b", cost_usd=0.2),
            ],
            planner_cost_usd=0.05,
        )
        assert result.cost_usd == pytest.approx(0.35)

    @pytest.mark.asyncio
    async def test_planner_cost_captured_via_execute(self) -> None:
        """A real planner call (dict response) populates planner_cost_usd, and the
        total rolls planner + every sub-agent cost (FRE-501).
        """
        controller = ExpansionController()
        # A Mapping planner response exercises the real planner path (not fallback)
        # AND carries the planner call cost.
        client = AsyncMock()
        client.respond = AsyncMock(return_value={"content": _make_plan_json(3), "cost_usd": 0.02})
        mock_results = [_make_sub_agent_result(f"task_{i}", cost_usd=0.01) for i in range(3)]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            result = await controller.execute(
                query="Compare Redis, Memcached, and Hazelcast",
                strategy="HYBRID",
                llm_client=client,
                trace_id="test-trace-cost",
                messages=[{"role": "user", "content": "q"}],
            )

        assert result.plan is not None
        assert result.plan.is_fallback is False  # real planner path was taken
        assert result.planner_cost_usd == pytest.approx(0.02)
        # total = planner 0.02 + 3 sub-agents × 0.01
        assert result.cost_usd == pytest.approx(0.05)


class TestSerializedDispatch:
    """FRE-1380 — the fan-out is sequential.

    Sub-agents exist for context isolation, not latency, and the owner ruled
    15.8% wall-clock is worth deleting the FRE-1374 admission race outright
    rather than merely narrowing its window.
    """

    @pytest.fixture
    def controller(self) -> ExpansionController:
        return ExpansionController()

    @staticmethod
    def _timed_run_sub_agent(delay_s: float = 0.02) -> tuple[Any, list[tuple[float, float]]]:
        """A run_sub_agent stand-in that independently records its own call window."""
        observed: list[tuple[float, float]] = []

        async def _run(**kwargs: Any) -> SubAgentResult:
            observed_start = time.monotonic()
            await asyncio.sleep(delay_s)
            observed.append((observed_start, time.monotonic()))
            return _make_sub_agent_result(kwargs["spec"].task)

        return _run, observed

    @pytest.mark.asyncio
    async def test_intervals_never_overlap(self, controller: ExpansionController) -> None:
        """AC-1 — proven by real recorded timestamps, not by reading the code."""
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = _validate_plan_json(_make_plan_json(3))
        assert plan is not None
        run_stub, observed = self._timed_run_sub_agent()
        expansion_result = ExpansionResult()

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=run_stub,
        ):
            results = await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="test-trace-serialized",
                messages=[],
                result=expansion_result,
            )

        assert len(results) == 3
        intervals = expansion_result.dispatch_intervals
        assert len(intervals) == 3
        assert [iv.task_name for iv in intervals] == [t.name for t in plan.tasks]

        for earlier, later in zip(intervals, intervals[1:], strict=False):
            assert later.start_monotonic >= earlier.end_monotonic

        # Cross-check against the independently-observed call windows: a mis-wired
        # recording could still produce a non-overlapping timeline if it measured
        # the wrong thing, so each controller-recorded interval must bracket the
        # stub's own (start, end) for that same task.
        for interval, (observed_start, observed_end) in zip(intervals, observed, strict=True):
            assert interval.start_monotonic <= observed_start
            assert interval.end_monotonic >= observed_end

    @pytest.mark.asyncio
    async def test_interval_recorded_even_on_failure(self, controller: ExpansionController) -> None:
        """AC-1 completeness.

        A raised exception still yields an interval, and dispatch continues
        past it (the finally guarantee).
        """
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = _validate_plan_json(_make_plan_json(3))
        assert plan is not None

        async def _run(**kwargs: Any) -> SubAgentResult:
            if kwargs["spec"].task == "Goal for task 1":
                raise RuntimeError("boom")
            return _make_sub_agent_result(kwargs["spec"].task)

        expansion_result = ExpansionResult()
        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=_run,
        ):
            results = await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="test-trace-partial-raise",
                messages=[],
                result=expansion_result,
            )

        assert len(results) == 2  # the raising task is filtered, not a SubAgentResult
        assert len(expansion_result.dispatch_intervals) == 3
        assert [iv.task_name for iv in expansion_result.dispatch_intervals] == [
            t.name for t in plan.tasks
        ]

    @pytest.mark.asyncio
    async def test_max_observed_concurrency_is_one(self, controller: ExpansionController) -> None:
        """AC-1, belt-and-braces — never more than one sub-agent in flight."""
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = _validate_plan_json(_make_plan_json(3))
        assert plan is not None
        state = {"concurrent": 0}
        observed: list[int] = []

        async def _run(**kwargs: Any) -> SubAgentResult:
            state["concurrent"] += 1
            observed.append(state["concurrent"])
            await asyncio.sleep(0.01)
            state["concurrent"] -= 1
            return _make_sub_agent_result(kwargs["spec"].task)

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=_run,
        ):
            await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="test-trace-max-concurrency",
                messages=[],
                result=ExpansionResult(),
            )

        assert max(observed) == 1

    @pytest.mark.asyncio
    async def test_all_tasks_admitted_beyond_old_ceiling(
        self, controller: ExpansionController
    ) -> None:
        """AC-2 — N=8 exceeds both HYBRID's old cap (4) and DECOMPOSE's (6).

        Every task still produces a real result, none carrying the deleted "not
        admitted" outcome, and the expansion is never marked degraded for
        admission reasons.
        """
        from personal_agent.orchestrator.expansion_controller import ExpansionResult
        from personal_agent.orchestrator.expansion_types import ExpansionPlan, PlanTask

        plan = ExpansionPlan(
            strategy="DECOMPOSE",
            tasks=[PlanTask(name=f"task_{i}", goal=f"Goal for task {i}") for i in range(8)],
        )
        expansion_result = ExpansionResult()

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=lambda **kwargs: _make_sub_agent_result(kwargs["spec"].task),
        ):
            results = await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="test-trace-beyond-ceiling",
                messages=[],
                result=expansion_result,
            )

        assert len(results) == 8
        assert all(r.success for r in results)
        assert all(r.error is None for r in results)
        assert expansion_result.degraded is False


class TestTurnBudgetBound:
    """FRE-1397 — dispatch cannot outlive the turn's own remaining budget.

    ``turn_deadline_monotonic`` is an absolute ``time.monotonic()`` reading
    the caller derives once from the turn's own clocks
    (``executor._turn_deadline_remaining``/``_turn_lifetime_remaining``), not
    a new setting (AC-4). Passed through unchanged rather than re-derived
    from a duration at dispatch time — re-anchoring "seconds remaining" to
    "now" after the planner phase already ran would silently hand dispatch
    back the time the planner just spent. A task that starts after the
    deadline passes is skipped outright rather than dispatched with a doomed
    near-zero budget, and it is never turned into a fabricated failed
    ``SubAgentResult`` (AC-3) — mirrors the deleted ``_not_admitted_result``
    shape FRE-1380 removed for the same reason.
    """

    @pytest.fixture
    def controller(self) -> ExpansionController:
        return ExpansionController()

    @pytest.mark.asyncio
    async def test_tasks_are_skipped_once_budget_exhausted(
        self, controller: ExpansionController
    ) -> None:
        """AC-2/AC-3 — 4 tasks at ~0.05s each against a budget covering only some."""
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = _validate_plan_json(_make_plan_json(4))
        assert plan is not None
        expansion_result = ExpansionResult()
        call_count = {"n": 0}

        async def _run(**kwargs: Any) -> SubAgentResult:
            call_count["n"] += 1
            await asyncio.sleep(0.05)
            return _make_sub_agent_result(kwargs["spec"].task)

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=_run,
        ):
            results = await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="test-trace-budget",
                messages=[],
                result=expansion_result,
                turn_deadline_monotonic=time.monotonic() + 0.11,
            )

        assert call_count["n"] < 4  # the budget did not cover every task
        assert len(results) == call_count["n"]
        assert len(expansion_result.skipped_tasks) == 4 - call_count["n"]
        assert set(expansion_result.skipped_tasks) <= {t.name for t in plan.tasks}
        # AC-3: a skipped task is never returned as a fabricated failed result.
        assert all(r.spec_task not in expansion_result.skipped_tasks for r in results)

    @pytest.mark.asyncio
    async def test_dispatched_deadline_shrinks_with_remaining_budget(
        self, controller: ExpansionController
    ) -> None:
        """Each dispatched call's cap reflects what is actually left, call over call."""
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = _validate_plan_json(_make_plan_json(2))
        assert plan is not None
        expansion_result = ExpansionResult()
        seen_caps: list[float] = []

        async def _run(**kwargs: Any) -> SubAgentResult:
            seen_caps.append(kwargs["max_deadline_seconds"])
            await asyncio.sleep(0.03)
            return _make_sub_agent_result(kwargs["spec"].task)

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=_run,
        ):
            await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="test-trace-cap",
                messages=[],
                result=expansion_result,
                turn_deadline_monotonic=time.monotonic() + 1.0,
            )

        assert len(seen_caps) == 2
        assert all(cap is not None for cap in seen_caps)
        assert seen_caps[0] <= 1.0
        assert seen_caps[0] > seen_caps[1]

    @pytest.mark.asyncio
    async def test_none_budget_preserves_todays_unbounded_behavior(
        self, controller: ExpansionController
    ) -> None:
        """AC-4 — a caller that omits the param gets exactly today's behavior."""
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = _validate_plan_json(_make_plan_json(3))
        assert plan is not None
        expansion_result = ExpansionResult()
        seen_caps: list[Any] = []

        async def _run(**kwargs: Any) -> SubAgentResult:
            seen_caps.append(kwargs.get("max_deadline_seconds"))
            return _make_sub_agent_result(kwargs["spec"].task)

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=_run,
        ):
            results = await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="test-trace-nobudget",
                messages=[],
                result=expansion_result,
            )

        assert len(results) == 3
        assert expansion_result.skipped_tasks == []
        assert all(cap is None for cap in seen_caps)

    @pytest.mark.asyncio
    async def test_already_negative_budget_skips_everything(
        self, controller: ExpansionController
    ) -> None:
        """The turn is already over budget when expansion starts — nothing dispatches."""
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = _validate_plan_json(_make_plan_json(3))
        assert plan is not None
        expansion_result = ExpansionResult()

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
        ) as mock_run:
            results = await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="test-trace-negative",
                messages=[],
                result=expansion_result,
                turn_deadline_monotonic=time.monotonic() - 5.0,
            )

        mock_run.assert_not_called()
        assert results == []
        assert set(expansion_result.skipped_tasks) == {t.name for t in plan.tasks}

    @pytest.mark.asyncio
    async def test_execute_forwards_turn_deadline_to_dispatch_unchanged(
        self, controller: ExpansionController
    ) -> None:
        """The public entry point threads the caller's absolute deadline through

        UNCHANGED — never re-anchored to "now" after the planner phase, which
        would silently hand dispatch back the time the planner just spent.
        """
        mock_llm = AsyncMock()

        async def _slow_plan(*args: Any, **kwargs: Any) -> str:
            await asyncio.sleep(0.05)
            return _make_plan_json(2)

        mock_llm.respond = _slow_plan
        mock_results = [_make_sub_agent_result(f"task_{i}") for i in range(2)]
        deadline = time.monotonic() + 42.0

        with (
            patch(
                "personal_agent.orchestrator.expansion_controller.run_sub_agent",
                side_effect=mock_results,
            ),
            patch.object(
                controller, "_run_dispatch", wraps=controller._run_dispatch
            ) as spy_dispatch,
        ):
            await controller.execute(
                query="Compare Redis and Memcached",
                strategy="HYBRID",
                llm_client=mock_llm,
                trace_id="test-trace-thread-budget",
                messages=[],
                turn_deadline_monotonic=deadline,
            )

        _, kwargs = spy_dispatch.call_args
        assert kwargs["turn_deadline_monotonic"] == deadline

    def test_synthesis_context_notes_skipped_tasks(self, controller: ExpansionController) -> None:
        """A skipped task is surfaced to the primary distinctly from a failed one."""
        plan = _validate_plan_json(_make_plan_json(2))
        assert plan is not None
        results = [_make_sub_agent_result("task_0")]

        context = controller._build_synthesis_context(
            plan=plan, sub_results=results, skipped_tasks=["task_1"]
        )

        assert "task_1" in context
        assert "not run" in context.lower()


class TestSubAgentToolGrant:
    """FRE-1388 — a sub-agent's requested tools are filtered against the

    sub-agent tool principal's grant set before dispatch. Drives
    ``_run_dispatch`` with a hand-built plan carrying real tool names
    (bypassing the planner-JSON path, which always zeroes ``tools`` per
    FRE-884) so the grant filter has something real to refuse — a seeded
    negative, not a vacuous check against an empty request (AC-3).

    ``load_governance_config`` is patched to a hermetic, in-memory config in
    every test here so these assertions depend only on the dispatch logic
    under test, never on what ``config/governance/tools.yaml`` currently says.
    """

    @pytest.fixture
    def controller(self) -> ExpansionController:
        return ExpansionController()

    @staticmethod
    def _hermetic_config() -> Any:
        from personal_agent.governance.models import GovernanceConfig

        return GovernanceConfig(
            modes={}, tools={}, sub_agent_tools=["run_python"], mode_constraints={}
        )

    @staticmethod
    def _one_task_plan(tools: list[str]) -> Any:
        from personal_agent.orchestrator.expansion_types import ExpansionPlan, PlanTask

        return ExpansionPlan(
            strategy="HYBRID",
            tasks=[PlanTask(name="task_0", goal="Goal for task 0", tools=tools)],
        )

    @pytest.mark.asyncio
    async def test_tool_outside_grant_set_is_stripped_before_dispatch(
        self, controller: ExpansionController
    ) -> None:
        """AC-2/AC-3: bash (outside the grant set) is refused; run_python (granted) passes."""
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = self._one_task_plan(["bash", "run_python"])
        captured_specs: list[Any] = []

        async def _capture_spec(**kwargs: Any) -> SubAgentResult:
            spec = kwargs["spec"]
            captured_specs.append(spec)
            # Mirrors sub_agent.run_sub_agent's real contract: denied_tools is
            # threaded from the spec into every terminal result (tested directly
            # in test_sub_agent.py) — echoed here since run_sub_agent is mocked.
            return _make_sub_agent_result("task_0", denied_tools=spec.denied_tools)

        with (
            patch(
                "personal_agent.orchestrator.expansion_controller.get_current_mode",
                return_value=Mode.NORMAL,
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.load_governance_config",
                return_value=self._hermetic_config(),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.run_sub_agent",
                side_effect=_capture_spec,
            ),
        ):
            results = await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="test-trace-tool-grant",
                messages=[],
                result=ExpansionResult(),
            )

        assert len(captured_specs) == 1
        spec = captured_specs[0]
        assert spec.tools == ["run_python"]
        assert spec.denied_tools == ("bash",)
        assert results[0].denied_tools == ("bash",)

    @pytest.mark.asyncio
    async def test_denial_is_legible_in_the_synthesis_context(
        self, controller: ExpansionController
    ) -> None:
        """AC-4: the refusal reaches the primary's report, not only a log line."""
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = self._one_task_plan(["bash"])

        async def _echo_denial(**kwargs: Any) -> SubAgentResult:
            spec = kwargs["spec"]
            return _make_sub_agent_result("task_0", denied_tools=spec.denied_tools)

        with (
            patch(
                "personal_agent.orchestrator.expansion_controller.get_current_mode",
                return_value=Mode.NORMAL,
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.load_governance_config",
                return_value=self._hermetic_config(),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.run_sub_agent",
                side_effect=_echo_denial,
            ),
        ):
            results = await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="test-trace-tool-grant-synth",
                messages=[],
                result=ExpansionResult(),
            )
            context = controller._build_synthesis_context(plan=plan, sub_results=results)

        assert "bash" in context
        assert "not granted" in context

    @pytest.mark.asyncio
    async def test_alert_mode_denies_the_grant_set_entirely(
        self, controller: ExpansionController
    ) -> None:
        """Owner directive: sub-agents hold no tools in ALERT — even run_python."""
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = self._one_task_plan(["run_python"])
        captured_specs: list[Any] = []

        async def _capture_spec(**kwargs: Any) -> SubAgentResult:
            captured_specs.append(kwargs["spec"])
            return _make_sub_agent_result("task_0")

        with (
            patch(
                "personal_agent.orchestrator.expansion_controller.get_current_mode",
                return_value=Mode.ALERT,
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.load_governance_config",
                return_value=self._hermetic_config(),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.run_sub_agent",
                side_effect=_capture_spec,
            ),
        ):
            await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="test-trace-tool-grant-alert",
                messages=[],
                result=ExpansionResult(),
            )

        assert captured_specs[0].tools == []
        assert captured_specs[0].denied_tools == ("run_python",)

    @pytest.mark.asyncio
    async def test_no_tools_requested_leaves_spec_and_synthesis_unaffected(
        self, controller: ExpansionController
    ) -> None:
        """AC-5: today's real shape (planner never requests tools) is unchanged."""
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = self._one_task_plan([])
        captured_specs: list[Any] = []

        async def _capture_spec(**kwargs: Any) -> SubAgentResult:
            captured_specs.append(kwargs["spec"])
            return _make_sub_agent_result("task_0")

        with (
            patch(
                "personal_agent.orchestrator.expansion_controller.get_current_mode",
                return_value=Mode.NORMAL,
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.load_governance_config",
                return_value=self._hermetic_config(),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.run_sub_agent",
                side_effect=_capture_spec,
            ),
        ):
            results = await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="test-trace-tool-grant-noop",
                messages=[],
                result=ExpansionResult(),
            )
            context = controller._build_synthesis_context(plan=plan, sub_results=results)

        assert captured_specs[0].tools == []
        assert captured_specs[0].denied_tools == ()
        assert "denied" not in context.lower()

    @pytest.mark.asyncio
    async def test_governance_lookup_failure_fails_safe_to_deny(
        self, controller: ExpansionController
    ) -> None:
        """A config-load error denies every requested tool rather than aborting the turn."""
        from personal_agent.config import GovernanceConfigError
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = self._one_task_plan(["run_python"])
        captured_specs: list[Any] = []

        async def _capture_spec(**kwargs: Any) -> SubAgentResult:
            spec = kwargs["spec"]
            captured_specs.append(spec)
            return _make_sub_agent_result("task_0", denied_tools=spec.denied_tools)

        with (
            patch(
                "personal_agent.orchestrator.expansion_controller.get_current_mode",
                return_value=Mode.NORMAL,
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.load_governance_config",
                side_effect=GovernanceConfigError("config directory missing"),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.run_sub_agent",
                side_effect=_capture_spec,
            ),
        ):
            results = await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="test-trace-tool-grant-failsafe",
                messages=[],
                result=ExpansionResult(),
            )

        assert captured_specs[0].tools == []
        assert captured_specs[0].denied_tools == ("run_python",)
        assert results[0].success is True


class TestSubAgentGapRedispatch:
    """FRE-1389 AC-5 — a stated tool gap gets ONE replacement dispatch, never a

    sub-agent acquiring the tool itself. ``_maybe_redispatch_on_gap`` is
    exercised through ``_run_dispatch`` end to end, using the same hermetic
    governance-mocking pattern as ``TestSubAgentToolGrant``.
    """

    @pytest.fixture
    def controller(self) -> ExpansionController:
        return ExpansionController()

    @staticmethod
    def _hermetic_config(sub_agent_tools: list[str]) -> Any:
        from personal_agent.governance.models import GovernanceConfig

        return GovernanceConfig(
            modes={}, tools={}, sub_agent_tools=sub_agent_tools, mode_constraints={}
        )

    @staticmethod
    def _one_task_plan(tools: list[str] | None = None) -> Any:
        from personal_agent.orchestrator.expansion_types import ExpansionPlan, PlanTask

        return ExpansionPlan(
            strategy="HYBRID",
            tasks=[PlanTask(name="task_0", goal="Goal for task 0", tools=tools or [])],
        )

    @staticmethod
    def _stub_registry_knowing(*names: str) -> MagicMock:
        """A get_shared_tool_execution_layer() stub whose registry recognizes ``names``."""
        layer = MagicMock()
        layer.registry.get_tool = lambda n: object() if n in names else None
        return layer

    @pytest.mark.asyncio
    async def test_refused_attempt_triggers_one_replacement_dispatch(
        self, controller: ExpansionController
    ) -> None:
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = self._one_task_plan()
        calls: list[Any] = []

        async def _dispatch(**kwargs: Any) -> SubAgentResult:
            spec = kwargs["spec"]
            calls.append(spec)
            if len(calls) == 1:
                return _make_sub_agent_result("task_0", refused_tool_attempts=("run_python",))
            return _make_sub_agent_result("task_0")

        with (
            patch(
                "personal_agent.orchestrator.expansion_controller.get_current_mode",
                return_value=Mode.NORMAL,
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.load_governance_config",
                return_value=self._hermetic_config(["run_python"]),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.get_shared_tool_execution_layer",
                return_value=self._stub_registry_knowing("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.run_sub_agent",
                side_effect=_dispatch,
            ),
        ):
            results = await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="t-gap-refused",
                messages=[],
                result=ExpansionResult(),
            )

        assert len(calls) == 2
        assert calls[1].tools == ["run_python"]
        assert "retry" in calls[1].task
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_stated_tool_gap_triggers_one_replacement_dispatch(
        self, controller: ExpansionController
    ) -> None:
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = self._one_task_plan()
        calls: list[Any] = []

        async def _dispatch(**kwargs: Any) -> SubAgentResult:
            spec = kwargs["spec"]
            calls.append(spec)
            if len(calls) == 1:
                return _make_sub_agent_result("task_0", stated_tool_gap="run_python")
            return _make_sub_agent_result("task_0")

        with (
            patch(
                "personal_agent.orchestrator.expansion_controller.get_current_mode",
                return_value=Mode.NORMAL,
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.load_governance_config",
                return_value=self._hermetic_config(["run_python"]),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.get_shared_tool_execution_layer",
                return_value=self._stub_registry_knowing("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.run_sub_agent",
                side_effect=_dispatch,
            ),
        ):
            results = await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="t-gap-stated",
                messages=[],
                result=ExpansionResult(),
            )

        assert len(calls) == 2
        assert calls[1].tools == ["run_python"]
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_retry_is_single_shot(self, controller: ExpansionController) -> None:
        """The replacement's OWN stated gap is never acted on — no chained retries."""
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = self._one_task_plan()
        calls: list[Any] = []

        async def _always_states_a_gap(**kwargs: Any) -> SubAgentResult:
            calls.append(kwargs["spec"])
            return _make_sub_agent_result("task_0", stated_tool_gap="run_python")

        with (
            patch(
                "personal_agent.orchestrator.expansion_controller.get_current_mode",
                return_value=Mode.NORMAL,
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.load_governance_config",
                return_value=self._hermetic_config(["run_python"]),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.get_shared_tool_execution_layer",
                return_value=self._stub_registry_knowing("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.run_sub_agent",
                side_effect=_always_states_a_gap,
            ),
        ):
            results = await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="t-gap-single-shot",
                messages=[],
                result=ExpansionResult(),
            )

        assert len(calls) == 2  # original + exactly one replacement, never a third
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_unregistered_gap_name_is_ignored(self, controller: ExpansionController) -> None:
        """A hallucinated name that isn't even a registered tool spends no retry."""
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = self._one_task_plan()
        calls: list[Any] = []

        async def _dispatch(**kwargs: Any) -> SubAgentResult:
            calls.append(kwargs["spec"])
            return _make_sub_agent_result("task_0", stated_tool_gap="not_a_real_tool")

        with (
            patch(
                "personal_agent.orchestrator.expansion_controller.get_current_mode",
                return_value=Mode.NORMAL,
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.load_governance_config",
                return_value=self._hermetic_config(["run_python"]),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.get_shared_tool_execution_layer",
                return_value=self._stub_registry_knowing("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.run_sub_agent",
                side_effect=_dispatch,
            ),
        ):
            results = await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="t-gap-unregistered",
                messages=[],
                result=ExpansionResult(),
            )

        assert len(calls) == 1
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_gap_still_denied_on_retry_yields_no_extra_result(
        self, controller: ExpansionController
    ) -> None:
        """A gap outside the sub-agent grant surface entirely stays denied — no retry."""
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = self._one_task_plan()
        calls: list[Any] = []

        async def _dispatch(**kwargs: Any) -> SubAgentResult:
            calls.append(kwargs["spec"])
            return _make_sub_agent_result("task_0", stated_tool_gap="web_search")

        with (
            patch(
                "personal_agent.orchestrator.expansion_controller.get_current_mode",
                return_value=Mode.NORMAL,
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.load_governance_config",
                # web_search is registered but NOT in the sub-agent grant surface.
                return_value=self._hermetic_config(["run_python"]),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.get_shared_tool_execution_layer",
                return_value=self._stub_registry_knowing("run_python", "web_search"),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.run_sub_agent",
                side_effect=_dispatch,
            ),
        ):
            results = await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="t-gap-still-denied",
                messages=[],
                result=ExpansionResult(),
            )

        assert len(calls) == 1
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_redispatch_pairs_with_the_right_task_after_earlier_exception(
        self, controller: ExpansionController
    ) -> None:
        """An earlier task's raw dispatch exception must not shift the retry pairing."""
        from personal_agent.orchestrator.expansion_controller import ExpansionResult
        from personal_agent.orchestrator.expansion_types import ExpansionPlan, PlanTask

        plan = ExpansionPlan(
            strategy="HYBRID",
            tasks=[
                PlanTask(name="task_boom", goal="raises"),
                PlanTask(name="task_gap", goal="states a gap"),
            ],
        )
        calls: list[Any] = []

        async def _dispatch(**kwargs: Any) -> SubAgentResult:
            spec = kwargs["spec"]
            calls.append(spec)
            if "raises" in spec.task:
                raise RuntimeError("boom")
            if "retry" in spec.task:
                return _make_sub_agent_result("task_gap")
            return _make_sub_agent_result("task_gap", stated_tool_gap="run_python")

        with (
            patch(
                "personal_agent.orchestrator.expansion_controller.get_current_mode",
                return_value=Mode.NORMAL,
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.load_governance_config",
                return_value=self._hermetic_config(["run_python"]),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.get_shared_tool_execution_layer",
                return_value=self._stub_registry_knowing("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.run_sub_agent",
                side_effect=_dispatch,
            ),
        ):
            results = await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="t-gap-alignment",
                messages=[],
                result=ExpansionResult(),
            )

        # task_boom's exception drops it entirely; task_gap gets its own retry,
        # correctly paired (not confused with task_boom's slot).
        assert len(results) == 2
        assert all(r.spec_task == "task_gap" for r in results)
        assert calls[-1].task.startswith("states a gap")

    @pytest.mark.asyncio
    async def test_replacement_cost_is_not_dropped(self, controller: ExpansionController) -> None:
        """AC-6: the original attempt's cost survives even though it was incomplete."""
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = self._one_task_plan()
        calls: list[Any] = []

        async def _dispatch(**kwargs: Any) -> SubAgentResult:
            calls.append(kwargs["spec"])
            if len(calls) == 1:
                return _make_sub_agent_result("task_0", stated_tool_gap="run_python", cost_usd=0.01)
            return _make_sub_agent_result("task_0", cost_usd=0.02)

        with (
            patch(
                "personal_agent.orchestrator.expansion_controller.get_current_mode",
                return_value=Mode.NORMAL,
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.load_governance_config",
                return_value=self._hermetic_config(["run_python"]),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.get_shared_tool_execution_layer",
                return_value=self._stub_registry_knowing("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.run_sub_agent",
                side_effect=_dispatch,
            ),
        ):
            results = await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="t-gap-cost",
                messages=[],
                result=ExpansionResult(),
            )

        assert sum(r.cost_usd for r in results) == pytest.approx(0.03)

    @pytest.mark.asyncio
    async def test_gap_for_an_already_denied_requested_tool_is_not_retried(
        self, controller: ExpansionController
    ) -> None:
        """A partially-granted task's gap must compare against what was actually

        GRANTED (spec.tools), not merely requested (task.tools) — a gap for a
        tool the planner already asked for and governance already denied must
        not spend a retry re-asking the same, unchanged question.
        """
        from personal_agent.orchestrator.expansion_controller import ExpansionResult
        from personal_agent.orchestrator.expansion_types import ExpansionPlan, PlanTask

        # bash was requested but denied; run_python was requested and granted.
        plan = ExpansionPlan(
            strategy="HYBRID",
            tasks=[PlanTask(name="task_0", goal="Goal for task 0", tools=["bash", "run_python"])],
        )
        calls: list[Any] = []

        async def _dispatch(**kwargs: Any) -> SubAgentResult:
            spec = kwargs["spec"]
            calls.append(spec)
            return _make_sub_agent_result(
                "task_0", denied_tools=spec.denied_tools, refused_tool_attempts=("bash",)
            )

        with (
            patch(
                "personal_agent.orchestrator.expansion_controller.get_current_mode",
                return_value=Mode.NORMAL,
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.load_governance_config",
                # bash is never in the sub-agent grant surface — always denied.
                return_value=self._hermetic_config(["run_python"]),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.get_shared_tool_execution_layer",
                return_value=self._stub_registry_knowing("run_python", "bash"),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.run_sub_agent",
                side_effect=_dispatch,
            ),
        ):
            results = await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="t-gap-already-denied",
                messages=[],
                result=ExpansionResult(),
            )

        assert calls[0].tools == ["run_python"]
        assert len(calls) == 1  # no retry — bash was already, and still, denied
        assert len(results) == 1


class TestSynthesisContextExcludesFullOutput:
    """FRE-1380 AC-4 (lock half) — synthesis is built from digests only.

    The primary's synthesis context is built from ``summary`` alone;
    ``full_output`` never reaches it. This is the whole justification for
    running sub-agents at all (context isolation), so a regression here must
    fail a test, not wait to be noticed in production.
    """

    def test_full_output_never_appears_in_synthesis_context(self) -> None:
        controller = ExpansionController()
        plan = _validate_plan_json(_make_plan_json(1))
        assert plan is not None

        digest = "SHORT_DIGEST_MARKER"
        full_output = "LONG_FULL_OUTPUT_MARKER" * 200
        result = SubAgentResult(
            task_id=uuid4(),
            spec_task="task_0",
            summary=digest,
            full_output=full_output,
            tools_used=[],
            token_count=10,
            duration_ms=10,
            success=True,
        )

        context = controller._build_synthesis_context(plan=plan, sub_results=[result])

        assert digest in context
        assert full_output not in context
        assert "LONG_FULL_OUTPUT_MARKER" not in context


class TestPlannerRoleBinding:
    """FRE-1390 — the planner call must reason about a turn that has not happened

    yet, so it runs on a thinking-capable deployment. ``ModelRole.SUB_AGENT``
    binds to the instruct sibling with ``disable_thinking: true``
    (config/model_roles.yaml); ``ModelRole.PRIMARY`` is the thinking-capable
    deployment. AC-1's live-container verification is evidence for the PR, not
    a unit test — this asserts the one thing a unit test can: which role the
    call site actually requests.
    """

    @pytest.fixture
    def controller(self) -> ExpansionController:
        return ExpansionController()

    @pytest.mark.asyncio
    async def test_planner_call_requests_primary_role(
        self, controller: ExpansionController
    ) -> None:
        client = AsyncMock()
        client.respond = AsyncMock(return_value={"content": _make_plan_json(3), "cost_usd": 0.0})
        mock_results = [_make_sub_agent_result(f"task_{i}") for i in range(3)]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            result = await controller.execute(
                query="Compare Redis, Memcached, and Hazelcast",
                strategy="HYBRID",
                llm_client=client,
                trace_id="test-trace-role",
                messages=[],
            )

        assert result.plan is not None
        assert result.plan.is_fallback is False  # real planner path, not fallback
        client.respond.assert_awaited_once()
        call_kwargs = client.respond.call_args.kwargs
        assert call_kwargs["role"] is ModelRole.PRIMARY
        assert call_kwargs["role"] is not ModelRole.SUB_AGENT


class TestPlannerServerErrorFallback:
    """FRE-1390 AC-4 — a 503 from the busier, larger-context deployment the

    planner now shares must still hand off to the deterministic fallback
    planner. Exercised with the real exception class the client raises for a
    5xx after exhausting retries (``LLMServerError``,
    llm_client/types.py:202), not assumed from the generic ``except
    Exception`` in ``_run_planner``.
    """

    @pytest.fixture
    def controller(self) -> ExpansionController:
        return ExpansionController()

    @pytest.mark.asyncio
    async def test_planner_503_falls_back_to_deterministic_planner(
        self, controller: ExpansionController
    ) -> None:
        client = AsyncMock()
        client.respond = AsyncMock(side_effect=LLMServerError("Server error 503: shared GPU busy"))

        # Comma-list query so the fallback planner's entity path fires
        # (evaluate_redis, evaluate_memcached, evaluate_hazelcast, synthesize).
        mock_results = [
            _make_sub_agent_result("evaluate_redis"),
            _make_sub_agent_result("evaluate_memcached"),
            _make_sub_agent_result("evaluate_hazelcast"),
            _make_sub_agent_result("synthesize_recommendation"),
        ]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            result = await controller.execute(
                query="Compare Redis, Memcached, and Hazelcast",
                strategy="HYBRID",
                llm_client=client,
                trace_id="test-trace-503",
                messages=[],
            )

        assert result.plan is not None
        assert result.plan.is_fallback is True
        assert result.degraded is False
        assert result.successful_count == 4
