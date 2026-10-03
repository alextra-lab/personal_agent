"""FRE-1512 AC-3 (ADR-0154 D6) — ``conversation_history_chars`` matches the planner's render.

The field is recorded on the four register types whether or not the planner runs. On a
turn where the planner does not run, it must equal the length of ``_render_planner_history``
for the same messages and budget, so a pre-change baseline compares like with like.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import personal_agent.orchestrator.executor as ex
from personal_agent.config import settings
from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.expansion_controller import (
    _render_planner_history,
    conversation_history_chars,
)
from personal_agent.orchestrator.types import ExecutionContext, TaskState
from personal_agent.request_gateway.types import (
    AssembledContext,
    Complexity,
    DecompositionResult,
    DecompositionStrategy,
    GatewayOutput,
    GovernanceContext,
    IntentResult,
    TaskType,
)
from personal_agent.telemetry.trace import TraceContext

QUERY = "what did we decide about the cache?"
HISTORY = [
    {"role": "user", "content": "We should freeze the layout."},
    {"role": "assistant", "content": "Agreed: append-only, reset on schedule."},
    {"role": "user", "content": "And the summary goes where?"},
    {"role": "assistant", "content": "Into the volatile block, never the prefix."},
]


def _gateway(task_type: TaskType, strategy: DecompositionStrategy) -> GatewayOutput:
    return GatewayOutput(
        intent=IntentResult(
            task_type=task_type, complexity=Complexity.SIMPLE, confidence=0.9, signals=[]
        ),
        governance=GovernanceContext(mode=Mode.NORMAL, expansion_permitted=True),
        decomposition=DecompositionResult(strategy=strategy, reason="test"),
        context=AssembledContext(
            messages=[{"role": "user", "content": QUERY}],
            memory_context=None,
            tool_definitions=None,
        ),
        session_id="s1",
        trace_id="t1",
    )


def _session_manager() -> MagicMock:
    session = MagicMock()
    session.messages = list(HISTORY)
    manager = MagicMock()
    manager.get_session = MagicMock(return_value=session)
    return manager


async def _run_step_init(
    monkeypatch: pytest.MonkeyPatch, task_type: TaskType, strategy: DecompositionStrategy
) -> tuple[ExecutionContext, MagicMock, TaskState]:
    ctx = ExecutionContext(
        session_id="s1",
        trace_id="t1",
        user_message=QUERY,
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        gateway_output=_gateway(task_type, strategy),
    )
    controller = MagicMock()
    controller.execute = AsyncMock(side_effect=RuntimeError("planner must not run here"))
    controller_ctor = MagicMock(return_value=controller)
    monkeypatch.setattr(
        "personal_agent.orchestrator.expansion_controller.ExpansionController", controller_ctor
    )
    monkeypatch.setattr(
        "personal_agent.llm_client.factory.get_llm_client", lambda role_name=None: MagicMock()
    )
    state = await ex.step_init(
        ctx, _session_manager(), TraceContext(trace_id="t1", session_id="s1")
    )
    return ctx, controller_ctor, state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_type",
    [TaskType.CONVERSATIONAL, TaskType.TOOL_USE, TaskType.ANALYSIS, TaskType.PLANNING],
)
async def test_single_turn_records_the_planner_render_length(
    monkeypatch: pytest.MonkeyPatch, task_type: TaskType
) -> None:
    ctx, controller_ctor, state = await _run_step_init(
        monkeypatch, task_type, DecompositionStrategy.SINGLE
    )

    assert state == TaskState.LLM_CALL
    controller_ctor.assert_not_called()  # the planner did not run
    expected = len(_render_planner_history(HISTORY, settings.planner_history_max_chars))
    assert expected > 0
    assert ctx.conversation_history_chars == expected


@pytest.mark.asyncio
async def test_value_follows_the_configured_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    budget = 90
    monkeypatch.setattr(settings, "planner_history_max_chars", budget)

    ctx, _, _ = await _run_step_init(
        monkeypatch, TaskType.CONVERSATIONAL, DecompositionStrategy.SINGLE
    )

    full = len(_render_planner_history(HISTORY, 10_000))
    assert ctx.conversation_history_chars == len(_render_planner_history(HISTORY, budget))
    assert ctx.conversation_history_chars <= budget < full  # trimmed from the oldest end


@pytest.mark.asyncio
async def test_other_task_types_stay_null(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, _, _ = await _run_step_init(
        monkeypatch, TaskType.MEMORY_RECALL, DecompositionStrategy.SINGLE
    )
    assert ctx.conversation_history_chars is None


def test_helper_drops_the_current_query_like_the_planner_call() -> None:
    """The helper renders the history without the trailing current message."""
    messages = [*HISTORY, {"role": "user", "content": QUERY}]
    assert conversation_history_chars(TaskType.CONVERSATIONAL, messages, 8000) == len(
        _render_planner_history(HISTORY, 8000)
    )
    assert conversation_history_chars(TaskType.CONVERSATIONAL, [], 8000) == 0
    assert conversation_history_chars(TaskType.MEMORY_RECALL, messages, 8000) is None


@pytest.mark.asyncio
async def test_synthesis_appended_is_set_only_when_a_synthesis_message_is_added(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0154 D6: ``synthesis_appended`` mirrors the synthesis message on an expansion."""
    from personal_agent.orchestrator.expansion_controller import ExpansionResult
    from personal_agent.orchestrator.sub_agent_types import SubAgentResult

    async def _no_progress(_ctx: ExecutionContext) -> None:
        return None

    monkeypatch.setattr(ex, "_report_turn_progress", _no_progress)

    async def _expand(results: list[SubAgentResult]) -> ExecutionContext:
        ctx = ExecutionContext(
            session_id="s1",
            trace_id="t1",
            user_message=QUERY,
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
            gateway_output=_gateway(TaskType.CONVERSATIONAL, DecompositionStrategy.HYBRID),
        )
        controller = MagicMock()
        controller.execute = AsyncMock(
            return_value=ExpansionResult(
                plan=MagicMock(is_fallback=False),
                sub_agent_results=results,
                synthesis_context="SYN",
            )
        )
        monkeypatch.setattr(
            "personal_agent.orchestrator.expansion_controller.ExpansionController",
            lambda: controller,
        )
        monkeypatch.setattr(
            "personal_agent.llm_client.factory.get_llm_client", lambda role_name=None: MagicMock()
        )
        await ex.step_init(ctx, _session_manager(), TraceContext(trace_id="t1", session_id="s1"))
        return ctx

    from uuid import uuid4

    sub = SubAgentResult(
        task_id=uuid4(),
        spec_task="x",
        summary="s",
        full_output="o",
        tools_used=[],
        token_count=1,
        duration_ms=1,
        success=True,
        cost_usd=0.0,
    )
    with_results = await _expand([sub])
    assert with_results.synthesis_appended is True
    assert with_results.messages[-1]["content"].startswith("SYN")
    # The history stamp is made before the planner runs, on a HYBRID turn too.
    assert with_results.conversation_history_chars is not None

    nothing = await _expand([])
    assert nothing.synthesis_appended is False
