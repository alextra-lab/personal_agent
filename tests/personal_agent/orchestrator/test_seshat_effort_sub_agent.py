"""FRE-1622 (ADR-0157 D3 item 3): the helper's round budget is the turn's level (AC-2).

The global ceiling is 30, unlike every level, so a reader that still takes the global value
fails. All three places read the spec: the loop's enforced cap, the task message and
``sub_agent_start.round_budget``.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
import structlog.testing

from personal_agent.config import get_settings
from personal_agent.config.seshat_effort import (
    effort_spec,
    reset_current_effort,
    set_current_effort,
)
from personal_agent.orchestrator.sub_agent import run_sub_agent
from tests.personal_agent.orchestrator.test_sub_agent import (
    _dispatch_result,
    _llm_response,
    _spec_with_tools,
    _stub_tool_layer,
)


@pytest.fixture(autouse=True)
def _globals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "expansion_enabled", True)
    monkeypatch.setattr(get_settings(), "sub_agent_max_tool_iterations", 30)


@pytest.fixture
def level(request: pytest.FixtureRequest) -> Iterator[str]:
    token = set_current_effort(effort_spec(request.param))
    try:
        yield request.param
    finally:
        reset_current_effort(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(("level", "rounds"), [("standard", 12), ("deep", 20)], indirect=["level"])
async def test_a_helper_that_always_asks_for_a_tool_stops_at_the_level_budget(
    level: str, rounds: int
) -> None:
    client = AsyncMock()
    client.respond = AsyncMock(
        return_value=_llm_response(
            "still working", tool_calls=[{"id": "c0", "name": "run_python", "arguments": "{}"}]
        )
    )
    with (
        patch(
            "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
            return_value=_stub_tool_layer("run_python"),
        ),
        patch(
            "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
            AsyncMock(return_value=_dispatch_result("c0", "run_python", "ok")),
        ),
        structlog.testing.capture_logs() as logs,
    ):
        result = await run_sub_agent(
            spec=_spec_with_tools(["run_python"]), llm_client=client, trace_id="t"
        )

    assert result.tool_iterations == rounds
    first_call_messages = client.respond.call_args_list[0].kwargs["messages"]
    task_message = next(m["content"] for m in first_call_messages if "Thoroughness:" in m["content"])
    assert f"budget of {rounds} tool round(s)" in task_message
    assert "budget of 30" not in str(client.respond.call_args_list)
    (start,) = [e for e in logs if e.get("event") == "sub_agent_start"]
    assert start["round_budget"] == rounds
