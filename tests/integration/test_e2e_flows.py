"""End-to-end integration tests for key system scenarios.

These tests validate complete flows across the entire system stack:
telemetry → governance → orchestrator → LLM/tools → response.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.orchestrator import Channel, Orchestrator
from personal_agent.telemetry.metrics import get_trace_events

# ============================================================================
# Chat Scenario Tests
# ============================================================================


@pytest.mark.asyncio
async def test_e2e_simple_chat_query():
    """Test simple chat query handled by router."""
    with patch("personal_agent.orchestrator.executor.LocalLLMClient") as mock_llm_class:
        mock_llm = AsyncMock()
        mock_llm_class.return_value = mock_llm

        # Mock router response (HANDLE decision)
        mock_llm.respond.return_value = MagicMock(
            message_content="Hello! I'm doing well, thank you for asking.",
            model="qwen3-router",
            usage={"input_tokens": 50, "output_tokens": 15},
            tool_calls=None,
        )

        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Hello! How are you?",
            mode=None,
            channel=Channel.CHAT,
        )

        assert result["success"]
        assert result["reply"]
        assert "hello" in result["reply"].lower() or "well" in result["reply"].lower()

        # Verify telemetry
        trace_events = get_trace_events(result["trace_id"])
        assert any(e.get("event") == "task_started" for e in trace_events)
        assert any(e.get("event") == "task_completed" for e in trace_events)


@pytest.mark.asyncio
async def test_e2e_complex_chat_delegation():
    """Test complex query delegated from router to reasoning model."""
    with patch("personal_agent.orchestrator.executor.LocalLLMClient") as mock_llm_class:
        mock_llm = AsyncMock()
        mock_llm_class.return_value = mock_llm

        # Router delegates to STANDARD
        mock_llm.respond.side_effect = [
            # Router response (DELEGATE to STANDARD)
            MagicMock(
                message_content='{"decision": "DELEGATE", "target_model": "STANDARD", "reason": "Complex question"}',
                model="qwen3-router",
                usage={"input_tokens": 100, "output_tokens": 30},
                tool_calls=None,
            ),
            # STANDARD response
            MagicMock(
                message_content="Python is a high-level programming language known for its simplicity and readability.",
                model="qwen3-standard",
                usage={"input_tokens": 120, "output_tokens": 25},
                tool_calls=None,
            ),
        ]

        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="What is Python?",
            mode=None,
            channel=Channel.CHAT,
        )

        assert result["success"]
        assert result["reply"]
        assert "python" in result["reply"].lower()

        # Verify routing delegation happened
        trace_events = get_trace_events(result["trace_id"])
        assert any(
            e.get("event") == "routing_decision" and e.get("decision") == "DELEGATE"
            for e in trace_events
        )


# ============================================================================
# System Health Scenario Tests
# ============================================================================


@pytest.mark.asyncio
async def test_e2e_system_health_with_tools():
    """Test system health query using tools."""
    with (
        patch("personal_agent.orchestrator.executor.LocalLLMClient") as mock_llm_class,
        patch("personal_agent.tools.system_health.collect_system_metrics") as mock_metrics,
    ):
        mock_llm = AsyncMock()
        mock_llm_class.return_value = mock_llm

        # Mock system metrics
        mock_metrics.return_value = {
            "cpu_load_percent": 45.2,
            "memory_used_percent": 62.5,
            "disk_used_percent": 70.0,
        }

        # Router delegates to STANDARD
        # STANDARD requests tool
        # STANDARD synthesizes response
        mock_llm.respond.side_effect = [
            # Router: DELEGATE to STANDARD
            MagicMock(
                message_content='{"decision": "DELEGATE", "target_model": "STANDARD", "reason": "System health query"}',
                model="qwen3-router",
                usage={"input_tokens": 100, "output_tokens": 30},
                tool_calls=None,
            ),
            # STANDARD: Request tool
            MagicMock(
                message_content="I'll check your system health.",
                model="qwen3-standard",
                usage={"input_tokens": 120, "output_tokens": 20},
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "system_metrics_snapshot",
                            "arguments": "{}",
                        },
                    }
                ],
            ),
            # STANDARD: Synthesize response
            MagicMock(
                message_content="Your Mac is running well. CPU at 45%, memory at 63%, disk at 70%.",
                model="qwen3-standard",
                usage={"input_tokens": 200, "output_tokens": 25},
                tool_calls=None,
            ),
        ]

        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="What is my Mac's health?",
            mode=None,
            channel=Channel.SYSTEM_HEALTH,
        )

        assert result["success"]
        assert result["reply"]
        # Should mention metrics
        assert any(
            keyword in result["reply"].lower()
            for keyword in ["cpu", "memory", "disk", "45", "63", "70"]
        )

        # Verify tool was called
        trace_events = get_trace_events(result["trace_id"])
        assert any(
            e.get("event") == "tool_call_started"
            and e.get("tool_name") == "system_metrics_snapshot"
            for e in trace_events
        )


# ============================================================================
# Error Handling Tests
# ============================================================================


@pytest.mark.asyncio
async def test_e2e_llm_timeout_handling():
    """Test graceful handling of LLM timeouts."""
    with patch("personal_agent.orchestrator.executor.LocalLLMClient") as mock_llm_class:
        mock_llm = AsyncMock()
        mock_llm_class.return_value = mock_llm

        # Simulate timeout
        mock_llm.respond.side_effect = asyncio.TimeoutError("Model timeout")

        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Hello",
            mode=None,
            channel=Channel.CHAT,
        )

        # Should not crash, should return error state
        assert not result["success"]
        assert result.get("error") is not None

        # Verify error logged
        trace_events = get_trace_events(result["trace_id"])
        assert any(e.get("event") == "task_failed" for e in trace_events)


@pytest.mark.asyncio
async def test_e2e_tool_execution_failure():
    """Test handling of tool execution failures."""
    with (
        patch("personal_agent.orchestrator.executor.LocalLLMClient") as mock_llm_class,
        patch("personal_agent.tools.filesystem.read_file") as mock_read,
    ):
        mock_llm = AsyncMock()
        mock_llm_class.return_value = mock_llm

        # Mock tool failure
        mock_read.side_effect = FileNotFoundError("File not found")

        # Router delegates to STANDARD
        # STANDARD requests tool (which fails)
        # STANDARD synthesizes error response
        mock_llm.respond.side_effect = [
            # Router: DELEGATE to STANDARD
            MagicMock(
                message_content='{"decision": "DELEGATE", "target_model": "STANDARD"}',
                model="qwen3-router",
                usage={"input_tokens": 100, "output_tokens": 30},
                tool_calls=None,
            ),
            # STANDARD: Request tool
            MagicMock(
                message_content="I'll read that file.",
                model="qwen3-standard",
                usage={"input_tokens": 120, "output_tokens": 10},
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "/nonexistent/file.txt"}',
                        },
                    }
                ],
            ),
            # STANDARD: Synthesize error response
            MagicMock(
                message_content="I couldn't read the file - it doesn't exist.",
                model="qwen3-standard",
                usage={"input_tokens": 150, "output_tokens": 15},
                tool_calls=None,
            ),
        ]

        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Read /nonexistent/file.txt",
            mode=None,
            channel=Channel.CODE_TASK,
        )

        # Should complete (not crash), but indicate tool failure in response
        assert result["success"]  # Orchestrator completed successfully
        assert "exist" in result["reply"].lower() or "not found" in result["reply"].lower()

        # Verify tool failure logged
        trace_events = get_trace_events(result["trace_id"])
        assert any(e.get("event") == "tool_call_failed" for e in trace_events)


# ============================================================================
# Governance Integration Tests
# ============================================================================


@pytest.mark.asyncio
async def test_e2e_mode_enforcement():
    """Test that mode constraints are enforced."""
    with (
        patch("personal_agent.orchestrator.executor.LocalLLMClient") as mock_llm_class,
        patch("personal_agent.brainstem.mode_manager.ModeManager") as mock_mode_mgr_class,
    ):
        mock_llm = AsyncMock()
        mock_llm_class.return_value = mock_llm

        mock_mode_mgr = MagicMock()
        mock_mode_mgr_class.return_value = mock_mode_mgr
        mock_mode_mgr.get_current_mode.return_value = "NORMAL"

        mock_llm.respond.return_value = MagicMock(
            message_content="Hello!",
            model="qwen3-router",
            usage={"input_tokens": 50, "output_tokens": 10},
            tool_calls=None,
        )

        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Hello",
            mode=None,  # Should query brainstem
            channel=Channel.CHAT,
        )

        assert result["success"]
        # Verify mode was queried
        mock_mode_mgr.get_current_mode.assert_called()


# ============================================================================
# Telemetry Integration Tests
# ============================================================================


@pytest.mark.asyncio
async def test_e2e_telemetry_trace_reconstruction():
    """Test that full execution can be reconstructed from telemetry."""
    with patch("personal_agent.orchestrator.executor.LocalLLMClient") as mock_llm_class:
        mock_llm = AsyncMock()
        mock_llm_class.return_value = mock_llm

        mock_llm.respond.return_value = MagicMock(
            message_content="Hello!",
            model="qwen3-router",
            usage={"input_tokens": 50, "output_tokens": 10},
            tool_calls=None,
        )

        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Hello",
            mode=None,
            channel=Channel.CHAT,
        )

        assert result["success"]

        # Verify trace can be reconstructed
        trace_events = get_trace_events(result["trace_id"])
        assert len(trace_events) > 0

        # Should have start and end events
        event_types = [e.get("event") for e in trace_events]
        assert "task_started" in event_types
        assert "task_completed" in event_types

        # All events should have trace_id
        assert all(e.get("trace_id") == result["trace_id"] for e in trace_events)


# ============================================================================
# Performance Tests
# ============================================================================


@pytest.mark.asyncio
async def test_e2e_simple_query_performance():
    """Test that simple queries complete within acceptable time."""
    import time

    with patch("personal_agent.orchestrator.executor.LocalLLMClient") as mock_llm_class:
        mock_llm = AsyncMock()
        mock_llm_class.return_value = mock_llm

        mock_llm.respond.return_value = MagicMock(
            message_content="Hello!",
            model="qwen3-router",
            usage={"input_tokens": 50, "output_tokens": 10},
            tool_calls=None,
        )

        orchestrator = Orchestrator()
        start = time.time()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Hello",
            mode=None,
            channel=Channel.CHAT,
        )
        elapsed_ms = (time.time() - start) * 1000

        assert result["success"]
        # Simple queries should complete quickly (orchestrator overhead only)
        # With mocks, should be <1000ms (increased due to background tasks)
        assert elapsed_ms < 1000
