"""Tests for the full gateway pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.governance.models import Mode
from personal_agent.request_gateway.pipeline import run_gateway_pipeline
from personal_agent.request_gateway.types import (
    DecompositionStrategy,
    GatewayOutput,
    TaskType,
)


class TestRunGatewayPipeline:
    """Tests for run_gateway_pipeline() — full gateway orchestration."""

    @pytest.mark.asyncio
    async def test_simple_conversational_request(self) -> None:
        """Conversational message produces valid GatewayOutput."""
        result = await run_gateway_pipeline(
            user_message="Hello, how are you?",
            session_id="test-session",
            session_messages=[],
            trace_id="test-trace",
            mode=Mode.NORMAL,
            memory_adapter=None,
        )
        assert isinstance(result, GatewayOutput)
        assert result.intent.task_type == TaskType.CONVERSATIONAL
        assert result.decomposition.strategy == DecompositionStrategy.SINGLE
        assert result.session_id == "test-session"
        assert result.trace_id == "test-trace"

    @pytest.mark.asyncio
    async def test_memory_recall_request(self) -> None:
        """Memory recall request enriches context via adapter."""
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=True)
        mock_adapter.recall_broad = AsyncMock(
            return_value=MagicMock(
                entities_by_type={"Topic": [{"name": "Python"}]},
                recent_sessions=[],
                total_entity_count=1,
            )
        )
        result = await run_gateway_pipeline(
            user_message="What have I asked about?",
            session_id="test-session",
            session_messages=[],
            trace_id="test-trace",
            mode=Mode.NORMAL,
            memory_adapter=mock_adapter,
        )
        assert result.intent.task_type == TaskType.MEMORY_RECALL
        assert result.context.memory_context is not None

    @pytest.mark.asyncio
    async def test_coding_maps_to_delegation(self) -> None:
        """Coding request maps to DELEGATION task type."""
        result = await run_gateway_pipeline(
            user_message="Write a function to sort a list",
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=None,
        )
        assert result.intent.task_type == TaskType.DELEGATION

    @pytest.mark.asyncio
    async def test_alert_mode_disables_expansion(self) -> None:
        """ALERT mode disables expansion in governance context."""
        result = await run_gateway_pipeline(
            user_message="Hello",
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.ALERT,
            memory_adapter=None,
        )
        assert result.governance.expansion_permitted is False

    @pytest.mark.asyncio
    async def test_pipeline_emits_telemetry_events(self) -> None:
        """Pipeline emits intent_classified and gateway_output structlog events."""
        import structlog.testing

        with structlog.testing.capture_logs() as cap_logs:
            await run_gateway_pipeline(
                user_message="Hello",
                session_id="s",
                session_messages=[],
                trace_id="t",
                mode=Mode.NORMAL,
                memory_adapter=None,
            )

        # intent_classified event (emitted after Stage 4)
        intent_events = [e for e in cap_logs if e.get("event") == "intent_classified"]
        assert len(intent_events) == 1
        ie = intent_events[0]
        assert "task_type" in ie
        assert "complexity" in ie
        assert "confidence" in ie
        assert "signals" in ie
        assert ie["trace_id"] == "t"

        # gateway_output summary event (emitted at end of pipeline)
        output_events = [e for e in cap_logs if e.get("event") == "gateway_output"]
        assert len(output_events) == 1
        evt = output_events[0]
        assert "task_type" in evt
        assert "complexity" in evt
        assert "trace_id" in evt
        assert "strategy" in evt
        assert "has_memory" in evt
        assert "degraded_stages" in evt

    @pytest.mark.asyncio
    async def test_pipeline_logs_intent_classification_to_es(self) -> None:
        """Analysis intent emits telemetry with correct task_type value.

        Verifies the structured log event contains the specific intent
        classification result (not just field presence). ES indexing is
        handled by the existing structlog → ElasticsearchHandler.
        """
        import structlog.testing

        with structlog.testing.capture_logs() as cap_logs:
            await run_gateway_pipeline(
                user_message="Analyze the trade-offs",
                session_id="s",
                session_messages=[],
                trace_id="t",
                mode=Mode.NORMAL,
                memory_adapter=None,
            )
        # Check intent_classified event for analysis classification
        intent_events = [
            e for e in cap_logs if e.get("event") == "intent_classified"
        ]
        assert len(intent_events) == 1
        ie = intent_events[0]
        assert ie["task_type"] == "analysis"
        assert "confidence" in ie
        assert ie["trace_id"] == "t"

        # Check gateway_output summary event also present
        output_events = [
            e for e in cap_logs if e.get("event") == "gateway_output"
        ]
        assert len(output_events) == 1

    @pytest.mark.asyncio
    async def test_degraded_stages_tracked(self) -> None:
        """Disconnected memory adapter produces degraded_stages entry."""
        # Memory adapter that fails
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=False)

        result = await run_gateway_pipeline(
            user_message="What have I asked about?",
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=mock_adapter,
        )
        # Context assembly should report degraded memory
        assert result.context.memory_context is None
        assert "context_assembly:memory_unavailable" in result.degraded_stages

    # --- Slice 2 integration tests ---

    @pytest.mark.asyncio
    async def test_complex_analysis_produces_decompose_strategy(self) -> None:
        """Complex multi-part analysis request routes to DECOMPOSE strategy.

        Uses 3+ question marks to trigger COMPLEX complexity (question_count >= 3).
        """
        message = (
            "Analyze the trade-offs between microservices and monolithic architecture. "
            "What are the scalability implications? What are the hidden maintenance costs? "
            "What are the team-size thresholds where each approach breaks down?"
        )
        result = await run_gateway_pipeline(
            user_message=message,
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=None,
        )
        assert result.intent.task_type == TaskType.ANALYSIS
        assert result.decomposition.strategy == DecompositionStrategy.DECOMPOSE

    @pytest.mark.asyncio
    async def test_delegation_produces_delegate_strategy(self) -> None:
        """Coding/delegation request produces DELEGATE strategy.

        Uses 'write a function' keyword (exact substring match in _CODING_KEYWORDS).
        """
        result = await run_gateway_pipeline(
            user_message="write a function to parse and validate JSON schemas",
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=None,
        )
        assert result.intent.task_type == TaskType.DELEGATION
        assert result.decomposition.strategy == DecompositionStrategy.DELEGATE

    @pytest.mark.asyncio
    async def test_budget_trim_when_context_exceeds_limit(self) -> None:
        """apply_budget() trims context when messages exceed max_tokens."""
        long_content = " ".join(["word"] * 200)
        large_history = [
            {"role": "user", "content": long_content},
            {"role": "assistant", "content": long_content},
            {"role": "user", "content": long_content},
            {"role": "assistant", "content": long_content},
        ]
        result = await run_gateway_pipeline(
            user_message="current question",
            session_id="s",
            session_messages=large_history,
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=None,
            expansion_budget=3,
            max_context_tokens=50,  # tiny budget to force trimming
        )
        # Context should have been trimmed due to large history
        assert result.context.trimmed is True
        assert result.context.overflow_action is not None
        # Last user message must always be preserved
        last_user = next(
            (m for m in reversed(result.context.messages) if m["role"] == "user"),
            None,
        )
        assert last_user is not None
        assert last_user["content"] == "current question"

    @pytest.mark.asyncio
    async def test_zero_expansion_budget_forces_single(self) -> None:
        """Pipeline with expansion_budget=0 forces SINGLE regardless of intent."""
        message = (
            "Analyze the trade-offs between microservices and monolithic architecture "
            "in detail. Consider scalability, team size, deployment complexity, "
            "observability, and data consistency. What are the hidden costs?"
        )
        result = await run_gateway_pipeline(
            user_message=message,
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=None,
            expansion_budget=0,
        )
        assert result.decomposition.strategy == DecompositionStrategy.SINGLE
        assert result.decomposition.reason == "zero_budget"

    @pytest.mark.asyncio
    async def test_telemetry_includes_budget_fields(self) -> None:
        """gateway_output event includes budget_trimmed and overflow_action."""
        import structlog.testing

        with structlog.testing.capture_logs() as cap_logs:
            await run_gateway_pipeline(
                user_message="Hello",
                session_id="s",
                session_messages=[],
                trace_id="t",
                mode=Mode.NORMAL,
                memory_adapter=None,
            )
        events = [e for e in cap_logs if e.get("event") == "gateway_output"]
        assert len(events) == 1
        evt = events[0]
        assert "budget_trimmed" in evt
        assert "overflow_action" in evt
        assert "expansion_budget" in evt


class TestPivot1Regression:
    """ADR-0063 §D1 / FRE-260 — TaskType→tool-filter wire severed."""

    def test_governance_allowed_tool_categories_always_none(self) -> None:
        """evaluate_governance no longer populates allowed_tool_categories.

        Before PIVOT-1, a conversational intent intersected to
        allowed_categories=[] and stripped every tool from the request,
        causing the FRE-254 <tool_code> pseudo-code failure.  After PIVOT-1
        the field is always None (deprecated) and mode is the only gate.
        """
        from personal_agent.request_gateway.governance import evaluate_governance

        for mode in (Mode.NORMAL, Mode.ALERT, Mode.DEGRADED, Mode.LOCKDOWN):
            ctx = evaluate_governance(mode=mode)
            assert ctx.allowed_tool_categories is None, (
                f"allowed_tool_categories must be None in {mode} (ADR-0063 §D1)"
            )

    def test_tool_registry_returns_tools_in_normal_mode(self) -> None:
        """Mode-only gate: NORMAL mode always yields a non-empty tool list.

        This is the direct regression guard for the FRE-254 failure class —
        a conversational intent previously produced tool_count=0 and forced
        the model to emit <tool_code> pseudo-code.
        """
        from personal_agent.tools import get_default_registry

        registry = get_default_registry()
        tools = registry.get_tool_definitions_for_llm(mode=Mode.NORMAL)
        assert len(tools) > 0, "NORMAL mode must expose at least one tool (FRE-260 regression)"
