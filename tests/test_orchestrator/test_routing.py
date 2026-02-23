"""Tests for intelligent routing logic in the orchestrator.

This test suite validates:
1. Router correctly classifies simple vs complex queries
2. Router delegates to appropriate models (REASONING, CODING)
3. Router handles queries directly when appropriate
4. Routing overhead is <200ms
5. Fallback behavior when router parsing fails

Related:
- Implementation: src/personal_agent/orchestrator/executor.py
- Prompts: src/personal_agent/orchestrator/prompts.py
- Plan: docs/plans/router_routing_logic_implementation_plan.md
"""

import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.governance.models import Mode
from personal_agent.llm_client import ModelRole
from personal_agent.orchestrator import Channel, Orchestrator
from personal_agent.orchestrator.executor import (
    _determine_initial_model_role,
    _parse_routing_decision,
)
from personal_agent.orchestrator.types import ExecutionContext, RoutingDecision

# ============================================================================
# Unit Tests: Helper Functions
# ============================================================================


class TestDetermineInitialModelRole:
    """Test _determine_initial_model_role helper function."""

    def test_chat_channel_returns_router(self) -> None:
        """CHAT channel should start with ROUTER."""
        ctx = MagicMock(spec=ExecutionContext)
        ctx.channel = Channel.CHAT

        result = _determine_initial_model_role(ctx)

        assert result == ModelRole.ROUTER

    def test_code_task_channel_returns_coding(self) -> None:
        """CODE_TASK channel should start with CODING."""
        ctx = MagicMock(spec=ExecutionContext)
        ctx.channel = Channel.CODE_TASK

        result = _determine_initial_model_role(ctx)

        assert result == ModelRole.CODING

    def test_system_health_channel_returns_reasoning(self) -> None:
        """SYSTEM_HEALTH channel should start with REASONING."""
        ctx = MagicMock(spec=ExecutionContext)
        ctx.channel = Channel.SYSTEM_HEALTH

        result = _determine_initial_model_role(ctx)

        assert result == ModelRole.REASONING


class TestParseRoutingDecision:
    """Test _parse_routing_decision helper function."""

    def test_parse_handle_decision(self) -> None:
        """Parse HANDLE decision (router answers directly)."""
        response = json.dumps(
            {
                "routing_decision": "HANDLE",
                "confidence": 1.0,
                "reasoning_depth": 1,
                "reason": "Simple greeting",
                "response": "Hello! How can I help you today?",
            }
        )
        ctx = MagicMock(spec=ExecutionContext, trace_id="test-trace")

        result = _parse_routing_decision(response, ctx)

        assert result is not None
        assert result["decision"] == RoutingDecision.HANDLE
        assert result["confidence"] == 1.0
        assert result["reasoning_depth"] == 1
        assert result["response"] == "Hello! How can I help you today?"

    def test_parse_delegate_to_reasoning(self) -> None:
        """Parse DELEGATE decision to REASONING model."""
        response = json.dumps(
            {
                "routing_decision": "DELEGATE",
                "target_model": "REASONING",
                "confidence": 0.9,
                "reasoning_depth": 7,
                "reason": "Complex analysis required",
            }
        )
        ctx = MagicMock(spec=ExecutionContext, trace_id="test-trace")

        result = _parse_routing_decision(response, ctx)

        assert result is not None
        assert result["decision"] == RoutingDecision.DELEGATE
        assert result["target_model"] == ModelRole.REASONING
        assert result["confidence"] == 0.9
        assert result["reasoning_depth"] == 7

    def test_parse_delegate_to_coding(self) -> None:
        """Parse DELEGATE decision to CODING model."""
        response = json.dumps(
            {
                "routing_decision": "DELEGATE",
                "target_model": "CODING",
                "confidence": 1.0,
                "reasoning_depth": 5,
                "reason": "Code generation task",
            }
        )
        ctx = MagicMock(spec=ExecutionContext, trace_id="test-trace")

        result = _parse_routing_decision(response, ctx)

        assert result is not None
        assert result["decision"] == RoutingDecision.DELEGATE
        assert result["target_model"] == ModelRole.CODING

    def test_parse_with_markdown_code_fence(self) -> None:
        """Parse JSON wrapped in markdown code fence."""
        response = """```json
{
    "routing_decision": "HANDLE",
    "confidence": 1.0,
    "reasoning_depth": 2,
    "reason": "Simple fact"
}
```"""
        ctx = MagicMock(spec=ExecutionContext, trace_id="test-trace")

        result = _parse_routing_decision(response, ctx)

        assert result is not None
        assert result["decision"] == RoutingDecision.HANDLE

    def test_parse_invalid_json_falls_back_to_standard(self) -> None:
        """Invalid JSON should fallback to STANDARD model."""
        response = "This is not valid JSON"
        ctx = MagicMock(spec=ExecutionContext, trace_id="test-trace")

        result = _parse_routing_decision(response, ctx)

        assert result is not None
        assert result["decision"] == RoutingDecision.DELEGATE
        assert result["target_model"] == ModelRole.STANDARD
        assert result["confidence"] == 0.5
        assert "parse failed" in result["reason"].lower()


# ============================================================================
# Integration Tests: Routing Scenarios
# ============================================================================


@pytest.mark.asyncio
class TestRoutingScenarios:
    """Integration tests for routing scenarios with mocked LLM calls."""

    @patch("personal_agent.orchestrator.executor.LocalLLMClient")
    async def test_simple_greeting_router_handles(self, mock_client_class: Any) -> None:
        """Simple greeting: Router should handle directly."""
        # Mock router response (HANDLE)
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        router_response = {
            "role": "assistant",
            "content": json.dumps(
                {
                    "routing_decision": "HANDLE",
                    "confidence": 1.0,
                    "reasoning_depth": 1,
                    "reason": "Simple greeting",
                    "response": "Hello! How can I help you?",
                }
            ),
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 50},
            "raw": {},
        }

        mock_client.respond.return_value = router_response

        # Execute
        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session", user_message="Hello", mode=Mode.NORMAL, channel=Channel.CHAT
        )

        # Verify
        assert result["reply"] is not None
        assert "Hello" in result["reply"]

        # Verify only router was called (no delegation)
        assert mock_client.respond.call_count == 1
        call_args = mock_client.respond.call_args
        assert call_args.kwargs["role"] == ModelRole.ROUTER
        response_format = call_args.kwargs.get("response_format")
        assert isinstance(response_format, dict)
        assert response_format.get("type") == "json_schema"

    @patch("personal_agent.orchestrator.executor.LocalLLMClient")
    async def test_complex_query_router_delegates_to_reasoning(
        self, mock_client_class: Any
    ) -> None:
        """Complex query: Router should delegate to REASONING."""
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        # Mock router response (DELEGATE to REASONING)
        router_response = {
            "role": "assistant",
            "content": json.dumps(
                {
                    "routing_decision": "DELEGATE",
                    "target_model": "REASONING",
                    "confidence": 0.9,
                    "reasoning_depth": 8,
                    "reason": "Complex philosophical analysis",
                }
            ),
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 80},
            "raw": {},
        }

        # Mock reasoning model response
        reasoning_response = {
            "role": "assistant",
            "content": "Python is a high-level, interpreted programming language...",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 300},
            "raw": {},
        }

        mock_client.respond.side_effect = [router_response, reasoning_response]

        # Execute
        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Explain the philosophical implications of quantum mechanics",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        # Verify
        assert result["reply"] is not None
        assert "Python" in result["reply"]

        # Verify both router and reasoning were called
        assert mock_client.respond.call_count == 2

        # First call: ROUTER
        first_call = mock_client.respond.call_args_list[0]
        assert first_call.kwargs["role"] == ModelRole.ROUTER

        # Second call: REASONING
        second_call = mock_client.respond.call_args_list[1]
        assert second_call.kwargs["role"] == ModelRole.REASONING

    @patch("personal_agent.orchestrator.executor.LocalLLMClient")
    async def test_code_query_router_delegates_to_coding(self, mock_client_class: Any) -> None:
        """Code query: Router should delegate to CODING."""
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        # Mock router response (DELEGATE to CODING)
        router_response = {
            "role": "assistant",
            "content": json.dumps(
                {
                    "routing_decision": "DELEGATE",
                    "target_model": "CODING",
                    "confidence": 1.0,
                    "reasoning_depth": 5,
                    "reason": "Code generation task",
                }
            ),
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 70},
            "raw": {},
        }

        # Mock coding model response
        coding_response = {
            "role": "assistant",
            "content": "```python\ndef divide(a, b):\n    if b == 0:\n        raise ValueError('Cannot divide by zero')\n    return a / b\n```",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 200},
            "raw": {},
        }

        mock_client.respond.side_effect = [router_response, coding_response]

        # Execute
        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Write a Python function to divide two numbers safely",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        # Verify
        assert result["reply"] is not None
        assert "def divide" in result["reply"]

        # Verify both router and coding were called
        assert mock_client.respond.call_count == 2

        # Second call: CODING
        second_call = mock_client.respond.call_args_list[1]
        assert second_call.kwargs["role"] == ModelRole.CODING

    @patch("personal_agent.orchestrator.executor.LocalLLMClient")
    async def test_code_task_channel_bypasses_router(self, mock_client_class: Any) -> None:
        """CODE_TASK channel should bypass router and go directly to CODING."""
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        # Mock coding model response
        coding_response = {
            "role": "assistant",
            "content": "```python\nprint('Hello, World!')\n```",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 100},
            "raw": {},
        }

        mock_client.respond.return_value = coding_response

        # Execute with CODE_TASK channel
        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Write a hello world program",
            mode=Mode.NORMAL,
            channel=Channel.CODE_TASK,
        )

        # Verify
        assert result["reply"] is not None
        assert "print" in result["reply"]

        # Verify only coding model was called (no router)
        assert mock_client.respond.call_count == 1
        call_args = mock_client.respond.call_args
        assert call_args.kwargs["role"] == ModelRole.CODING


# ============================================================================
# Performance Tests
# ============================================================================


@pytest.mark.asyncio
class TestRoutingPerformance:
    """Test routing overhead and performance."""

    @patch("personal_agent.orchestrator.executor.LocalLLMClient")
    async def test_routing_overhead_under_200ms(self, mock_client_class: Any) -> None:
        """Routing decision overhead should be <200ms (excluding LLM call time)."""
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        # Mock fast router response
        router_response = {
            "role": "assistant",
            "content": json.dumps(
                {
                    "routing_decision": "HANDLE",
                    "confidence": 1.0,
                    "reasoning_depth": 1,
                    "reason": "Simple query",
                    "response": "Quick response",
                }
            ),
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 50},
            "raw": {},
        }

        # Simulate LLM call taking 500ms (realistic)
        async def mock_respond(*args: Any, **kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(0.5)  # 500ms LLM call
            return router_response

        mock_client.respond = mock_respond

        # Measure total time
        import asyncio

        start = time.time()

        orchestrator = Orchestrator()
        await orchestrator.handle_user_request(
            session_id="test-session", user_message="Hello", mode=Mode.NORMAL, channel=Channel.CHAT
        )

        total_time = time.time() - start

        # Total time should be ~500ms (LLM) + <200ms (routing overhead)
        # Allow 800ms total (generous buffer)
        assert total_time < 0.8, f"Routing took {total_time:.3f}s, expected <0.8s"


# ============================================================================
# Edge Cases and Error Handling
# ============================================================================


@pytest.mark.asyncio
class TestRoutingEdgeCases:
    """Test edge cases and error handling in routing."""

    @patch("personal_agent.orchestrator.executor.LocalLLMClient")
    async def test_router_parse_failure_falls_back_to_reasoning(
        self, mock_client_class: Any
    ) -> None:
        """If router returns invalid JSON, should fallback to REASONING."""
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        # Mock router returning invalid JSON
        invalid_router_response = {
            "role": "assistant",
            "content": "This is not valid JSON, just a text response",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 50},
            "raw": {},
        }

        # Mock reasoning model response (fallback)
        reasoning_response = {
            "role": "assistant",
            "content": "Fallback response from reasoning model",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 200},
            "raw": {},
        }

        mock_client.respond.side_effect = [invalid_router_response, reasoning_response]

        # Execute
        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="What is Python?",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        # Verify fallback occurred
        assert result["reply"] is not None
        assert "Fallback" in result["reply"]

        # Verify both calls were made
        assert mock_client.respond.call_count == 2

    @patch("personal_agent.orchestrator.executor.LocalLLMClient")
    async def test_low_confidence_routing_still_proceeds(self, mock_client_class: Any) -> None:
        """Low confidence routing decision should still proceed (no retry)."""
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        # Mock router with low confidence
        router_response = {
            "role": "assistant",
            "content": json.dumps(
                {
                    "routing_decision": "DELEGATE",
                    "target_model": "REASONING",
                    "confidence": 0.4,  # Low confidence
                    "reasoning_depth": 5,
                    "reason": "Uncertain, defaulting to reasoning",
                }
            ),
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 70},
            "raw": {},
        }

        reasoning_response = {
            "role": "assistant",
            "content": "Response from reasoning model",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 200},
            "raw": {},
        }

        mock_client.respond.side_effect = [router_response, reasoning_response]

        # Execute
        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Ambiguous query",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        # Verify it proceeded despite low confidence
        assert result["reply"] is not None
        assert mock_client.respond.call_count == 2

    @patch("personal_agent.orchestrator.executor.LocalLLMClient")
    async def test_invalid_target_model_fallback_to_standard(self, mock_client_class: Any) -> None:
        """Invalid target_model in DELEGATE decision should fallback to STANDARD (prevents infinite loop)."""
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        # Mock router with invalid target_model
        router_response = {
            "role": "assistant",
            "content": json.dumps(
                {
                    "routing_decision": "DELEGATE",
                    "target_model": "INVALID_MODEL",  # Invalid model name
                    "confidence": 0.9,
                    "reasoning_depth": 6,
                    "reason": "Invalid model name in response",
                }
            ),
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 80},
            "raw": {},
        }

        # Mock standard model response (fallback)
        standard_response = {
            "role": "assistant",
            "content": "Fallback response from standard model due to invalid target_model",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 200},
            "raw": {},
        }

        mock_client.respond.side_effect = [router_response, standard_response]

        # Execute
        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Test query with invalid routing",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        # Verify fallback occurred (prevents infinite loop)
        assert result["reply"] is not None
        assert "Fallback" in result["reply"] or "reasoning" in result["reply"].lower()

        # Verify exactly 2 calls: router (invalid target) + standard (fallback)
        # Should NOT loop back to router (infinite loop prevention)
        assert mock_client.respond.call_count == 2

        # Verify second call was to STANDARD (fallback), not ROUTER
        second_call = mock_client.respond.call_args_list[1]
        assert second_call.kwargs["role"] == ModelRole.STANDARD

    @patch("personal_agent.orchestrator.executor.LocalLLMClient")
    async def test_missing_target_model_fallback_to_standard(self, mock_client_class: Any) -> None:
        """Missing target_model in DELEGATE decision should fallback to STANDARD."""
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        # Mock router with missing target_model
        router_response = {
            "role": "assistant",
            "content": json.dumps(
                {
                    "routing_decision": "DELEGATE",
                    # target_model is missing
                    "confidence": 0.9,
                    "reasoning_depth": 6,
                    "reason": "Forgot to include target_model",
                }
            ),
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 70},
            "raw": {},
        }

        # Mock standard model response (fallback)
        standard_response = {
            "role": "assistant",
            "content": "Fallback response due to missing target_model",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 200},
            "raw": {},
        }

        mock_client.respond.side_effect = [router_response, standard_response]

        # Execute
        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Test query with missing target_model",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        # Verify fallback occurred
        assert result["reply"] is not None
        assert mock_client.respond.call_count == 2

        # Verify second call was to STANDARD (fallback)
        second_call = mock_client.respond.call_args_list[1]
        assert second_call.kwargs["role"] == ModelRole.STANDARD
