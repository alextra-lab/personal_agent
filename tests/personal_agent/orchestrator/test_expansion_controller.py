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
from personal_agent.orchestrator.expansion_types import ExpansionPlan, PlanTask
from personal_agent.orchestrator.sub_agent_types import SubAgentResult
from personal_agent.orchestrator.worker_types import (
    WORKER_TYPES,
    Finding,
    Gap,
    Thoroughness,
    WorkerReport,
    WorkerType,
    render_worker_report_summary,
)

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
                "type": "general",
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
        bad = '{"strategy": "HYBRID", "tasks": [{"goal": "g", "type": "general"}]}'
        assert _validate_plan_json(bad) is None

    def test_task_missing_goal(self) -> None:
        bad = '{"strategy": "HYBRID", "tasks": [{"name": "n", "type": "general"}]}'
        assert _validate_plan_json(bad) is None

    def test_caps_task_count_hybrid(self) -> None:
        """ADR-0150 D4: HYBRID holds at most 3 tasks — no reserved combine slot."""
        plan = _validate_plan_json(_make_plan_json(10))
        assert plan is not None
        assert len(plan.tasks) == 3

    def test_caps_task_count_decompose(self) -> None:
        plan = _validate_plan_json(_make_plan_json(10), "DECOMPOSE")
        assert plan is not None
        assert len(plan.tasks) == 5


class TestPlannerDiscoveryRetired:
    """FRE-884 / ADR-0150 D2 — retired plan fields are ignored, never obeyed.

    The old discovery-slice ``mode`` field is still ignored — ``SubAgentMode``
    only has PARALLEL_INFERENCE. The per-task ``tools`` list and free-text
    ``expected_output`` (FRE-1389) are ignored too: the worker type decides both.
    """

    @staticmethod
    def _legacy_plan(tools: object) -> str:
        return json.dumps(
            {
                "strategy": "HYBRID",
                "tasks": [
                    {
                        "name": "discover_flow",
                        "goal": "map the request flow",
                        "type": "general",
                        "mode": "tooled_sequential",
                        "tools": tools,
                        "expected_output": "a table",
                    }
                ],
            }
        )

    def test_mode_field_is_still_ignored(self) -> None:
        from personal_agent.orchestrator.expansion_types import SubAgentMode

        plan = _validate_plan_json(self._legacy_plan(["bash", "read"]))
        assert plan is not None
        assert plan.tasks[0].mode == SubAgentMode.PARALLEL_INFERENCE

    def test_legacy_tools_field_is_ignored(self) -> None:
        """ADR-0150 D2: a planner that still writes `tools` gets its type's tools, not these."""
        plan = _validate_plan_json(self._legacy_plan(["bash", "read"]))
        assert plan is not None
        assert plan.tasks[0].type == WorkerType.GENERAL
        assert not hasattr(plan.tasks[0], "tools")

    def test_non_list_tools_field_is_ignored(self) -> None:
        plan = _validate_plan_json(self._legacy_plan("run_python"))
        assert plan is not None

    def test_planner_prompt_never_mentions_tooled_sequential(self) -> None:
        from personal_agent.orchestrator.expansion_controller import (
            _build_planner_system_prompt,
        )

        assert "tooled_sequential" not in _build_planner_system_prompt(["run_python"])
        assert "tooled_sequential" not in _build_planner_system_prompt([])


class TestPlannerPromptToolSurface:
    """FRE-1389 / ADR-0150 D2: the planner prompt renders the registry with the LIVE grant."""

    def test_each_type_shows_its_description_and_granted_tools(self) -> None:
        from personal_agent.orchestrator.expansion_controller import (
            _build_planner_system_prompt,
        )

        prompt = _build_planner_system_prompt(["web_search", "run_python"])
        researcher = WORKER_TYPES[WorkerType.RESEARCHER].description
        general = WORKER_TYPES[WorkerType.GENERAL].description
        assert f"  - researcher: {researcher}. Tools granted now: web_search" in prompt
        assert f"  - general: {general}. Tools granted now: run_python" in prompt

    def test_the_schema_asks_for_type_and_thoroughness_not_tools(self) -> None:
        from personal_agent.orchestrator.expansion_controller import (
            _build_planner_system_prompt,
        )

        prompt = _build_planner_system_prompt(["web_search"])
        assert '"type": "researcher|general"' in prompt
        assert '"thoroughness": "quick|standard|thorough"' in prompt
        assert '"tools"' not in prompt
        assert '"expected_output"' not in prompt

    def test_empty_surface_keeps_every_type_with_no_tools(self) -> None:
        """A type stays pickable with no tools — a general worker can still answer."""
        from personal_agent.orchestrator.expansion_controller import (
            _build_planner_system_prompt,
        )

        prompt = _build_planner_system_prompt([])
        assert prompt.count("Tools granted now: none") == 2
        assert "  - researcher: " in prompt
        assert "  - general: " in prompt

    def test_a_granted_tool_no_type_declares_is_never_rendered(self) -> None:
        from personal_agent.orchestrator.expansion_controller import (
            _build_planner_system_prompt,
        )

        assert "fetch_url" not in _build_planner_system_prompt(["fetch_url", "web_search"])

    def test_surface_lookup_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A governance/mode lookup error yields an empty surface, not a crash."""
        from personal_agent.config import GovernanceConfigError
        from personal_agent.orchestrator import expansion_controller as ec

        def _boom() -> None:
            raise GovernanceConfigError("boom")

        monkeypatch.setattr(ec, "get_current_mode", lambda: Mode.NORMAL)
        monkeypatch.setattr(ec, "load_governance_config", _boom)

        assert ec._current_sub_agent_tool_surface("t") == []

    def test_a_refused_decision_is_never_advertised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FRE-1463 — the one bug the decision-record shape can introduce.

        ``sub_agent_tools`` is a mapping now, so iterating it would offer every
        key to the planner, refusals included. The planner would then request a
        tool the grant evaluation then refuses, on every turn.
        """
        from personal_agent.governance.models import GovernanceConfig, SubAgentToolDecision
        from personal_agent.orchestrator import expansion_controller as ec

        config = GovernanceConfig(
            modes={},
            tools={},
            sub_agent_tools={
                "run_python": SubAgentToolDecision(granted=True, reason="granted"),
                "fetch_url": SubAgentToolDecision(granted=False, reason="refused on 2026-09-08"),
            },
            mode_constraints={},
        )
        monkeypatch.setattr(ec, "get_current_mode", lambda: Mode.NORMAL)
        monkeypatch.setattr(ec, "load_governance_config", lambda: config)

        assert ec._current_sub_agent_tool_surface("t") == ["run_python"]

    def test_the_live_surface_carries_the_new_grants(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FRE-1463 AC-2 (config half) — the planner may now ask for the new tools."""
        from personal_agent.orchestrator import expansion_controller as ec

        monkeypatch.setattr(ec, "get_current_mode", lambda: Mode.NORMAL)

        surface = ec._current_sub_agent_tool_surface("t")
        assert "web_search" in surface
        assert "search_memory" in surface
        assert "fetch_url" not in surface
        # FRE-1467 granted this one: FRE-1463 refused it only because it could
        # not work without an identity, and FRE-1467 threads the identity.
        assert "recall_personal_history" in surface

    @pytest.mark.parametrize("mode", [Mode.ALERT, Mode.DEGRADED])
    def test_the_denied_modes_advertise_nothing(
        self, mode: Mode, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FRE-1463 AC-5 — the revocation reaches the planner surface too."""
        from personal_agent.orchestrator import expansion_controller as ec

        monkeypatch.setattr(ec, "get_current_mode", lambda: mode)

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
        from personal_agent.orchestrator.expansion_types import ExpansionPlan

        plan = ExpansionPlan(
            strategy="DECOMPOSE",
            tasks=[_task(f"task_{i}", f"Goal for task {i}") for i in range(8)],
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

        plan = _validate_plan_json(_make_plan_json(4), "DECOMPOSE")
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


class TestSynthesisContextTerminalFacts:
    """ADR-0149 D4 (FRE-1484) AC-5 — the synthesis context tells the truth.

    Each worker's header carries its terminal facts, and the closing sentence
    is replaced with counts rather than the flat "completed" claim.
    """

    @pytest.fixture
    def controller(self) -> ExpansionController:
        return ExpansionController()

    def test_header_carries_stop_reason_and_report_kind(
        self, controller: ExpansionController
    ) -> None:
        """A failed worker's header is not just FAILED: <error> as before."""
        plan = _validate_plan_json(_make_plan_json(1))
        assert plan is not None
        from dataclasses import replace

        result = replace(
            _make_sub_agent_result("task_0", success=False),
            stop_reason="cap",
            report_kind="synthesized",
            tool_iterations=5,
            tool_result_chars_absorbed=154_755,
        )

        context = controller._build_synthesis_context(plan=plan, sub_results=[result])

        assert "stop=cap" in context
        assert "report=synthesized" in context
        assert "5 round(s)" in context
        assert "154,755 chars absorbed" in context

    def test_closing_sentence_is_counted_not_flat(self, controller: ExpansionController) -> None:
        """The old flat close is gone; the new one reports real counts."""
        from dataclasses import replace

        plan = _validate_plan_json(_make_plan_json(3))
        assert plan is not None
        results = [
            _make_sub_agent_result("task_0", success=True),
            replace(
                _make_sub_agent_result("task_1", success=False),
                stop_reason="cap",
                report_kind="synthesized",
            ),
            replace(
                _make_sub_agent_result("task_2", success=False),
                stop_reason="deadline",
                report_kind="ledger",
            ),
        ]

        context = controller._build_synthesis_context(plan=plan, sub_results=results)

        assert "The sub-tasks above have been completed" not in context
        assert "1 of 3 sub-tasks completed" in context
        assert "1 stopped at their budget and wrote a partial report" in context
        assert "1 stopped without a report" in context

    def test_no_closing_sentence_when_no_results_dispatched(
        self, controller: ExpansionController
    ) -> None:
        """No dispatched results (only skipped tasks) yields no n-of-0 claim."""
        plan = _validate_plan_json(_make_plan_json(1))
        assert plan is not None

        context = controller._build_synthesis_context(
            plan=plan, sub_results=[], skipped_tasks=["task_0"]
        )

        assert "sub-tasks completed" not in context


class TestSynthesisContextRendersReports:
    """ADR-0150 D4 (FRE-1494) AC-5 — the primary receives data it can read."""

    @pytest.fixture
    def controller(self) -> ExpansionController:
        return ExpansionController()

    @staticmethod
    def _schema_result(
        task_name: str, findings: list[Finding], gaps: list[Gap], notes: str = ""
    ) -> SubAgentResult:
        from dataclasses import replace

        report = WorkerReport(working_notes=notes, findings=findings, gaps=gaps, tool_gap="")
        return replace(
            _make_sub_agent_result(
                task_name, success=True, summary=render_worker_report_summary(report)
            ),
            report=report,
            report_schema="worker_report_v1",
            report_kind="synthesized",
            stop_reason="completed",
        )

    def test_renders_exactly_f_findings_and_g_gaps_once(
        self, controller: ExpansionController
    ) -> None:
        plan = _validate_plan_json(_make_plan_json(2))
        assert plan is not None
        f1 = Finding(
            claim="c1", source_url="https://a.example", date_or_period="", why_it_matters="w1"
        )
        f2 = Finding(
            claim="c2", source_url="https://b.example", date_or_period="", why_it_matters="w2"
        )
        g1 = Gap(looked_for="missing1", where="web_search")
        r1 = self._schema_result("task_0", findings=[f1], gaps=[g1])
        r2 = self._schema_result("task_1", findings=[f2], gaps=[])

        context = controller._build_synthesis_context(plan=plan, sub_results=[r1, r2])

        assert context.count("c1 —") == 1
        assert context.count("c2 —") == 1
        assert context.count("missing1") == 1
        assert context.count("### Not found") == 1
        assert "{" not in context

    def test_text_reporting_worker_renders_summary_verbatim(
        self, controller: ExpansionController
    ) -> None:
        plan = _validate_plan_json(_make_plan_json(1))
        assert plan is not None
        text_result = _make_sub_agent_result("task_0", summary="plain text summary")

        context = controller._build_synthesis_context(plan=plan, sub_results=[text_result])

        assert "plain text summary" in context

    def test_a_schema_workers_own_section_carries_no_gaps(
        self, controller: ExpansionController
    ) -> None:
        """Gaps appear once, in the combined section — never inline per worker."""
        plan = _validate_plan_json(_make_plan_json(1))
        assert plan is not None
        f = Finding(
            claim="c", source_url="https://a.example", date_or_period="", why_it_matters="w"
        )
        g = Gap(looked_for="missing_here", where="somewhere")
        r = self._schema_result("task_0", findings=[f], gaps=[g])

        context = controller._build_synthesis_context(plan=plan, sub_results=[r])

        header_idx = context.index("### task_0")
        not_found_idx = context.index("### Not found")
        assert "missing_here" not in context[header_idx:not_found_idx]
        assert "missing_here" in context[not_found_idx:]

    def test_no_not_found_section_when_no_gaps(self, controller: ExpansionController) -> None:
        plan = _validate_plan_json(_make_plan_json(1))
        assert plan is not None
        f = Finding(
            claim="c", source_url="https://a.example", date_or_period="", why_it_matters="w"
        )
        r = self._schema_result("task_0", findings=[f], gaps=[])

        context = controller._build_synthesis_context(plan=plan, sub_results=[r])

        assert "Not found" not in context


def _task(
    name: str = "task_0",
    goal: str = "Goal for task 0",
    type_: WorkerType = WorkerType.GENERAL,
    thoroughness: Thoroughness = "quick",
) -> PlanTask:
    return PlanTask(name=name, goal=goal, type=type_, thoroughness=thoroughness)


def _one_task_plan(type_: WorkerType = WorkerType.GENERAL) -> ExpansionPlan:
    return ExpansionPlan(strategy="HYBRID", tasks=[_task(type_=type_)])


def _hermetic_config(granted: tuple[str, ...]) -> Any:
    """An in-memory governance config granting exactly ``granted`` to sub-agents."""
    from personal_agent.governance.models import GovernanceConfig, SubAgentToolDecision

    return GovernanceConfig(
        modes={},
        tools={},
        sub_agent_tools={
            name: SubAgentToolDecision(granted=True, reason="granted for this test")
            for name in granted
        },
        mode_constraints={},
    )


def _stub_registry_knowing(*names: str) -> MagicMock:
    """A get_shared_tool_execution_layer() stub whose registry recognizes ``names``."""
    layer = MagicMock()
    layer.registry.get_tool = lambda n: object() if n in names else None
    return layer


async def _dispatch_hermetic(
    controller: ExpansionController,
    plan: ExpansionPlan,
    run: Any,
    *,
    granted: tuple[str, ...] = (),
    known: tuple[str, ...] = (),
    mode: Mode = Mode.NORMAL,
    config_error: Exception | None = None,
) -> list[SubAgentResult]:
    """Drive ``_run_dispatch`` with hermetic governance, registry and worker."""
    from personal_agent.orchestrator.expansion_controller import ExpansionResult

    config_patch = (
        {"side_effect": config_error}
        if config_error is not None
        else {"return_value": _hermetic_config(granted)}
    )
    with (
        patch(
            "personal_agent.orchestrator.expansion_controller.get_current_mode",
            return_value=mode,
        ),
        patch(
            "personal_agent.orchestrator.expansion_controller.load_governance_config",
            **config_patch,
        ),
        patch(
            "personal_agent.orchestrator.expansion_controller.get_shared_tool_execution_layer",
            return_value=_stub_registry_knowing(*known),
        ),
        patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=run,
        ),
    ):
        return await controller._run_dispatch(
            plan=plan,
            llm_client=AsyncMock(),
            trace_id="t-hermetic",
            messages=[],
            result=ExpansionResult(),
        )


_GENERAL_TOOLS = WORKER_TYPES[WorkerType.GENERAL].tools


class TestSubAgentToolGrant:
    """FRE-1388 — a task's tools are filtered against the sub-agent grant set before dispatch.

    ADR-0150 D2: the request is the task type's closed tool list; the filter is
    unchanged. Governance is hermetic in every test here, so these assertions
    depend only on the dispatch logic under test, never on what
    ``config/governance/tools.yaml`` currently says.
    """

    @pytest.fixture
    def controller(self) -> ExpansionController:
        return ExpansionController()

    @staticmethod
    def _capture(specs: list[Any]) -> Any:
        async def _run(**kwargs: Any) -> SubAgentResult:
            spec = kwargs["spec"]
            specs.append(spec)
            # Mirrors run_sub_agent's real contract: denied_tools is threaded from
            # the spec into every terminal result (tested in test_sub_agent.py).
            return _make_sub_agent_result("task_0", denied_tools=spec.denied_tools)

        return _run

    @pytest.mark.asyncio
    async def test_type_tools_outside_grant_set_are_stripped(
        self, controller: ExpansionController
    ) -> None:
        """AC-2/AC-3: of general's three tools, only the granted one is passed."""
        specs: list[Any] = []
        results = await _dispatch_hermetic(
            controller, _one_task_plan(), self._capture(specs), granted=("run_python",)
        )

        assert specs[0].tools == ["run_python"]
        assert specs[0].denied_tools == ("search_memory", "recall_personal_history")
        assert results[0].denied_tools == ("search_memory", "recall_personal_history")

    @pytest.mark.asyncio
    async def test_denial_is_legible_in_the_synthesis_context(
        self, controller: ExpansionController
    ) -> None:
        """AC-4: the refusal reaches the primary's report, not only a log line."""
        specs: list[Any] = []
        plan = _one_task_plan(WorkerType.RESEARCHER)
        results = await _dispatch_hermetic(
            controller, plan, self._capture(specs), granted=("run_python",)
        )
        context = controller._build_synthesis_context(plan=plan, sub_results=results)

        assert specs[0].denied_tools == ("web_search",)
        assert "web_search" in context
        assert "not granted" in context

    @pytest.mark.asyncio
    async def test_alert_mode_denies_the_grant_set_entirely(
        self, controller: ExpansionController
    ) -> None:
        """Owner directive: sub-agents hold no tools in ALERT — even run_python."""
        specs: list[Any] = []
        await _dispatch_hermetic(
            controller,
            _one_task_plan(),
            self._capture(specs),
            granted=_GENERAL_TOOLS,
            mode=Mode.ALERT,
        )

        assert specs[0].tools == []
        assert specs[0].denied_tools == _GENERAL_TOOLS

    @pytest.mark.asyncio
    async def test_a_fully_granted_type_leaves_spec_and_synthesis_unaffected(
        self, controller: ExpansionController
    ) -> None:
        specs: list[Any] = []
        plan = _one_task_plan()
        results = await _dispatch_hermetic(
            controller, plan, self._capture(specs), granted=_GENERAL_TOOLS
        )
        context = controller._build_synthesis_context(plan=plan, sub_results=results)

        assert specs[0].tools == list(_GENERAL_TOOLS)
        assert specs[0].denied_tools == ()
        assert "denied" not in context.lower()

    @pytest.mark.asyncio
    async def test_governance_lookup_failure_fails_safe_to_deny(
        self, controller: ExpansionController
    ) -> None:
        """A config-load error denies every requested tool rather than aborting the turn."""
        from personal_agent.config import GovernanceConfigError

        specs: list[Any] = []
        results = await _dispatch_hermetic(
            controller,
            _one_task_plan(),
            self._capture(specs),
            config_error=GovernanceConfigError("config directory missing"),
        )

        assert specs[0].tools == []
        assert specs[0].denied_tools == _GENERAL_TOOLS
        assert results[0].success is True


class TestSubAgentGapRedispatch:
    """FRE-1493 AC-6 (ADR-0150 D2) — the gap redispatch, closed by type.

    A stated or refused tool gets ONE replacement, and only as the one other
    registry type that declares the tool — never a same-type worker with a
    widened list, never a second replacement.
    """

    @pytest.fixture
    def controller(self) -> ExpansionController:
        return ExpansionController()

    @pytest.mark.asyncio
    async def test_general_stating_web_search_gets_one_researcher_replacement(
        self, controller: ExpansionController
    ) -> None:
        plan = ExpansionPlan(
            strategy="HYBRID",
            tasks=[
                _task("find_facts", "Goal A", WorkerType.GENERAL, "thorough"),
                _task("other_part", "Goal B", WorkerType.GENERAL, "quick"),
            ],
        )
        calls: list[Any] = []

        async def _run(**kwargs: Any) -> SubAgentResult:
            spec = kwargs["spec"]
            calls.append(spec)
            if spec.task == "Goal A":
                return _make_sub_agent_result("find_facts", stated_tool_gap="web_search")
            return _make_sub_agent_result(spec.task)

        results = await _dispatch_hermetic(
            controller, plan, _run, granted=("web_search", "run_python"), known=("web_search",)
        )

        assert len(calls) == 3  # Goal A, its one replacement, Goal B
        original, replacement = calls[0], calls[1]
        assert replacement.worker_type == WorkerType.RESEARCHER
        assert replacement.tools == ["web_search"]
        assert replacement.task.startswith("Goal A")
        assert "retry" in replacement.task
        assert replacement.thoroughness == original.thoroughness == "thorough"
        assert replacement.sibling_tasks == original.sibling_tasks == ("other_part",)
        assert calls[2].task == "Goal B"
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_refused_attempt_triggers_the_same_replacement(
        self, controller: ExpansionController
    ) -> None:
        calls: list[Any] = []

        async def _run(**kwargs: Any) -> SubAgentResult:
            calls.append(kwargs["spec"])
            if len(calls) == 1:
                return _make_sub_agent_result("task_0", refused_tool_attempts=("web_search",))
            return _make_sub_agent_result("task_0")

        results = await _dispatch_hermetic(
            controller, _one_task_plan(), _run, granted=("web_search",), known=("web_search",)
        )

        assert len(calls) == 2
        assert calls[1].worker_type == WorkerType.RESEARCHER
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_retry_is_single_shot(self, controller: ExpansionController) -> None:
        """The replacement's OWN stated gap is never acted on — no chained retries."""
        calls: list[Any] = []

        async def _always_states_a_gap(**kwargs: Any) -> SubAgentResult:
            calls.append(kwargs["spec"])
            return _make_sub_agent_result("task_0", stated_tool_gap="run_python")

        # general states web_search first; the researcher replacement then states
        # run_python, which general declares — and nothing acts on it.
        first = True

        async def _run(**kwargs: Any) -> SubAgentResult:
            nonlocal first
            if first:
                first = False
                calls.append(kwargs["spec"])
                return _make_sub_agent_result("task_0", stated_tool_gap="web_search")
            return await _always_states_a_gap(**kwargs)

        results = await _dispatch_hermetic(
            controller,
            _one_task_plan(),
            _run,
            granted=("web_search", "run_python"),
            known=("web_search", "run_python"),
        )

        assert len(calls) == 2  # original + exactly one replacement, never a third
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_a_tool_no_type_declares_triggers_none_and_reaches_the_result(
        self, controller: ExpansionController
    ) -> None:
        calls: list[Any] = []

        async def _run(**kwargs: Any) -> SubAgentResult:
            calls.append(kwargs["spec"])
            return _make_sub_agent_result("task_0", stated_tool_gap="fetch_url")

        results = await _dispatch_hermetic(
            controller, _one_task_plan(), _run, granted=("fetch_url",), known=("fetch_url",)
        )

        assert len(calls) == 1
        assert results[0].stated_tool_gap == "fetch_url"

    @pytest.mark.asyncio
    async def test_a_same_type_gap_is_never_widened(self, controller: ExpansionController) -> None:
        """A researcher naming web_search, which only its own type declares, gets nothing."""
        calls: list[Any] = []

        async def _run(**kwargs: Any) -> SubAgentResult:
            calls.append(kwargs["spec"])
            return _make_sub_agent_result("task_0", stated_tool_gap="web_search")

        await _dispatch_hermetic(
            controller,
            _one_task_plan(WorkerType.RESEARCHER),
            _run,
            granted=("web_search",),
            known=("web_search",),
        )

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_unregistered_gap_name_is_ignored(self, controller: ExpansionController) -> None:
        """A hallucinated name that isn't even a registered tool spends no retry."""
        calls: list[Any] = []

        async def _run(**kwargs: Any) -> SubAgentResult:
            calls.append(kwargs["spec"])
            return _make_sub_agent_result("task_0", stated_tool_gap="web_search")

        # Declared by researcher and granted, but the registry does not know it.
        await _dispatch_hermetic(
            controller, _one_task_plan(), _run, granted=("web_search",), known=()
        )

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_gap_still_denied_for_the_target_type_yields_no_replacement(
        self, controller: ExpansionController
    ) -> None:
        calls: list[Any] = []

        async def _run(**kwargs: Any) -> SubAgentResult:
            calls.append(kwargs["spec"])
            return _make_sub_agent_result("task_0", stated_tool_gap="web_search")

        # web_search is registered but NOT in the sub-agent grant surface.
        results = await _dispatch_hermetic(
            controller, _one_task_plan(), _run, granted=("run_python",), known=("web_search",)
        )

        assert len(calls) == 1
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_redispatch_pairs_with_the_right_task_after_earlier_exception(
        self, controller: ExpansionController
    ) -> None:
        """An earlier task's raw dispatch exception must not shift the retry pairing."""
        plan = ExpansionPlan(
            strategy="HYBRID",
            tasks=[_task("task_boom", "raises"), _task("task_gap", "states a gap")],
        )
        calls: list[Any] = []

        async def _run(**kwargs: Any) -> SubAgentResult:
            spec = kwargs["spec"]
            calls.append(spec)
            if "raises" in spec.task:
                raise RuntimeError("boom")
            if "retry" in spec.task:
                return _make_sub_agent_result("task_gap")
            return _make_sub_agent_result("task_gap", stated_tool_gap="web_search")

        results = await _dispatch_hermetic(
            controller, plan, _run, granted=("web_search",), known=("web_search",)
        )

        # task_boom's exception drops it entirely; task_gap gets its own retry,
        # correctly paired (not confused with task_boom's slot).
        assert len(results) == 2
        assert all(r.spec_task == "task_gap" for r in results)
        assert calls[-1].task.startswith("states a gap")

    @pytest.mark.asyncio
    async def test_replacement_cost_is_not_dropped(self, controller: ExpansionController) -> None:
        """AC-6: the original attempt's cost survives even though it was incomplete."""
        calls: list[Any] = []

        async def _run(**kwargs: Any) -> SubAgentResult:
            calls.append(kwargs["spec"])
            if len(calls) == 1:
                return _make_sub_agent_result("task_0", stated_tool_gap="web_search", cost_usd=0.01)
            return _make_sub_agent_result("task_0", cost_usd=0.02)

        results = await _dispatch_hermetic(
            controller, _one_task_plan(), _run, granted=("web_search",), known=("web_search",)
        )

        assert sum(r.cost_usd for r in results) == pytest.approx(0.03)


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
    resolves to its own ``worker`` mode with thinking hard-disabled
    (ADR-0145 D1, config/model_roles.yaml); ``ModelRole.PRIMARY`` is the
    thinking-capable deployment. AC-1's live-container verification is
    evidence for the PR, not a unit test — this asserts the one thing a unit
    test can: which role the call site actually requests.
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

    @pytest.mark.asyncio
    async def test_planner_call_does_not_override_max_tokens(
        self, controller: ExpansionController
    ) -> None:
        """FRE-1413: the planner call must NOT pass a hardcoded max_tokens.

        The prior ``max_tokens=1024`` was sized for the retired thinking-disabled
        SUB_AGENT-bound call; PRIMARY is thinking-capable and the local llama.cpp
        completion budget includes thinking (ADR-0141 D5), so any hardcoded
        override here reproduces the truncation this ticket fixes. Omitting the
        kwarg entirely defers to the resolved client's own catalog ceiling — the
        same pattern the main orchestrator turn call already uses
        (executor.py's llm_client.respond() call has no max_tokens override).
        """
        client = AsyncMock()
        client.respond = AsyncMock(return_value={"content": _make_plan_json(3), "cost_usd": 0.0})
        mock_results = [_make_sub_agent_result(f"task_{i}") for i in range(3)]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            await controller.execute(
                query="Compare Redis, Memcached, and Hazelcast",
                strategy="HYBRID",
                llm_client=client,
                trace_id="test-trace-no-cap",
                messages=[],
            )

        call_kwargs = client.respond.call_args.kwargs
        assert "max_tokens" not in call_kwargs


class TestPlannerTruncationDistinguishedFromParseFailure:
    """FRE-1413 AC-3 — truncation must be distinguishable from a parse failure.

    A planner response cut off at the token ceiling must be distinguishable in
    telemetry from one that merely fails to parse. Before this fix both
    surfaced as an undifferentiated ``schema_validation_failed``, which is
    exactly what let the FRE-1390 cap-sizing defect run unnoticed.
    """

    @pytest.fixture
    def controller(self) -> ExpansionController:
        return ExpansionController()

    @pytest.mark.asyncio
    async def test_truncated_response_logs_output_truncated(
        self, controller: ExpansionController, caplog: pytest.LogCaptureFixture
    ) -> None:
        """AC-3's own seeded check: a tiny cap truncates mid-JSON.

        finish_reason reports "length", and the failure must name truncation
        specifically.
        """
        caplog.set_level("WARNING", logger="personal_agent.orchestrator.expansion_controller")
        client = AsyncMock()
        client.respond = AsyncMock(
            return_value={
                "content": '{"strategy": "HYBRID", "tasks": [{"name": "a", "go',
                "finish_reason": "length",
                "cost_usd": 0.0,
            }
        )

        mock_results = [_make_sub_agent_result("evaluate_redis")]
        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            result = await controller.execute(
                query="Compare Redis and Memcached",
                strategy="HYBRID",
                llm_client=client,
                trace_id="test-trace-truncated",
                messages=[],
            )

        assert result.plan is not None
        assert result.plan.is_fallback is True
        assert "output_truncated" in caplog.text
        assert "schema_validation_failed" not in caplog.text

    @pytest.mark.asyncio
    async def test_non_truncated_invalid_json_still_logs_schema_validation_failed(
        self, controller: ExpansionController, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Regression: a genuinely malformed response (finish_reason="stop") must keep its reason.

        The new distinction must not collapse both directions into
        "output_truncated".
        """
        caplog.set_level("WARNING", logger="personal_agent.orchestrator.expansion_controller")
        client = AsyncMock()
        client.respond = AsyncMock(
            return_value={
                "content": "I'll just answer directly...",
                "finish_reason": "stop",
                "cost_usd": 0.0,
            }
        )

        mock_results = [_make_sub_agent_result("evaluate_redis")]
        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            result = await controller.execute(
                query="Compare Redis and Memcached",
                strategy="HYBRID",
                llm_client=client,
                trace_id="test-trace-malformed",
                messages=[],
            )

        assert result.plan is not None
        assert result.plan.is_fallback is True
        assert "schema_validation_failed" in caplog.text
        assert "output_truncated" not in caplog.text

    @pytest.mark.asyncio
    async def test_length_finish_reason_with_valid_json_is_still_accepted(
        self, controller: ExpansionController
    ) -> None:
        """A schema-valid plan is used as-is even if finish_reason=="length".

        The prompt requires bare JSON with nothing after it, so a successful
        parse is strong evidence the content is complete. finish_reason is
        consulted only once validation has already failed (deliberate — see
        the plan doc).
        """
        client = AsyncMock()
        client.respond = AsyncMock(
            return_value={
                "content": _make_plan_json(3),
                "finish_reason": "length",
                "cost_usd": 0.0,
            }
        )
        mock_results = [_make_sub_agent_result(f"task_{i}") for i in range(3)]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            result = await controller.execute(
                query="Compare Redis, Memcached, and Hazelcast",
                strategy="HYBRID",
                llm_client=client,
                trace_id="test-trace-valid-length",
                messages=[],
            )

        assert result.plan is not None
        assert result.plan.is_fallback is False


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
        # One task per entity; no combine task since ADR-0150 D4.
        assert result.successful_count == 3


# ==========================================================================
# ADR-0149 — the planner is told the worker's budget, and the date travels
# ==========================================================================


class TestPlannerKnowsTheWorkerBudget:
    """ADR-0149 D2 / ADR-0150 D3 — the planner is told what each level buys.

    The planner wrote "research events in Mallorca for that week" with no
    knowledge that the worker had five rounds to answer it in. The budget per
    thoroughness level is rendered live from the settings.
    """

    def test_prompt_names_each_level_budget_from_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.config import get_settings
        from personal_agent.orchestrator.expansion_controller import (
            _build_planner_system_prompt,
        )

        monkeypatch.setattr(get_settings(), "sub_agent_max_tool_iterations", 4)

        prompt = _build_planner_system_prompt(["web_search"])

        assert (
            "quick (4 tool round(s)), standard (4 tool round(s)), thorough (4 tool round(s))"
            in prompt
        )
        assert "Scope every task so a worker can answer it inside its budget." in prompt

    def test_the_numbers_track_the_setting_rather_than_being_written_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Seeded negative for the live render: change the setting, change the prompt."""
        from personal_agent.config import get_settings
        from personal_agent.orchestrator.expansion_controller import (
            _build_planner_system_prompt,
        )

        monkeypatch.setattr(get_settings(), "sub_agent_max_tool_iterations", 9)
        monkeypatch.setattr(get_settings(), "sub_agent_rounds_by_thoroughness", {"quick": 2})

        prompt = _build_planner_system_prompt(["web_search"])
        assert "quick (2 tool round(s)), standard (9 tool round(s))" in prompt


class TestTurnTimestampReachesEverySpec:
    """ADR-0149 D3 move 2 — the date travels one stated path, end to end."""

    @pytest.mark.asyncio
    async def test_every_dispatched_spec_carries_the_turn_timestamp(self) -> None:
        from datetime import datetime, timezone

        from personal_agent.orchestrator.expansion_controller import ExpansionController

        turn_started_at = datetime(2026, 9, 10, 12, 17, tzinfo=timezone.utc)
        specs: list[Any] = []

        async def _capture(**kwargs: Any) -> Any:
            specs.append(kwargs["spec"])
            return _make_sub_agent_result(kwargs["spec"].task)

        client = AsyncMock()
        client.respond = AsyncMock(return_value="not-json")

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=_capture,
        ):
            await ExpansionController().execute(
                query="what is on this week",
                strategy="HYBRID",
                llm_client=client,
                trace_id="t",
                messages=[],
                turn_started_at=turn_started_at,
            )

        assert specs
        assert all(spec.turn_started_at == turn_started_at for spec in specs)

    @pytest.mark.asyncio
    async def test_a_caller_that_passes_none_leaves_the_specs_dateless(self) -> None:
        """Seeded negative, and the honest default for a caller not yet updated."""
        from personal_agent.orchestrator.expansion_controller import ExpansionController

        specs: list[Any] = []

        async def _capture(**kwargs: Any) -> Any:
            specs.append(kwargs["spec"])
            return _make_sub_agent_result(kwargs["spec"].task)

        client = AsyncMock()
        client.respond = AsyncMock(return_value="not-json")

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=_capture,
        ):
            await ExpansionController().execute(
                query="what is on this week",
                strategy="HYBRID",
                llm_client=client,
                trace_id="t",
                messages=[],
            )

        assert specs
        assert all(spec.turn_started_at is None for spec in specs)


# ==========================================================================
# ADR-0150 T2 (FRE-1493) — typed plans, no combine task, siblings
# ==========================================================================


def _typed_plan_json(*tasks: dict[str, Any], strategy: str = "HYBRID") -> str:
    return json.dumps({"strategy": strategy, "tasks": list(tasks)})


class TestPlanTypes:
    """FRE-1493 AC-3 (ADR-0150 AC-7, fixture half) — the planner picks registry types."""

    def test_unknown_type_is_rejected(self) -> None:
        raw = _typed_plan_json({"name": "a", "goal": "g", "type": "analyst"})
        assert _validate_plan_json(raw) is None

    def test_missing_type_is_rejected(self) -> None:
        assert _validate_plan_json(_typed_plan_json({"name": "a", "goal": "g"})) is None

    def test_one_unknown_type_rejects_the_whole_plan(self) -> None:
        raw = _typed_plan_json(
            {"name": "a", "goal": "g", "type": "researcher"},
            {"name": "b", "goal": "g", "type": "analyst"},
        )
        assert _validate_plan_json(raw) is None

    def test_unknown_thoroughness_is_rejected(self) -> None:
        raw = _typed_plan_json(
            {"name": "a", "goal": "g", "type": "researcher", "thoroughness": "exhaustive"}
        )
        assert _validate_plan_json(raw) is None

    def test_missing_thoroughness_takes_the_type_default(self) -> None:
        plan = _validate_plan_json(
            _typed_plan_json(
                {"name": "a", "goal": "g", "type": "researcher"},
                {"name": "b", "goal": "g", "type": "general"},
            )
        )
        assert plan is not None
        assert [t.thoroughness for t in plan.tasks] == ["standard", "quick"]

    @pytest.mark.asyncio
    async def test_mixed_plan_dispatches_both_with_their_tools(self) -> None:
        plan = _validate_plan_json(
            _typed_plan_json(
                {
                    "name": "find_events",
                    "goal": "g1",
                    "type": "researcher",
                    "thoroughness": "quick",
                },
                {"name": "sum_costs", "goal": "g2", "type": "general", "thoroughness": "thorough"},
            )
        )
        assert plan is not None
        specs: list[Any] = []

        async def _run(**kwargs: Any) -> SubAgentResult:
            specs.append(kwargs["spec"])
            return _make_sub_agent_result(kwargs["spec"].task)

        await _dispatch_hermetic(
            ExpansionController(),
            plan,
            _run,
            granted=("web_search", *_GENERAL_TOOLS),
        )

        assert [(s.worker_type, s.thoroughness) for s in specs] == [
            (WorkerType.RESEARCHER, "quick"),
            (WorkerType.GENERAL, "thorough"),
        ]
        assert specs[0].tools == ["web_search"]
        assert specs[1].tools == list(_GENERAL_TOOLS)

    @pytest.mark.asyncio
    async def test_an_unknown_type_never_reaches_dispatch(self) -> None:
        """Seeded end to end: the invalid plan falls back, and every dispatched spec is typed."""
        specs: list[Any] = []

        async def _run(**kwargs: Any) -> SubAgentResult:
            specs.append(kwargs["spec"])
            return _make_sub_agent_result(kwargs["spec"].task)

        client = AsyncMock()
        client.respond = AsyncMock(
            return_value={
                "content": _typed_plan_json({"name": "a", "goal": "g", "type": "analyst"}),
                "cost_usd": 0.0,
            }
        )
        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent", side_effect=_run
        ):
            result = await ExpansionController().execute(
                query="what is on in Palma this week",
                strategy="HYBRID",
                llm_client=client,
                trace_id="t",
                messages=[],
            )

        assert result.plan is not None
        assert result.plan.is_fallback is True
        assert specs
        assert all(isinstance(s.worker_type, WorkerType) for s in specs)


# The real 2026-09-11 evidence plan (session bbc0ddaa, read from
# agent-captains-captures-subagents-2026-09): two research tasks and one combine
# task. Goals shortened; names are illustrative — captures record goals only.
_RECORDED_EVENTS_GOAL = (
    "Identify all events, festivals, concerts, markets, and cultural happenings taking "
    "place in Mallorca between September 19 and September 26, 2026."
)
_RECORDED_PLACES_GOAL = (
    "Compile a curated list of the best towns, beaches, and archaeological sites to "
    "visit in Mallorca, within about one hour's drive of Playa de Palma."
)
_RECORDED_COMBINE_GOAL = (
    "Combine the events research and the towns/beaches/archaeology research into a "
    "single cohesive 7-day trip guide."
)


class TestNoCombineTask:
    """FRE-1493 AC-4 (ADR-0150 AC-8, structural) — no reserved slot, no combine rule."""

    def test_max_tasks_holds_no_reserved_slot(self) -> None:
        from personal_agent.orchestrator.expansion_controller import _MAX_TASKS

        assert _MAX_TASKS == {"HYBRID": 3, "DECOMPOSE": 5}

    def test_the_planner_prompt_holds_no_combine_instruction(self) -> None:
        from personal_agent.orchestrator.expansion_controller import (
            _build_planner_system_prompt,
        )

        prompt = _build_planner_system_prompt(["web_search"])
        assert "synthesis task" not in prompt
        assert "recommendation task" not in prompt
        assert "+ 1" not in prompt
        assert "- HYBRID: 1-3 tasks\n" in prompt
        assert "- DECOMPOSE: 2-5 tasks\n" in prompt
        assert "do not add a task that combines or synthesises other tasks' results" in prompt

    def test_three_research_tasks_plus_a_combine_task_yield_three(self) -> None:
        """The ticket's seeded form: the combine task, last, is cut by the cap."""
        raw = _typed_plan_json(
            {"name": "find_events", "goal": _RECORDED_EVENTS_GOAL, "type": "researcher"},
            {"name": "find_places", "goal": _RECORDED_PLACES_GOAL, "type": "researcher"},
            {"name": "find_food", "goal": "Find restaurants near Palma.", "type": "researcher"},
            {"name": "combine_guide", "goal": _RECORDED_COMBINE_GOAL, "type": "general"},
        )
        plan = _validate_plan_json(raw, "HYBRID")
        assert plan is not None
        assert [t.name for t in plan.tasks] == ["find_events", "find_places", "find_food"]

    def test_the_recorded_plan_shows_the_cap_cannot_see_a_combine_task(self) -> None:
        """The real plan was 2+1, which fits the cap: validation keeps the combine task.

        Recorded so the gate sees it: removal of a combine task inside the cap rests
        on the planner prompt's rule alone, because the validator reads shape, not
        meaning.
        """
        raw = _typed_plan_json(
            {"name": "find_events", "goal": _RECORDED_EVENTS_GOAL, "type": "researcher"},
            {"name": "find_places", "goal": _RECORDED_PLACES_GOAL, "type": "researcher"},
            {"name": "combine_guide", "goal": _RECORDED_COMBINE_GOAL, "type": "general"},
        )
        plan = _validate_plan_json(raw, "HYBRID")
        assert plan is not None
        assert len(plan.tasks) == 3


class TestSiblingLine:
    """FRE-1493 AC-5 (ADR-0150 AC-9) — each worker is told what its siblings own."""

    @staticmethod
    async def _specs(plan: ExpansionPlan) -> list[Any]:
        specs: list[Any] = []

        async def _run(**kwargs: Any) -> SubAgentResult:
            specs.append(kwargs["spec"])
            return _make_sub_agent_result(kwargs["spec"].task)

        await _dispatch_hermetic(ExpansionController(), plan, _run, granted=("web_search",))
        return specs

    @pytest.mark.asyncio
    async def test_two_task_fanout_each_names_the_other(self) -> None:
        from personal_agent.orchestrator.sub_agent import _build_task_message

        plan = ExpansionPlan(
            strategy="HYBRID",
            tasks=[
                _task("find_events", "g1", WorkerType.RESEARCHER),
                _task("find_places", "g2", WorkerType.RESEARCHER),
            ],
        )
        specs = await self._specs(plan)

        assert [s.sibling_tasks for s in specs] == [("find_places",), ("find_events",)]
        first = _build_task_message(specs[0], "t", None)
        second = _build_task_message(specs[1], "t", None)
        assert "Other workers in this turn own: find_places. Stay inside your task." in first
        assert "Other workers in this turn own: find_events. Stay inside your task." in second

    @pytest.mark.asyncio
    async def test_a_one_task_fanout_reads_none(self) -> None:
        from personal_agent.orchestrator.sub_agent import _build_task_message

        specs = await self._specs(_one_task_plan(WorkerType.RESEARCHER))

        assert specs[0].sibling_tasks == ()
        assert "Other workers in this turn own: none." in _build_task_message(specs[0], "t", None)
