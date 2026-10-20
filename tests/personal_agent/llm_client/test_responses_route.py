"""FRE-1627 — the chat-to-Responses translation behind the guarded Responses route.

Offline. The request side checks the kwargs handed to ``litellm.aresponses``. The
response side builds ``ResponsesAPIResponse`` objects from the documented API shape.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from litellm.types.llms.openai import ResponsesAPIResponse

from personal_agent.llm_client.responses_route import (
    routes_to_responses,
    to_chat_response,
    to_responses_request,
)
from personal_agent.llm_client.types import LLMClientError

_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search_memory",
        "description": "Search stored memory.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
}

_CLIENT = MagicMock(name="guarded_handler")


def _request(**extra: Any) -> dict[str, Any]:
    return {
        "model": "openai/gpt-6.1-sol",
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "paris?"},
        ],
        "max_tokens": 512,
        "api_key": "k",
        "client": object(),
        **extra,
    }


def _response(**fields: Any) -> ResponsesAPIResponse:
    body: dict[str, Any] = {
        "id": "resp_1",
        "object": "response",
        "created_at": 1760900000,
        "status": "completed",
        "model": "gpt-6.1-sol-2026-09-29",
        "output": [],
        "usage": {
            "input_tokens": 50,
            "output_tokens": 20,
            "total_tokens": 70,
            "input_tokens_details": {"cached_tokens": 10},
            "output_tokens_details": {"reasoning_tokens": 8},
        },
        **fields,
    }
    return ResponsesAPIResponse(**body)


class TestRoutesToResponses:
    def test_sol_with_a_tool_at_medium_is_bridged(self) -> None:
        assert routes_to_responses(_request(tools=[_TOOL], reasoning_effort="medium"))

    def test_sol_without_tools_stays_on_chat(self) -> None:
        assert not routes_to_responses(_request(reasoning_effort="medium"))


class TestToResponsesRequest:
    def test_the_mapped_fields(self) -> None:
        out = to_responses_request(
            _request(
                tools=[_TOOL],
                tool_choice="auto",
                reasoning_effort="medium",
                timeout=180,
                num_retries=2,
                api_base="https://api.example/v1",
            ),
            client=_CLIENT,
        )
        assert out["model"] == "openai/gpt-6.1-sol"
        assert out["instructions"] == "be brief"
        assert out["input"] == [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "paris?"}],
            }
        ]
        assert out["max_output_tokens"] == 512
        assert out["reasoning"] == {"effort": "medium"}
        assert out["tools"] == [
            {
                "type": "function",
                "name": "search_memory",
                "strict": False,
                "description": "Search stored memory.",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            }
        ]
        assert out["tool_choice"] == "auto"
        assert out["store"] is False
        assert out["client"] is _CLIENT
        assert (out["api_key"], out["api_base"], out["timeout"], out["num_retries"]) == (
            "k",
            "https://api.example/v1",
            180,
            2,
        )
        assert "messages" not in out
        assert "max_tokens" not in out

    def test_tool_history_becomes_function_call_items(self) -> None:
        messages = [
            {"role": "user", "content": "paris?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "search_memory", "arguments": '{"query": "paris"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "Paris is in France."},
        ]
        out = to_responses_request(_request(messages=messages), client=_CLIENT)
        assert [item["type"] for item in out["input"]] == [
            "message",
            "function_call",
            "function_call_output",
        ]
        assert out["input"][1]["call_id"] == out["input"][2]["call_id"] == "call_1"
        assert "instructions" not in out

    def test_a_declared_strict_tool_keeps_its_value(self) -> None:
        """Only an undeclared ``strict`` takes the chat default (false)."""
        strict_tool = {**_TOOL, "function": {**_TOOL["function"], "strict": True}}
        out = to_responses_request(_request(tools=[strict_tool]), client=_CLIENT)
        assert out["tools"][0]["strict"] is True

    def test_a_named_tool_choice_is_flattened(self) -> None:
        out = to_responses_request(
            _request(tool_choice={"type": "function", "function": {"name": "search_memory"}}),
            client=_CLIENT,
        )
        assert out["tool_choice"] == {"type": "function", "name": "search_memory"}

    def test_response_format_becomes_text_format(self) -> None:
        out = to_responses_request(
            _request(
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "x", "schema": {"type": "object"}},
                }
            ),
            client=_CLIENT,
        )
        assert out["text"]["format"]["type"] == "json_schema"
        assert out["text"]["format"]["name"] == "x"

    def test_a_system_only_request_carries_the_system_message_as_input(self) -> None:
        out = to_responses_request(
            _request(messages=[{"role": "system", "content": "only"}]), client=_CLIENT
        )
        assert out["input"] == [
            {
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": "only"}],
            }
        ]
        assert "instructions" not in out

    @pytest.mark.parametrize("key", ["seed", "frequency_penalty", "extra_headers", "thinking"])
    def test_an_unmapped_key_is_refused_not_dropped(self, key: str) -> None:
        with pytest.raises(LLMClientError, match=key):
            to_responses_request(_request(**{key: 1}), client=_CLIENT)


class TestToChatResponse:
    def test_a_function_call_becomes_a_tool_call(self) -> None:
        response = _response(
            output=[
                {"type": "reasoning", "id": "rs_1", "summary": []},
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "search_memory",
                    "arguments": '{"query": "paris"}',
                    "status": "completed",
                },
            ]
        )
        chat = to_chat_response(response)
        choice = chat.choices[0]
        assert choice.finish_reason == "tool_calls"
        assert choice.message.content is None
        [call] = choice.message.tool_calls
        assert (call.id, call.function.name, call.function.arguments) == (
            "call_1",
            "search_memory",
            '{"query": "paris"}',
        )
        assert chat.id == "resp_1"
        assert chat.model == "gpt-6.1-sol-2026-09-29"

    def test_usage_carries_cache_and_reasoning_tokens(self) -> None:
        usage = to_chat_response(_response()).usage
        assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (50, 20, 70)
        assert usage.prompt_tokens_details.cached_tokens == 10
        assert usage.completion_tokens_details.reasoning_tokens == 8

    def test_a_text_reply(self) -> None:
        response = _response(
            output=[
                {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": "It is ", "annotations": []},
                        {"type": "output_text", "text": "in France.", "annotations": []},
                    ],
                }
            ]
        )
        choice = to_chat_response(response).choices[0]
        assert choice.message.content == "It is in France."
        assert choice.message.tool_calls is None
        assert choice.finish_reason == "stop"

    def test_a_refusal_part_becomes_the_message_refusal(self) -> None:
        """Codex plan review: ADR-0150 D1 reads ``message.refusal``; the text is in ``refusal``."""
        response = _response(
            output=[
                {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "refusal", "refusal": "I cannot help with that."}],
                }
            ]
        )
        message = to_chat_response(response).choices[0].message
        assert message.refusal == "I cannot help with that."
        assert message.content is None

    def test_an_incomplete_reply_at_the_token_ceiling_finishes_with_length(self) -> None:
        response = _response(
            status="incomplete",
            incomplete_details={"reason": "max_output_tokens"},
            output=[{"type": "reasoning", "id": "rs_1", "summary": []}],
        )
        assert to_chat_response(response).choices[0].finish_reason == "length"

    def test_a_response_error_raises(self) -> None:
        response = _response(status="failed", error={"code": "server_error", "message": "boom"})
        with pytest.raises(LLMClientError, match="boom"):
            to_chat_response(response)
