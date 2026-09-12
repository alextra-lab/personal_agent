"""Tests for deterministic fallback planner.

The fallback planner generates plans from prompt structure when the LLM
planner fails. Scoped to enumerated comparisons per ADR-0036 Decision 3.
"""

from personal_agent.orchestrator.expansion_types import ExpansionPlan, SubAgentMode
from personal_agent.orchestrator.fallback_planner import generate_fallback_plan
from personal_agent.orchestrator.worker_types import WorkerType

_WEB = ("web_search", "run_python")


class TestHybridFallback:
    def test_enumerated_entities(self) -> None:
        """HYBRID with explicit named entities → one task per entity, no combine task."""
        plan = generate_fallback_plan(
            query="Compare Redis, Memcached, and Hazelcast for our session caching",
            strategy="HYBRID",
        )
        assert isinstance(plan, ExpansionPlan)
        assert plan.is_fallback is True
        assert plan.strategy == "HYBRID"
        # Entity names should be clean (no trailing "for our session caching")
        assert [t.name for t in plan.tasks] == [
            "evaluate_redis",
            "evaluate_memcached",
            "evaluate_hazelcast",
        ]

    def test_enumerated_dimensions(self) -> None:
        """HYBRID with explicit dimensions → one task per dimension."""
        plan = generate_fallback_plan(
            query="Analyze performance, memory usage, and operational complexity of our caching layer",
            strategy="HYBRID",
        )
        assert plan.is_fallback is True
        assert len(plan.tasks) == 3

    def test_vs_pattern(self) -> None:
        """X vs Y pattern → two entities extracted cleanly."""
        plan = generate_fallback_plan(
            query="Compare Redis vs Memcached for caching",
            strategy="HYBRID",
        )
        assert plan.is_fallback is True
        assert [t.name for t in plan.tasks] == ["evaluate_redis", "evaluate_memcached"]

    def test_no_entities_generic_single_task(self) -> None:
        """No enumerable structure → one research task."""
        plan = generate_fallback_plan(
            query="Research the best approach to scaling our API layer",
            strategy="HYBRID",
        )
        assert plan.is_fallback is True
        assert [t.name for t in plan.tasks] == ["research_analysis"]


class TestDecomposeFallback:
    def test_enumerated_entities(self) -> None:
        """DECOMPOSE with entities → one task per entity."""
        plan = generate_fallback_plan(
            query="Evaluate Redis, Memcached, and Hazelcast for 10k rps microservices",
            strategy="DECOMPOSE",
        )
        assert plan.is_fallback is True
        assert plan.strategy == "DECOMPOSE"
        assert [t.name for t in plan.tasks] == [
            "evaluate_redis",
            "evaluate_memcached",
            "evaluate_hazelcast",
        ]

    def test_generic_decompose(self) -> None:
        plan = generate_fallback_plan(
            query="Design a comprehensive monitoring strategy",
            strategy="DECOMPOSE",
        )
        assert plan.is_fallback is True
        assert len(plan.tasks) == 1


class TestExpansionBudgetCap:
    """FRE-1382 AC-2: the turn's load-shed budget binds the fallback planner too."""

    def test_budget_below_strategy_cap_binds(self) -> None:
        plan = generate_fallback_plan(
            query="Compare Redis, Memcached, and Hazelcast for our session caching",
            strategy="HYBRID",
            max_tasks=1,
        )
        assert len(plan.tasks) == 1

    def test_budget_above_strategy_cap_still_caps_at_strategy(self) -> None:
        plan = generate_fallback_plan(
            query="Compare Redis, Memcached, and Hazelcast for our session caching",
            strategy="HYBRID",
            max_tasks=10,
        )
        assert len(plan.tasks) == 3

    def test_negative_budget_does_not_widen_the_cap(self) -> None:
        """A negative budget must clamp to zero, not become a Python negative slice
        index (``tasks[:-1]`` would otherwise keep 2 of 3 tasks).
        """
        plan = generate_fallback_plan(
            query="Compare Redis, Memcached, and Hazelcast for our session caching",
            strategy="HYBRID",
            max_tasks=-1,
        )
        assert plan.tasks == []

    def test_budget_binds_the_generic_no_entities_path_too(self) -> None:
        """FRE-1382: the single-task generic branch used to ignore max_tasks
        entirely — it must be bound by the same cap as the entity branch.
        """
        plan = generate_fallback_plan(
            query="Research the best approach to scaling our API layer",
            strategy="HYBRID",
            max_tasks=0,
        )
        assert plan.tasks == []


class TestNoCombineTask:
    """FRE-1493 AC-4 / ADR-0150 D4: the fallback planner holds no combine task either."""

    def test_no_task_synthesises_the_others(self) -> None:
        for query in (
            "Compare Redis, Memcached, and Hazelcast for our session caching",
            "Compare Redis vs Memcached for caching",
            "Research the best approach to scaling our API layer",
        ):
            plan = generate_fallback_plan(query=query, strategy="HYBRID")
            for task in plan.tasks:
                assert "synth" not in task.name and "recommend" not in task.name


class TestTypeAssignment:
    """FRE-1493 AC-3 (ADR-0150 AC-7 fixture half): the fallback's type rule."""

    def test_researcher_when_web_search_is_grantable(self) -> None:
        plan = generate_fallback_plan(
            query="Compare Redis vs Memcached for caching",
            strategy="HYBRID",
            sub_agent_tool_surface=_WEB,
        )
        assert {t.type for t in plan.tasks} == {WorkerType.RESEARCHER}
        assert {t.thoroughness for t in plan.tasks} == {"standard"}

    def test_general_when_web_search_is_not_grantable(self) -> None:
        plan = generate_fallback_plan(
            query="Compare Redis vs Memcached for caching",
            strategy="HYBRID",
            sub_agent_tool_surface=("run_python",),
        )
        assert {t.type for t in plan.tasks} == {WorkerType.GENERAL}
        assert {t.thoroughness for t in plan.tasks} == {"quick"}

    def test_an_empty_surface_fails_closed_to_general(self) -> None:
        plan = generate_fallback_plan(query="Research something", strategy="HYBRID")
        assert {t.type for t in plan.tasks} == {WorkerType.GENERAL}

    def test_research_tasks_default_to_parallel_inference(self) -> None:
        """The fallback planner never assigns a tooled mode — always PARALLEL_INFERENCE."""
        plan = generate_fallback_plan(
            query="Research and compare Redis vs Memcached performance benchmarks",
            strategy="HYBRID",
            sub_agent_tool_surface=_WEB,
        )
        assert all(t.mode == SubAgentMode.PARALLEL_INFERENCE for t in plan.tasks)


class TestEdgeCases:
    def test_empty_query(self) -> None:
        """Empty query → one generic task."""
        plan = generate_fallback_plan(query="", strategy="HYBRID")
        assert plan.is_fallback is True
        assert len(plan.tasks) == 1
        assert plan.tasks[0].goal == "Research the topic"

    def test_single_entity(self) -> None:
        """Single entity → still produces a valid plan."""
        plan = generate_fallback_plan(
            query="Evaluate Redis for our caching needs",
            strategy="HYBRID",
        )
        assert plan.is_fallback is True
        assert len(plan.tasks) >= 1
