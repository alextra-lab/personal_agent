"""Unit tests for ClaudeClient.respond() and related helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.llm_client.claude import ClaudeClient


class TestConvertToolsToAnthropic:
    """Test OpenAI → Anthropic tool format conversion."""

    def test_basic_tool(self) -> None:
        """OpenAI function tool converts to Anthropic name/description/input_schema."""
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": "search_memory",
                    "description": "Search memory",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            }
        ]
        result = ClaudeClient._convert_tools_to_anthropic(openai_tools)
        assert len(result) == 1
        assert result[0]["name"] == "search_memory"
        assert result[0]["description"] == "Search memory"
        assert result[0]["input_schema"]["properties"]["query"]["type"] == "string"

    def test_empty_tools(self) -> None:
        """Empty list returns empty list."""
        assert ClaudeClient._convert_tools_to_anthropic([]) == []

    def test_missing_parameters_defaults_to_empty_object(self) -> None:
        """Tool without parameters gets an empty object schema."""
        tools = [{"type": "function", "function": {"name": "ping", "description": "Ping"}}]
        result = ClaudeClient._convert_tools_to_anthropic(tools)
        assert result[0]["input_schema"] == {"type": "object", "properties": {}}


class TestConvertMessagesToAnthropic:
    """Test OpenAI → Anthropic message format conversion."""

    def test_extracts_system_message(self) -> None:
        """System role is extracted as separate string, not included in messages."""
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        system, anthropic_msgs = ClaudeClient._convert_messages_to_anthropic(msgs)
        assert system == "You are helpful."
        assert len(anthropic_msgs) == 1
        assert anthropic_msgs[0]["role"] == "user"

    def test_user_and_assistant_pass_through(self) -> None:
        """User and assistant messages pass through unchanged."""
        msgs = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        _, anthropic_msgs = ClaudeClient._convert_messages_to_anthropic(msgs)
        assert len(anthropic_msgs) == 2
        assert anthropic_msgs[0] == {"role": "user", "content": "Hello"}
        assert anthropic_msgs[1] == {"role": "assistant", "content": "Hi there"}

    def test_tool_result_conversion(self) -> None:
        """OpenAI tool role converts to Anthropic user/tool_result block."""
        msgs = [{"role": "tool", "tool_call_id": "tc_123", "content": "result data"}]
        _, anthropic_msgs = ClaudeClient._convert_messages_to_anthropic(msgs)
        assert anthropic_msgs[0]["role"] == "user"
        block = anthropic_msgs[0]["content"][0]
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "tc_123"
        assert block["content"] == "result data"

    def test_assistant_tool_calls_conversion(self) -> None:
        """Assistant message with tool_calls converts to Anthropic tool_use blocks."""
        msgs = [
            {
                "role": "assistant",
                "content": "Let me search.",
                "tool_calls": [
                    {
                        "id": "tc_456",
                        "function": {"name": "search", "arguments": '{"q": "test"}'},
                    }
                ],
            }
        ]
        _, anthropic_msgs = ClaudeClient._convert_messages_to_anthropic(msgs)
        blocks = anthropic_msgs[0]["content"]
        assert blocks[0] == {"type": "text", "text": "Let me search."}
        assert blocks[1]["type"] == "tool_use"
        assert blocks[1]["name"] == "search"
        assert blocks[1]["input"] == {"q": "test"}
        assert blocks[1]["id"] == "tc_456"

    def test_assistant_tool_calls_no_content(self) -> None:
        """Assistant tool_calls with empty content produces only tool_use blocks."""
        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "tc_789", "function": {"name": "ping", "arguments": "{}"}}],
            }
        ]
        _, anthropic_msgs = ClaudeClient._convert_messages_to_anthropic(msgs)
        blocks = anthropic_msgs[0]["content"]
        assert len(blocks) == 1
        assert blocks[0]["type"] == "tool_use"

    def test_no_system_returns_none(self) -> None:
        """Messages without system role return None for system."""
        msgs = [{"role": "user", "content": "Hello"}]
        system, _ = ClaudeClient._convert_messages_to_anthropic(msgs)
        assert system is None


class TestClaudeRespond:
    """Test ClaudeClient.respond() with mocked Anthropic API."""

    def _make_mock_response(self, content_text: str = "Hello from Claude") -> MagicMock:
        """Build a minimal mock Anthropic API response."""
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = content_text

        mock_usage = MagicMock()
        mock_usage.input_tokens = 100
        mock_usage.output_tokens = 50

        mock_response = MagicMock()
        mock_response.content = [mock_block]
        mock_response.usage = mock_usage
        mock_response.id = "msg_123"
        mock_response.stop_reason = "end_turn"
        return mock_response

    def _make_client(self) -> ClaudeClient:
        """Build a ClaudeClient with mocked settings (no real API key needed)."""
        import personal_agent.llm_client.claude as claude_module

        mock_settings = MagicMock()
        mock_settings.anthropic_api_key = "test-key"
        mock_settings.cloud_weekly_budget_usd = 100.0

        with patch.object(claude_module, "settings", mock_settings):
            client = ClaudeClient(model_id="claude-sonnet-4-6")

        # Replace the real Anthropic client and cost tracker
        client.client = MagicMock()
        client.cost_tracker = MagicMock()
        client.cost_tracker.pool = True
        client.cost_tracker.record_api_call = AsyncMock()
        return client

    @pytest.mark.asyncio()
    async def test_respond_returns_llm_response(self) -> None:
        """respond() returns a correctly shaped LLMResponse dict."""
        mock_response = self._make_mock_response()
        client = self._make_client()
        client.client.messages.create = AsyncMock(return_value=mock_response)

        with patch.object(ClaudeClient, "_check_weekly_budget", new_callable=AsyncMock):
            from personal_agent.llm_client.types import ModelRole

            response = await client.respond(
                role=ModelRole.STANDARD,
                messages=[{"role": "user", "content": "Hello"}],
            )

        assert response["role"] == "assistant"
        assert response["content"] == "Hello from Claude"
        assert response["tool_calls"] == []
        assert response["usage"]["prompt_tokens"] == 100
        assert response["usage"]["completion_tokens"] == 50
        assert response["usage"]["total_tokens"] == 150
        assert response["response_id"] == "msg_123"

    @pytest.mark.asyncio()
    async def test_respond_with_tool_calls(self) -> None:
        """respond() parses tool_use blocks into ToolCall dicts."""
        mock_text = MagicMock()
        mock_text.type = "text"
        mock_text.text = "Searching..."

        mock_tool = MagicMock()
        mock_tool.type = "tool_use"
        mock_tool.id = "tu_789"
        mock_tool.name = "search_memory"
        mock_tool.input = {"query": "databases"}

        mock_usage = MagicMock()
        mock_usage.input_tokens = 200
        mock_usage.output_tokens = 100

        mock_response = MagicMock()
        mock_response.content = [mock_text, mock_tool]
        mock_response.usage = mock_usage
        mock_response.id = "msg_456"
        mock_response.stop_reason = "tool_use"

        client = self._make_client()
        client.client.messages.create = AsyncMock(return_value=mock_response)

        with patch.object(ClaudeClient, "_check_weekly_budget", new_callable=AsyncMock):
            from personal_agent.llm_client.types import ModelRole

            response = await client.respond(
                role=ModelRole.STANDARD,
                messages=[{"role": "user", "content": "Search"}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "search_memory",
                            "description": "Search",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
            )

        assert len(response["tool_calls"]) == 1
        assert response["tool_calls"][0]["name"] == "search_memory"
        assert response["tool_calls"][0]["id"] == "tu_789"
        assert response["content"] == "Searching..."

    @pytest.mark.asyncio()
    async def test_respond_with_system_prompt(self) -> None:
        """system_prompt is passed as Anthropic system parameter."""
        mock_response = self._make_mock_response()
        client = self._make_client()
        create_mock = AsyncMock(return_value=mock_response)
        client.client.messages.create = create_mock

        with patch.object(ClaudeClient, "_check_weekly_budget", new_callable=AsyncMock):
            from personal_agent.llm_client.types import ModelRole

            await client.respond(
                role=ModelRole.STANDARD,
                messages=[{"role": "user", "content": "Hello"}],
                system_prompt="You are a helpful assistant.",
            )

        call_kwargs = create_mock.call_args[1]
        assert call_kwargs["system"] == "You are a helpful assistant."


class TestGetLLMClientFactory:
    """Test the get_llm_client() factory function."""

    @patch("personal_agent.llm_client.factory.load_model_config")
    def test_returns_claude_for_anthropic_provider(self, mock_load_config: MagicMock) -> None:
        """Factory returns ClaudeClient when model's provider is "anthropic"."""
        import personal_agent.llm_client.claude as claude_module
        from personal_agent.llm_client.models import ModelDefinition

        mock_model = ModelDefinition(
            id="claude-sonnet-4-6",
            provider="anthropic",
            context_length=200000,
            max_concurrency=10,
            default_timeout=60,
        )
        mock_load_config.return_value.models = {"standard": mock_model}

        mock_settings = MagicMock()
        mock_settings.anthropic_api_key = "test-key"
        mock_settings.cloud_weekly_budget_usd = 100.0

        with patch.object(claude_module, "settings", mock_settings):
            from personal_agent.llm_client.factory import get_llm_client

            client = get_llm_client(role_name="standard")

        assert type(client).__name__ == "ClaudeClient"

    @patch("personal_agent.llm_client.factory.load_model_config")
    def test_returns_local_for_null_provider(self, mock_load_config: MagicMock) -> None:
        """Factory returns LocalLLMClient when model's provider is None."""
        from personal_agent.llm_client.models import ModelDefinition

        mock_model = ModelDefinition(
            id="qwen3.5-9b",
            provider=None,
            context_length=32768,
            max_concurrency=2,
            default_timeout=45,
        )
        mock_load_config.return_value.models = {"standard": mock_model}

        from personal_agent.llm_client.factory import get_llm_client

        client = get_llm_client(role_name="standard")

        assert type(client).__name__ == "LocalLLMClient"

    @patch("personal_agent.llm_client.factory.load_model_config")
    def test_returns_local_for_missing_role(self, mock_load_config: MagicMock) -> None:
        """Factory falls back to LocalLLMClient when role is not in config."""
        mock_load_config.return_value.models = {}

        from personal_agent.llm_client.factory import get_llm_client

        client = get_llm_client(role_name="nonexistent")

        assert type(client).__name__ == "LocalLLMClient"
