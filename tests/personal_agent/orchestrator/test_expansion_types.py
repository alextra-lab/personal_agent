"""Tests for expansion controller types."""

from personal_agent.orchestrator.expansion_types import (
    ExpansionPhase,
    ExpansionPlan,
    PhaseResult,
    PlanTask,
    SubAgentMode,
)


class TestSubAgentMode:
    def test_modes_defined(self) -> None:
        assert SubAgentMode.PARALLEL_INFERENCE.value == "parallel_inference"
        assert SubAgentMode.TOOLED_SEQUENTIAL.value == "tooled_sequential"


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
            constraints=["Focus on 10k rps scenario"],
            expected_output="Performance comparison with recommendation signal",
        )
        assert task.name == "compare_performance"
        assert len(task.constraints) == 1

    def test_frozen(self) -> None:
        task = PlanTask(
            name="t1",
            goal="g1",
            constraints=[],
            expected_output="text",
        )
        try:
            task.name = "t2"  # type: ignore[misc]
            assert False, "Should be frozen"
        except (AttributeError, TypeError):
            pass

    def test_defaults(self) -> None:
        task = PlanTask(
            name="t1",
            goal="g1",
        )
        assert task.constraints == []
        assert task.expected_output == "text"
        assert task.mode == SubAgentMode.PARALLEL_INFERENCE
        assert task.tools == []


class TestExpansionPlan:
    def test_construction(self) -> None:
        plan = ExpansionPlan(
            strategy="HYBRID",
            tasks=[
                PlanTask(name="t1", goal="g1"),
                PlanTask(name="t2", goal="g2"),
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
