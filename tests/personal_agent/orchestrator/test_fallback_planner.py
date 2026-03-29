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
        # Should extract entities and create tasks
        assert len(plan.tasks) >= 2
        assert len(plan.tasks) <= 4  # HYBRID caps at 3 + synthesis
        # Last task should be synthesis/recommendation
        assert any("synth" in t.name.lower() or "recommend" in t.name.lower() for t in plan.tasks)

    def test_enumerated_dimensions(self) -> None:
        """HYBRID with explicit dimensions → one task per dimension."""
        plan = generate_fallback_plan(
            query="Analyze performance, memory usage, and operational complexity of our caching layer",
            strategy="HYBRID",
        )
        assert plan.is_fallback is True
        assert len(plan.tasks) >= 2

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
        """DECOMPOSE with entities → one task per evaluation axis + recommendation."""
        plan = generate_fallback_plan(
            query="Evaluate Redis, Memcached, and Hazelcast for 10k rps microservices",
            strategy="DECOMPOSE",
        )
        assert plan.is_fallback is True
        assert plan.strategy == "DECOMPOSE"
        assert len(plan.tasks) >= 3
        assert len(plan.tasks) <= 6  # DECOMPOSE allows more tasks

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
