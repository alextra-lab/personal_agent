"""FRE-1622 (ADR-0157 D8): ``planner_outcome`` names the level and the policy of its prompt (AC-6).

``planner_policy`` must agree with ``planner_system_prompt_sha256`` on every event: ``p2``
exactly when the hashed prompt holds the D6 block, ``baseline`` on a D2 turn without it, and
``None`` when the prompt admits no decline.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from personal_agent.config import get_settings
from personal_agent.config.seshat_effort import (
    effort_spec,
    reset_current_effort,
    set_current_effort,
)
from personal_agent.orchestrator.expansion_controller import (
    _P2_DECOMPOSITION_POLICY,
    ExpansionController,
    ExpansionResult,
)

_CONTROLLER_LOGGER = "personal_agent.orchestrator.expansion_controller"


@pytest.fixture(autouse=True)
def _ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "sub_agent_max_tool_iterations", 30)


@pytest.fixture
def spec(request: pytest.FixtureRequest) -> Iterator[Any]:
    level, policy = request.param
    value = effort_spec(level).model_copy(update={"planner_policy": policy})
    token = set_current_effort(value)
    try:
        yield value
    finally:
        reset_current_effort(token)


def _client() -> AsyncMock:
    plan = {"strategy": "HYBRID", "tasks": [{"name": "t", "goal": "g", "type": "general"}]}
    client = AsyncMock()
    client.respond = AsyncMock(return_value={"content": json.dumps(plan), "cost_usd": 0.0})
    return client


async def _run(client: AsyncMock, *, may_decline: bool) -> None:
    await ExpansionController()._run_planner(
        query="Plan lunch",
        strategy="HYBRID",
        llm_client=client,
        trace_id="test-trace-fre1622",
        timeout_s=5.0,
        result=ExpansionResult(),
        may_decline=may_decline,
    )


def _event(caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    (event,) = [
        r.msg
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get("event") == "planner_outcome"
    ]
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spec",
    [("standard", "p2"), ("standard", "baseline"), ("deep", "baseline"), ("deep", "p2")],
    indirect=True,
)
@pytest.mark.parametrize("may_decline", [True, False])
async def test_the_event_names_the_level_and_a_policy_that_agrees_with_the_hash(
    spec: Any, may_decline: bool, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger=_CONTROLLER_LOGGER)
    client = _client()

    await _run(client, may_decline=may_decline)

    event = _event(caplog)
    system = str(client.respond.call_args.kwargs["messages"][0]["content"])
    assert event["seshat_effort"] == spec.level
    assert event["planner_system_prompt_sha256"] == hashlib.sha256(system.encode()).hexdigest()
    assert event["planner_policy"] == (spec.planner_policy if may_decline else None)
    assert (_P2_DECOMPOSITION_POLICY in system) == (event["planner_policy"] == "p2")
    assert f"({spec.sub_agent_rounds} tool round(s) each)" in system
