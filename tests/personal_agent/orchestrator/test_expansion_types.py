"""Tests for expansion controller types."""

from personal_agent.orchestrator.expansion_types import (
    ExpansionPhase,
    ExpansionPlan,
    PhaseResult,
    PlanTask,
    SubAgentMode,
)
from personal_agent.orchestrator.worker_types import WorkerType


class TestSubAgentMode:
    def test_modes_defined(self) -> None:
        assert SubAgentMode.PARALLEL_INFERENCE.value == "parallel_inference"


class TestExpansionPhase:
    def test_phases_defined(self) -> None:
        assert ExpansionPhase.PLANNING.value == "planning"
        assert ExpansionPhase.DISPATCH.value == "dispatch"
        assert ExpansionPhase.SYNTHESIS.value == "synthesis"


class TestPlanTask:
    def test_construction(self) -> None:
        task = PlanTask(
            name="compare_performance",
            goal="Compare Redis and Memcached on raw throughput",
            type=WorkerType.RESEARCHER,
            thoroughness="thorough",
            constraints=["Focus on 10k rps scenario"],
        )
        assert task.name == "compare_performance"
        assert task.type == WorkerType.RESEARCHER
        assert task.thoroughness == "thorough"
        assert len(task.constraints) == 1

    def test_frozen(self) -> None:
        task = PlanTask(name="t1", goal="g1", type=WorkerType.GENERAL, thoroughness="quick")
        try:
            task.name = "t2"  # type: ignore[misc]
            assert False, "Should be frozen"
        except (AttributeError, TypeError):
            pass

    def test_defaults(self) -> None:
        task = PlanTask(name="t1", goal="g1", type=WorkerType.GENERAL, thoroughness="quick")
        assert task.constraints == []
        assert task.mode == SubAgentMode.PARALLEL_INFERENCE

    def test_the_planner_no_longer_picks_tools_or_output_shape(self) -> None:
        """ADR-0150 D2: the type decides both; the fields are gone, not defaulted."""
        fields = PlanTask.__dataclass_fields__
        assert "tools" not in fields
        assert "expected_output" not in fields


class TestExpansionPlan:
    def test_construction(self) -> None:
        plan = ExpansionPlan(
            strategy="HYBRID",
            tasks=[
                PlanTask(name="t1", goal="g1", type=WorkerType.RESEARCHER, thoroughness="standard"),
                PlanTask(name="t2", goal="g2", type=WorkerType.GENERAL, thoroughness="quick"),
            ],
        )
        assert plan.strategy == "HYBRID"
        assert len(plan.tasks) == 2

    def test_is_fallback_default(self) -> None:
        plan = ExpansionPlan(strategy="HYBRID", tasks=[])
        assert plan.is_fallback is False


class TestPhaseResult:
    def test_success(self) -> None:
        result = PhaseResult(
            phase=ExpansionPhase.PLANNING,
            duration_ms=4500,
            success=True,
        )
        assert result.success
        assert result.error is None

    def test_failure(self) -> None:
        result = PhaseResult(
            phase=ExpansionPhase.DISPATCH,
            duration_ms=90000,
            success=False,
            error="Global timeout exceeded",
        )
        assert not result.success
        assert "timeout" in result.error.lower()
