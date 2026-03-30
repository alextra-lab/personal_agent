"""Tests for context compressor — summarizes evicted conversation turns."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.orchestrator.context_compressor import (
    FALLBACK_MARKER,
    _format_messages_for_compression,
    compress_turns,
)


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
