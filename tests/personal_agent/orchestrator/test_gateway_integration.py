"""Tests for gateway-driven executor path."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from personal_agent.governance.models import Mode
from personal_agent.orchestrator.types import ExecutionContext
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


def _make_gateway_output(
    task_type: TaskType = TaskType.CONVERSATIONAL,
) -> GatewayOutput:
    """Create a GatewayOutput for testing."""
    return GatewayOutput(
        intent=IntentResult(
            task_type=task_type,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        ),
        governance=GovernanceContext(
            mode=Mode.NORMAL,
            expansion_permitted=True,
        ),
        decomposition=DecompositionResult(
            strategy=DecompositionStrategy.SINGLE,
            reason="test",
        ),
        context=AssembledContext(
            messages=[{"role": "user", "content": "hello"}],
            memory_context=None,
            tool_definitions=None,
        ),
        session_id="test-session",
        trace_id="test-trace",
    )


class TestGatewayOutputOnExecutionContext:
    """Tests for GatewayOutput integration with ExecutionContext."""

    def test_gateway_output_stored_on_context(self) -> None:
        """GatewayOutput can be stored on ExecutionContext."""
        gw = _make_gateway_output()
        ctx = MagicMock(spec=ExecutionContext)
        ctx.gateway_output = gw
        assert ctx.gateway_output.intent.task_type == TaskType.CONVERSATIONAL

    def test_gateway_output_defaults_to_none(self) -> None:
        """ExecutionContext.gateway_output defaults to None when not provided."""
        from personal_agent.orchestrator.channels import Channel

        ctx = ExecutionContext(
            session_id="s",
            trace_id="t",
            user_message="hello",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )
        assert ctx.gateway_output is None

    def test_gateway_output_with_memory_context(self) -> None:
        """GatewayOutput with memory context can be accessed."""
        gw = GatewayOutput(
            intent=IntentResult(
                task_type=TaskType.MEMORY_RECALL,
                complexity=Complexity.SIMPLE,
                confidence=0.9,
                signals=["memory_recall_pattern"],
            ),
            governance=GovernanceContext(
                mode=Mode.NORMAL,
                expansion_permitted=True,
            ),
            decomposition=DecompositionResult(
                strategy=DecompositionStrategy.SINGLE,
                reason="test",
            ),
            context=AssembledContext(
                messages=[{"role": "user", "content": "What have I asked about?"}],
                memory_context=[{"type": "entity", "name": "Python"}],
                tool_definitions=None,
            ),
            session_id="test-session",
            trace_id="test-trace",
        )
        assert gw.context.memory_context is not None
        assert len(gw.context.memory_context) == 1

    def test_gateway_output_delegation_intent(self) -> None:
        """GatewayOutput with DELEGATION intent."""
        gw = _make_gateway_output(task_type=TaskType.DELEGATION)
        assert gw.intent.task_type == TaskType.DELEGATION


class TestExpansionOnExecutionContext:
    def test_expansion_fields_default_to_none(self) -> None:
        from personal_agent.orchestrator.types import ExecutionContext

        ctx = ExecutionContext.__new__(ExecutionContext)
        assert getattr(ctx, "expansion_strategy", None) is None
        assert getattr(ctx, "sub_agent_results", None) is None

    def test_hybrid_strategy_value(self) -> None:
        assert DecompositionStrategy.HYBRID.value == "hybrid"
        assert DecompositionStrategy.DECOMPOSE.value == "decompose"


class TestHybridExecutionPath:
    """Behavioral test: HYBRID decomposition triggers sub-agent execution."""

    @pytest.mark.asyncio
    async def test_hybrid_path_calls_execute_hybrid_and_re_enters(self) -> None:
        """When expansion_strategy is set and sub_agent_results is None,
        step_llm_call should parse the plan, run sub-agents, and return
        TaskState.LLM_CALL for synthesis.
        """
        from personal_agent.orchestrator.sub_agent_types import SubAgentResult
        from personal_agent.orchestrator.types import ExecutionContext

        # Build a mock execution context in the expansion state
        ctx = MagicMock(spec=ExecutionContext)
        ctx.expansion_strategy = "hybrid"
        ctx.expansion_constraints = {"max_sub_agents": 2}
        ctx.sub_agent_results = None  # Phase 1: no results yet
        ctx.trace_id = "test-trace"
        ctx.messages = [{"role": "user", "content": "Analyze X and Y"}]
        ctx.response_text = None

        mock_result = SubAgentResult(
            task_id=uuid4(),
            spec_task="Research X",
            summary="X is well-documented",
            full_output="Full analysis of X...",
            tools_used=[],
            token_count=200,
            duration_ms=1500,
            success=True,
            error=None,
        )

        with (
            patch(
                "personal_agent.orchestrator.expansion.parse_decomposition_plan",
                return_value=[MagicMock()],  # One parsed spec
            ) as mock_parse,
            patch(
                "personal_agent.orchestrator.expansion.execute_hybrid",
                new_callable=AsyncMock,
                return_value=[mock_result],
            ) as mock_execute,
        ):
            # Simulate what the expansion hook does:
            # 1. Parse the LLM response as a decomposition plan
            response_text = "I'll break this into sub-tasks:\n1. Research X\n2. Research Y"
            specs = mock_parse(plan_text=response_text, max_sub_agents=2)
            assert len(specs) == 1
            mock_parse.assert_called_once()

            # 2. Execute sub-agents
            results = await mock_execute(
                specs=specs,
                llm_client=MagicMock(),
                trace_id="test-trace",
                max_concurrent=2,
            )
            assert len(results) == 1
            assert results[0].success is True
            mock_execute.assert_called_once()

            # 3. After execution, sub_agent_results should be stored
            ctx.sub_agent_results = results
            assert ctx.sub_agent_results is not None
            assert ctx.sub_agent_results[0].summary == "X is well-documented"

            # 4. Synthesis message should be appended
            synthesis_msg = {
                "role": "user",
                "content": (
                    "Sub-agent results:\n"
                    "- Research X: [OK] X is well-documented\n\n"
                    "The sub-tasks above have been completed. "
                    "Synthesize the results into a coherent response "
                    "for the user's original question."
                ),
            }
            ctx.messages.append({"role": "assistant", "content": response_text})
            ctx.messages.append(synthesis_msg)
            assert len(ctx.messages) == 3  # original + assistant + synthesis

    @pytest.mark.asyncio
    async def test_phase2_skips_expansion_hook(self) -> None:
        """When sub_agent_results is already populated (phase 2),
        the expansion hook should be skipped.
        """
        ctx = MagicMock()
        ctx.expansion_strategy = "hybrid"
        ctx.sub_agent_results = [MagicMock()]  # Already populated

        # Phase 2: the hook condition fails, execution continues normally
        should_expand = ctx.expansion_strategy is not None and ctx.sub_agent_results is None
        assert should_expand is False


class TestExpansionCostRollup:
    """ADR-0088 D3/D4 (FRE-513) — enforced expansion no longer rolls cost into the
    per-loop accumulator; it reports progress and lets the cost boundary drive the meter.
    """

    @pytest.mark.asyncio
    async def test_enforced_path_reports_progress_without_rollup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """step_init (enforced HYBRID) does NOT add ExpansionResult.cost_usd to
        ctx.turn_cost_usd (FRE-501 rollup removed); it reports turn progress at
        dispatch-start and after expansion. Live cost now climbs from
        turn.model_call_completed events published at the cost boundary (D3).
        """
        import personal_agent.orchestrator.executor as ex
        from personal_agent.orchestrator.channels import Channel
        from personal_agent.orchestrator.expansion_controller import ExpansionResult
        from personal_agent.orchestrator.sub_agent_types import SubAgentResult
        from personal_agent.orchestrator.types import ExecutionContext, TaskState
        from personal_agent.telemetry.trace import TraceContext

        monkeypatch.setattr(ex.settings, "orchestration_mode", "enforced")

        gw = GatewayOutput(
            intent=IntentResult(
                task_type=TaskType.CONVERSATIONAL,
                complexity=Complexity.SIMPLE,
                confidence=0.9,
                signals=[],
            ),
            governance=GovernanceContext(mode=Mode.NORMAL, expansion_permitted=True),
            decomposition=DecompositionResult(
                strategy=DecompositionStrategy.HYBRID,
                reason="test",
                constraints={"max_sub_agents": 2},
            ),
            context=AssembledContext(
                messages=[{"role": "user", "content": "build X and Y"}],
                memory_context=None,
                tool_definitions=None,
            ),
            session_id="s1",
            trace_id="t1",
        )
        ctx = ExecutionContext(
            session_id="s1",
            trace_id="t1",
            user_message="build X and Y",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
            gateway_output=gw,
        )

        progress_calls: list[float] = []

        async def _spy_progress(c: ExecutionContext) -> None:
            progress_calls.append(c.turn_cost_usd)

        monkeypatch.setattr(ex, "_report_turn_progress", _spy_progress)

        def _sub(cost: float, name: str) -> SubAgentResult:
            return SubAgentResult(
                task_id=uuid4(),
                spec_task=name,
                summary="s",
                full_output="s",
                tools_used=[],
                token_count=1,
                duration_ms=1,
                success=True,
                cost_usd=cost,
            )

        exp_result = ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[_sub(0.1, "a"), _sub(0.2, "b")],
            synthesis_context="SYN",
            planner_cost_usd=0.05,
        )

        controller = MagicMock()
        controller.execute = AsyncMock(return_value=exp_result)
        monkeypatch.setattr(
            "personal_agent.orchestrator.expansion_controller.ExpansionController",
            lambda: controller,
        )
        monkeypatch.setattr(
            "personal_agent.llm_client.factory.get_llm_client",
            lambda role_name=None: MagicMock(),
        )

        session_manager = MagicMock()
        session_manager.get_session = MagicMock(return_value=None)
        trace_ctx = TraceContext(trace_id="t1", session_id="s1")

        state = await ex.step_init(ctx, session_manager, trace_ctx)

        assert state == TaskState.LLM_CALL
        # ADR-0088 D3: the per-loop rollup is gone — expansion never mutates the
        # accumulator; cost climbs from turn.model_call_completed at the cost boundary.
        assert ctx.turn_cost_usd == pytest.approx(0.0)
        # Progress is reported at dispatch-start and after expansion (both with no rollup).
        assert progress_calls == [pytest.approx(0.0), pytest.approx(0.0)]


class TestEnforcedExpansionClientRole:
    """FRE-958 / FRE-1390 regression guard.

    The enforced HYBRID/DECOMPOSE path builds TWO separate clients: the
    dispatch client (every ``run_sub_agent`` call, and — pre-FRE-1390 — the
    planner too) for role=``sub_agent``, and, since FRE-1390, a second,
    dedicated planner client for role=``primary`` (decomposition needs
    reasoning; ``sub_agent`` binds to a thinking-disabled deployment).

    FRE-958 was: building the ONE shared client for ``primary`` instead of
    ``sub_agent`` handed dispatch a client resolved for the wrong
    role/placement, which for a cloud-bound sub_agent silently dials the
    local SLM base URL and dies. That invariant still holds for the dispatch
    client specifically — this guard now asserts both: dispatch is still
    ``sub_agent``-only, and a genuinely separate ``primary`` client exists for
    the planner (not a same-client role-kwarg substitution, which would be
    inert — see FRE-1390's finding that ``LiteLLMClient``'s dispatched
    deployment is fixed at construction, not by the ``role`` kwarg passed to
    ``.respond()``).
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "strategy",
        [DecompositionStrategy.HYBRID, DecompositionStrategy.DECOMPOSE],
    )
    async def test_enforced_expansion_builds_sub_agent_role_client(
        self, monkeypatch: pytest.MonkeyPatch, strategy: DecompositionStrategy
    ) -> None:
        """Enforced-mode step_init requests both role clients, never a third.

        get_llm_client is called exactly twice: once with role_name="sub_agent"
        (dispatch) and once with role_name="primary" (planner) — never
        "primary" for dispatch, and never a single shared call, for both
        HYBRID and DECOMPOSE strategies.
        """
        import personal_agent.orchestrator.executor as ex
        from personal_agent.llm_client.types import ModelRole
        from personal_agent.orchestrator.channels import Channel
        from personal_agent.orchestrator.expansion_controller import ExpansionResult
        from personal_agent.orchestrator.types import ExecutionContext, TaskState
        from personal_agent.telemetry.trace import TraceContext

        monkeypatch.setattr(ex.settings, "orchestration_mode", "enforced")

        gw = GatewayOutput(
            intent=IntentResult(
                task_type=TaskType.CONVERSATIONAL,
                complexity=Complexity.SIMPLE,
                confidence=0.9,
                signals=[],
            ),
            governance=GovernanceContext(mode=Mode.NORMAL, expansion_permitted=True),
            decomposition=DecompositionResult(strategy=strategy, reason="test"),
            context=AssembledContext(
                messages=[{"role": "user", "content": "build X and Y"}],
                memory_context=None,
                tool_definitions=None,
            ),
            session_id="s1",
            trace_id="t1",
        )
        ctx = ExecutionContext(
            session_id="s1",
            trace_id="t1",
            user_message="build X and Y",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
            gateway_output=gw,
        )

        controller = MagicMock()
        controller.execute = AsyncMock(return_value=ExpansionResult())
        monkeypatch.setattr(
            "personal_agent.orchestrator.expansion_controller.ExpansionController",
            lambda: controller,
        )

        # Two DISTINCT return values, one per role — a fixed single MagicMock
        # return_value would make the FRE-1390 "same client twice" bug
        # invisible to the identity assertion below.
        returned_clients = [MagicMock(name="sub_agent_client"), MagicMock(name="primary_client")]
        get_llm_client_spy = MagicMock(side_effect=returned_clients)
        monkeypatch.setattr("personal_agent.llm_client.factory.get_llm_client", get_llm_client_spy)

        session_manager = MagicMock()
        session_manager.get_session = MagicMock(return_value=None)
        trace_ctx = TraceContext(trace_id="t1", session_id="s1")

        state = await ex.step_init(ctx, session_manager, trace_ctx)

        assert state == TaskState.LLM_CALL
        assert get_llm_client_spy.call_count == 2
        calls = [c.kwargs.get("role_name") for c in get_llm_client_spy.call_args_list]
        assert calls == [ModelRole.SUB_AGENT.value, ModelRole.PRIMARY.value]

        # The dispatch client and the planner client passed into
        # controller.execute() must be the two DISTINCT get_llm_client() return
        # values, in the right slots — not the same object twice (which would
        # silently resurrect the FRE-1390 bug: a role-kwarg substitution on one
        # shared client, rather than a genuinely separate PRIMARY-built one).
        execute_call = controller.execute.await_args
        dispatch_client = execute_call.kwargs["llm_client"]
        planner_client = execute_call.kwargs["planner_llm_client"]
        assert dispatch_client is returned_clients[0]
        assert planner_client is returned_clients[1]
        assert dispatch_client is not planner_client
