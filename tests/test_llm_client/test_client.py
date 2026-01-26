"""Tests for LocalLLMClient."""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from personal_agent.config import ModelConfigError
from personal_agent.llm_client.client import LocalLLMClient
from personal_agent.llm_client.types import (
    LLMClientError,
    LLMConnectionError,
    LLMInvalidResponse,
    LLMRateLimit,
    LLMServerError,
    LLMTimeout,
    ModelRole,
)
from personal_agent.telemetry.trace import TraceContext


class TestLocalLLMClient:
    """Test LocalLLMClient class."""

    @pytest.fixture
    def mock_model_config(self, tmp_path: Path) -> Path:
        """Create a temporary model config file."""
        config_file = tmp_path / "models.yaml"
        config_file.write_text(
            """
models:
  router:
    id: "test-router"
    context_length: 8192
    quantization: "8bit"
    max_concurrency: 4
    default_timeout: 5
  reasoning:
    id: "test-reasoning"
    context_length: 32768
    quantization: "8bit"
    max_concurrency: 2
    default_timeout: 60
  coding:
    id: "test-coding"
    context_length: 32768
    quantization: "8bit"
    max_concurrency: 2
    default_timeout: 45
"""
        )
        return config_file

    @pytest.fixture
    def client(self, mock_model_config: Path) -> LocalLLMClient:
        """Create a LocalLLMClient instance."""
        return LocalLLMClient(
            base_url="http://localhost:1234/v1",
            timeout_seconds=30,
            max_retries=2,
            model_config_path=mock_model_config,
        )

    @pytest.mark.asyncio
    async def test_respond_success(self, client: LocalLLMClient) -> None:
        """Test successful LLM response."""
        # Responses API format (preferred per spec)
        mock_response = {
            "role": "assistant",
            "content": "Hello, world!",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 3,
                "total_tokens": 13,
            },
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response_obj = MagicMock()
            mock_response_obj.json.return_value = mock_response
            mock_response_obj.raise_for_status = MagicMock()
            mock_client.post = AsyncMock(return_value=mock_response_obj)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            response = await client.respond(
                role=ModelRole.ROUTER,
                messages=[{"role": "user", "content": "Hello"}],
                trace_ctx=trace_ctx,
            )

            assert response["content"] == "Hello, world!"
            assert response["role"] == "assistant"
            assert len(response["tool_calls"]) == 0
            assert response["usage"]["prompt_tokens"] == 10

    @pytest.mark.asyncio
    async def test_respond_with_system_prompt(self, client: LocalLLMClient) -> None:
        """Test response with system prompt."""
        # Responses API format
        mock_response = {
            "role": "assistant",
            "content": "OK",
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response_obj = MagicMock()
            mock_response_obj.json.return_value = mock_response
            mock_response_obj.raise_for_status = MagicMock()
            mock_client.post = AsyncMock(return_value=mock_response_obj)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            await client.respond(
                role=ModelRole.REASONING,
                messages=[{"role": "user", "content": "Test"}],
                system_prompt="You are a helpful assistant.",
                trace_ctx=trace_ctx,
            )

            # Verify system prompt was added to messages
            # Note: responses API uses "input" field, chat_completions uses "messages"
            call_args = mock_client.post.call_args
            payload = call_args[1]["json"]
            # Check which API format was used
            if "input" in payload:
                # Responses API format - system prompt should be in the input string
                assert "You are a helpful assistant." in payload["input"]
            else:
                # Chat completions format - system prompt should be first message
                assert payload["messages"][0]["role"] == "system"
                assert payload["messages"][0]["content"] == "You are a helpful assistant."

    @pytest.mark.asyncio
    async def test_respond_with_tools(self, client: LocalLLMClient) -> None:
        """Test response with tool calls."""
        # Responses API format
        mock_response = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_123",
                    "name": "read_file",
                    "arguments": '{"path": "/tmp/test.txt"}',
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response_obj = MagicMock()
            mock_response_obj.json.return_value = mock_response
            mock_response_obj.raise_for_status = MagicMock()
            mock_client.post = AsyncMock(return_value=mock_response_obj)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
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

            response = await client.respond(
                role=ModelRole.CODING,
                messages=[{"role": "user", "content": "Read a file"}],
                tools=tools,
                trace_ctx=trace_ctx,
            )

            assert len(response["tool_calls"]) == 1
            assert response["tool_calls"][0]["name"] == "read_file"

    @pytest.mark.asyncio
    async def test_respond_timeout(self, client: LocalLLMClient) -> None:
        """Test timeout handling."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("Timeout"))
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            with pytest.raises(LLMTimeout):
                await client.respond(
                    role=ModelRole.ROUTER,
                    messages=[{"role": "user", "content": "Test"}],
                    trace_ctx=trace_ctx,
                )

    @pytest.mark.asyncio
    async def test_respond_connection_error(self, client: LocalLLMClient) -> None:
        """Test connection error handling."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Connection failed"))
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            with pytest.raises(LLMConnectionError):
                await client.respond(
                    role=ModelRole.ROUTER,
                    messages=[{"role": "user", "content": "Test"}],
                    trace_ctx=trace_ctx,
                )

    @pytest.mark.asyncio
    async def test_respond_rate_limit(self, client: LocalLLMClient) -> None:
        """Test rate limit error handling."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response_obj = MagicMock()
            mock_response_obj.status_code = 429
            mock_response_obj.raise_for_status.side_effect = httpx.HTTPStatusError(
                "Rate limit", request=MagicMock(), response=mock_response_obj
            )
            mock_client.post = AsyncMock(return_value=mock_response_obj)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            with pytest.raises(LLMRateLimit):
                await client.respond(
                    role=ModelRole.ROUTER,
                    messages=[{"role": "user", "content": "Test"}],
                    trace_ctx=trace_ctx,
                )

    @pytest.mark.asyncio
    async def test_respond_server_error(self, client: LocalLLMClient) -> None:
        """Test server error handling."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response_obj = MagicMock()
            mock_response_obj.status_code = 500
            mock_response_obj.raise_for_status.side_effect = httpx.HTTPStatusError(
                "Server error", request=MagicMock(), response=mock_response_obj
            )
            mock_client.post = AsyncMock(return_value=mock_response_obj)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            with pytest.raises(LLMServerError):
                await client.respond(
                    role=ModelRole.ROUTER,
                    messages=[{"role": "user", "content": "Test"}],
                    trace_ctx=trace_ctx,
                )

    @pytest.mark.asyncio
    async def test_respond_invalid_response(self, client: LocalLLMClient) -> None:
        """Test invalid response handling."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response_obj = MagicMock()
            # Invalid responses format - tool_calls contains non-dict items that will cause error
            mock_response_obj.json.return_value = {
                "role": "assistant",
                "content": "Test",
                "tool_calls": [
                    {"id": "call_1", "name": "test", "arguments": "{}"},  # Valid
                    "invalid_tool_call",  # Invalid - not a dict, will cause error in adapter
                ],
            }
            mock_response_obj.raise_for_status = MagicMock()
            mock_client.post = AsyncMock(return_value=mock_response_obj)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            # The adapter should handle this gracefully (skips invalid items)
            # So we test with a response that's completely malformed
            mock_response_obj.json.return_value = None  # None response will cause error
            with pytest.raises((LLMInvalidResponse, LLMClientError)):
                await client.respond(
                    role=ModelRole.ROUTER,
                    messages=[{"role": "user", "content": "Test"}],
                    trace_ctx=trace_ctx,
                )

    @pytest.mark.asyncio
    async def test_respond_retry_on_timeout(self, client: LocalLLMClient) -> None:
        """Test that client retries on timeout."""
        # Responses API format
        mock_response = {
            "role": "assistant",
            "content": "Success",
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }

        with patch("httpx.AsyncClient") as mock_client_class, patch("asyncio.sleep") as mock_sleep:
            mock_client = AsyncMock()
            # First call times out, second succeeds
            mock_response_obj = MagicMock()
            mock_response_obj.json.return_value = mock_response
            mock_response_obj.raise_for_status = MagicMock()
            mock_client.post = AsyncMock(
                side_effect=[
                    httpx.TimeoutException("Timeout"),
                    mock_response_obj,
                ]
            )
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            response = await client.respond(
                role=ModelRole.ROUTER,
                messages=[{"role": "user", "content": "Test"}],
                trace_ctx=trace_ctx,
            )

            assert response["content"] == "Success"
            assert mock_client.post.call_count == 2
            assert mock_sleep.call_count == 1  # One retry

    def test_missing_model_config(self, tmp_path: Path) -> None:
        """Test that missing model config uses defaults."""
        config_file = tmp_path / "nonexistent.yaml"

        # Client should handle missing config gracefully with defaults
        client = LocalLLMClient(model_config_path=config_file)
        assert client.model_configs == {}

    def test_invalid_model_config(self, tmp_path: Path) -> None:
        """Test that invalid model config uses defaults."""
        config_file = tmp_path / "invalid.yaml"
        config_file.write_text("invalid: yaml: content: [unclosed")

        # Client should handle invalid config gracefully with defaults
        client = LocalLLMClient(model_config_path=config_file)
        assert client.model_configs == {}

    @pytest.mark.asyncio
    async def test_missing_role_config(self, client: LocalLLMClient) -> None:
        """Test that missing role in config raises error."""
        trace_ctx = TraceContext.new_trace()

        # Create a client with empty configs to test missing role
        client_empty = LocalLLMClient(
            base_url="http://localhost:1234/v1",
            model_config_path=Path("/nonexistent.yaml"),
        )

        with pytest.raises(ModelConfigError, match="No configuration found for role"):
            await client_empty.respond(
                role=ModelRole.ROUTER,  # Router not in empty config
                messages=[{"role": "user", "content": "Test"}],
                trace_ctx=trace_ctx,
            )

    @pytest.mark.asyncio
    async def test_automatic_fallback_404(self, client: LocalLLMClient) -> None:
        """Test automatic fallback from responses to chat_completions on 404."""
        # Mock response for chat_completions (fallback)
        mock_response = {
            "choices": [{"message": {"role": "assistant", "content": "Fallback success"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response_obj = MagicMock()
            mock_response_obj.json.return_value = mock_response
            mock_response_obj.raise_for_status = MagicMock()

            # First call to /responses returns 404, second to /chat/completions succeeds
            mock_response_404 = MagicMock()
            mock_response_404.status_code = 404
            mock_response_404.raise_for_status.side_effect = httpx.HTTPStatusError(
                "Not found", request=MagicMock(), response=mock_response_404
            )

            call_count = 0

            async def mock_post(url: str, **kwargs: Any) -> Any:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    # First call to responses endpoint - return 404
                    raise httpx.HTTPStatusError(
                        "Not found", request=MagicMock(), response=mock_response_404
                    )
                else:
                    # Second call to chat_completions - succeed
                    return mock_response_obj

            mock_client.post = AsyncMock(side_effect=mock_post)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            response = await client.respond(
                role=ModelRole.ROUTER,
                messages=[{"role": "user", "content": "Test"}],
                trace_ctx=trace_ctx,
            )

            # Should succeed using fallback
            assert response["content"] == "Fallback success"
            assert call_count == 2  # Tried responses, then chat_completions

    @pytest.mark.asyncio
    async def test_per_model_endpoint(self, tmp_path: Path) -> None:
        """Test that models can use different endpoints/providers."""
        # Create config with per-model endpoints
        config_file = tmp_path / "models.yaml"
        config_file.write_text(
            """
models:
  router:
    id: "test-router"
    endpoint: "http://localhost:8001/v1"
    context_length: 8192
    quantization: "8bit"
    max_concurrency: 4
    default_timeout: 5
  reasoning:
    id: "test-reasoning"
    endpoint: "http://localhost:8002/v1"
    context_length: 32768
    quantization: "8bit"
    max_concurrency: 2
    default_timeout: 60
  coding:
    id: "test-coding"
    # No endpoint - uses base_url
    context_length: 32768
    quantization: "8bit"
    max_concurrency: 2
    default_timeout: 45
"""
        )

        client = LocalLLMClient(
            base_url="http://localhost:1234/v1",
            model_config_path=config_file,
        )

        # Mock response
        mock_response = {
            "role": "assistant",
            "content": "Success",
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response_obj = MagicMock()
            mock_response_obj.json.return_value = mock_response
            mock_response_obj.raise_for_status = MagicMock()
            mock_client.post = AsyncMock(return_value=mock_response_obj)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()

            # Test router with custom endpoint
            await client.respond(
                role=ModelRole.ROUTER,
                messages=[{"role": "user", "content": "Test"}],
                trace_ctx=trace_ctx,
            )
            # Verify it used the custom endpoint
            call_args = mock_client.post.call_args
            assert "http://localhost:8001/v1/responses" in str(call_args)

            # Test coding without endpoint (uses base_url)
            await client.respond(
                role=ModelRole.CODING,
                messages=[{"role": "user", "content": "Test"}],
                trace_ctx=trace_ctx,
            )
            # Verify it used the base_url endpoint
            call_args = mock_client.post.call_args
            assert "http://localhost:1234/v1/responses" in str(call_args)

    @pytest.mark.asyncio
    async def test_automatic_fallback_connection_error(self, client: LocalLLMClient) -> None:
        """Test automatic fallback from responses to chat_completions on connection error."""
        # Mock response for chat_completions (fallback)
        mock_response = {
            "choices": [{"message": {"role": "assistant", "content": "Fallback success"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response_obj = MagicMock()
            mock_response_obj.json.return_value = mock_response
            mock_response_obj.raise_for_status = MagicMock()

            call_count = 0

            async def mock_post(url: str, **kwargs: Any) -> Any:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    # First call to responses endpoint - connection error
                    raise httpx.ConnectError("Connection refused")
                else:
                    # Second call to chat_completions - succeed
                    return mock_response_obj

            mock_client.post = AsyncMock(side_effect=mock_post)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            response = await client.respond(
                role=ModelRole.ROUTER,
                messages=[{"role": "user", "content": "Test"}],
                trace_ctx=trace_ctx,
            )

            # Should succeed using fallback
            assert response["content"] == "Fallback success"
            assert call_count == 2  # Tried responses, then chat_completions
