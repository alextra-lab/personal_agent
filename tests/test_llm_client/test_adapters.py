"""Tests for LLM API adapters."""

from typing import Any

import pytest

from personal_agent.llm_client.adapters import (
    adapt_chat_completions_response,
    adapt_responses_response,
    build_chat_completions_request,
    build_responses_request,
)
from personal_agent.llm_client.types import LLMInvalidResponse


class TestAdaptResponsesResponse:
    """Test responses API response adapter."""

    def test_basic_response(self) -> None:
        """Test adapting a basic responses API response."""
        response_data = {
            "role": "assistant",
            "content": "Hello, world!",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 3,
                "total_tokens": 13,
            },
        }

        result = adapt_responses_response(response_data)

        assert result["role"] == "assistant"
        assert result["content"] == "Hello, world!"
        assert result["tool_calls"] == []
        assert result["reasoning_trace"] is None
        assert result["usage"]["prompt_tokens"] == 10
        assert result["usage"]["completion_tokens"] == 3
        assert result["raw"] == response_data

    def test_response_with_reasoning_trace(self) -> None:
        """Test adapting a response with reasoning trace (responses API feature)."""
        response_data = {
            "role": "assistant",
            "content": "The answer is 42",
            "reasoning_trace": "<thinking>Let me calculate...</thinking>",
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 5,
                "total_tokens": 25,
            },
        }

        result = adapt_responses_response(response_data)

        assert result["content"] == "The answer is 42"
        assert result["reasoning_trace"] == "<thinking>Let me calculate...</thinking>"

    def test_response_with_tool_calls(self) -> None:
        """Test adapting a response with tool calls."""
        response_data = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_123",
                    "name": "read_file",
                    "arguments": '{"path": "/tmp/test.txt"}',
                }
            ],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 5,
                "total_tokens": 25,
            },
        }

        result = adapt_responses_response(response_data)

        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["id"] == "call_123"
        assert result["tool_calls"][0]["name"] == "read_file"
        assert result["tool_calls"][0]["arguments"] == '{"path": "/tmp/test.txt"}'

    def test_response_without_usage(self) -> None:
        """Test that missing usage defaults to zero tokens."""
        response_data = {
            "role": "assistant",
            "content": "Test",
        }

        result = adapt_responses_response(response_data)

        assert result["usage"]["prompt_tokens"] == 0
        assert result["usage"]["completion_tokens"] == 0
        assert result["usage"]["total_tokens"] == 0


class TestAdaptChatCompletionsResponse:
    """Test chat_completions response adapter."""

    def test_basic_response(self) -> None:
        """Test adapting a basic chat_completions response."""
        response_data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Hello, world!",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 3,
                "total_tokens": 13,
            },
        }

        result = adapt_chat_completions_response(response_data)

        assert result["role"] == "assistant"
        assert result["content"] == "Hello, world!"
        assert result["tool_calls"] == []
        assert result["reasoning_trace"] is None
        assert result["usage"]["prompt_tokens"] == 10
        assert result["usage"]["completion_tokens"] == 3
        assert result["raw"] == response_data

    def test_response_with_tool_calls(self) -> None:
        """Test adapting a response with tool calls."""
        response_data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path": "/tmp/test.txt"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 5,
                "total_tokens": 25,
            },
        }

        result = adapt_chat_completions_response(response_data)

        assert result["role"] == "assistant"
        assert result["content"] == ""
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["id"] == "call_123"
        assert result["tool_calls"][0]["name"] == "read_file"
        assert result["tool_calls"][0]["arguments"] == '{"path": "/tmp/test.txt"}'

    def test_response_with_empty_choices(self) -> None:
        """Test that empty choices raises LLMInvalidResponse."""
        response_data: dict[str, Any] = {"choices": []}

        with pytest.raises(LLMInvalidResponse, match="no choices"):
            adapt_chat_completions_response(response_data)

    def test_response_missing_choices(self) -> None:
        """Test that missing choices raises LLMInvalidResponse."""
        response_data: dict[str, Any] = {}

        with pytest.raises(LLMInvalidResponse, match="no choices"):
            adapt_chat_completions_response(response_data)

    def test_response_without_usage(self) -> None:
        """Test that missing usage defaults to zero tokens."""
        response_data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Test",
                    }
                }
            ]
        }

        result = adapt_chat_completions_response(response_data)

        assert result["usage"]["prompt_tokens"] == 0
        assert result["usage"]["completion_tokens"] == 0
        assert result["usage"]["total_tokens"] == 0


class TestBuildResponsesRequest:
    """Test building responses API request payload."""

    def test_basic_request(self) -> None:
        """Test building a basic request."""
        messages = [{"role": "user", "content": "Hello"}]
        payload = build_responses_request(messages=messages, model="test-model")

        assert payload["model"] == "test-model"
        assert payload["input"] == "Hello"  # Responses API uses "input" field, not "messages"
        assert "tools" not in payload
        assert "max_tokens" not in payload
        assert "temperature" not in payload

    def test_request_with_tools(self) -> None:
        """Test building a request with tools."""
        messages = [{"role": "user", "content": "Hello"}]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        payload = build_responses_request(messages=messages, model="test-model", tools=tools)

        assert payload["tools"] == tools
        assert payload["tool_choice"] == "auto"

    def test_request_with_tool_choice(self) -> None:
        """Test building a request with explicit tool_choice."""
        messages = [{"role": "user", "content": "Hello"}]
        tools = [{"type": "function", "function": {"name": "read_file"}}]

        payload = build_responses_request(
            messages=messages, model="test-model", tools=tools, tool_choice="none"
        )

        assert payload["tool_choice"] == "none"

    def test_request_with_max_tokens(self) -> None:
        """Test building a request with max_tokens."""
        messages = [{"role": "user", "content": "Hello"}]
        payload = build_responses_request(messages=messages, model="test-model", max_tokens=100)

        assert payload["max_tokens"] == 100

    def test_request_with_temperature(self) -> None:
        """Test building a request with temperature."""
        messages = [{"role": "user", "content": "Hello"}]
        payload = build_responses_request(messages=messages, model="test-model", temperature=0.7)

        assert payload["temperature"] == 0.7


class TestBuildChatCompletionsRequest:
    """Test building chat_completions request payload."""

    def test_basic_request(self) -> None:
        """Test building a basic request."""
        messages = [{"role": "user", "content": "Hello"}]
        payload = build_chat_completions_request(messages=messages, model="test-model")

        assert payload["model"] == "test-model"
        assert payload["messages"] == messages
        assert "tools" not in payload
        assert "max_tokens" not in payload
        assert "temperature" not in payload

    def test_request_with_tools(self) -> None:
        """Test building a request with tools."""
        messages = [{"role": "user", "content": "Hello"}]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        payload = build_chat_completions_request(messages=messages, model="test-model", tools=tools)

        assert payload["tools"] == tools
        assert payload["tool_choice"] == "auto"

    def test_request_with_tool_choice(self) -> None:
        """Test building a request with explicit tool_choice."""
        messages = [{"role": "user", "content": "Hello"}]
        tools = [{"type": "function", "function": {"name": "read_file"}}]

        payload = build_chat_completions_request(
            messages=messages, model="test-model", tools=tools, tool_choice="none"
        )

        assert payload["tool_choice"] == "none"

    def test_request_with_max_tokens(self) -> None:
        """Test building a request with max_tokens."""
        messages = [{"role": "user", "content": "Hello"}]
        payload = build_chat_completions_request(
            messages=messages, model="test-model", max_tokens=100
        )

        assert payload["max_tokens"] == 100

    def test_request_with_temperature(self) -> None:
        """Test building a request with temperature."""
        messages = [{"role": "user", "content": "Hello"}]
        payload = build_chat_completions_request(
            messages=messages, model="test-model", temperature=0.7
        )

        assert payload["temperature"] == 0.7

    def test_request_with_response_format(self) -> None:
        """Test building a request with response_format."""
        messages = [{"role": "user", "content": "Hello"}]
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": "test_schema", "schema": {"type": "object"}},
        }
        payload = build_chat_completions_request(
            messages=messages, model="test-model", response_format=response_format
        )

        assert payload["response_format"] == response_format
