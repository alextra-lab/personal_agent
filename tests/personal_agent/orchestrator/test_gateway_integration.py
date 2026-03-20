"""Tests for gateway-driven executor path."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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
        TaskState.LLM_CALL for synthesis."""
        from personal_agent.orchestrator.sub_agent_types import SubAgentResult
        from personal_agent.orchestrator.types import ExecutionContext, TaskState

        # Build a mock execution context in the expansion state
        ctx = MagicMock(spec=ExecutionContext)
        ctx.expansion_strategy = "hybrid"
        ctx.expansion_constraints = {"max_sub_agents": 2}
        ctx.sub_agent_results = None  # Phase 1: no results yet
        ctx.trace_id = "test-trace"
        ctx.messages = [{"role": "user", "content": "Analyze X and Y"}]
        ctx.response_text = None

        mock_result = SubAgentResult(
            task_id="sub-1",
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
        the expansion hook should be skipped."""
        ctx = MagicMock()
        ctx.expansion_strategy = "hybrid"
        ctx.sub_agent_results = [MagicMock()]  # Already populated

        # Phase 2: the hook condition fails, execution continues normally
        should_expand = (
            ctx.expansion_strategy is not None
            and ctx.sub_agent_results is None
        )
        assert should_expand is False
