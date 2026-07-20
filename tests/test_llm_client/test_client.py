"""Tests for LocalLLMClient."""

import json
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


def _stream_mock_for_response(response: dict[str, Any]) -> MagicMock:
    """Build a streaming-shaped httpx mock from a non-streaming response dict.

    The client now calls ``async with client.stream("POST", ...) as resp:``
    and reads SSE lines via ``resp.aiter_lines()``. This helper converts a
    final response dict into a one-shot stream that emits the full message
    in a single chunk, then ``[DONE]``. Sufficient to test orchestration —
    the streaming aggregator itself has dedicated tests.
    """
    choice = response.get("choices", [{}])[0]
    msg = choice.get("message", {})
    delta = {k: v for k, v in msg.items() if v is not None}
    chunk = {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": choice.get("finish_reason", "stop"),
            }
        ],
        "usage": response.get("usage"),
    }
    lines = [f"data: {json.dumps(chunk)}", "data: [DONE]"]

    async def aiter_lines() -> Any:
        for line in lines:
            yield line

    response_obj = MagicMock()
    response_obj.raise_for_status = MagicMock()
    response_obj.aiter_lines = aiter_lines

    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=response_obj)
    stream_cm.__aexit__ = AsyncMock(return_value=None)
    return stream_cm


def _stream_mock_raising(exc: Exception) -> MagicMock:
    """Streaming context manager that raises during the response phase.

    Suitable for simulating connect/timeout failures and HTTP errors that
    surface via ``raise_for_status`` mid-stream.
    """
    response_obj = MagicMock()
    response_obj.raise_for_status = MagicMock(side_effect=exc)

    async def aiter_lines() -> Any:
        if False:  # pragma: no cover — generator never advances
            yield ""

    response_obj.aiter_lines = aiter_lines

    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=response_obj)
    stream_cm.__aexit__ = AsyncMock(return_value=None)
    return stream_cm


class TestLocalLLMClient:
    """Test LocalLLMClient class."""

    @pytest.fixture
    def mock_model_config(self, tmp_path: Path) -> Path:
        """Create a temporary model config file."""
        config_file = tmp_path / "models.yaml"
        config_file.write_text(
            """
models:
  primary:
    id: "test-primary"
    context_length: 32768
    quantization: "8bit"
    max_concurrency: 2
    default_timeout: 60
  sub_agent:
    id: "test-sub-agent"
    context_length: 32768
    quantization: "8bit"
    max_concurrency: 4
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
        mock_response = {
            "choices": [{"message": {"role": "assistant", "content": "Hello, world!"}}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 3,
                "total_tokens": 13,
            },
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.stream = MagicMock(return_value=_stream_mock_for_response(mock_response))
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            response = await client.respond(
                role=ModelRole.PRIMARY,
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
        mock_response = {
            "choices": [{"message": {"role": "assistant", "content": "OK"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.stream = MagicMock(return_value=_stream_mock_for_response(mock_response))
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "Test"}],
                system_prompt="You are a helpful assistant.",
                trace_ctx=trace_ctx,
            )

            call_args = mock_client.stream.call_args
            payload = call_args[1]["json"]
            assert payload["messages"][0]["role"] == "system"
            assert payload["messages"][0]["content"] == "You are a helpful assistant."

    @pytest.fixture
    def tunnel_client(self, mock_model_config: Path) -> LocalLLMClient:
        """Client pointing at the SLM Cloudflare tunnel hostname."""
        return LocalLLMClient(
            base_url="https://slm.example.com/v1",
            timeout_seconds=30,
            max_retries=1,
            model_config_path=mock_model_config,
        )

    @pytest.mark.asyncio
    async def test_respond_sends_trace_headers(self, client: LocalLLMClient) -> None:
        """X-Trace-Id, X-Span-Id, and X-Session-Id are sent on every SLM call."""
        mock_response = {
            "choices": [{"message": {"role": "assistant", "content": "OK"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.stream = MagicMock(return_value=_stream_mock_for_response(mock_response))
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace(session_id="sess-abc")
            await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "hi"}],
                trace_ctx=trace_ctx,
            )

            headers = mock_client.stream.call_args[1]["headers"]
            assert headers["X-Trace-Id"] == str(trace_ctx.trace_id)
            assert "X-Span-Id" in headers
            assert headers["X-Session-Id"] == "sess-abc"
            assert "CF-Access-Client-Id" not in headers

    @pytest.mark.asyncio
    async def test_respond_sends_cf_access_headers_with_trace_on_tunnel(
        self, tunnel_client: LocalLLMClient
    ) -> None:
        """CF-Access headers coexist with trace headers on the tunnel hostname."""
        mock_response = {
            "choices": [{"message": {"role": "assistant", "content": "OK"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }
        with (
            patch("httpx.AsyncClient") as mock_client_class,
            patch("personal_agent.llm_client.client.settings") as mock_settings,
        ):
            mock_settings.cf_access_client_id = "test-client-id"
            mock_settings.cf_access_client_secret = "test-client-secret"
            mock_settings.slm_tunnel_base_url = "https://slm.example.com"
            mock_client = AsyncMock()
            mock_client.stream = MagicMock(return_value=_stream_mock_for_response(mock_response))
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace(session_id="sess-xyz")
            await tunnel_client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "hi"}],
                trace_ctx=trace_ctx,
            )

            headers = mock_client.stream.call_args[1]["headers"]
            assert headers["X-Trace-Id"] == str(trace_ctx.trace_id)
            assert "X-Span-Id" in headers
            assert headers["X-Session-Id"] == "sess-xyz"
            assert headers["CF-Access-Client-Id"] == "test-client-id"
            assert headers["CF-Access-Client-Secret"] == "test-client-secret"

    @pytest.mark.asyncio
    async def test_respond_with_tools(self, client: LocalLLMClient) -> None:
        """Test response with tool calls."""
        mock_response = {
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
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.stream = MagicMock(return_value=_stream_mock_for_response(mock_response))
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
                role=ModelRole.PRIMARY,
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
            mock_client.stream = MagicMock(
                return_value=_stream_mock_raising(httpx.TimeoutException("Timeout"))
            )
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            with pytest.raises(LLMTimeout):
                await client.respond(
                    role=ModelRole.PRIMARY,
                    messages=[{"role": "user", "content": "Test"}],
                    trace_ctx=trace_ctx,
                )

    @pytest.mark.asyncio
    async def test_respond_error_logs_session_id(self, client: LocalLLMClient) -> None:
        """FRE-552: model_call_error carries session_id from the trace context.

        Patches the module logger (capturing log.error calls); capture_logs()
        is unreliable under the shared suite due to structlog logger caching.
        """
        calls: list[tuple[str, dict]] = []
        mock_log = MagicMock()
        mock_log.error = MagicMock(side_effect=lambda event, **kw: calls.append((event, kw)))
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.stream = MagicMock(
                return_value=_stream_mock_raising(httpx.TimeoutException("Timeout"))
            )
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace(session_id="sess-552")
            with patch("personal_agent.llm_client.client.log", mock_log):
                with pytest.raises(LLMTimeout):
                    await client.respond(
                        role=ModelRole.PRIMARY,
                        messages=[{"role": "user", "content": "Test"}],
                        trace_ctx=trace_ctx,
                    )
        errors = [kw for event, kw in calls if event == "model_call_error"]
        assert errors, "expected a model_call_error event"
        assert errors[0]["session_id"] == "sess-552"

    @pytest.mark.asyncio
    async def test_respond_connection_error(self, client: LocalLLMClient) -> None:
        """Test connection error handling."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.stream = MagicMock(
                return_value=_stream_mock_raising(httpx.ConnectError("Connection failed"))
            )
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            with pytest.raises(LLMConnectionError):
                await client.respond(
                    role=ModelRole.PRIMARY,
                    messages=[{"role": "user", "content": "Test"}],
                    trace_ctx=trace_ctx,
                )

    @pytest.mark.asyncio
    async def test_respond_rate_limit(self, client: LocalLLMClient) -> None:
        """Test rate limit error handling."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            err_response = MagicMock(status_code=429)
            mock_client.stream = MagicMock(
                return_value=_stream_mock_raising(
                    httpx.HTTPStatusError("Rate limit", request=MagicMock(), response=err_response)
                )
            )
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            with pytest.raises(LLMRateLimit):
                await client.respond(
                    role=ModelRole.PRIMARY,
                    messages=[{"role": "user", "content": "Test"}],
                    trace_ctx=trace_ctx,
                )

    @pytest.mark.asyncio
    async def test_respond_server_error(self, client: LocalLLMClient) -> None:
        """Test server error handling."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            err_response = MagicMock(status_code=500)
            mock_client.stream = MagicMock(
                return_value=_stream_mock_raising(
                    httpx.HTTPStatusError(
                        "Server error", request=MagicMock(), response=err_response
                    )
                )
            )
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            with pytest.raises(LLMServerError):
                await client.respond(
                    role=ModelRole.PRIMARY,
                    messages=[{"role": "user", "content": "Test"}],
                    trace_ctx=trace_ctx,
                )

    @pytest.mark.asyncio
    async def test_respond_invalid_response(self, client: LocalLLMClient) -> None:
        """An empty stream (no chunks at all) is invalid."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()

            # Stream that emits zero chunks → aggregator raises LLMInvalidResponse.
            async def aiter_lines() -> Any:
                if False:  # pragma: no cover
                    yield ""

            response_obj = MagicMock()
            response_obj.raise_for_status = MagicMock()
            response_obj.aiter_lines = aiter_lines
            stream_cm = MagicMock()
            stream_cm.__aenter__ = AsyncMock(return_value=response_obj)
            stream_cm.__aexit__ = AsyncMock(return_value=None)
            mock_client.stream = MagicMock(return_value=stream_cm)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            with pytest.raises((LLMInvalidResponse, LLMClientError)):
                await client.respond(
                    role=ModelRole.PRIMARY,
                    messages=[{"role": "user", "content": "Test"}],
                    trace_ctx=trace_ctx,
                )

    @pytest.mark.asyncio
    async def test_respond_retry_on_timeout(self, client: LocalLLMClient) -> None:
        """Test that client retries on timeout."""
        mock_response = {
            "choices": [{"message": {"role": "assistant", "content": "Success"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }

        with patch("httpx.AsyncClient") as mock_client_class, patch("asyncio.sleep") as mock_sleep:
            mock_client = AsyncMock()
            # First call times out, second succeeds.
            mock_client.stream = MagicMock(
                side_effect=[
                    _stream_mock_raising(httpx.TimeoutException("Timeout")),
                    _stream_mock_for_response(mock_response),
                ]
            )
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            response = await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "Test"}],
                trace_ctx=trace_ctx,
            )

            assert response["content"] == "Success"
            assert mock_client.stream.call_count == 2
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
                role=ModelRole.PRIMARY,  # Router not in empty config
                messages=[{"role": "user", "content": "Test"}],
                trace_ctx=trace_ctx,
            )

    @pytest.mark.asyncio
    async def test_respond_artifact_builder_resolves_via_selection(
        self, client: LocalLLMClient
    ) -> None:
        """ADR-0118 T1 / FRE-879 regression, carried forward by ADR-0121 T5 / FRE-920:
        role=ARTIFACT_BUILDER must not look itself up in model_configs directly —
        this fixture's catalog intentionally has no "artifact_builder" key (it is an
        open role resolved via the selection context, not a bare model_configs
        entry). With a per-turn selection naming "sub_agent", respond() must resolve
        to the "sub_agent" config entry and reach the HTTP call, not raise
        ModelConfigError. (Code-review caught this on FRE-879: the original wiring
        called self.model_configs.get(role.value) directly, which broke every
        artifact_draft call once the resolved key stopped matching the role name.)
        """
        from personal_agent.config.selection import (
            reset_current_selection,
            set_current_selection,
        )

        mock_response = {
            "choices": [{"message": {"role": "assistant", "content": "<html></html>"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }
        token = set_current_selection({"artifact_builder": "sub_agent"})
        try:
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_client = AsyncMock()
                mock_client.stream = MagicMock(
                    return_value=_stream_mock_for_response(mock_response)
                )
                mock_client_class.return_value.__aenter__.return_value = mock_client

                response = await client.respond(
                    role=ModelRole.ARTIFACT_BUILDER,
                    messages=[{"role": "user", "content": "Generate HTML"}],
                    trace_ctx=TraceContext.new_trace(),
                )

            assert response["content"] == "<html></html>"
        finally:
            reset_current_selection(token)

    @pytest.mark.asyncio
    async def test_404_raises_client_error(self, client: LocalLLMClient) -> None:
        """Test that 404 from server raises LLMClientError (no retry for 4xx)."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            err_response = MagicMock(status_code=404)
            mock_client.stream = MagicMock(
                return_value=_stream_mock_raising(
                    httpx.HTTPStatusError("Not found", request=MagicMock(), response=err_response)
                )
            )
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            with pytest.raises(LLMClientError):
                await client.respond(
                    role=ModelRole.PRIMARY,
                    messages=[{"role": "user", "content": "Test"}],
                    trace_ctx=trace_ctx,
                )

    @pytest.mark.asyncio
    async def test_per_model_endpoint(self, tmp_path: Path) -> None:
        """Test that models can use different endpoints/providers."""
        config_file = tmp_path / "models.yaml"
        config_file.write_text(
            """
models:
  primary:
    id: "test-primary"
    endpoint: "http://localhost:8001/v1"
    context_length: 32768
    quantization: "8bit"
    max_concurrency: 2
    default_timeout: 60
  sub_agent:
    id: "test-sub-agent"
    context_length: 32768
    quantization: "8bit"
    max_concurrency: 4
    default_timeout: 45
"""
        )

        client = LocalLLMClient(
            base_url="http://localhost:1234/v1",
            model_config_path=config_file,
        )

        mock_response = {
            "choices": [{"message": {"role": "assistant", "content": "Success"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            # Re-create the stream mock per call so call_args reflects only the latest invocation
            mock_client.stream = MagicMock(
                side_effect=lambda *a, **k: _stream_mock_for_response(mock_response)
            )
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()

            await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "Test"}],
                trace_ctx=trace_ctx,
            )
            call_args = mock_client.stream.call_args
            assert "http://localhost:8001/v1/chat/completions" in str(call_args)

            await client.respond(
                role=ModelRole.SUB_AGENT,
                messages=[{"role": "user", "content": "Test"}],
                trace_ctx=trace_ctx,
            )
            call_args = mock_client.stream.call_args
            assert "http://localhost:1234/v1/chat/completions" in str(call_args)

    @pytest.mark.asyncio
    async def test_connection_error_retries_then_raises(self, client: LocalLLMClient) -> None:
        """Test that persistent connection errors raise after all retries exhausted."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.stream = MagicMock(
                side_effect=lambda *a, **k: _stream_mock_raising(
                    httpx.ConnectError("Connection refused")
                )
            )
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            with pytest.raises(LLMConnectionError):
                await client.respond(
                    role=ModelRole.PRIMARY,
                    messages=[{"role": "user", "content": "Test"}],
                    trace_ctx=trace_ctx,
                )

    @pytest.mark.asyncio
    async def test_respond_uses_model_default_temperature(self, tmp_path: Path) -> None:
        """Use model temperature when caller does not pass one."""
        config_file = tmp_path / "models.yaml"
        config_file.write_text(
            """
models:
  primary:
    id: "test-primary"
    context_length: 32768
    quantization: "8bit"
    max_concurrency: 2
    default_timeout: 60
    temperature: 0.15
"""
        )
        client = LocalLLMClient(base_url="http://localhost:1234/v1", model_config_path=config_file)
        mock_response = {
            "choices": [{"message": {"role": "assistant", "content": "OK"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.stream = MagicMock(return_value=_stream_mock_for_response(mock_response))
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "Test"}],
                trace_ctx=trace_ctx,
            )

            payload = mock_client.stream.call_args.kwargs["json"]
            assert payload["temperature"] == 0.15

    @pytest.mark.asyncio
    async def test_respond_caller_temperature_overrides_model_default(self, tmp_path: Path) -> None:
        """Caller-supplied temperature should override model default."""
        config_file = tmp_path / "models.yaml"
        config_file.write_text(
            """
models:
  primary:
    id: "test-primary"
    context_length: 32768
    quantization: "8bit"
    max_concurrency: 2
    default_timeout: 60
    temperature: 0.15
"""
        )
        client = LocalLLMClient(base_url="http://localhost:1234/v1", model_config_path=config_file)
        mock_response = {
            "choices": [{"message": {"role": "assistant", "content": "OK"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.stream = MagicMock(return_value=_stream_mock_for_response(mock_response))
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "Test"}],
                temperature=0.6,
                trace_ctx=trace_ctx,
            )

            payload = mock_client.stream.call_args.kwargs["json"]
            assert payload["temperature"] == 0.6

    @pytest.mark.asyncio
    async def test_respond_includes_response_format(self, client: LocalLLMClient) -> None:
        """Structured response_format should be included in payload when provided."""
        mock_response = {
            "choices": [{"message": {"role": "assistant", "content": "OK"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": "router_decision", "schema": {"type": "object"}},
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.stream = MagicMock(return_value=_stream_mock_for_response(mock_response))
            mock_client_class.return_value.__aenter__.return_value = mock_client

            trace_ctx = TraceContext.new_trace()
            await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "Test"}],
                response_format=response_format,
                trace_ctx=trace_ctx,
            )

            payload = mock_client.stream.call_args.kwargs["json"]
            assert payload["response_format"] == response_format

    @pytest.mark.asyncio
    async def test_cf_access_headers_injected_for_slm_endpoint(self, tmp_path: Path) -> None:
        """CF-Access headers are injected when endpoint matches settings.slm_tunnel_base_url."""
        config_file = tmp_path / "models_slm.yaml"
        config_file.write_text(
            """
models:
  primary:
    id: "test-primary"
    context_length: 32768
    max_concurrency: 1
    default_timeout: 60
    endpoint: "https://slm.example.com/v1"
  sub_agent:
    id: "test-sub"
    context_length: 32768
    max_concurrency: 1
    default_timeout: 60
"""
        )
        slm_client = LocalLLMClient(
            base_url="https://slm.example.com/v1",
            timeout_seconds=30,
            max_retries=0,
            model_config_path=config_file,
        )

        mock_response = {
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

        with (
            patch("personal_agent.llm_client.client.settings") as mock_settings,
            patch("httpx.AsyncClient") as mock_client_class,
        ):
            mock_settings.cf_access_client_id = "test-id-123"
            mock_settings.cf_access_client_secret = "test-secret-456"
            mock_settings.slm_tunnel_base_url = "https://slm.example.com"
            mock_http = AsyncMock()
            mock_http.stream = MagicMock(return_value=_stream_mock_for_response(mock_response))
            mock_client_class.return_value.__aenter__.return_value = mock_http

            trace_ctx = TraceContext.new_trace()
            await slm_client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "hi"}],
                trace_ctx=trace_ctx,
            )

            call_kwargs = mock_http.stream.call_args[1]
            headers = call_kwargs.get("headers") or {}
            assert headers.get("CF-Access-Client-Id") == "test-id-123"
            assert headers.get("CF-Access-Client-Secret") == "test-secret-456"

    @pytest.mark.asyncio
    async def test_no_cf_headers_for_localhost_endpoint(self, client: LocalLLMClient) -> None:
        """CF-Access headers are NOT added for localhost endpoints."""
        mock_response = {
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

        with (
            patch("personal_agent.llm_client.client.settings") as mock_settings,
            patch("httpx.AsyncClient") as mock_client_class,
        ):
            mock_settings.cf_access_client_id = "test-id-123"
            mock_settings.cf_access_client_secret = "test-secret-456"
            mock_settings.slm_tunnel_base_url = "https://slm.example.com"
            mock_http = AsyncMock()
            mock_http.stream = MagicMock(return_value=_stream_mock_for_response(mock_response))
            mock_client_class.return_value.__aenter__.return_value = mock_http

            trace_ctx = TraceContext.new_trace()
            await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "hi"}],
                trace_ctx=trace_ctx,
            )

            call_kwargs = mock_http.stream.call_args[1]
            headers = call_kwargs.get("headers")
            assert headers is None or "CF-Access-Client-Id" not in (headers or {})
            assert headers is None or "CF-Access-Client-Secret" not in (headers or {})
