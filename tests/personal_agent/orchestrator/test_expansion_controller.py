"""Tests for the expansion controller.

Tests the enforced expansion path: planner → validate → dispatch → synthesize.
Uses mocked LLM client and sub-agent runner.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

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
    """FRE-884 — ADR-0086's tooled discovery-slice parsing is retired.

    A raw plan carrying a ``mode``/``tools`` field (the old discovery-slice
    shape) is always ignored: every task parses as plain PARALLEL_INFERENCE.
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

    def test_mode_and_tools_fields_are_ignored(self) -> None:
        from personal_agent.orchestrator.expansion_types import SubAgentMode

        plan = _validate_plan_json(self._tooled_plan(["bash", "read"]))
        assert plan is not None
        assert plan.tasks[0].mode == SubAgentMode.PARALLEL_INFERENCE
        assert plan.tasks[0].tools == []

    def test_planner_prompt_never_mentions_tooled_sequential(self) -> None:
        from personal_agent.orchestrator.expansion_controller import _PLANNER_SYSTEM_PROMPT

        assert "tooled_sequential" not in _PLANNER_SYSTEM_PROMPT


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
        mock_settings.worker_global_timeout_seconds = 90.0
        mock_settings.sub_agent_max_tokens = 4096

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
    fallback) so the assertion is purely about dispatch concurrency + phase pairing.
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

        # Staggered completion so children finish in a different order than they
        # start — proving independent child lifecycles, not lockstep.
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


class TestFanOutRespectsCeiling:
    """FRE-1374 Defect 2 — dispatch is bounded by the sub-agent deployment's real
    concurrency ceiling, not fanned out unconditionally.
    """

    @pytest.fixture
    def controller(self) -> ExpansionController:
        return ExpansionController()

    @staticmethod
    def _tracking_run_sub_agent() -> tuple[Any, list[int]]:
        """A run_sub_agent stand-in that records observed in-flight concurrency."""
        state = {"concurrent": 0}
        observed: list[int] = []

        async def _run(**kwargs: Any) -> SubAgentResult:
            state["concurrent"] += 1
            observed.append(state["concurrent"])
            await asyncio.sleep(0.02)
            state["concurrent"] -= 1
            return _make_sub_agent_result(kwargs["spec"].task)

        return _run, observed

    @staticmethod
    def _stub_controller(ceiling: int) -> MagicMock:
        stub = MagicMock()
        stub.effective_ceiling.return_value = ceiling
        return stub

    @pytest.mark.asyncio
    async def test_low_ceiling_bounds_concurrency_and_all_tasks_still_complete(
        self, controller: ExpansionController
    ) -> None:
        """AC-3 — ceiling seeded low, plan calls for more: every sub-agent still
        completes, none time out, and observed concurrency never exceeds the ceiling.
        """
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = _validate_plan_json(_make_plan_json(4))
        assert plan is not None
        run_stub, observed = self._tracking_run_sub_agent()

        with (
            patch(
                "personal_agent.orchestrator.expansion_controller.run_sub_agent",
                side_effect=run_stub,
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.get_inference_concurrency_controller",
                return_value=self._stub_controller(2),
            ),
        ):
            results = await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="test-trace-ceiling",
                messages=[],
                result=ExpansionResult(),
            )

        assert len(results) == 4
        assert all(r.success for r in results)
        assert max(observed) <= 2

    @pytest.mark.asyncio
    async def test_ceiling_change_changes_observed_concurrency(
        self, controller: ExpansionController
    ) -> None:
        """AC-4 — the same plan against ceilings of 2 and 4 dispatches differently."""
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = _validate_plan_json(_make_plan_json(4))
        assert plan is not None

        max_observed: dict[int, int] = {}
        for ceiling in (2, 4):
            run_stub, observed = self._tracking_run_sub_agent()
            with (
                patch(
                    "personal_agent.orchestrator.expansion_controller.run_sub_agent",
                    side_effect=run_stub,
                ),
                patch(
                    "personal_agent.orchestrator.expansion_controller.get_inference_concurrency_controller",
                    return_value=self._stub_controller(ceiling),
                ),
            ):
                await controller._run_dispatch(
                    plan=plan,
                    llm_client=AsyncMock(),
                    trace_id=f"test-trace-ceiling-{ceiling}",
                    messages=[],
                    result=ExpansionResult(),
                )
            max_observed[ceiling] = max(observed)

        assert max_observed[2] <= 2
        assert max_observed[4] <= 4
        assert max_observed[2] != max_observed[4]

    @pytest.mark.asyncio
    async def test_unresolvable_role_does_not_constrain_dispatch(
        self, controller: ExpansionController
    ) -> None:
        """An llm_client with no resolvable model_key (e.g. a bare test double) must
        not silently degrade to some ambient default ceiling — dispatch stays
        unconstrained, matching today's behavior for every existing caller.
        """
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = _validate_plan_json(_make_plan_json(3))
        assert plan is not None
        run_stub, observed = self._tracking_run_sub_agent()

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=run_stub,
        ):
            results = await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="test-trace-unresolvable",
                messages=[],
                result=ExpansionResult(),
            )

        assert len(results) == 3
        # Unconstrained: all 3 could run concurrently (no artificial ceiling applied).
        assert max(observed) == 3


class TestAdmittedWorkerIndependentOfGlobalBound:
    """FRE-1374 AC-1 (owner direction, verbatim: "The timeout need be applied to
    each individual subagent session. not globally.") — worker_global_timeout_seconds
    bounds ONLY the wait for a concurrency-ceiling slot. A worker already admitted
    runs on its own clock, full stop — it is never truncated for time it spent
    waiting, or for time another worker spent working.
    """

    @pytest.fixture
    def controller(self) -> ExpansionController:
        return ExpansionController()

    @staticmethod
    def _stub_controller(ceiling: int) -> MagicMock:
        stub = MagicMock()
        stub.effective_ceiling.return_value = ceiling
        return stub

    @pytest.mark.asyncio
    async def test_admitted_worker_completes_past_the_old_global_deadline(
        self, controller: ExpansionController
    ) -> None:
        """Once admitted (ceiling covers both, so admission is immediate for
        both — no queuing race to make this flaky under real scheduler load),
        a worker's own execution is never re-checked against
        worker_global_timeout_seconds. An asyncio.Event (not a sleep-vs-sleep
        race) holds both workers open well past the nominal window and proves
        the dispatch is still running, not cancelled.
        """
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = _validate_plan_json(_make_plan_json(2))
        assert plan is not None
        completed: list[str] = []
        release_workers = asyncio.Event()

        async def _run(**kwargs: Any) -> SubAgentResult:
            await release_workers.wait()
            completed.append(kwargs["spec"].task)
            return _make_sub_agent_result(kwargs["spec"].task)

        mock_settings = MagicMock()
        # Small enough that the later assertion (held open 6x longer) is
        # unambiguous, but large enough to absorb ordinary admission overhead —
        # ceiling=2 means admission has zero contention, so it should never take
        # anywhere near this long on its own.
        mock_settings.worker_global_timeout_seconds = 0.05
        mock_settings.sub_agent_max_tokens = 4096
        mock_settings.worker_timeout_seconds = 60.0
        mock_settings.worker_hard_deadline_seconds = 85.0

        with (
            patch(
                "personal_agent.orchestrator.expansion_controller.run_sub_agent",
                side_effect=_run,
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.get_inference_concurrency_controller",
                return_value=self._stub_controller(2),  # covers both — admission is instant
            ),
        ):
            dispatch_task = asyncio.create_task(
                controller._run_dispatch(
                    plan=plan,
                    llm_client=AsyncMock(),
                    trace_id="test-trace-admitted",
                    messages=[],
                    result=ExpansionResult(),
                )
            )
            # Generous margin (300x the nominal window) against scheduler jitter —
            # both workers are blocked on release_workers well past the window by
            # now, so if the global bound still reached an admitted worker, the
            # dispatch would already be finished (with failures) at this point.
            await asyncio.sleep(0.3)
            assert not dispatch_task.done(), (
                "dispatch finished before being released — the global bound "
                "reached an admitted worker"
            )
            release_workers.set()
            results = await dispatch_task

        assert len(completed) == 2
        assert len(results) == 2
        assert all(r.success for r in results)

    @pytest.mark.asyncio
    async def test_never_admitted_worker_is_reported_not_run(
        self, controller: ExpansionController
    ) -> None:
        """AC-3 — a worker that never gets a ceiling slot within the fan-out window
        is reported as a failed/not-run result, never silently dropped, and the
        expansion result is marked degraded with a specific reason.
        """
        from personal_agent.orchestrator.expansion_controller import ExpansionResult

        plan = _validate_plan_json(_make_plan_json(2))
        assert plan is not None

        async def _run(**kwargs: Any) -> SubAgentResult:
            await asyncio.sleep(0.2)
            return _make_sub_agent_result(kwargs["spec"].task)

        mock_settings = MagicMock()
        # Only enough window for ONE worker to ever get admitted.
        mock_settings.worker_global_timeout_seconds = 0.05
        mock_settings.sub_agent_max_tokens = 4096
        mock_settings.worker_timeout_seconds = 60.0
        mock_settings.worker_hard_deadline_seconds = 85.0

        expansion_result = ExpansionResult()
        with (
            patch(
                "personal_agent.orchestrator.expansion_controller.run_sub_agent",
                side_effect=_run,
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.get_inference_concurrency_controller",
                return_value=self._stub_controller(1),
            ),
        ):
            results = await controller._run_dispatch(
                plan=plan,
                llm_client=AsyncMock(),
                trace_id="test-trace-not-admitted",
                messages=[],
                result=expansion_result,
            )

        assert len(results) == 2
        succeeded = [r for r in results if r.success]
        not_run = [r for r in results if not r.success]
        assert len(succeeded) == 1
        assert len(not_run) == 1
        assert not_run[0].error is not None
        assert "Not dispatched" in not_run[0].error
        assert expansion_result.degraded is True
        assert "not admitted" in (expansion_result.degradation_reason or "")
