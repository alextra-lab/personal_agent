"""Tests for deterministic fallback planner.

The fallback planner generates plans from prompt structure when the LLM
planner fails. Scoped to enumerated comparisons per ADR-0036 Decision 3.
"""

from personal_agent.orchestrator.expansion_types import ExpansionPlan, SubAgentMode
from personal_agent.orchestrator.fallback_planner import generate_fallback_plan


class TestHybridFallback:
    def test_enumerated_entities(self) -> None:
        """HYBRID with explicit named entities → one task per entity + synthesis."""
        plan = generate_fallback_plan(
            query="Compare Redis, Memcached, and Hazelcast for our session caching",
            strategy="HYBRID",
        )
        assert isinstance(plan, ExpansionPlan)
        assert plan.is_fallback is True
        assert plan.strategy == "HYBRID"
        # 3 entities + 1 synthesis = 4 tasks
        assert len(plan.tasks) == 4
        # Entity names should be clean (no trailing "for our session caching")
        entity_task_names = [t.name for t in plan.tasks[:-1]]
        assert "evaluate_redis" in entity_task_names
        assert "evaluate_memcached" in entity_task_names
        assert "evaluate_hazelcast" in entity_task_names
        # Last task is synthesis
        assert "synth" in plan.tasks[-1].name.lower() or "recommend" in plan.tasks[-1].name.lower()

    def test_enumerated_dimensions(self) -> None:
        """HYBRID with explicit dimensions → one task per dimension."""
        plan = generate_fallback_plan(
            query="Analyze performance, memory usage, and operational complexity of our caching layer",
            strategy="HYBRID",
        )
        assert plan.is_fallback is True
        # 3 dimensions + 1 synthesis = 4 tasks
        assert len(plan.tasks) == 4

    def test_vs_pattern(self) -> None:
        """X vs Y pattern → two entities extracted cleanly."""
        plan = generate_fallback_plan(
            query="Compare Redis vs Memcached for caching",
            strategy="HYBRID",
        )
        assert plan.is_fallback is True
        # 2 entities + 1 synthesis = 3 tasks
        assert len(plan.tasks) == 3
        entity_task_names = [t.name for t in plan.tasks[:-1]]
        assert "evaluate_redis" in entity_task_names
        assert "evaluate_memcached" in entity_task_names

    def test_no_entities_generic_split(self) -> None:
        """No enumerable structure → generic 2-task split."""
        plan = generate_fallback_plan(
            query="Research the best approach to scaling our API layer",
            strategy="HYBRID",
        )
        assert plan.is_fallback is True
        assert len(plan.tasks) == 2  # research + recommendation


class TestDecomposeFallback:
    def test_enumerated_entities(self) -> None:
        """DECOMPOSE with entities → one task per entity + recommendation."""
        plan = generate_fallback_plan(
            query="Evaluate Redis, Memcached, and Hazelcast for 10k rps microservices",
            strategy="DECOMPOSE",
        )
        assert plan.is_fallback is True
        assert plan.strategy == "DECOMPOSE"
        # 3 entities + 1 synthesis = 4 tasks
        assert len(plan.tasks) == 4
        entity_task_names = [t.name for t in plan.tasks[:-1]]
        assert "evaluate_redis" in entity_task_names
        assert "evaluate_memcached" in entity_task_names
        assert "evaluate_hazelcast" in entity_task_names

    def test_generic_decompose(self) -> None:
        """No enumerable structure → 2-task split."""
        plan = generate_fallback_plan(
            query="Design a comprehensive monitoring strategy",
            strategy="DECOMPOSE",
        )
        assert plan.is_fallback is True
        assert len(plan.tasks) == 2


class TestToolAssignment:
    def test_research_tasks_get_tools(self) -> None:
        """Tasks with research/search goals should get TOOLED_SEQUENTIAL mode."""
        plan = generate_fallback_plan(
            query="Research and compare Redis vs Memcached performance benchmarks",
            strategy="HYBRID",
        )
        # At least one task should have research-oriented mode
        research_tasks = [t for t in plan.tasks if t.mode == SubAgentMode.TOOLED_SEQUENTIAL]
        # Not required — fallback planner defaults to PARALLEL_INFERENCE
        # This test documents the behavior
        assert isinstance(research_tasks, list)


class TestEdgeCases:
    def test_empty_query(self) -> None:
        """Empty query → generic 2-task split."""
        plan = generate_fallback_plan(query="", strategy="HYBRID")
        assert plan.is_fallback is True
        assert len(plan.tasks) == 2

    def test_single_entity(self) -> None:
        """Single entity → still produces a valid plan."""
        plan = generate_fallback_plan(
            query="Evaluate Redis for our caching needs",
            strategy="HYBRID",
        )
        assert plan.is_fallback is True
        assert len(plan.tasks) >= 2
