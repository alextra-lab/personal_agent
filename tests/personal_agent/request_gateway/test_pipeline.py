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
    async def test_pipeline_emits_telemetry_event(self) -> None:
        """Pipeline emits gateway_pipeline_complete structlog event."""
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
        pipeline_events = [e for e in cap_logs if e.get("event") == "gateway_pipeline_complete"]
        assert len(pipeline_events) == 1
        evt = pipeline_events[0]
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
        events = [
            e for e in cap_logs if e.get("event") == "gateway_pipeline_complete"
        ]
        assert len(events) == 1
        evt = events[0]
        assert evt["task_type"] == "analysis"
        assert "confidence" in evt
        assert evt["trace_id"] == "t"

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
