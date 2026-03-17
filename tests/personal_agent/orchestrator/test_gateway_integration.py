"""Tests for gateway-driven executor path."""

from __future__ import annotations

from unittest.mock import MagicMock

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
