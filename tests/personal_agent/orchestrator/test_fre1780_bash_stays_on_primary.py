"""FRE-1780 — work that needs bash stays with the primary, not a helper.

On 2026-11-20 the planner sent a deep health check to two helpers. The ``service-health`` skill
needs ``bash``, which only the primary holds, so the probe never ran and the answer called
Postgres and Neo4j "not verifiable".

- AC-1: the planner prompt states the rule, and names the skills no worker type can run.
- AC-2: a plan whose task names ``bash`` or one of those skills runs the turn on the primary,
  with no helper. A plan of helper work still spawns helpers.
"""

from __future__ import annotations

import json

import pytest

from personal_agent.orchestrator.expansion_controller import (
    _build_planner_system_prompt,
    primary_only_skill_names,
    tasks_needing_the_primary,
)
from personal_agent.orchestrator.expansion_types import ExpansionPlan, PlanTask
from personal_agent.orchestrator.skills import get_all_skills
from personal_agent.orchestrator.types import TaskState
from personal_agent.orchestrator.worker_types import WORKER_TYPES, WorkerType
from tests.personal_agent.orchestrator.test_fre1515_decline_and_failure import (
    _ASKED,
    _client,
    _gateway,
    _Harness,
    _step_init,
)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    return _Harness(monkeypatch)


def _plan_json(*goals: str) -> str:
    return json.dumps(
        {
            "strategy": "HYBRID",
            "tasks": [
                {"name": f"task_{i}", "goal": goal, "type": "general"}
                for i, goal in enumerate(goals)
            ],
        }
    )


def _plan(*goals: str, constraints: tuple[str, ...] = ()) -> ExpansionPlan:
    return ExpansionPlan(
        strategy="HYBRID",
        tasks=[
            PlanTask(
                name=f"task_{i}",
                goal=goal,
                type=WorkerType.GENERAL,
                thoroughness="quick",
                constraints=list(constraints),
            )
            for i, goal in enumerate(goals)
        ],
    )


class TestAC1ThePromptStatesTheRule:
    @pytest.mark.parametrize("admit_single", [False, True])
    def test_the_rule_is_in_the_rules_list(self, admit_single: bool) -> None:
        prompt = _build_planner_system_prompt(["search_logs"], admit_single=admit_single)
        rules = prompt[prompt.index("Rules:\n") :]
        assert "No worker holds bash" in rules
        assert "service-health" in rules
        assert "do not plan a task for it" in rules

    def test_the_rule_names_every_skill_no_worker_type_can_run(self) -> None:
        prompt = _build_planner_system_prompt(["search_logs"])
        for name in primary_only_skill_names():
            assert name in prompt

    def test_the_rule_comes_after_the_identity_line(self) -> None:
        """FRE-1779 edits the prompt's first line; this rule stays inside the Rules list."""
        prompt = _build_planner_system_prompt([])
        assert prompt.index("No worker holds bash") > prompt.index("Rules:\n")


class TestThePrimaryOnlySkills:
    def test_no_worker_type_holds_bash(self) -> None:
        assert all("bash" not in spec.tools for spec in WORKER_TYPES.values())

    def test_service_health_is_primary_only(self) -> None:
        assert "service-health" in primary_only_skill_names()

    def test_a_skill_a_worker_type_can_run_is_not_listed(self) -> None:
        runnable = {
            name
            for name, doc in get_all_skills().items()
            if any(set(doc.requires_tools) <= set(s.tools) for s in WORKER_TYPES.values())
        }
        assert runnable, "the check needs at least one skill a worker can run"
        assert not runnable & set(primary_only_skill_names())

    def test_the_names_are_sorted(self) -> None:
        names = primary_only_skill_names()
        assert list(names) == sorted(names)


class TestTheCheck:
    @pytest.mark.parametrize(
        "goal",
        [
            "Run the service-health probe for Postgres and Neo4j",
            "Follow the service_health skill",
            "Use Bash to run the probe script",
            "Read the graph-inspection skill and count nodes",
        ],
    )
    def test_a_task_that_names_primary_work_is_flagged(self, goal: str) -> None:
        assert tasks_needing_the_primary(_plan(goal)) == ("task_0",)

    def test_a_constraint_counts_too(self) -> None:
        plan = _plan("Check Postgres", constraints=("use the service-health skill",))
        assert tasks_needing_the_primary(plan) == ("task_0",)

    @pytest.mark.parametrize(
        "goal",
        [
            "Search the logs for errors in the last hour",
            "Explain the bashful character in Snow White",
            "Summarise the system health monitor document",
        ],
    )
    def test_helper_work_is_not_flagged(self, goal: str) -> None:
        assert tasks_needing_the_primary(_plan(goal)) == ()


class TestAC2TheTurnStaysOnThePrimary:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("may_decline", [False, True])
    async def test_a_bash_plan_spawns_no_helper(self, harness: _Harness, may_decline: bool) -> None:
        client = _client(
            _plan_json("Run the service-health probe script", "Search the logs for errors")
        )
        result = await harness.run(client, may_decline=may_decline)

        harness.workers.assert_not_called()
        assert result.kept_on_primary is True
        assert result.sub_agent_results == []
        assert result.synthesis_context == ""
        harness.degradations.assert_not_called()
        (event,) = harness.named("expansion_kept_on_primary")
        assert event["trace_id"] == "t-fre1515"
        assert event["session_id"] == "s-fre1515"
        assert event["reason"] == "task_needs_primary_tool"
        assert event["tasks"] == ["task_0"]

    @pytest.mark.asyncio
    async def test_a_helper_plan_still_spawns_helpers(self, harness: _Harness) -> None:
        client = _client(_plan_json("Search the logs for errors", "Recall my notes on Redis"))
        result = await harness.run(client, may_decline=True)

        assert harness.workers.await_count == 2
        assert result.kept_on_primary is False
        assert not harness.named("expansion_kept_on_primary")

    @pytest.mark.asyncio
    async def test_the_executor_returns_to_the_tool_loop(
        self, harness: _Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, state = await _step_init(
            monkeypatch, _gateway(**_ASKED), _client(_plan_json("Use bash to probe Redis"))
        )

        assert state == TaskState.LLM_CALL
        harness.workers.assert_not_called()
        assert ctx.synthesis_appended is False
        assert ctx.sub_agents_dispatched == 0
        assert [m["role"] for m in ctx.messages] == ["user"]
        assert ctx.expansion_plan is None
        # The turn did not expand, so it carries no expansion label (as a decline does).
        assert ctx.expansion_strategy is None
        assert ctx.planner_run is not None and ctx.planner_run.decision == "expanded"
