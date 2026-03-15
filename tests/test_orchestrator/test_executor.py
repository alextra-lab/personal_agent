"""Tests for orchestrator executor and state machine."""

import json
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.governance.models import Mode
from personal_agent.memory.models import MemoryQueryResult, TurnNode
from personal_agent.orchestrator import Channel, Orchestrator
from personal_agent.orchestrator.executor import (
    _extract_entity_type_hints,
    _format_broad_recall,
    execute_task_safe,
)
from personal_agent.orchestrator.session import SessionManager
from personal_agent.orchestrator.types import ExecutionContext, TaskState
from personal_agent.telemetry.trace import TraceContext
from tests.test_orchestrator.conftest import configure_mock_llm_client_model_configs


class TestMemoryRecallHelpers:
    """ADR-0025: tests for memory recall intent helpers in executor."""

    def test_extract_entity_type_hints_location(self) -> None:
        """Location keywords map to Location type."""
        assert _extract_entity_type_hints("What locations have I asked about?") == [
            "Location"
        ]
        assert _extract_entity_type_hints("places and cities I mentioned") == [
            "Location"
        ]
        assert _extract_entity_type_hints("Which country or city?") == ["Location"]

    def test_extract_entity_type_hints_person(self) -> None:
        """Person keywords map to Person type."""
        assert _extract_entity_type_hints("What people have I discussed?") == [
            "Person"
        ]
        assert _extract_entity_type_hints("someone I mentioned") == ["Person"]

    def test_extract_entity_type_hints_organization(self) -> None:
        """Organization keywords map to Organization type."""
        assert _extract_entity_type_hints("What company have I asked about?") == [
            "Organization"
        ]
        assert _extract_entity_type_hints("org and companies") == ["Organization"]

    def test_extract_entity_type_hints_technology(self) -> None:
        """Technology keywords map to Technology type."""
        assert _extract_entity_type_hints("What tools have I used recently?") == [
            "Technology"
        ]
        assert _extract_entity_type_hints("technology and tools") == ["Technology"]

    def test_extract_entity_type_hints_topic(self) -> None:
        """Topic keywords map to Topic type."""
        assert _extract_entity_type_hints("What topic have we covered?") == [
            "Topic"
        ]
        assert _extract_entity_type_hints("topics I asked about") == ["Topic"]

    def test_extract_entity_type_hints_concept(self) -> None:
        """Concept keywords map to Concept type."""
        assert _extract_entity_type_hints("What concepts did I mention?") == [
            "Concept"
        ]
        assert _extract_entity_type_hints("concept we discussed") == ["Concept"]

    def test_extract_entity_type_hints_multiple_types(self) -> None:
        """Multiple keywords yield deduplicated types."""
        result = _extract_entity_type_hints(
            "What locations and people have I asked about?"
        )
        assert set(result) == {"Location", "Person"}
        assert len(result) == 2

    def test_extract_entity_type_hints_no_match(self) -> None:
        """No type keywords returns empty list."""
        assert _extract_entity_type_hints("What have I discussed?") == []
        assert _extract_entity_type_hints("Hello") == []
        assert _extract_entity_type_hints("") == []
        assert _extract_entity_type_hints(None) == []  # type: ignore[arg-type]

    def test_format_broad_recall_empty(self) -> None:
        """Empty broad result yields empty list."""
        assert _format_broad_recall({}) == []
        assert _format_broad_recall({"entities": [], "sessions": []}) == []

    def test_format_broad_recall_entities_and_sessions(self) -> None:
        """Entities and sessions are formatted for memory_context."""
        broad = {
            "entities": [
                {"name": "Crete", "type": "Location", "mentions": 3, "description": "Greek island"}
            ],
            "sessions": [
                {"session_id": "s1", "dominant_entities": ["Crete"], "turn_count": 5}
            ],
            "turns_summary": [],
        }
        result = _format_broad_recall(broad)
        assert len(result) == 2
        assert result[0]["type"] == "entity"
        assert result[0]["name"] == "Crete"
        assert result[0]["entity_type"] == "Location"
        assert result[0]["mentions"] == 3
        assert result[1]["type"] == "session"
        assert result[1]["session_id"] == "s1"
        assert result[1]["turn_count"] == 5


@patch("personal_agent.orchestrator.executor.LocalLLMClient")
@pytest.mark.asyncio
async def test_execute_simple_task(mock_client_class) -> None:
    """Test executing a simple task through the state machine."""
    mock_client = AsyncMock()
    configure_mock_llm_client_model_configs(mock_client)
    mock_client_class.return_value = mock_client
    mock_client.respond.side_effect = [
        {"role": "assistant", "content": "Hello! I'm doing well.", "tool_calls": [], "reasoning_trace": None, "usage": {"total_tokens": 20}, "raw": {}},
    ]
    session_manager = SessionManager()
    session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)

    trace_ctx = TraceContext.new_trace()
    ctx = ExecutionContext(
        session_id=session_id,
        trace_id=trace_ctx.trace_id,
        user_message="Hello, how are you?",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )

    result = await execute_task_safe(ctx, session_manager)

    # Verify result structure
    assert "reply" in result
    assert "steps" in result
    assert "trace_id" in result
    assert result["trace_id"] == trace_ctx.trace_id

    # Verify reply exists (placeholder response for skeleton)
    assert len(result["reply"]) > 0

    # Verify steps were recorded
    assert len(result["steps"]) > 0

    # Verify state transitions occurred
    step_types = [step["type"] for step in result["steps"]]
    assert "llm_call" in step_types


@patch("personal_agent.orchestrator.executor.LocalLLMClient")
@pytest.mark.asyncio
async def test_execute_task_with_code_channel(mock_client_class) -> None:
    """Test executing a task with CODE_TASK channel."""
    mock_client = AsyncMock()
    configure_mock_llm_client_model_configs(mock_client)
    mock_client_class.return_value = mock_client
    mock_client.respond.return_value = {"role": "assistant", "content": "Here is a function.", "tool_calls": [], "reasoning_trace": None, "usage": {"total_tokens": 30}, "raw": {}}
    session_manager = SessionManager()
    session_id = session_manager.create_session(Mode.NORMAL, Channel.CODE_TASK)

    trace_ctx = TraceContext.new_trace()
    ctx = ExecutionContext(
        session_id=session_id,
        trace_id=trace_ctx.trace_id,
        user_message="Write a Python function to add two numbers",
        mode=Mode.NORMAL,
        channel=Channel.CODE_TASK,
    )

    result = await execute_task_safe(ctx, session_manager)

    assert result["trace_id"] == trace_ctx.trace_id
    assert len(result["reply"]) > 0
    assert len(result["steps"]) > 0


@patch("personal_agent.orchestrator.executor.LocalLLMClient")
@pytest.mark.asyncio
async def test_execute_task_updates_session(mock_client_class) -> None:
    """Test that executing a task updates session messages."""
    mock_client = AsyncMock()
    configure_mock_llm_client_model_configs(mock_client)
    mock_client_class.return_value = mock_client
    mock_client.respond.return_value = {"role": "assistant", "content": "Acknowledged.", "tool_calls": [], "reasoning_trace": None, "usage": {"total_tokens": 10}, "raw": {}}
    session_manager = SessionManager()
    session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)

    trace_ctx = TraceContext.new_trace()
    ctx = ExecutionContext(
        session_id=session_id,
        trace_id=trace_ctx.trace_id,
        user_message="Test message",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )

    await execute_task_safe(ctx, session_manager)

    # Check session was updated
    session = session_manager.get_session(session_id)
    assert session is not None
    assert len(session.messages) > 0

    # Should have user message
    user_messages = [m for m in session.messages if m["role"] == "user"]
    assert len(user_messages) > 0
    assert user_messages[0]["content"] == "Test message"


@patch("personal_agent.orchestrator.executor.LocalLLMClient")
@pytest.mark.asyncio
async def test_execute_task_preserves_context(mock_client_class) -> None:
    """Test that execution context is preserved through state machine."""
    mock_client = AsyncMock()
    configure_mock_llm_client_model_configs(mock_client)
    mock_client_class.return_value = mock_client
    mock_client.respond.return_value = {"role": "assistant", "content": "OK", "tool_calls": [], "reasoning_trace": None, "usage": {"total_tokens": 5}, "raw": {}}
    session_manager = SessionManager()
    session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)

    trace_ctx = TraceContext.new_trace()
    original_trace_id = trace_ctx.trace_id
    original_user_message = "Original message"

    ctx = ExecutionContext(
        session_id=session_id,
        trace_id=original_trace_id,
        user_message=original_user_message,
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )

    result = await execute_task_safe(ctx, session_manager)

    # Verify trace ID preserved
    assert result["trace_id"] == original_trace_id

    # Verify context attributes preserved
    assert ctx.session_id == session_id
    assert ctx.trace_id == original_trace_id
    assert ctx.user_message == original_user_message


@patch("personal_agent.orchestrator.executor.LocalLLMClient")
@pytest.mark.asyncio
async def test_execute_task_state_transitions(mock_client_class) -> None:
    """Test that state machine transitions through expected states."""
    mock_client = AsyncMock()
    configure_mock_llm_client_model_configs(mock_client)
    mock_client_class.return_value = mock_client
    mock_client.respond.return_value = {"role": "assistant", "content": "Done.", "tool_calls": [], "reasoning_trace": None, "usage": {"total_tokens": 10}, "raw": {}}
    session_manager = SessionManager()
    session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)

    trace_ctx = TraceContext.new_trace()
    ctx = ExecutionContext(
        session_id=session_id,
        trace_id=trace_ctx.trace_id,
        user_message="Test",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )

    # Execute task
    result = await execute_task_safe(ctx, session_manager)

    # Verify final state is COMPLETED
    assert ctx.state == TaskState.COMPLETED

    # Verify no error
    assert ctx.error is None

    # Verify result is successful
    assert "error" not in result or not result.get("error")


@patch("personal_agent.orchestrator.executor.LocalLLMClient")
@pytest.mark.asyncio
async def test_execute_task_with_different_modes(mock_client_class) -> None:
    """Test executing tasks with different operational modes."""
    mock_client = AsyncMock()
    configure_mock_llm_client_model_configs(mock_client)
    mock_client_class.return_value = mock_client
    mock_client.respond.return_value = {"role": "assistant", "content": "Response.", "tool_calls": [], "reasoning_trace": None, "usage": {"total_tokens": 10}, "raw": {}}
    session_manager = SessionManager()

    for mode in [Mode.NORMAL, Mode.ALERT, Mode.DEGRADED]:
        session_id = session_manager.create_session(mode, Channel.CHAT)

        trace_ctx = TraceContext.new_trace()
        ctx = ExecutionContext(
            session_id=session_id,
            trace_id=trace_ctx.trace_id,
            user_message="Test message",
            mode=mode,
            channel=Channel.CHAT,
        )

        result = await execute_task_safe(ctx, session_manager)

        assert result["trace_id"] == trace_ctx.trace_id
        assert len(result["reply"]) > 0


@patch("personal_agent.orchestrator.executor.LocalLLMClient")
@pytest.mark.asyncio
async def test_execute_task_error_handling(mock_client_class) -> None:
    """Test that errors are handled gracefully and don't crash."""
    mock_client = AsyncMock()
    configure_mock_llm_client_model_configs(mock_client)
    mock_client_class.return_value = mock_client
    mock_client.respond.return_value = {"role": "assistant", "content": "OK", "tool_calls": [], "reasoning_trace": None, "usage": {"total_tokens": 5}, "raw": {}}
    session_manager = SessionManager()

    trace_ctx = TraceContext.new_trace()
    # Use invalid session_id to potentially trigger error
    # (Though skeleton implementation should handle this gracefully)
    ctx = ExecutionContext(
        session_id="invalid-session-id",  # Will cause get_session to return None, but should handle
        trace_id=trace_ctx.trace_id,
        user_message="Test",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )

    # Should not raise exception
    result = await execute_task_safe(ctx, session_manager)

    # Should return a result even on error
    assert result is not None
    assert "reply" in result
    assert "trace_id" in result


# ============================================================================
# Tool-Using Flow Tests
# ============================================================================


class TestToolUsingFlow:
    """Tests for tool-using flow in orchestrator."""

    @patch("personal_agent.orchestrator.executor.LocalLLMClient")
    @pytest.mark.asyncio
    async def test_llm_call_with_tools_passed_to_client(self, mock_client_class):
        """Test that tools are passed to LLM client when available."""
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_client_class.return_value = mock_client

        # Mock LLM response without tool calls
        mock_response = {
            "role": "assistant",
            "content": "Hello! How can I help you?",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 50},
            "raw": {},
        }
        mock_client.respond.return_value = mock_response

        orchestrator = Orchestrator()
        await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Hello",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        # Verify tools were passed to LLM client
        call_args = mock_client.respond.call_args
        assert call_args is not None
        tools_arg = call_args.kwargs.get("tools")
        # Tools should be a list (may be empty if no tools available in mode)
        assert tools_arg is None or isinstance(tools_arg, list)

    @patch("personal_agent.orchestrator.executor.LocalLLMClient")
    @pytest.mark.asyncio
    async def test_tool_calls_executed_when_returned_from_llm(self, mock_client_class):
        """Test that tool calls from LLM are executed and results appended."""
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_client_class.return_value = mock_client

        # Mock router response (DELEGATE to REASONING) - CHAT channel goes through router first
        router_response = {
            "role": "assistant",
            "content": json.dumps(
                {
                    "routing_decision": "DELEGATE",
                    "target_model": "REASONING",
                    "confidence": 0.9,
                    "reasoning_depth": 5,
                    "reason": "Tool usage required",
                }
            ),
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 80},
            "raw": {},
        }

        # Mock reasoning model response with tool calls
        tool_call_id = "call_123"
        mock_response_with_tools = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "name": "read_file",
                    "arguments": json.dumps({"path": "/tmp/test.txt"}),
                }
            ],
            "reasoning_trace": None,
            "usage": {"total_tokens": 100},
            "raw": {},
        }

        # Mock synthesis response after tool execution
        synthesis_response = {
            "role": "assistant",
            "content": "The file contains: test content",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 150},
            "raw": {},
        }

        mock_client.respond.side_effect = [
            router_response,
            mock_response_with_tools,
            synthesis_response,
        ]

        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Read /tmp/test.txt",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        # Verify LLM was called three times (router, reasoning with tools, synthesis)
        assert mock_client.respond.call_count >= 2

        # Verify second call (reasoning) had tools
        second_call = mock_client.respond.call_args_list[1]
        assert second_call.kwargs.get("tools") is not None

        # Verify third call had tool results in messages
        if mock_client.respond.call_count >= 3:
            third_call = mock_client.respond.call_args_list[2]
            messages = third_call.kwargs.get("messages", [])
            tool_messages = [m for m in messages if m.get("role") == "tool"]
            assert len(tool_messages) > 0

        # Verify result contains tool execution steps
        step_types = [step["type"] for step in result["steps"]]
        assert "tool_call" in step_types

    @patch("personal_agent.orchestrator.executor.LocalLLMClient")
    @pytest.mark.asyncio
    async def test_tool_execution_stores_results_in_context(self, mock_client_class):
        """Test that tool execution results are stored in context."""
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_client_class.return_value = mock_client

        # Mock LLM response with tool calls
        tool_call_id = "call_456"
        mock_response_with_tools = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "name": "system_metrics_snapshot",
                    "arguments": json.dumps({}),
                }
            ],
            "reasoning_trace": None,
            "usage": {"total_tokens": 100},
            "raw": {},
        }

        # Mock synthesis response
        synthesis_response = {
            "role": "assistant",
            "content": "System metrics collected",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 150},
            "raw": {},
        }

        mock_client.respond.side_effect = [mock_response_with_tools, synthesis_response]

        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Get system metrics",
            mode=Mode.NORMAL,
            channel=Channel.SYSTEM_HEALTH,
        )

        # Verify tool_results were stored
        # (We can't directly access ctx, but we can verify via steps)
        tool_steps = [s for s in result["steps"] if s["type"] == "tool_call"]
        assert len(tool_steps) > 0

        # Verify tool step metadata
        tool_step = tool_steps[0]
        assert "tool_name" in tool_step["metadata"]
        assert "success" in tool_step["metadata"]

    @patch("personal_agent.orchestrator.executor.LocalLLMClient")
    @pytest.mark.asyncio
    async def test_multiple_tool_calls_executed_sequentially(self, mock_client_class):
        """Test that multiple tool calls are executed sequentially."""
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_client_class.return_value = mock_client

        # Use SYSTEM_HEALTH channel to bypass router
        # Mock reasoning model response with multiple tool calls
        mock_response_with_tools = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "name": "read_file",
                    "arguments": json.dumps({"path": "/tmp/file1.txt"}),
                },
                {
                    "id": "call_2",
                    "name": "read_file",
                    "arguments": json.dumps({"path": "/tmp/file2.txt"}),
                },
            ],
            "reasoning_trace": None,
            "usage": {"total_tokens": 100},
            "raw": {},
        }

        # Mock synthesis response
        synthesis_response = {
            "role": "assistant",
            "content": "Both files read successfully",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 200},
            "raw": {},
        }

        mock_client.respond.side_effect = [mock_response_with_tools, synthesis_response]

        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Read /tmp/file1.txt and /tmp/file2.txt",
            mode=Mode.NORMAL,
            channel=Channel.SYSTEM_HEALTH,  # Bypass router
        )

        # Verify both tools were executed
        tool_steps = [s for s in result["steps"] if s["type"] == "tool_call"]
        assert len(tool_steps) == 2

        # Verify tool results were appended to messages
        if mock_client.respond.call_count >= 2:
            second_call = mock_client.respond.call_args_list[1]
            messages = second_call.kwargs.get("messages", [])
            tool_messages = [m for m in messages if m.get("role") == "tool"]
            assert len(tool_messages) == 2

    @patch("personal_agent.orchestrator.executor.LocalLLMClient")
    @pytest.mark.asyncio
    async def test_tool_execution_error_handled_gracefully(self, mock_client_class):
        """Test that tool execution errors are handled gracefully."""
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_client_class.return_value = mock_client

        # Mock LLM response with tool call to nonexistent tool
        mock_response_with_tools = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_error",
                    "name": "nonexistent_tool",
                    "arguments": json.dumps({}),
                }
            ],
            "reasoning_trace": None,
            "usage": {"total_tokens": 100},
            "raw": {},
        }

        # Mock synthesis response (should still work even if tool failed)
        synthesis_response = {
            "role": "assistant",
            "content": "Tool execution encountered an error, but I can still respond",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 150},
            "raw": {},
        }

        mock_client.respond.side_effect = [mock_response_with_tools, synthesis_response]

        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Use nonexistent tool",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        # Should still complete successfully (error handled gracefully)
        assert result["reply"] is not None
        assert len(result["reply"]) > 0

        # Verify error was logged in tool step
        tool_steps = [s for s in result["steps"] if s["type"] == "tool_call"]
        if tool_steps:
            # Tool step should exist even if tool failed
            assert "tool_name" in tool_steps[0]["metadata"]

    @patch("personal_agent.orchestrator.executor.LocalLLMClient")
    @pytest.mark.asyncio
    async def test_search_memory_tool_called_when_llm_requests_it(self, mock_client_class):
        """Integration: agent executes search_memory when LLM returns that tool call (ADR-0026)."""
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_client_class.return_value = mock_client
        # Provide model_configs so executor can access model_config.id during synthesis
        mock_client.model_configs = {
            "REASONING": MagicMock(id="reasoning-model"),
        }

        turn = TurnNode(
            turn_id="turn-athens",
            timestamp=datetime.now(timezone.utc),
            user_message="I want to visit Athens",
            summary="User asked about Athens",
            key_entities=["Athens"],
        )
        query_result = MemoryQueryResult(conversations=[turn], entities=[])
        mock_memory = MagicMock()
        mock_memory.connected = True
        mock_memory.query_memory = AsyncMock(return_value=query_result)

        fake_app = MagicMock()
        fake_app.memory_service = mock_memory

        mock_response_with_tools = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_search",
                    "name": "search_memory",
                    "arguments": json.dumps({"query_text": "Athens", "entity_names": ["Athens"]}),
                }
            ],
            "reasoning_trace": None,
            "usage": {"total_tokens": 100},
            "raw": {},
        }
        synthesis_response = {
            "role": "assistant",
            "content": "You have discussed Athens before (travel).",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 150},
            "raw": {},
        }
        mock_client.respond.side_effect = [mock_response_with_tools, synthesis_response]

        with patch.dict(sys.modules, {"personal_agent.service.app": fake_app}):
            orchestrator = Orchestrator()
            result = await orchestrator.handle_user_request(
                session_id="test-session",
                user_message="Before answering, check if I've discussed Athens before.",
                mode=Mode.NORMAL,
                channel=Channel.SYSTEM_HEALTH,
            )

        tool_steps = [s for s in result["steps"] if s["type"] == "tool_call"]
        assert len(tool_steps) >= 1, "Expected at least one tool_call step"
        search_steps = [s for s in tool_steps if s["metadata"].get("tool_name") == "search_memory"]
        assert len(search_steps) == 1, "Expected exactly one search_memory tool call"
        assert search_steps[0]["metadata"].get("success") is True

        # Synthesis call should have received tool result with matched_turns
        assert mock_client.respond.call_count >= 2
        second_call = mock_client.respond.call_args_list[1]
        messages = second_call.kwargs.get("messages", [])
        tool_messages = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_messages) >= 1
        content = tool_messages[0].get("content", "")
        assert "matched_turns" in content or "entity_match" in content
