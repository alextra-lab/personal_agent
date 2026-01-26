"""Tests for LLM client types."""

from personal_agent.llm_client.types import (
    LLMClientError,
    LLMConnectionError,
    LLMInvalidResponse,
    LLMRateLimit,
    LLMServerError,
    LLMTimeout,
    ModelRole,
    ToolCall,
)


class TestModelRole:
    """Test ModelRole enum."""

    def test_model_role_values(self) -> None:
        """Test that ModelRole has expected values."""
        assert ModelRole.ROUTER == "router"
        assert ModelRole.REASONING == "reasoning"
        assert ModelRole.CODING == "coding"

    def test_model_role_string_representation(self) -> None:
        """Test that ModelRole values are strings."""
        assert isinstance(ModelRole.ROUTER.value, str)
        assert ModelRole.ROUTER.value == "router"


class TestToolCall:
    """Test ToolCall TypedDict."""

    def test_tool_call_structure(self) -> None:
        """Test that ToolCall has required fields."""
        tool_call: ToolCall = {
            "id": "call_123",
            "name": "read_file",
            "arguments": '{"path": "/tmp/test.txt"}',
        }
        assert tool_call["id"] == "call_123"
        assert tool_call["name"] == "read_file"
        assert tool_call["arguments"] == '{"path": "/tmp/test.txt"}'


class TestErrorHierarchy:
    """Test LLM client error hierarchy."""

    def test_llm_client_error_is_base(self) -> None:
        """Test that LLMClientError is the base exception."""
        assert issubclass(LLMTimeout, LLMClientError)
        assert issubclass(LLMConnectionError, LLMClientError)
        assert issubclass(LLMRateLimit, LLMClientError)
        assert issubclass(LLMServerError, LLMClientError)
        assert issubclass(LLMInvalidResponse, LLMClientError)

    def test_error_messages(self) -> None:
        """Test that errors can be created with messages."""
        timeout = LLMTimeout("Request timed out")
        assert str(timeout) == "Request timed out"

        conn_error = LLMConnectionError("Connection failed")
        assert str(conn_error) == "Connection failed"

        rate_limit = LLMRateLimit("Rate limit exceeded")
        assert str(rate_limit) == "Rate limit exceeded"

        server_error = LLMServerError("Server error 500")
        assert str(server_error) == "Server error 500"

        invalid_response = LLMInvalidResponse("Invalid JSON")
        assert str(invalid_response) == "Invalid JSON"
