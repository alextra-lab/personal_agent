"""FRE-1622 (ADR-0157 D3, D4): the decomposition step takes the turn's effort spec (AC-2, AC-7).

The globals are set to ``expansion_enabled=True`` and ``sub_agent_max_tool_iterations=30`` so
that each level's value differs from the global one.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest

from personal_agent.config import get_settings
from personal_agent.config.seshat_effort import (
    EFFORT_LEVELS,
    effort_spec,
    reset_current_effort,
    set_current_effort,
)
from personal_agent.governance.models import Mode
from personal_agent.request_gateway.decomposition import assess_decomposition
from personal_agent.request_gateway.pipeline import run_gateway_pipeline
from personal_agent.request_gateway.types import (
    Complexity,
    DecompositionStrategy,
    GovernanceContext,
    IntentResult,
    TaskType,
)

_HYBRID_MESSAGE = (
    "Analyze the trade-offs between microservices and monolithic architecture. "
    "What are the scalability implications? What are the hidden maintenance costs?"
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


def _intent(task_type: TaskType, complexity: Complexity) -> IntentResult:
    return IntentResult(task_type=task_type, complexity=complexity, confidence=0.9, signals=[])


_PERMITTED = GovernanceContext(mode=Mode.NORMAL, expansion_permitted=True, expansion_budget=3)


class TestFastRoutesEveryTurnToSingle:
    @pytest.mark.parametrize("task_type", list(TaskType))
    @pytest.mark.parametrize("complexity", list(Complexity))
    @pytest.mark.parametrize("planner_mode_declared", [True, False])
    def test_every_task_type_is_single_with_effort_fast(
        self, task_type: TaskType, complexity: Complexity, planner_mode_declared: bool
    ) -> None:
        result = assess_decomposition(
            intent=_intent(task_type, complexity),
            governance=_PERMITTED,
            delegation_enabled=True,
            expansion_enabled=True,
            planner_mode_declared=planner_mode_declared,
            effort_expansion=False,
        )
        assert result.strategy == DecompositionStrategy.SINGLE
        assert result.reason == "effort_fast"

    def test_the_resource_forcings_keep_their_reasons(self) -> None:
        intent = _intent(TaskType.ANALYSIS, Complexity.MODERATE)
        denied = GovernanceContext(mode=Mode.NORMAL, expansion_permitted=False, expansion_budget=3)
        empty = GovernanceContext(mode=Mode.NORMAL, expansion_permitted=True, expansion_budget=0)
        assert (
            assess_decomposition(intent, denied, effort_expansion=False).reason
            == "expansion_denied"
        )
        assert assess_decomposition(intent, empty, effort_expansion=False).reason == "zero_budget"

    def test_expansion_disabled_wins_over_effort_fast(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.ANALYSIS, Complexity.MODERATE),
            _PERMITTED,
            expansion_enabled=False,
            effort_expansion=False,
        )
        assert result.reason == "expansion_disabled"

    def test_a_level_that_expands_keeps_the_matrix(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.ANALYSIS, Complexity.MODERATE), _PERMITTED, effort_expansion=True
        )
        assert (result.strategy, result.reason) == (
            DecompositionStrategy.HYBRID,
            "analysis_moderate_hybrid",
        )


class TestThePipelineReadsTheSpec:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("level", ["fast"], indirect=True)
    async def test_fast_routes_a_hybrid_intent_to_single(self, level: str) -> None:
        result = await run_gateway_pipeline(
            user_message=_HYBRID_MESSAGE,
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=None,
        )
        assert (result.intent.task_type, result.intent.complexity) == (
            TaskType.ANALYSIS,
            Complexity.MODERATE,
        )
        assert result.decomposition.strategy == DecompositionStrategy.SINGLE
        assert result.decomposition.reason == "effort_fast"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("level", ["standard", "deep"], indirect=True)
    async def test_an_expanding_level_asks_the_planner(
        self, level: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "personal_agent.config.model_loader.primary_declares_planner_mode", lambda **_: True
        )
        result = await run_gateway_pipeline(
            user_message=_HYBRID_MESSAGE,
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=None,
        )
        assert result.decomposition.reason == "planner_asked"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("level", list(EFFORT_LEVELS), indirect=True)
    async def test_expansion_disabled_overrides_every_level(
        self, level: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-7: the kill switch wins at every level, with expansion permitted and a budget."""
        monkeypatch.setattr(get_settings(), "expansion_enabled", False)
        monkeypatch.setattr(
            "personal_agent.config.model_loader.primary_declares_planner_mode", lambda **_: True
        )
        result = await run_gateway_pipeline(
            user_message=_HYBRID_MESSAGE,
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=None,
            expansion_budget=3,
        )
        assert result.governance.expansion_permitted is True
        assert result.governance.expansion_budget == 3
        assert result.decomposition.strategy == DecompositionStrategy.SINGLE
        assert result.decomposition.reason == "expansion_disabled"


class TestNoPlannerAndNoHelperOnAForcedSingle:
    """The executor builds no planner or dispatch client for a forced SINGLE (AC-2, AC-7)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", ["effort_fast", "expansion_disabled"])
    @pytest.mark.parametrize("level", list(EFFORT_LEVELS), indirect=True)
    async def test_step_init_never_touches_the_expansion_machinery(
        self, level: str, reason: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import personal_agent.orchestrator.executor as ex
        from personal_agent.orchestrator.channels import Channel
        from personal_agent.orchestrator.types import ExecutionContext, TaskState
        from personal_agent.request_gateway.types import (
            AssembledContext,
            DecompositionResult,
            GatewayOutput,
        )
        from personal_agent.telemetry.trace import TraceContext

        gw = GatewayOutput(
            intent=_intent(TaskType.ANALYSIS, Complexity.MODERATE),
            governance=_PERMITTED,
            decomposition=DecompositionResult(strategy=DecompositionStrategy.SINGLE, reason=reason),
            context=AssembledContext(
                messages=[{"role": "user", "content": "analyze the trade-offs"}],
                memory_context=None,
                tool_definitions=None,
            ),
            session_id="s1",
            trace_id="t1",
        )
        ctx = ExecutionContext(
            session_id="s1",
            trace_id="t1",
            user_message="analyze the trade-offs",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
            gateway_output=gw,
        )
        controller_ctor_spy = MagicMock()
        monkeypatch.setattr(
            "personal_agent.orchestrator.expansion_controller.ExpansionController",
            controller_ctor_spy,
        )
        get_llm_client_spy = MagicMock()
        monkeypatch.setattr("personal_agent.llm_client.factory.get_llm_client", get_llm_client_spy)
        session_manager = MagicMock()
        session_manager.get_session = MagicMock(return_value=None)

        state = await ex.step_init(
            ctx, session_manager, TraceContext(trace_id="t1", session_id="s1")
        )

        assert state == TaskState.LLM_CALL
        controller_ctor_spy.assert_not_called()
        get_llm_client_spy.assert_not_called()
        assert ctx.expansion_strategy is None
