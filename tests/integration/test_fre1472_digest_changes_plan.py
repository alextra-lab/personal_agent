"""FRE-1472 AC-2 (ADR-0154 AC-8 part 1, ADR-0147 AC-2): the seeded digest pair, real planner.

A coined token in a relevant digest must reach at least one task goal, and the planner must
judge the digest ``used``. A coined token in an unrelated digest must reach no goal and no
constraint, and the planner must judge the digest ``none_relevant``. Plan text merely
differing between the two runs is not a pass.

Fires real planner calls on the primary deployment in its ``planner`` mode. Run only with
``PERSONAL_AGENT_INTEGRATION=1 make test-integration`` (the Makefile guard), never in an
agent session.
"""

from __future__ import annotations

import pytest

from personal_agent.config import get_settings
from personal_agent.llm_client.factory import get_llm_client
from personal_agent.orchestrator.executor import _build_planner_memory_digest
from personal_agent.orchestrator.expansion_controller import ExpansionController, ExpansionResult
from personal_agent.orchestrator.expansion_types import ExpansionPlan

pytestmark = pytest.mark.integration

_RELEVANT_TOKEN = "Zorblaxian"
_UNRELATED_TOKEN = "Vextrollium"


def _entity(name: str, description: str) -> dict[str, str]:
    return {"type": "entity", "name": name, "entity_type": "Concept", "description": description}


async def _plan(query: str, memory: list[dict[str, str]], trace_id: str) -> ExpansionPlan:
    settings = get_settings()
    digest = _build_planner_memory_digest(memory)
    assert digest.item_count == len(memory), "the seeded digest must carry every seeded line"
    return await ExpansionController()._run_planner(
        query=query,
        strategy="HYBRID",
        llm_client=get_llm_client(role_name="primary", mode="planner"),
        trace_id=trace_id,
        timeout_s=settings.planner_timeout_seconds,
        result=ExpansionResult(),
        messages=[{"role": "user", "content": query}],
        history_max_chars=settings.planner_history_max_chars,
        input_max_chars=settings.planner_input_max_chars,
        memory_digest=digest,
    )


@pytest.mark.asyncio
async def test_a_relevant_digest_reaches_a_goal() -> None:
    plan = await _plan(
        "Find three walking routes near home for this weekend, and check that each suits "
        "the family dog.",
        [
            _entity(
                "Home",
                f"Home is in the village of {_RELEVANT_TOKEN}, in Brittany.",
            ),
            _entity("Family dog", "The family dog is old and cannot walk more than 5 km."),
        ],
        "fre1472-ac2-relevant",
    )

    assert not plan.is_fallback, "the planner must produce a plan for the pair to mean anything"
    assert any(_RELEVANT_TOKEN.lower() in task.goal.lower() for task in plan.tasks), [
        task.goal for task in plan.tasks
    ]
    assert plan.memory_relevance == "used"


@pytest.mark.asyncio
async def test_an_unrelated_digest_leaks_nothing() -> None:
    plan = await _plan(
        "Compare three air-to-water heat pump models for a small, well-insulated house.",
        [
            _entity(
                _UNRELATED_TOKEN,
                f"{_UNRELATED_TOKEN} is a houseplant fertiliser brand tried once in 2024.",
            ),
            _entity("Chess club", "The chess club meets on Thursday evenings."),
        ],
        "fre1472-ac2-unrelated",
    )

    assert not plan.is_fallback, "the planner must produce a plan for the pair to mean anything"
    plan_text = " ".join(
        [task.goal for task in plan.tasks] + [c for task in plan.tasks for c in task.constraints]
    ).lower()
    assert _UNRELATED_TOKEN.lower() not in plan_text
    assert "chess" not in plan_text
    assert plan.memory_relevance == "none_relevant"
