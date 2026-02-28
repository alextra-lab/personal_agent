"""End-to-end integration tests for key system scenarios.

These tests validate complete flows across the entire system stack:
telemetry -> governance -> orchestrator -> LLM/tools -> response.
"""

import asyncio
import logging
from contextlib import contextmanager
from typing import Any, Generator
from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.orchestrator import Channel, Orchestrator


def _make_llm_response(
    content: str,
    model: str = "qwen3-router",
    tool_calls: list | None = None,
    usage: dict | None = None,
) -> dict:
    """Build a dict matching LLMResponse TypedDict."""
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls or [],
        "reasoning_trace": None,
        "usage": usage or {"prompt_tokens": 50, "completion_tokens": 15},
        "response_id": None,
        "raw": {},
    }


def _routing_delegate(target: str = "STANDARD", reason: str = "Complex question") -> str:
    """Build a properly-formatted routing DELEGATE decision JSON string."""
    import json

    return json.dumps(
        {
            "routing_decision": "DELEGATE",
            "target_model": target,
            "confidence": 0.9,
            "reasoning_depth": 5,
            "reason": reason,
        }
    )


@contextmanager
def _e2e_patches() -> Generator[Any, None, None]:
    """Patch external services so tests don't connect to Neo4j, monitoring, etc."""
    root_logger = logging.getLogger()
    original_level = root_logger.level
    root_logger.setLevel(logging.DEBUG)
    try:
        with (
            patch("personal_agent.orchestrator.executor.settings") as mock_settings,
            patch("personal_agent.orchestrator.executor.LocalLLMClient") as mock_llm_class,
            patch("personal_agent.captains_log.background.run_in_background", lambda coro: None),
            patch("personal_agent.captains_log.capture.write_capture"),
        ):
            mock_settings.request_monitoring_enabled = False
            mock_settings.enable_memory_graph = False
            mock_settings.conversation_max_context_tokens = 6000
            mock_settings.conversation_context_strategy = "truncate"
            mock_settings.orchestrator_max_tool_iterations = 3
            mock_settings.orchestrator_max_repeated_tool_calls = 1
            mock_settings.mcp_gateway_enabled = False
            mock_settings.llm_no_think_suffix = "/no_think"
            mock_settings.llm_append_no_think_to_tool_prompts = True
            yield mock_llm_class
    finally:
        root_logger.setLevel(original_level)


# ============================================================================
# Chat Scenario Tests
# ============================================================================


@pytest.mark.asyncio
async def test_e2e_simple_chat_query():
    """Test simple chat query handled by router."""
    with _e2e_patches() as mock_llm_class:
        mock_llm = AsyncMock()
        mock_llm_class.return_value = mock_llm

        mock_llm.respond.return_value = _make_llm_response(
            content="Hello! I'm doing well, thank you for asking.",
            model="qwen3-router",
        )

        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Hello! How are you?",
            mode=None,
            channel=Channel.CHAT,
        )

        assert result["reply"]
        assert "hello" in result["reply"].lower() or "well" in result["reply"].lower()
        assert result["trace_id"]
        assert any(s.get("type") == "llm_call" for s in result["steps"])


@pytest.mark.asyncio
async def test_e2e_complex_chat_delegation():
    """Test complex query delegated from router to reasoning model."""
    with _e2e_patches() as mock_llm_class:
        mock_llm = AsyncMock()
        mock_llm_class.return_value = mock_llm

        mock_llm.respond.side_effect = [
            _make_llm_response(
                content=_routing_delegate("STANDARD", "Complex question"),
                model="qwen3-router",
                usage={"prompt_tokens": 100, "completion_tokens": 30},
            ),
            _make_llm_response(
                content="Python is a high-level programming language known for its simplicity and readability.",
                model="qwen3-standard",
                usage={"prompt_tokens": 120, "completion_tokens": 25},
            ),
        ]

        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="What is Python?",
            mode=None,
            channel=Channel.CHAT,
        )

        assert result["reply"]
        assert "python" in result["reply"].lower()
        assert len(result["steps"]) >= 2


# ============================================================================
# System Health Scenario Tests
# ============================================================================


@pytest.mark.asyncio
async def test_e2e_system_health_with_tools():
    """Test system health query using tools."""
    with _e2e_patches() as mock_llm_class:
        mock_llm = AsyncMock()
        mock_llm_class.return_value = mock_llm

        mock_llm.respond.side_effect = [
            _make_llm_response(
                content="I'll check your system health.",
                model="qwen3-reasoning",
                tool_calls=[{"id": "call_1", "name": "system_metrics_snapshot", "arguments": "{}"}],
                usage={"prompt_tokens": 120, "completion_tokens": 20},
            ),
            _make_llm_response(
                content="Your Mac is running well. CPU usage is normal.",
                model="qwen3-reasoning",
                usage={"prompt_tokens": 200, "completion_tokens": 25},
            ),
        ]

        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="What is my Mac's health?",
            mode=None,
            channel=Channel.SYSTEM_HEALTH,
        )

        assert result["reply"]
        assert any(
            step.get("type") == "tool_call"
            and (step.get("metadata") or {}).get("tool_name") == "system_metrics_snapshot"
            for step in result["steps"]
        )


# ============================================================================
# Error Handling Tests
# ============================================================================


@pytest.mark.asyncio
async def test_e2e_llm_timeout_handling():
    """Test graceful handling of LLM timeouts."""
    with _e2e_patches() as mock_llm_class:
        mock_llm = AsyncMock()
        mock_llm_class.return_value = mock_llm

        mock_llm.respond.side_effect = asyncio.TimeoutError("Model timeout")

        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Hello",
            mode=None,
            channel=Channel.CHAT,
        )

        assert "error" in result["reply"].lower() or "recovering" in result["reply"].lower()
        assert any(s.get("type") == "error" for s in result["steps"])


@pytest.mark.asyncio
async def test_e2e_tool_execution_failure():
    """Test handling of tool execution for a nonexistent file.

    The read_file tool handles missing files internally (returns error in output
    dict, not an exception). The orchestrator should still complete gracefully.
    """
    with _e2e_patches() as mock_llm_class:
        mock_llm = AsyncMock()
        mock_llm_class.return_value = mock_llm

        # Configure model_configs so the synthesis path doesn't fail on attribute access
        mock_llm.model_configs = {}

        mock_llm.respond.side_effect = [
            _make_llm_response(
                content="I'll read that file.",
                model="qwen3-coding",
                tool_calls=[
                    {
                        "id": "call_1",
                        "name": "read_file",
                        "arguments": '{"path": "/nonexistent/file.txt"}',
                    }
                ],
                usage={"prompt_tokens": 120, "completion_tokens": 10},
            ),
            _make_llm_response(
                content="The file could not be found at the specified path.",
                model="qwen3-coding",
                usage={"prompt_tokens": 150, "completion_tokens": 15},
            ),
        ]

        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Read /nonexistent/file.txt",
            mode=None,
            channel=Channel.CODE_TASK,
        )

        assert result["reply"]
        tool_steps = [s for s in result["steps"] if s.get("type") == "tool_call"]
        assert len(tool_steps) > 0
        assert tool_steps[0]["metadata"]["tool_name"] == "read_file"


# ============================================================================
# Governance Integration Tests
# ============================================================================


@pytest.mark.asyncio
async def test_e2e_mode_enforcement():
    """Test that mode constraints are enforced."""
    from personal_agent.governance.models import Mode

    with (
        _e2e_patches() as mock_llm_class,
        patch("personal_agent.orchestrator.orchestrator.get_current_mode") as mock_get_mode,
    ):
        mock_llm = AsyncMock()
        mock_llm_class.return_value = mock_llm
        mock_get_mode.return_value = Mode.NORMAL

        mock_llm.respond.return_value = _make_llm_response(
            content="Hello!",
            model="qwen3-router",
        )

        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Hello",
            mode=None,
            channel=Channel.CHAT,
        )

        assert result["reply"]
        mock_get_mode.assert_called()


# ============================================================================
# Telemetry Integration Tests
# ============================================================================


@pytest.mark.asyncio
async def test_e2e_telemetry_trace_reconstruction():
    """Test that full execution can be reconstructed from telemetry."""
    with _e2e_patches() as mock_llm_class:
        mock_llm = AsyncMock()
        mock_llm_class.return_value = mock_llm

        mock_llm.respond.return_value = _make_llm_response(
            content="Hello!",
            model="qwen3-router",
        )

        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="test-session",
            user_message="Hello",
            mode=None,
            channel=Channel.CHAT,
        )

        assert result["reply"]
        assert result["trace_id"]
        assert len(result["steps"]) > 0
        assert any(s.get("type") == "llm_call" for s in result["steps"])


# ============================================================================
# Performance Tests
# ============================================================================


@pytest.mark.asyncio
async def test_e2e_simple_query_performance():
    """Test that simple queries complete within acceptable time."""
    import time

    with _e2e_patches() as mock_llm_class:
        mock_llm = AsyncMock()
        mock_llm_class.return_value = mock_llm

        mock_llm.respond.return_value = _make_llm_response(
            content="Hello!",
            model="qwen3-router",
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

        assert result["reply"]
        assert elapsed_ms < 1000
