"""Tests for routing: heuristic classification + two-tier model taxonomy (ADR-0033)."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.config import settings
from personal_agent.governance.models import Mode
from personal_agent.llm_client import ModelRole
from personal_agent.orchestrator import Channel, Orchestrator
from personal_agent.orchestrator.executor import (
    _determine_initial_model_role,
)
from personal_agent.orchestrator.routing import (
    heuristic_routing,
    is_memory_recall_query,
    resolve_role,
)
from personal_agent.orchestrator.types import ExecutionContext
from tests.test_orchestrator.conftest import configure_mock_llm_client_model_configs


class TestRoutingHelpers:
    """Unit tests for routing helper functions."""

    def test_heuristic_gate_coding(self) -> None:
        """Routes stack-trace/code-like input to PRIMARY (two-tier taxonomy)."""
        plan = heuristic_routing("Debug this stack trace: Traceback ... def foo():")
        assert plan["target_model"] == ModelRole.PRIMARY
        assert plan["used_heuristics"] is True

    def test_heuristic_gate_standard_tool_intent(self) -> None:
        """Routes explicit web/tool intent to PRIMARY."""
        plan = heuristic_routing("Please search web for latest news on Rust")
        assert plan["target_model"] == ModelRole.PRIMARY

    def test_heuristic_gate_reasoning(self) -> None:
        """Routes formal proof style prompts to PRIMARY."""
        plan = heuristic_routing("Prove this rigorously with multi-step formal analysis")
        assert plan["target_model"] == ModelRole.PRIMARY

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
            # Eval CP-26 turn 4: broad recall (executor memory_recall_broad_query)
            "What do you remember about the DataForge project?",
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

    def test_resolve_role_primary_maps_to_primary(self, monkeypatch: Any) -> None:
        """Identity mapping: PRIMARY → PRIMARY (two-tier taxonomy, ADR-0033)."""
        monkeypatch.setattr(settings, "enable_reasoning_role", False)
        assert resolve_role(ModelRole.PRIMARY) == ModelRole.PRIMARY

    def test_resolve_role_sub_agent_maps_to_sub_agent(self) -> None:
        """Identity mapping: SUB_AGENT → SUB_AGENT (ADR-0033)."""
        assert resolve_role(ModelRole.SUB_AGENT) == ModelRole.SUB_AGENT

    def test_determine_initial_model_role_chat(self, monkeypatch: Any) -> None:
        """Starts chat channel on PRIMARY role (two-tier taxonomy)."""
        monkeypatch.setattr(settings, "router_role", "PRIMARY")
        ctx = MagicMock(spec=ExecutionContext)
        ctx.channel = Channel.CHAT
        assert _determine_initial_model_role(ctx) == ModelRole.PRIMARY


@pytest.mark.asyncio
class TestRoutingFlow:
    """Integration tests for orchestrator routing with two-tier taxonomy (ADR-0033)."""

    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_chat_request_uses_primary_model(self, mock_client_class: Any) -> None:
        """All chat requests route directly to PRIMARY — no router LLM call (ADR-0033)."""
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_client_class.return_value = mock_client
        mock_client.respond.return_value = {
            "role": "assistant",
            "content": "Answer to Python question",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 80},
            "response_id": None,
            "raw": {},
        }

        orchestrator = Orchestrator()
        await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="What is Python?",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        # Single LLM call — no router step
        assert mock_client.respond.call_count == 1
        call = mock_client.respond.call_args_list[0]
        assert call.kwargs["role"] == ModelRole.PRIMARY
        assert "What is Python?" in str(call.kwargs["messages"])

    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_single_model_mode_uses_primary_for_chat(
        self, mock_client_class: Any, monkeypatch: Any
    ) -> None:
        """PRIMARY is always used for chat requests in two-tier model (ADR-0033)."""
        monkeypatch.setattr(settings, "router_role", "PRIMARY")
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_client_class.return_value = mock_client
        mock_client.respond.return_value = {
            "role": "assistant",
            "content": "single model response",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 20},
            "response_id": None,
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
        assert mock_client.respond.call_args.kwargs["role"] == ModelRole.PRIMARY
