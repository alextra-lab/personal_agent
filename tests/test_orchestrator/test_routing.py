"""Tests for router refactor: heuristic gate + delegate-only router."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.config import settings
from personal_agent.governance.models import Mode
from personal_agent.llm_client import ModelRole
from personal_agent.orchestrator import Channel, Orchestrator
from personal_agent.orchestrator.executor import (
    _determine_initial_model_role,
    _parse_routing_decision,
    _router_response_format,
)
from personal_agent.orchestrator.routing import (
    heuristic_routing,
    is_memory_recall_query,
    resolve_role,
)
from personal_agent.orchestrator.types import ExecutionContext, RoutingDecision


class TestRoutingHelpers:
    """Unit tests for routing helper functions and schema."""

    def test_heuristic_gate_coding(self) -> None:
        """Routes stack-trace/code-like input to CODING."""
        plan = heuristic_routing("Debug this stack trace: Traceback ... def foo():")
        assert plan["target_model"] == ModelRole.CODING
        assert plan["used_heuristics"] is True

    def test_heuristic_gate_standard_tool_intent(self) -> None:
        """Routes explicit web/tool intent to STANDARD."""
        plan = heuristic_routing("Please search web for latest news on Rust")
        assert plan["target_model"] == ModelRole.STANDARD

    def test_heuristic_gate_reasoning(self) -> None:
        """Routes formal proof style prompts to REASONING."""
        plan = heuristic_routing("Prove this rigorously with multi-step formal analysis")
        assert plan["target_model"] == ModelRole.REASONING

    def test_is_memory_recall_query_positive_cases(self) -> None:
        """ADR-0025: recall intent detected for history questions."""
        positive = [
            "What Greek locations have I asked about in the past?",
            "What have I ever asked you about?",
            "What topics have I discussed with you?",
            "What things have I mentioned?",
            "Have I ever asked about Paris?",
            "Have I mentioned my trip to Rome?",
            "Did I ask about the weather?",
            "Did I talk about Python?",
            "Do you remember what we discussed?",
            "My past conversation about travel",
            "Our previous session on cooking",
            "Last time we talked about books",
            "Remind me what we covered",
            "Remind me about that project",
            "What else have we talked about?",
            "What have we discussed so far?",
        ]
        for msg in positive:
            assert is_memory_recall_query(msg), f"Expected recall: {msg!r}"

    def test_is_memory_recall_query_negative_cases(self) -> None:
        """ADR-0025: no recall intent for task-assist or other queries."""
        negative = [
            "What is the weather in Crete?",
            "What is the capital of France?",
            "Search the web for news",
            "Tell me about Python",
            "How do I install Rust?",
            "Debug this stack trace",
            "Write a function to add two numbers",
            "What time is it?",
            "Hello",
            "Thanks",
            "Prove this rigorously",
            "List files in the current directory",
            "Open url https://example.com",
            "What Greek locations are worth visiting?",  # not "have I asked"
            "Have you seen the report?",  # "have you" not "have I"
        ]
        for msg in negative:
            assert not is_memory_recall_query(msg), f"Expected non-recall: {msg!r}"

    def test_is_memory_recall_query_empty_or_none(self) -> None:
        """None or empty message is not recall."""
        assert not is_memory_recall_query("")
        assert not is_memory_recall_query(None)  # type: ignore[arg-type]

    def test_schema_is_minimal_and_strict(self) -> None:
        """Ensures router schema is strict and minimal."""
        schema = _router_response_format()["json_schema"]["schema"]
        assert schema["additionalProperties"] is False
        assert "target_model" in schema["required"]
        assert "routing_decision" not in schema["properties"]
        assert schema["properties"]["target_model"]["enum"] == ["STANDARD", "REASONING", "CODING"]

    def test_parse_delegate_only_success(self) -> None:
        """Parses minimal delegate-only router JSON."""
        ctx = MagicMock(spec=ExecutionContext, trace_id="test-trace")
        result = _parse_routing_decision(
            json.dumps({"target_model": "REASONING", "confidence": 0.9, "reason": "formal proof"}),
            ctx,
        )
        assert result is not None
        assert result["decision"] == RoutingDecision.DELEGATE
        assert result["target_model"] == ModelRole.REASONING

    def test_parse_missing_target_model_returns_none(self) -> None:
        """Returns None when required router fields are missing."""
        ctx = MagicMock(spec=ExecutionContext, trace_id="test-trace")
        result = _parse_routing_decision(json.dumps({"confidence": 0.8}), ctx)
        assert result is None

    def test_resolve_role_single_model_reasoning_maps_to_standard(self, monkeypatch: Any) -> None:
        """Maps REASONING to STANDARD when reasoning role is disabled."""
        monkeypatch.setattr(settings, "enable_reasoning_role", False)
        assert resolve_role(ModelRole.REASONING) == ModelRole.STANDARD

    def test_determine_initial_model_role_chat(self, monkeypatch: Any) -> None:
        """Starts chat channel on the configured router role."""
        monkeypatch.setattr(settings, "router_role", "ROUTER")
        ctx = MagicMock(spec=ExecutionContext)
        ctx.channel = Channel.CHAT
        assert _determine_initial_model_role(ctx) == ModelRole.ROUTER


@pytest.mark.asyncio
class TestRoutingFlow:
    """Integration tests for orchestrator routing behavior."""

    @patch("personal_agent.orchestrator.executor.LocalLLMClient")
    async def test_router_request_messages_are_router_only(
        self, mock_client_class: Any, monkeypatch: Any
    ) -> None:
        """Router call receives only the current user message."""
        monkeypatch.setattr(settings, "routing_policy", "llm_only")
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
        mock_client.respond.side_effect = [
            {
                "role": "assistant",
                "content": json.dumps(
                    {"target_model": "STANDARD", "confidence": 0.9, "reason": "default"}
                ),
                "tool_calls": [],
                "reasoning_trace": None,
                "usage": {"total_tokens": 30},
                "raw": {},
            },
            {
                "role": "assistant",
                "content": "Delegated answer",
                "tool_calls": [],
                "reasoning_trace": None,
                "usage": {"total_tokens": 80},
                "raw": {},
            },
        ]

        orchestrator = Orchestrator()
        await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="What is Python?",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        first_call = mock_client.respond.call_args_list[0]
        assert first_call.kwargs["role"] == ModelRole.ROUTER
        assert first_call.kwargs["messages"] == [{"role": "user", "content": "What is Python?"}]

    @patch("personal_agent.orchestrator.executor.LocalLLMClient")
    async def test_heuristic_high_confidence_skips_router(
        self, mock_client_class: Any, monkeypatch: Any
    ) -> None:
        """High-confidence heuristic route bypasses router LLM call."""
        monkeypatch.setattr(settings, "routing_policy", "heuristic_then_llm")
        monkeypatch.setattr(settings, "routing_heuristic_threshold", 0.8)
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
        mock_client.respond.return_value = {
            "role": "assistant",
            "content": "Here is the fix.",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 120},
            "raw": {},
        }

        orchestrator = Orchestrator()
        await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Debug this stack trace and refactor the function",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        assert mock_client.respond.call_count == 1
        call = mock_client.respond.call_args_list[0]
        assert call.kwargs["role"] == ModelRole.CODING

    @patch("personal_agent.orchestrator.executor.LocalLLMClient")
    async def test_memory_not_injected_into_router_prompt(
        self, mock_client_class: Any, monkeypatch: Any
    ) -> None:
        """Router system prompt excludes memory enrichment section."""
        monkeypatch.setattr(settings, "routing_policy", "llm_only")
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
        mock_client.respond.side_effect = [
            {
                "role": "assistant",
                "content": json.dumps(
                    {"target_model": "STANDARD", "confidence": 0.9, "reason": "default"}
                ),
                "tool_calls": [],
                "reasoning_trace": None,
                "usage": {"total_tokens": 30},
                "raw": {},
            },
            {
                "role": "assistant",
                "content": "ok",
                "tool_calls": [],
                "reasoning_trace": None,
                "usage": {"total_tokens": 30},
                "raw": {},
            },
        ]

        orchestrator = Orchestrator()
        await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="What is Python?",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        router_call = mock_client.respond.call_args_list[0]
        system_prompt = router_call.kwargs.get("system_prompt") or ""
        assert "Relevant Past Conversations" not in system_prompt

    @patch("personal_agent.orchestrator.executor.LocalLLMClient")
    async def test_invalid_router_output_falls_back_to_heuristic(
        self, mock_client_class: Any, monkeypatch: Any
    ) -> None:
        """Invalid router JSON falls back to heuristic delegation."""
        monkeypatch.setattr(settings, "routing_policy", "llm_only")
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
        mock_client.respond.side_effect = [
            {
                "role": "assistant",
                "content": '{"confidence": 0.9}',
                "tool_calls": [],
                "reasoning_trace": None,
                "usage": {"total_tokens": 20},
                "raw": {},
            },
            {
                "role": "assistant",
                "content": "Fallback standard response",
                "tool_calls": [],
                "reasoning_trace": None,
                "usage": {"total_tokens": 30},
                "raw": {},
            },
        ]

        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Tell me what Python is",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )
        assert "Fallback" in result["reply"]
        assert mock_client.respond.call_count == 2
        assert mock_client.respond.call_args_list[1].kwargs["role"] == ModelRole.STANDARD

    @patch("personal_agent.orchestrator.executor.LocalLLMClient")
    async def test_single_model_mode_uses_standard_for_chat(
        self, mock_client_class: Any, monkeypatch: Any
    ) -> None:
        """Single-model mode routes initial CHAT call to STANDARD."""
        monkeypatch.setattr(settings, "router_role", "STANDARD")
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
        mock_client.respond.return_value = {
            "role": "assistant",
            "content": "single model response",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 20},
            "raw": {},
        }

        orchestrator = Orchestrator()
        await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="hello",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        assert mock_client.respond.call_count == 1
        assert mock_client.respond.call_args.kwargs["role"] == ModelRole.STANDARD
