"""Tests for orchestrator executor and state machine."""

import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from personal_agent.config.profile import ExecutionProfile, _current_profile, set_current_profile
from personal_agent.governance.models import Mode
from personal_agent.llm_client.history_sanitiser import sanitise_messages
from personal_agent.llm_client.models import ToolCallingStrategy
from personal_agent.memory.models import MemoryQueryResult, TurnNode
from personal_agent.orchestrator import Channel, Orchestrator
from personal_agent.orchestrator.executor import (
    _ENTITY_TYPE_KEYWORDS,
    _build_assistant_tool_calls,
    _extract_entity_type_hints,
    _format_broad_recall,
    _maybe_confirm_attachment_cost,
    _validate_and_fix_conversation_roles,
    execute_task_safe,
    step_init,
    step_synthesis,
)
from personal_agent.orchestrator.session import SessionManager
from personal_agent.orchestrator.types import AttachmentRef, ExecutionContext, TaskState
from personal_agent.telemetry.trace import TraceContext
from tests.test_orchestrator.conftest import configure_mock_llm_client_model_configs


class TestMemoryRecallHelpers:
    """ADR-0025: tests for memory recall intent helpers in executor."""

    def test_extract_entity_type_hints_location(self) -> None:
        """Location keywords map to Location type."""
        assert _extract_entity_type_hints("What locations have I asked about?") == ["Location"]
        assert _extract_entity_type_hints("places and cities I mentioned") == ["Location"]
        assert _extract_entity_type_hints("Which country or city?") == ["Location"]

    def test_extract_entity_type_hints_person(self) -> None:
        """Person keywords map to Person type."""
        assert _extract_entity_type_hints("What people have I discussed?") == ["Person"]
        assert _extract_entity_type_hints("someone I mentioned") == ["Person"]

    def test_extract_entity_type_hints_organization(self) -> None:
        """Organization keywords map to Organization type."""
        assert _extract_entity_type_hints("What company have I asked about?") == ["Organization"]
        assert _extract_entity_type_hints("org and companies") == ["Organization"]

    def test_extract_entity_type_hints_technology(self) -> None:
        """Technology keywords map to the V2 TechnicalArtifact type (ADR-0109)."""
        assert _extract_entity_type_hints("What tools have I used recently?") == [
            "TechnicalArtifact"
        ]
        assert _extract_entity_type_hints("technology and tools") == ["TechnicalArtifact"]

    def test_extract_entity_type_hints_topic(self) -> None:
        """Topic keywords map to the V2 DomainOrTopic type (ADR-0109)."""
        assert _extract_entity_type_hints("What topic have we covered?") == ["DomainOrTopic"]
        assert _extract_entity_type_hints("topics I asked about") == ["DomainOrTopic"]

    def test_extract_entity_type_hints_concept(self) -> None:
        """Concept keywords map to the V2 MethodOrConcept type (ADR-0109)."""
        assert _extract_entity_type_hints("What concepts did I mention?") == ["MethodOrConcept"]
        assert _extract_entity_type_hints("concept we discussed") == ["MethodOrConcept"]

    def test_extract_entity_type_hints_phenomenon(self) -> None:
        """Phenomenon keywords map to the V2 Phenomenon type (ADR-0109)."""
        assert _extract_entity_type_hints("What phenomenon did we discuss?") == ["Phenomenon"]
        assert _extract_entity_type_hints("phenomena I asked about") == ["Phenomenon"]

    def test_extract_entity_type_hints_quantity_measure(self) -> None:
        """Quantity/measurement keywords map to the V2 QuantityMeasure type (ADR-0109)."""
        assert _extract_entity_type_hints("What quantity did we measure?") == ["QuantityMeasure"]
        assert _extract_entity_type_hints("measurements and quantities") == ["QuantityMeasure"]

    def test_entity_type_keywords_has_no_retired_v1_types(self) -> None:
        """ADR-0109: no keyword resolves to a retired V1 type string (FRE-794)."""
        retired = {"Technology", "Topic", "Concept"}
        assert not retired & set(_ENTITY_TYPE_KEYWORDS.values())

    def test_extract_entity_type_hints_multiple_types(self) -> None:
        """Multiple keywords yield deduplicated types."""
        result = _extract_entity_type_hints("What locations and people have I asked about?")
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
            "sessions": [{"session_id": "s1", "dominant_entities": ["Crete"], "turn_count": 5}],
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


@patch("personal_agent.llm_client.factory.get_llm_client")
@pytest.mark.asyncio
async def test_execute_simple_task(mock_client_class) -> None:
    """Test executing a simple task through the state machine."""
    mock_client = AsyncMock()
    configure_mock_llm_client_model_configs(mock_client)
    mock_client_class.return_value = mock_client
    mock_client.respond.side_effect = [
        {
            "role": "assistant",
            "content": "Hello! I'm doing well.",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 20},
            "raw": {},
        },
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


@patch("personal_agent.llm_client.factory.get_llm_client")
@pytest.mark.asyncio
async def test_execute_task_with_code_channel(mock_client_class) -> None:
    """Test executing a task with CODE_TASK channel."""
    mock_client = AsyncMock()
    configure_mock_llm_client_model_configs(mock_client)
    mock_client_class.return_value = mock_client
    mock_client.respond.return_value = {
        "role": "assistant",
        "content": "Here is a function.",
        "tool_calls": [],
        "reasoning_trace": None,
        "usage": {"total_tokens": 30},
        "raw": {},
    }
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


@patch("personal_agent.llm_client.factory.get_llm_client")
@pytest.mark.asyncio
async def test_execute_task_updates_session(mock_client_class) -> None:
    """Test that executing a task updates session messages."""
    mock_client = AsyncMock()
    configure_mock_llm_client_model_configs(mock_client)
    mock_client_class.return_value = mock_client
    mock_client.respond.return_value = {
        "role": "assistant",
        "content": "Acknowledged.",
        "tool_calls": [],
        "reasoning_trace": None,
        "usage": {"total_tokens": 10},
        "raw": {},
    }
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

    # Should have user message (content may carry inlined volatile context under
    # the frozen layout, so check containment rather than exact equality).
    user_messages = [m for m in session.messages if m["role"] == "user"]
    assert len(user_messages) > 0
    assert "Test message" in user_messages[0]["content"]


@patch("personal_agent.llm_client.factory.get_llm_client")
@pytest.mark.asyncio
async def test_execute_task_preserves_context(mock_client_class) -> None:
    """Test that execution context is preserved through state machine."""
    mock_client = AsyncMock()
    configure_mock_llm_client_model_configs(mock_client)
    mock_client_class.return_value = mock_client
    mock_client.respond.return_value = {
        "role": "assistant",
        "content": "OK",
        "tool_calls": [],
        "reasoning_trace": None,
        "usage": {"total_tokens": 5},
        "raw": {},
    }
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


@patch("personal_agent.llm_client.factory.get_llm_client")
@pytest.mark.asyncio
async def test_execute_task_state_transitions(mock_client_class) -> None:
    """Test that state machine transitions through expected states."""
    mock_client = AsyncMock()
    configure_mock_llm_client_model_configs(mock_client)
    mock_client_class.return_value = mock_client
    mock_client.respond.return_value = {
        "role": "assistant",
        "content": "Done.",
        "tool_calls": [],
        "reasoning_trace": None,
        "usage": {"total_tokens": 10},
        "raw": {},
    }
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


@patch("personal_agent.llm_client.factory.get_llm_client")
@pytest.mark.asyncio
async def test_execute_task_with_different_modes(mock_client_class) -> None:
    """Test executing tasks with different operational modes."""
    mock_client = AsyncMock()
    configure_mock_llm_client_model_configs(mock_client)
    mock_client_class.return_value = mock_client
    mock_client.respond.return_value = {
        "role": "assistant",
        "content": "Response.",
        "tool_calls": [],
        "reasoning_trace": None,
        "usage": {"total_tokens": 10},
        "raw": {},
    }
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


@patch("personal_agent.llm_client.factory.get_llm_client")
@pytest.mark.asyncio
async def test_execute_task_error_handling(mock_client_class) -> None:
    """Test that errors are handled gracefully and don't crash."""
    mock_client = AsyncMock()
    configure_mock_llm_client_model_configs(mock_client)
    mock_client_class.return_value = mock_client
    mock_client.respond.return_value = {
        "role": "assistant",
        "content": "OK",
        "tool_calls": [],
        "reasoning_trace": None,
        "usage": {"total_tokens": 5},
        "raw": {},
    }
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

    @patch("personal_agent.llm_client.factory.get_llm_client")
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

    @patch("personal_agent.llm_client.factory.get_llm_client")
    @pytest.mark.asyncio
    async def test_tool_calls_executed_when_returned_from_llm(self, mock_client_class):
        """Test that tool calls from LLM are executed and results appended."""
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_client_class.return_value = mock_client

        # Mock PRIMARY response with tool calls (ADR-0033: no router, PRIMARY called directly)
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

        # Verify LLM was called twice (PRIMARY with tools, then synthesis after tool execution)
        assert mock_client.respond.call_count >= 2

        # Verify first call (PRIMARY) had tools available
        first_call = mock_client.respond.call_args_list[0]
        assert first_call.kwargs.get("tools") is not None

        # Verify second call had tool results in messages
        if mock_client.respond.call_count >= 2:
            second_call = mock_client.respond.call_args_list[1]
            messages = second_call.kwargs.get("messages", [])
            tool_messages = [m for m in messages if m.get("role") == "tool"]
            assert len(tool_messages) > 0

        # Verify result contains tool execution steps
        step_types = [step["type"] for step in result["steps"]]
        assert "tool_call" in step_types

    @patch("personal_agent.llm_client.factory.get_llm_client")
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

    @patch("personal_agent.llm_client.factory.get_llm_client")
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

    @patch("personal_agent.llm_client.factory.get_llm_client")
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

    @patch("personal_agent.llm_client.factory.get_llm_client")
    @pytest.mark.asyncio
    async def test_search_memory_tool_called_when_llm_requests_it(self, mock_client_class):
        """Integration: agent executes search_memory when LLM returns that tool call (ADR-0026)."""
        mock_client = AsyncMock()
        configure_mock_llm_client_model_configs(mock_client)
        mock_client_class.return_value = mock_client
        # Provide model_configs so executor can access model_config.id during synthesis
        mock_client.model_configs = {
            "primary": MagicMock(id="primary-model"),
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


@patch("personal_agent.llm_client.factory.get_llm_client")
@pytest.mark.asyncio
async def test_execute_task_uses_profile_resolved_model_config(mock_client_class) -> None:
    """Executor reads model_config via profile-resolved key, not raw role name (ADR-0063 §D6).

    When a cloud profile is active (primary → claude_sonnet), the executor must
    look up model_configs["claude_sonnet"], not model_configs["primary"].
    The two configs carry different effective_tool_strategy values so we can
    observe which one was used.
    """
    mock_client = AsyncMock()

    # Two distinct configs: "primary" returns PROMPT_INJECTED (old, wrong path);
    # "claude_sonnet" returns NATIVE (correct, profile-resolved path).
    primary_def = MagicMock()
    primary_def.effective_tool_strategy = ToolCallingStrategy.PROMPT_INJECTED  # wrong path
    cloud_def = MagicMock()
    cloud_def.effective_tool_strategy = ToolCallingStrategy.NATIVE  # correct path

    mock_client.model_configs = {
        "primary": primary_def,
        "claude_sonnet": cloud_def,
    }
    mock_client_class.return_value = mock_client
    mock_client.respond.return_value = {
        "role": "assistant",
        "content": "Done.",
        "tool_calls": [],
        "reasoning_trace": None,
        "usage": {"total_tokens": 10},
        "raw": {},
    }

    cloud_profile = ExecutionProfile(
        name="cloud",
        primary_model="claude_sonnet",
        sub_agent_model="claude_haiku",
        provider_type="cloud",
    )
    token = set_current_profile(cloud_profile)
    try:
        session_manager = SessionManager()
        session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)
        trace_ctx = TraceContext.new_trace()
        ctx = ExecutionContext(
            session_id=session_id,
            trace_id=trace_ctx.trace_id,
            user_message="Hello",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )
        result = await execute_task_safe(ctx, session_manager)
    finally:
        _current_profile.reset(token)

    # If executor looked up model_configs["primary"] (bug), it would get PROMPT_INJECTED
    # strategy and pass a prompt-injected tool text in the respond() call.
    # If it looked up model_configs["claude_sonnet"] (fix), NATIVE strategy is used —
    # tools are passed as a tools= kwarg, not injected into the system prompt.
    assert result["reply"]  # execution completed
    call_kwargs = mock_client.respond.call_args_list[0].kwargs
    # NATIVE strategy: tools passed via tools= kwarg (may be None if no tools registered)
    # PROMPT_INJECTED strategy: system_prompt contains tool XML — distinct observable
    system_prompt = call_kwargs.get("system_prompt", "") or ""
    assert "<tools>" not in system_prompt, (
        "Executor used PROMPT_INJECTED strategy from wrong model config lookup. "
        "Fix: executor must resolve model key through the active ExecutionProfile."
    )


# ── tool_call ID collision regression tests (Bug 2) ─────────────────────────


class TestBuildAssistantToolCalls:
    """Regression tests for tool_call ID collision across turns.

    The qwen3 server-side parser regenerates ``call_<n>`` ids each turn.
    Without a turn prefix, a multi-round tool flow produces colliding ids and
    the history sanitiser strips half the tool results as orphans.
    """

    def test_single_turn_ids_are_well_formed(self) -> None:
        """Ids preserve the server-supplied suffix and carry the turn prefix."""
        raw = [
            {"id": "call_0", "name": "bash", "arguments": "{}"},
            {"id": "call_1", "name": "bash", "arguments": '{"x":1}'},
        ]
        out = _build_assistant_tool_calls(raw, turn_id=0)
        assert len(out) == 2
        assert all(tc["type"] == "function" for tc in out)
        assert out[0]["function"]["name"] == "bash"
        # Server-supplied id is preserved as a suffix for debuggability
        assert "call_0" in out[0]["id"]
        assert "call_1" in out[1]["id"]
        # Turn prefix is present
        assert out[0]["id"].startswith("call_t0_")

    def test_ids_are_unique_across_turns(self) -> None:
        """Reproduces the orphan-stripping bug.

        Server returns call_0 every turn; after our rewrite, ids must be
        unique across turns within a request.
        """
        raw_turn0 = [
            {"id": "call_0", "name": "bash", "arguments": "{}"},
            {"id": "call_1", "name": "bash", "arguments": "{}"},
        ]
        raw_turn1 = [
            {"id": "call_0", "name": "bash", "arguments": "{}"},
            {"id": "call_1", "name": "bash", "arguments": "{}"},
        ]
        out0 = _build_assistant_tool_calls(raw_turn0, turn_id=0)
        out1 = _build_assistant_tool_calls(raw_turn1, turn_id=1)

        all_ids = [tc["id"] for tc in out0 + out1]
        assert len(set(all_ids)) == len(all_ids), (
            f"Expected 4 unique ids across two turns, got duplicates in {all_ids}"
        )

    def test_missing_id_falls_back_to_turn_prefixed_id(self) -> None:
        """Empty server-supplied id falls back to a synthetic turn-prefixed id."""
        raw = [{"id": "", "name": "bash", "arguments": "{}"}]
        out = _build_assistant_tool_calls(raw, turn_id=3)
        # No empty id sneaks through
        assert out[0]["id"]
        assert out[0]["id"].startswith("call_t3_")

    def test_sanitiser_does_not_orphan_two_turn_history(self) -> None:
        """End-to-end regression: two-turn flow with colliding server ids stays clean.

        Builds two assistant turns plus their tool results using the executor's
        id-construction helper. Runs the sanitiser. Expects zero orphans —
        this would have failed with the pre-fix code.
        """
        # Turn 0: server emits call_0, call_1
        turn0 = _build_assistant_tool_calls(
            [
                {"id": "call_0", "name": "bash", "arguments": "{}"},
                {"id": "call_1", "name": "bash", "arguments": "{}"},
            ],
            turn_id=0,
        )
        # Turn 1: server emits call_0, call_1 again (the bug)
        turn1 = _build_assistant_tool_calls(
            [
                {"id": "call_0", "name": "bash", "arguments": "{}"},
                {"id": "call_1", "name": "bash", "arguments": "{}"},
            ],
            turn_id=1,
        )

        history: list[dict] = [
            {"role": "user", "content": "do work"},
            {"role": "assistant", "content": "ok", "tool_calls": turn0},
            {"role": "tool", "tool_call_id": turn0[0]["id"], "content": "ok0"},
            {"role": "tool", "tool_call_id": turn0[1]["id"], "content": "ok1"},
            {"role": "user", "content": "more"},
            {"role": "assistant", "content": "ok", "tool_calls": turn1},
            {"role": "tool", "tool_call_id": turn1[0]["id"], "content": "ok2"},
            {"role": "tool", "tool_call_id": turn1[1]["id"], "content": "ok3"},
        ]
        _, report = sanitise_messages(history)
        assert report.orphaned_results_stripped == 0, (
            "Tool results should not be orphaned across turns when ids are turn-prefixed."
        )
        assert report.orphaned_calls_stripped == 0


# ── conversation role validator merge bug regression tests (Bug 3) ──────────


class TestRoleValidatorMergeBug:
    """Regression tests for `_validate_and_fix_conversation_roles`.

    The valid OpenAI tool flow is::

        user, assistant{tool_calls}, tool, tool, …, assistant{tool_calls or content}

    Pre-fix the validator merged the two assistants because tool messages did
    not reset its same-role detector. The merge dropped the second assistant's
    ``tool_calls``, making the matching tool results orphans on the next turn —
    which the sanitiser then stripped. The runaway loop we observed today.
    """

    def test_two_assistants_with_intervening_tools_not_merged(self) -> None:
        """The valid multi-turn tool flow: two assistants separated by tool messages.

        Both assistants must survive with their own tool_calls intact. This is
        the regression test for the bug that caused the runaway loop.
        """
        history: list[dict] = [
            {"role": "user", "content": "do work"},
            {
                "role": "assistant",
                "content": "running checks",
                "tool_calls": [
                    {
                        "id": "A1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": "{}"},
                    },
                    {
                        "id": "A2",
                        "type": "function",
                        "function": {"name": "bash", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "A1", "content": "result1"},
            {"role": "tool", "tool_call_id": "A2", "content": "result2"},
            {
                "role": "assistant",
                "content": "synthesis",
                "tool_calls": [
                    {
                        "id": "B1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": "{}"},
                    },
                ],
            },
        ]
        out = _validate_and_fix_conversation_roles(history)
        roles = [m.get("role") for m in out]
        assert roles == ["user", "assistant", "tool", "tool", "assistant"]
        # Both assistants present, tool_calls intact on each
        asst_idxs = [i for i, m in enumerate(out) if m.get("role") == "assistant"]
        assert len(asst_idxs) == 2
        first_calls = [tc["id"] for tc in out[asst_idxs[0]]["tool_calls"]]
        second_calls = [tc["id"] for tc in out[asst_idxs[1]]["tool_calls"]]
        assert first_calls == ["A1", "A2"]
        assert second_calls == ["B1"]

    def test_consecutive_assistants_no_tools_still_merge(self) -> None:
        """True consecutive duplicates (no intervening tools) still merge."""
        history: list[dict] = [
            {"role": "user", "content": "ask"},
            {"role": "assistant", "content": "first part"},
            {"role": "assistant", "content": "second part"},
        ]
        out = _validate_and_fix_conversation_roles(history)
        roles = [m.get("role") for m in out]
        assert roles == ["user", "assistant"]
        merged = out[1]["content"]
        assert "first part" in merged and "second part" in merged

    def test_consecutive_assistants_no_tools_preserve_tool_calls(self) -> None:
        """Merging carries over the second assistant's tool_calls.

        When two assistants are truly adjacent (no intervening tool messages)
        and we have to merge them, the merged-in assistant's tool_calls must
        still survive rather than being silently dropped.
        """
        history: list[dict] = [
            {
                "role": "user",
                "content": "do",
            },
            {
                "role": "assistant",
                "content": "I'll think first",
            },
            {
                "role": "assistant",
                "content": "and now act",
                "tool_calls": [
                    {
                        "id": "X1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": "{}"},
                    },
                ],
            },
        ]
        out = _validate_and_fix_conversation_roles(history)
        # Merged into one assistant
        roles = [m.get("role") for m in out]
        assert roles == ["user", "assistant"]
        # The merged assistant carries the tool_calls
        assert out[1].get("tool_calls"), "merged assistant must preserve tool_calls"
        assert out[1]["tool_calls"][0]["id"] == "X1"

    def test_role_alternation_failure_log_does_not_fire_for_tool_separated_assistants(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No alternation_failed log for the valid tool-separated pattern.

        The final-validation pass must NOT emit
        ``conversation_role_alternation_failed`` for the OpenAI tool flow
        ``assistant{tool_calls} → tool → assistant{...}``.
        """
        history: list[dict] = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "calling tool",
                "tool_calls": [
                    {
                        "id": "T1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "T1", "content": "ok"},
            {"role": "assistant", "content": "done"},
        ]
        with caplog.at_level("ERROR"):
            _validate_and_fix_conversation_roles(history)
        for record in caplog.records:
            assert "conversation_role_alternation_failed" not in record.getMessage(), (
                f"alternation_failed fired incorrectly: {record.getMessage()}"
            )

    def test_three_round_tool_flow_keeps_every_assistant(self) -> None:
        """Multi-round tool flow: A → tool → A → tool → A. All three assistants survive."""
        history: list[dict] = [
            {"role": "user", "content": "ask"},
            {
                "role": "assistant",
                "content": "step1",
                "tool_calls": [
                    {
                        "id": "R1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "R1", "content": "r1"},
            {
                "role": "assistant",
                "content": "step2",
                "tool_calls": [
                    {
                        "id": "R2",
                        "type": "function",
                        "function": {"name": "bash", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "R2", "content": "r2"},
            {"role": "assistant", "content": "synthesis"},
        ]
        out = _validate_and_fix_conversation_roles(history)
        asst_idxs = [i for i, m in enumerate(out) if m.get("role") == "assistant"]
        assert len(asst_idxs) == 3
        assert out[asst_idxs[0]]["tool_calls"][0]["id"] == "R1"
        assert out[asst_idxs[1]]["tool_calls"][0]["id"] == "R2"
        assert "synthesis" in out[asst_idxs[2]]["content"]


class TestExecutorRecallIdentityThreading:
    """Executor inline recall path threads request identity (FRE-673).

    The inline enrichment runs only when ``gateway_output is None``. Before the
    fix both query_memory and query_memory_broad were called without identity, so
    the visibility filter dropped 100% of 'group' memory → candidate_set_size=0.
    """

    @staticmethod
    def _make_ctx(message: str, uid) -> ExecutionContext:
        return ExecutionContext(
            session_id="sess-673",
            trace_id="trace-673",
            user_message=message,
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
            gateway_output=None,  # forces the inline enrichment path
            user_id=uid,
            authenticated=True,
        )

    @pytest.mark.asyncio
    async def test_entity_path_threads_identity(self) -> None:
        """Entity-name recall passes ctx.user_id + ctx.authenticated to query_memory."""
        from personal_agent.config import settings

        uid = uuid4()
        mock_memory = MagicMock()
        mock_memory.connected = True
        mock_memory.query_memory = AsyncMock(return_value=MemoryQueryResult())
        mock_memory.query_memory_broad = AsyncMock(return_value={})

        ctx = self._make_ctx("Tell me about Athens", uid)
        trace_ctx = TraceContext(trace_id="trace-673", user_id=uid, session_id="sess-673")

        with (
            patch.object(settings, "enable_memory_graph", True),
            patch("personal_agent.service.app.memory_service", mock_memory),
            patch(
                "personal_agent.orchestrator.executor.is_memory_recall_query", return_value=False
            ),
        ):
            await step_init(ctx, SessionManager(), trace_ctx)

        mock_memory.query_memory.assert_awaited_once()
        kwargs = mock_memory.query_memory.call_args.kwargs
        assert kwargs.get("user_id") == uid
        assert kwargs.get("authenticated") is True

    @pytest.mark.asyncio
    async def test_broad_path_threads_identity(self) -> None:
        """Broad recall passes ctx.user_id + ctx.authenticated to query_memory_broad."""
        from personal_agent.config import settings

        uid = uuid4()
        mock_memory = MagicMock()
        mock_memory.connected = True
        mock_memory.query_memory = AsyncMock(return_value=MemoryQueryResult())
        mock_memory.query_memory_broad = AsyncMock(
            return_value={"entities": [], "sessions": [], "turns_summary": []}
        )

        ctx = self._make_ctx("What have we discussed before?", uid)
        trace_ctx = TraceContext(trace_id="trace-673", user_id=uid, session_id="sess-673")

        with (
            patch.object(settings, "enable_memory_graph", True),
            patch("personal_agent.service.app.memory_service", mock_memory),
            patch("personal_agent.orchestrator.executor.is_memory_recall_query", return_value=True),
        ):
            await step_init(ctx, SessionManager(), trace_ctx)

        mock_memory.query_memory_broad.assert_awaited_once()
        kwargs = mock_memory.query_memory_broad.call_args.kwargs
        assert kwargs.get("user_id") == uid
        assert kwargs.get("authenticated") is True


class TestStepInitAttachmentResolution:
    """FRE-666 / ADR-0101 §3, §4 — turn-assembly image-block injection."""

    @staticmethod
    def _make_ctx(message: str, attachments: tuple[AttachmentRef, ...] = ()) -> ExecutionContext:
        return ExecutionContext(
            session_id="sess-666",
            trace_id="trace-666",
            user_message=message,
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
            gateway_output=None,
            attachments=attachments,
        )

    @pytest.mark.asyncio
    async def test_no_attachments_content_stays_string(self) -> None:
        ctx = self._make_ctx("hello")
        trace_ctx = TraceContext(trace_id="trace-666", session_id="sess-666")

        await step_init(ctx, SessionManager(), trace_ctx)

        assert ctx.messages[-1]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_image_attachment_injects_block_list(self) -> None:
        """AC-3: the assembled user turn content is a list containing a typed image block."""
        attachment = AttachmentRef(
            artifact_id="a1",
            content_type="image/png",
            title="photo.png",
            r2_key="upload/u/g/photo.png",
        )
        ctx = self._make_ctx("Look at this", attachments=(attachment,))
        trace_ctx = TraceContext(trace_id="trace-666", session_id="sess-666")

        image_block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        with patch(
            "personal_agent.orchestrator.attachment_resolution.resolve_attachments",
            new=AsyncMock(return_value=SimpleNamespace(blocks=(image_block,), disclosures=())),
        ):
            await step_init(ctx, SessionManager(), trace_ctx)

        content = ctx.messages[-1]["content"]
        assert isinstance(content, list)
        types = {block["type"] for block in content}
        assert types == {"text", "image_url"}
        text_block = next(b for b in content if b["type"] == "text")
        assert text_block["text"] == "Look at this"

    @pytest.mark.asyncio
    async def test_empty_user_message_with_attachment_omits_text_block(self) -> None:
        attachment = AttachmentRef(
            artifact_id="a1",
            content_type="image/png",
            title="photo.png",
            r2_key="upload/u/g/photo.png",
        )
        ctx = self._make_ctx("", attachments=(attachment,))
        trace_ctx = TraceContext(trace_id="trace-666", session_id="sess-666")

        image_block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        with patch(
            "personal_agent.orchestrator.attachment_resolution.resolve_attachments",
            new=AsyncMock(return_value=SimpleNamespace(blocks=(image_block,), disclosures=())),
        ):
            await step_init(ctx, SessionManager(), trace_ctx)

        content = ctx.messages[-1]["content"]
        assert content == [image_block]

    @pytest.mark.asyncio
    async def test_disclosures_copied_onto_ctx(self) -> None:
        attachment = AttachmentRef(
            artifact_id="a1",
            content_type="image/png",
            title="photo.png",
            r2_key="upload/u/g/photo.png",
        )
        ctx = self._make_ctx("hi", attachments=(attachment,))
        trace_ctx = TraceContext(trace_id="trace-666", session_id="sess-666")

        with patch(
            "personal_agent.orchestrator.attachment_resolution.resolve_attachments",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    blocks=(),
                    disclosures=("Image 'photo.png' was downscaled to fit the size limit.",),
                )
            ),
        ):
            await step_init(ctx, SessionManager(), trace_ctx)

        assert ctx.attachment_disclosures == [
            "Image 'photo.png' was downscaled to fit the size limit."
        ]


class TestStepInitDocumentResolution:
    """FRE-684 / ADR-0102 T4 — turn-assembly document-block injection + routing."""

    @staticmethod
    def _make_ctx(message: str, attachments: tuple[AttachmentRef, ...] = ()) -> ExecutionContext:
        return ExecutionContext(
            session_id="sess-684",
            trace_id="trace-684",
            user_message=message,
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
            gateway_output=None,
            attachments=attachments,
        )

    @staticmethod
    def _pdf_attachment(**overrides: object) -> AttachmentRef:
        defaults: dict[str, object] = {
            "artifact_id": "doc-1",
            "content_type": "application/pdf",
            "title": "report.pdf",
            "r2_key": "upload/u/g/report.pdf",
        }
        defaults.update(overrides)
        return AttachmentRef(**defaults)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_document_attachment_injects_document_block(self) -> None:
        attachment = self._pdf_attachment()
        ctx = self._make_ctx("Look at this", attachments=(attachment,))
        trace_ctx = TraceContext(trace_id="trace-684", session_id="sess-684")

        doc_block = {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": "AAAA"},
        }
        with (
            patch(
                "personal_agent.orchestrator.document_resolution.resolve_documents",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        blocks=(doc_block,), disclosures=(), used_tier2=True
                    )
                ),
            ),
            patch(
                "personal_agent.orchestrator.executor._resolve_document_routing_key",
                return_value=("primary", "native_pdf"),
            ),
        ):
            await step_init(ctx, SessionManager(), trace_ctx)

        content = ctx.messages[-1]["content"]
        assert isinstance(content, list)
        types = {block["type"] for block in content}
        assert types == {"text", "document"}

    @pytest.mark.asyncio
    async def test_tier1_document_does_not_set_document_effective_model_key(self) -> None:
        attachment = self._pdf_attachment()
        ctx = self._make_ctx("Look at this", attachments=(attachment,))
        trace_ctx = TraceContext(trace_id="trace-684", session_id="sess-684")

        text_block = {"type": "text", "text": "extracted text"}
        with (
            patch(
                "personal_agent.orchestrator.document_resolution.resolve_documents",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        blocks=(text_block,), disclosures=(), used_tier2=False
                    )
                ),
            ),
            patch(
                "personal_agent.orchestrator.executor._resolve_document_routing_key",
                side_effect=AssertionError("must not be called for a Tier-1-only turn"),
            ),
        ):
            await step_init(ctx, SessionManager(), trace_ctx)

        assert ctx.document_effective_model_key is None

    @pytest.mark.asyncio
    async def test_tier2_document_sets_document_effective_model_key(self) -> None:
        attachment = self._pdf_attachment()
        ctx = self._make_ctx("Look at this", attachments=(attachment,))
        trace_ctx = TraceContext(trace_id="trace-684", session_id="sess-684")

        doc_block = {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AAAA"},
        }
        with (
            patch(
                "personal_agent.orchestrator.document_resolution.resolve_documents",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        blocks=(doc_block,), disclosures=(), used_tier2=True
                    )
                ),
            ),
            patch(
                "personal_agent.orchestrator.executor._resolve_document_routing_key",
                return_value=("claude_sonnet", "rasterize"),
            ),
        ):
            await step_init(ctx, SessionManager(), trace_ctx)

        assert ctx.document_effective_model_key == "claude_sonnet"

    @pytest.mark.asyncio
    async def test_rejected_oversized_document_does_not_force_document_effective_model_key(
        self,
    ) -> None:
        """A native-PDF block rejected for exceeding the payload cap (used_tier2=True,

        blocks=()) must not force the rest of the turn — including a plain-text
        question with no surviving document content — onto the escalated/cloud
        model (code-review finding: a rejected document was still dragging the
        whole turn onto cloud with no visible reason).
        """
        attachment = self._pdf_attachment()
        ctx = self._make_ctx("Just a question, no document content survived", (attachment,))
        trace_ctx = TraceContext(trace_id="trace-684", session_id="sess-684")

        with (
            patch(
                "personal_agent.orchestrator.document_resolution.resolve_documents",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        blocks=(),
                        disclosures=("Document 'report.pdf' was not included: too large.",),
                        used_tier2=True,
                    )
                ),
            ),
            patch(
                "personal_agent.orchestrator.executor._resolve_document_routing_key",
                return_value=("claude_sonnet", "native_pdf"),
            ),
        ):
            await step_init(ctx, SessionManager(), trace_ctx)

        assert ctx.document_effective_model_key is None
        # The turn's content stays plain text — no empty/dangling block list.
        assert ctx.messages[-1]["content"] == "Just a question, no document content survived"

    @pytest.mark.asyncio
    async def test_document_disclosures_merged_onto_ctx(self) -> None:
        attachment = self._pdf_attachment()
        ctx = self._make_ctx("Look at this", attachments=(attachment,))
        trace_ctx = TraceContext(trace_id="trace-684", session_id="sess-684")

        with (
            patch(
                "personal_agent.orchestrator.document_resolution.resolve_documents",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        blocks=(),
                        disclosures=("2 of 5 page(s) of 'report.pdf' were not included.",),
                        used_tier2=True,
                    )
                ),
            ),
            patch(
                "personal_agent.orchestrator.executor._resolve_document_routing_key",
                return_value=("primary", "native_pdf"),
            ),
        ):
            await step_init(ctx, SessionManager(), trace_ctx)

        assert ctx.attachment_disclosures == ["2 of 5 page(s) of 'report.pdf' were not included."]

    @pytest.mark.asyncio
    async def test_document_routing_failure_propagates_as_attachment_unsupported(self) -> None:
        from personal_agent.exceptions import AttachmentUnsupportedError

        attachment = self._pdf_attachment()
        ctx = self._make_ctx("Look at this", attachments=(attachment,))
        trace_ctx = TraceContext(trace_id="trace-684", session_id="sess-684")

        with patch(
            "personal_agent.orchestrator.document_resolution.resolve_documents",
            new=AsyncMock(
                side_effect=AttachmentUnsupportedError(
                    "This turn includes a document, but the model serving this "
                    "conversation does not support it."
                )
            ),
        ):
            with pytest.raises(AttachmentUnsupportedError):
                await step_init(ctx, SessionManager(), trace_ctx)

    @pytest.mark.asyncio
    async def test_document_blocks_do_not_reach_maybe_confirm_attachment_cost(self) -> None:
        """Document-only turn (no raster images) — the pre-flight cost-confirm

        UX stays image-only (T5/FRE-686 scope); it must not be called at all
        for a document-only turn, matching the real ``if resolved_blocks and
        not await ...`` gating.
        """
        attachment = self._pdf_attachment()
        ctx = self._make_ctx("Look at this", attachments=(attachment,))
        trace_ctx = TraceContext(trace_id="trace-684", session_id="sess-684")

        doc_block = {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": "AAAA"},
        }
        with (
            patch(
                "personal_agent.orchestrator.document_resolution.resolve_documents",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        blocks=(doc_block,), disclosures=(), used_tier2=True
                    )
                ),
            ),
            patch(
                "personal_agent.orchestrator.executor._resolve_document_routing_key",
                return_value=("primary", "native_pdf"),
            ),
            patch(
                "personal_agent.orchestrator.executor._maybe_confirm_attachment_cost",
                new=AsyncMock(return_value=True),
            ) as mock_confirm,
        ):
            await step_init(ctx, SessionManager(), trace_ctx)

        mock_confirm.assert_not_called()


class TestMaybeConfirmAttachmentCostDocumentConsistency:
    """FRE-684 — the image cost-gate must check the document-driven effective

    key when one was set at turn assembly, not silently recompute a stale
    image-only key.
    """

    @pytest.mark.asyncio
    async def test_uses_document_effective_model_key_when_set(self) -> None:
        from personal_agent.llm_client.models import ModelDefinition

        ctx = ExecutionContext(
            session_id="s1",
            trace_id="t1",
            user_message="hi",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
            attachments=(
                AttachmentRef(
                    artifact_id="img-1",
                    content_type="image/png",
                    title="photo.png",
                    r2_key="upload/u/g/photo.png",
                ),
            ),
            document_effective_model_key="claude_sonnet",
        )
        image_block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}

        mock_config = MagicMock()
        mock_config.models = {
            "claude_sonnet": ModelDefinition(
                id="claude-sonnet",
                context_length=200000,
                max_concurrency=4,
                default_timeout=60,
                provider_type="cloud",
                supports_vision=True,
                input_cost_per_token=0.000003,
            )
        }

        with (
            patch(
                "personal_agent.config.model_loader.load_model_config",
                return_value=mock_config,
            ),
            patch(
                "personal_agent.orchestrator.executor._resolve_vision_routing_key",
                side_effect=AssertionError(
                    "must not recompute image-only routing when "
                    "document_effective_model_key is already set"
                ),
            ),
            patch(
                "personal_agent.orchestrator.executor._maybe_pause_for_constraint",
                new=AsyncMock(return_value="proceed_cloud"),
            ),
        ):
            await _maybe_confirm_attachment_cost(ctx, (image_block,))

        # No assertion error raised above confirms _resolve_vision_routing_key
        # was never called — the document-driven key was used instead.


class TestStepSynthesisAttachmentDisclosure:
    """FRE-666 / ADR-0101 §6, FRE-690 — guardrail disclosures reach the actual reply."""

    @staticmethod
    def _make_ctx(final_reply: str | None, disclosures: list[str]) -> ExecutionContext:
        ctx = ExecutionContext(
            session_id="sess-666-synth",
            trace_id="trace-666-synth",
            user_message="hi",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )
        ctx.final_reply = final_reply
        ctx.attachment_disclosures = disclosures
        return ctx

    @pytest.mark.asyncio
    async def test_disclosures_appended_to_final_reply(self) -> None:
        ctx = self._make_ctx(
            "Here's what I see.",
            ["Image 'a.png' was downscaled to fit the size limit."],
        )
        trace_ctx = TraceContext(trace_id="trace-666-synth", session_id="sess-666-synth")

        session_manager = SessionManager()
        session_manager.create_session(Mode.NORMAL, Channel.CHAT, session_id=ctx.session_id)
        await step_synthesis(ctx, session_manager, trace_ctx)

        assert "Here's what I see." in ctx.final_reply
        assert "Image 'a.png' was downscaled to fit the size limit." in ctx.final_reply

    @pytest.mark.asyncio
    async def test_no_disclosures_leaves_final_reply_unchanged(self) -> None:
        ctx = self._make_ctx("Here's what I see.", [])
        trace_ctx = TraceContext(trace_id="trace-666-synth", session_id="sess-666-synth")

        session_manager = SessionManager()
        session_manager.create_session(Mode.NORMAL, Channel.CHAT, session_id=ctx.session_id)
        await step_synthesis(ctx, session_manager, trace_ctx)

        assert ctx.final_reply == "Here's what I see."
