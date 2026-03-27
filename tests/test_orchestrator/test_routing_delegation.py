"""Routing delegation regression tests (FRE-105).

Validates that:
- Router correctly delegates factual, live-data, and reasoning queries to STANDARD or REASONING.
- Tool-use system prompt is assembled and injected for STANDARD/REASONING (not ROUTER).
- Heuristic gate continues to bypass LLM call for high-confidence inputs.
- Heuristic fallback to STANDARD fires on ambiguous short queries.
- Prompt sizes stay within documented baselines (tests/fixtures/routing_token_baselines.json).

All tests are unit-level: no live LLM server required.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.config import settings
from personal_agent.governance.models import Mode
from personal_agent.llm_client import ModelRole
from personal_agent.orchestrator import Channel, Orchestrator
from personal_agent.orchestrator.prompts import (
    ROUTER_SYSTEM_PROMPT,
    TOOL_USE_SYSTEM_PROMPT,
    get_router_prompt,
)
from personal_agent.orchestrator.routing import heuristic_routing
from tests.test_orchestrator.conftest import configure_mock_llm_client_model_configs

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASELINES_PATH = Path(__file__).parent.parent / "fixtures" / "routing_token_baselines.json"


def _load_baselines() -> dict[str, Any]:
    with open(_BASELINES_PATH) as f:
        return json.load(f)


def _make_llm_response(content: str, role: ModelRole = ModelRole.PRIMARY) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [],
        "reasoning_trace": None,
        "usage": {"total_tokens": 50, "prompt_tokens": 30, "completion_tokens": 20},
        "raw": {},
    }


def _router_json(target: str, confidence: float = 0.9) -> str:
    return json.dumps({"target_model": target, "confidence": confidence, "reason": "test"})


# ---------------------------------------------------------------------------
# Prompt constant tests
# ---------------------------------------------------------------------------


class TestPromptConstants:
    """Ensure prompts exist, have correct shape, and fit within baselines."""

    def test_router_prompt_non_empty(self) -> None:
        """Router prompt must have substantive content."""
        assert len(ROUTER_SYSTEM_PROMPT) > 50

    def test_router_prompt_contains_required_targets(self) -> None:
        """Router prompt must describe all three target models."""
        assert "STANDARD" in ROUTER_SYSTEM_PROMPT
        assert "REASONING" in ROUTER_SYSTEM_PROMPT
        assert "CODING" in ROUTER_SYSTEM_PROMPT

    def test_router_prompt_delegate_only(self) -> None:
        """Router must not include a HANDLE path — delegate-only since router refactor."""
        assert "HANDLE" not in ROUTER_SYSTEM_PROMPT, "Router must be delegate-only"

    def test_router_prompt_within_baseline(self) -> None:
        """Router prompt char count must match the committed baseline."""
        baselines = _load_baselines()
        actual = len(ROUTER_SYSTEM_PROMPT)
        assert actual == baselines["router_system_prompt_chars"], (
            f"Router prompt size changed: {actual} chars "
            f"(baseline {baselines['router_system_prompt_chars']}). "
            "If intentional, update tests/fixtures/routing_token_baselines.json."
        )

    def test_router_prompt_below_max(self) -> None:
        """Router prompt must not exceed the upper guard rail."""
        baselines = _load_baselines()
        assert len(ROUTER_SYSTEM_PROMPT) <= baselines["router_system_prompt_max_chars"]

    def test_tool_use_prompt_within_baseline(self) -> None:
        """TOOL_USE_SYSTEM_PROMPT char count must match the committed baseline."""
        baselines = _load_baselines()
        actual = len(TOOL_USE_SYSTEM_PROMPT)
        assert actual == baselines["tool_use_system_prompt_chars"], (
            f"TOOL_USE_SYSTEM_PROMPT size changed: {actual} chars "
            f"(baseline {baselines['tool_use_system_prompt_chars']}). "
            "If intentional, update tests/fixtures/routing_token_baselines.json."
        )

    def test_tool_use_prompt_below_max(self) -> None:
        """TOOL_USE_SYSTEM_PROMPT must not exceed the upper guard rail."""
        baselines = _load_baselines()
        assert len(TOOL_USE_SYSTEM_PROMPT) <= baselines["tool_use_system_prompt_max_chars"]

    def test_tool_use_prompt_contains_perplexity_guidance(self) -> None:
        """Tool-use prompt must reference web_search as primary and mcp_perplexity_ask as fallback.

        ADR-0034: mcp_perplexity_research is no longer referenced in the default guidance;
        web_search (SearXNG) is the primary search tool, mcp_perplexity_ask is the fallback
        for synthesized answers with citations.
        """
        assert "web_search" in TOOL_USE_SYSTEM_PROMPT
        assert "mcp_perplexity_ask" in TOOL_USE_SYSTEM_PROMPT

    def test_get_router_prompt_returns_router_system_prompt(self) -> None:
        """get_router_prompt() must return the ROUTER_SYSTEM_PROMPT constant."""
        assert get_router_prompt() is ROUTER_SYSTEM_PROMPT

    def test_get_router_prompt_takes_no_parameters(self) -> None:
        """get_router_prompt() must be a no-arg function (dead param removed in FRE-105)."""
        import inspect

        sig = inspect.signature(get_router_prompt)
        assert len(sig.parameters) == 0, (
            "get_router_prompt() must be no-arg (dead include_format_detection param removed)"
        )


# ---------------------------------------------------------------------------
# Heuristic gate tests (no LLM call)
# ---------------------------------------------------------------------------


class TestHeuristicGate:
    """Heuristic pre-router gate should bypass LLM for high-confidence inputs."""

    def test_heuristic_delegates_code_to_primary(self) -> None:
        """Stack-trace / code-like input should route to PRIMARY via heuristic."""
        result = heuristic_routing("Debug this stack trace: Traceback ... def foo():")
        assert result["target_model"] == ModelRole.PRIMARY
        assert result["used_heuristics"] is True

    def test_heuristic_delegates_tool_intent_to_primary(self) -> None:
        """Explicit web/tool intent should route to PRIMARY via heuristic."""
        result = heuristic_routing("Please search the web for the latest Rust news")
        assert result["target_model"] == ModelRole.PRIMARY

    def test_heuristic_delegates_formal_proof_to_primary(self) -> None:
        """Formal proof style prompts should route to PRIMARY via heuristic."""
        result = heuristic_routing("Prove rigorously with formal multi-step analysis")
        assert result["target_model"] == ModelRole.PRIMARY

    def test_heuristic_low_confidence_returns_primary_fallback(self) -> None:
        """Short ambiguous queries fall back to PRIMARY (LLM routing will take over)."""
        result = heuristic_routing("Hi")
        assert result["target_model"] == ModelRole.PRIMARY


# ---------------------------------------------------------------------------
# LLM routing delegation tests (mocked LLM)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestLLMDelegation:
    """Verify PRIMARY model handles all query types directly (ADR-0033 two-tier taxonomy)."""

    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_primary_handles_factual_query(
        self, mock_get_llm_client: Any
    ) -> None:
        """Factual queries are handled by PRIMARY in a single LLM call (no router)."""
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_get_llm_client.return_value = mock_client
        mock_client.respond.return_value = _make_llm_response("The capital of France is Paris.")

        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-factual",
            user_message="What is the capital of France?",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        assert mock_client.respond.call_count == 1
        assert mock_client.respond.call_args_list[0].kwargs["role"] == ModelRole.PRIMARY
        assert "Paris" in result["reply"]

    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_primary_handles_live_data_query(
        self, mock_get_llm_client: Any
    ) -> None:
        """Live-data queries go to PRIMARY (tool-capable) in a single LLM call."""
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_get_llm_client.return_value = mock_client
        mock_client.respond.return_value = _make_llm_response("It is currently sunny in Paris.")

        orchestrator = Orchestrator()
        await orchestrator.handle_user_request(
            session_id="test-live",
            user_message="What is the weather in Paris today?",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        assert mock_client.respond.call_count == 1
        assert mock_client.respond.call_args_list[0].kwargs["role"] == ModelRole.PRIMARY

    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_primary_handles_reasoning_query(
        self, mock_get_llm_client: Any
    ) -> None:
        """Reasoning queries go directly to PRIMARY without a separate router call."""
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_get_llm_client.return_value = mock_client
        mock_client.respond.return_value = _make_llm_response(
            "Proof: assume sqrt(2) = p/q in lowest terms ..."
        )

        orchestrator = Orchestrator()
        await orchestrator.handle_user_request(
            session_id="test-reasoning",
            user_message="Prove that sqrt(2) is irrational step-by-step",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        assert mock_client.respond.call_count == 1
        assert mock_client.respond.call_args_list[0].kwargs["role"] == ModelRole.PRIMARY

    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_primary_handles_research_synthesis(
        self, mock_get_llm_client: Any
    ) -> None:
        """Research synthesis queries go to PRIMARY — no dedicated reasoning tier."""
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_get_llm_client.return_value = mock_client
        mock_client.respond.return_value = _make_llm_response(
            "Recent research on attention mechanisms shows ..."
        )

        orchestrator = Orchestrator()
        await orchestrator.handle_user_request(
            session_id="test-research",
            user_message="Synthesise recent research on attention mechanisms in deep learning",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        assert mock_client.respond.call_count == 1
        assert mock_client.respond.call_args_list[0].kwargs["role"] == ModelRole.PRIMARY

    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_all_queries_use_exactly_one_llm_call(
        self, mock_get_llm_client: Any
    ) -> None:
        """Any user query results in exactly one LLM call to PRIMARY (ADR-0033: no router)."""
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_get_llm_client.return_value = mock_client
        mock_client.respond.return_value = _make_llm_response("Answer.")

        orchestrator = Orchestrator()
        await orchestrator.handle_user_request(
            session_id="test-single-call",
            user_message="Tell me something interesting",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        assert mock_client.respond.call_count == 1
        assert mock_client.respond.call_args_list[0].kwargs["role"] == ModelRole.PRIMARY


# ---------------------------------------------------------------------------
# Tool-use prompt assembly tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestToolPromptAssembly:
    """Verify TOOL_USE_SYSTEM_PROMPT is assembled correctly for the PRIMARY role (ADR-0033)."""

    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_primary_role_receives_tool_use_prompt(
        self, mock_client_class: Any
    ) -> None:
        """PRIMARY model call must include TOOL_USE_SYSTEM_PROMPT in its system prompt."""
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_client_class.return_value = mock_client
        mock_client.respond.return_value = _make_llm_response("Here is the info.")

        orchestrator = Orchestrator()
        await orchestrator.handle_user_request(
            session_id="test-tool-prompt",
            user_message="What CVEs affect OpenSSH this month?",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        primary_call = mock_client.respond.call_args_list[0]
        system_prompt = primary_call.kwargs.get("system_prompt") or ""
        assert TOOL_USE_SYSTEM_PROMPT in system_prompt, (
            "PRIMARY model call must include TOOL_USE_SYSTEM_PROMPT"
        )

    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_primary_role_receives_tool_use_prompt_for_research(
        self, mock_client_class: Any
    ) -> None:
        """PRIMARY model call must include TOOL_USE_SYSTEM_PROMPT for research queries."""
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_client_class.return_value = mock_client
        mock_client.respond.return_value = _make_llm_response("Deep research synthesis answer.")

        orchestrator = Orchestrator()
        await orchestrator.handle_user_request(
            session_id="test-reasoning-tool-prompt",
            user_message="Give me a comprehensive survey of zero-trust network access vendors",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        primary_call = mock_client.respond.call_args_list[0]
        system_prompt = primary_call.kwargs.get("system_prompt") or ""
        assert TOOL_USE_SYSTEM_PROMPT in system_prompt

    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_primary_call_includes_tool_use_prompt(
        self, mock_client_class: Any
    ) -> None:
        """PRIMARY call must include TOOL_USE_SYSTEM_PROMPT — no separate router call (ADR-0033)."""
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_client_class.return_value = mock_client
        mock_client.respond.return_value = _make_llm_response("Answer.")

        orchestrator = Orchestrator()
        await orchestrator.handle_user_request(
            session_id="test-primary-tool-prompt",
            user_message="What is Python?",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        assert mock_client.respond.call_count == 1
        primary_call = mock_client.respond.call_args_list[0]
        system_prompt = primary_call.kwargs.get("system_prompt") or ""
        assert TOOL_USE_SYSTEM_PROMPT in system_prompt
        assert "mcp_perplexity_ask" in system_prompt

    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_assembled_tool_prompt_within_baseline(
        self, mock_client_class: Any
    ) -> None:
        """System prompt assembled for a PRIMARY tool call must meet minimum size baseline."""
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_client_class.return_value = mock_client
        mock_client.respond.return_value = _make_llm_response("Answer.")

        orchestrator = Orchestrator()
        await orchestrator.handle_user_request(
            session_id="test-prompt-size",
            user_message="What is the weather in Paris today?",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        baselines = _load_baselines()
        primary_call = mock_client.respond.call_args_list[0]
        system_prompt = primary_call.kwargs.get("system_prompt") or ""
        assert len(system_prompt) >= baselines["standard_with_tools_system_prompt_min_chars"], (
            f"PRIMARY system prompt too short ({len(system_prompt)} chars). "
            "Tool-use guidance may have been stripped."
        )

    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_primary_call_excludes_routing_instructions(
        self, mock_client_class: Any
    ) -> None:
        """PRIMARY call must not receive routing classification instructions (ADR-0033: no router)."""
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_client_class.return_value = mock_client
        mock_client.respond.return_value = _make_llm_response("Answer.")

        orchestrator = Orchestrator()
        await orchestrator.handle_user_request(
            session_id="test-no-routing-instructions",
            user_message="What is Python?",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        primary_call = mock_client.respond.call_args_list[0]
        system_prompt = primary_call.kwargs.get("system_prompt") or ""
        assert "Relevant Past Conversations" not in system_prompt
        assert "Memory Graph" not in system_prompt


# ---------------------------------------------------------------------------
# Heuristic LLM-bypass end-to-end tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestHeuristicBypass:
    """High-confidence heuristic routes should bypass the router LLM call entirely."""

    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_high_confidence_heuristic_skips_router_llm(
        self, mock_client_class: Any, monkeypatch: Any
    ) -> None:
        """High-confidence heuristic must result in exactly one LLM call (no router LLM call)."""
        monkeypatch.setattr(settings, "routing_policy", "heuristic_then_llm")
        monkeypatch.setattr(settings, "routing_heuristic_threshold", 0.8)
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_client_class.return_value = mock_client
        mock_client.respond.return_value = _make_llm_response("Here is the fix.")

        orchestrator = Orchestrator()
        await orchestrator.handle_user_request(
            session_id="test-heuristic-bypass",
            user_message="Debug this stack trace and refactor the function",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        assert mock_client.respond.call_count == 1
        call = mock_client.respond.call_args_list[0]
        assert call.kwargs["role"] == ModelRole.PRIMARY
