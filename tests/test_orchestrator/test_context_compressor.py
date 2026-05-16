"""Tests for context compressor — summarizes evicted conversation turns."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.orchestrator.context_compressor import (
    _COMPRESSOR_SYSTEM_PROMPT,
    FALLBACK_MARKER,
    _format_messages_for_compression,
    compress_turns,
)


class TestSystemPromptCacheEligibility:
    """The compressor system prompt must be ≥1024 tokens.

    OpenAI prompt caching activates when the prompt prefix is at least
    1024 tokens. The compressor's user message is fully variable (it is
    the evicted-turn payload), so the only stable cacheable region is the
    system message. Keeping it above 1024 tokens unlocks automatic
    caching on every compressor call after the first.

    See FRE-365 diagnostic (2026-05-16): before this lock-in the system
    prompt was ~188 tokens and every gpt-5.4-nano compressor call missed
    cache despite 50%+ of calls being eligible by total prompt size.
    """

    def test_system_prompt_is_cache_eligible_under_tiktoken(self) -> None:
        """Token count under tiktoken must be ≥1024.

        tiktoken is OpenAI's authoritative tokenizer for cache
        eligibility; the project's char-based estimator is only a fast
        approximation.
        """
        tiktoken = pytest.importorskip("tiktoken")
        encoding = tiktoken.encoding_for_model("gpt-4o")
        token_count = len(encoding.encode(_COMPRESSOR_SYSTEM_PROMPT))
        assert token_count >= 1024, (
            f"Compressor system prompt is {token_count} tokens — below the "
            "1024-token OpenAI cache-eligibility floor. Cache hits will fail."
        )

    def test_system_prompt_is_stable_across_invocations(self) -> None:
        """The constant must not be mutated at import or call time.

        Cache hits depend on byte-identical prefixes between requests; any
        mutation (timestamp interpolation, machine-specific path injection,
        randomised example shuffling) would defeat caching.
        """
        from personal_agent.orchestrator import context_compressor

        first = context_compressor._COMPRESSOR_SYSTEM_PROMPT
        # Re-import to simulate a fresh request boundary
        import importlib

        importlib.reload(context_compressor)
        second = context_compressor._COMPRESSOR_SYSTEM_PROMPT
        assert first == second


def _msg(role: str, content: str) -> dict[str, Any]:
    return {"role": role, "content": content}


class TestFormatMessages:
    def test_formats_user_and_assistant(self) -> None:
        messages = [
            _msg("user", "What database should we use?"),
            _msg("assistant", "I recommend PostgreSQL for this use case."),
        ]
        result = _format_messages_for_compression(messages)
        assert "[user]: What database should we use?" in result
        assert "[assistant]: I recommend PostgreSQL" in result

    def test_skips_empty_content(self) -> None:
        messages = [_msg("user", ""), _msg("assistant", "Hello")]
        result = _format_messages_for_compression(messages)
        assert "[user]" not in result
        assert "[assistant]: Hello" in result

    def test_empty_list(self) -> None:
        assert _format_messages_for_compression([]) == ""


class TestCompressTurns:
    @pytest.mark.asyncio
    async def test_returns_summary_on_success(self) -> None:
        mock_response = {
            "role": "assistant",
            "content": "## Conversation Summary\n- **Decisions:** Use PostgreSQL",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {},
            "response_id": None,
            "raw": {},
        }
        mock_client = AsyncMock()
        mock_client.respond.return_value = mock_response

        with patch(
            "personal_agent.orchestrator.context_compressor.get_llm_client",
            return_value=mock_client,
        ):
            result = await compress_turns(
                [_msg("user", "Use PostgreSQL"), _msg("assistant", "OK")],
                trace_id="test-trace",
            )

        assert "Conversation Summary" in result
        assert "PostgreSQL" in result

    @pytest.mark.asyncio
    async def test_returns_fallback_on_empty_response(self) -> None:
        mock_response = {
            "role": "assistant",
            "content": "",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {},
            "response_id": None,
            "raw": {},
        }
        mock_client = AsyncMock()
        mock_client.respond.return_value = mock_response

        with patch(
            "personal_agent.orchestrator.context_compressor.get_llm_client",
            return_value=mock_client,
        ):
            result = await compress_turns(
                [_msg("user", "Hello")],
                trace_id="test-trace",
            )

        assert result == FALLBACK_MARKER

    @pytest.mark.asyncio
    async def test_returns_fallback_on_llm_error(self) -> None:
        from personal_agent.llm_client.types import LLMClientError

        mock_client = AsyncMock()
        mock_client.respond.side_effect = LLMClientError("timeout")

        with patch(
            "personal_agent.orchestrator.context_compressor.get_llm_client",
            return_value=mock_client,
        ):
            result = await compress_turns(
                [_msg("user", "Hello")],
                trace_id="test-trace",
            )

        assert result == FALLBACK_MARKER

    @pytest.mark.asyncio
    async def test_returns_fallback_on_unexpected_error(self) -> None:
        mock_client = AsyncMock()
        mock_client.respond.side_effect = RuntimeError("unexpected")

        with patch(
            "personal_agent.orchestrator.context_compressor.get_llm_client",
            return_value=mock_client,
        ):
            result = await compress_turns(
                [_msg("user", "Hello")],
                trace_id="test-trace",
            )

        assert result == FALLBACK_MARKER

    @pytest.mark.asyncio
    async def test_returns_fallback_for_empty_messages(self) -> None:
        result = await compress_turns([], trace_id="test-trace")
        assert result == FALLBACK_MARKER

    @pytest.mark.asyncio
    async def test_missing_compressor_role_returns_fallback_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns FALLBACK_MARKER immediately and warns only once when role is absent."""
        import personal_agent.orchestrator.context_compressor as cc_module

        monkeypatch.setattr(cc_module, "_compressor_role_missing_logged", False)

        mock_config = MagicMock()
        mock_config.models = {}

        with patch(
            "personal_agent.orchestrator.context_compressor.load_model_config",
            return_value=mock_config,
        ):
            result1 = await compress_turns([_msg("user", "hello")], trace_id="t-1")
            result2 = await compress_turns([_msg("user", "world")], trace_id="t-2")

        assert result1 == FALLBACK_MARKER
        assert result2 == FALLBACK_MARKER
        assert cc_module._compressor_role_missing_logged is True
