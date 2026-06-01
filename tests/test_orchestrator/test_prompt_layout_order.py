"""Tests for volatility-gradient system prompt layout (ADR-0081 D1, FRE-422).

Verifies that the assembled system prompt places STATIC fragments (tool rules)
before VOLATILE fragments (memory section), so the KV-cache boundary sits between
them and the static prefix is reusable across turns.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.governance.models import Mode
from personal_agent.orchestrator import Channel
from personal_agent.orchestrator.executor import execute_task_safe
from personal_agent.orchestrator.session import SessionManager
from personal_agent.orchestrator.types import ExecutionContext
from personal_agent.telemetry.trace import TraceContext
from tests.test_orchestrator.conftest import configure_mock_llm_client_model_configs

_TOOL_PROMPT_MARKER = "You are a tool-using assistant"
_MEMORY_MARKER = "## Your Memory Graph"
_MEMORY_CONTEXT = [
    {
        "type": "entity",
        "name": "Python",
        "entity_type": "Technology",
        "description": "A programming language the user works with daily",
        "mentions": 5,
    }
]


def _make_mock_client() -> AsyncMock:
    mock = AsyncMock()
    configure_mock_llm_client_model_configs(mock)
    mock.respond.return_value = {
        "role": "assistant",
        "content": "Done.",
        "tool_calls": [],
        "reasoning_trace": None,
        "usage": {"total_tokens": 10, "prompt_tokens": 8, "completion_tokens": 2},
        "raw": {},
        "cost_usd": 0.0,
    }
    return mock


@patch("personal_agent.llm_client.factory.get_llm_client")
@pytest.mark.asyncio
async def test_tool_prompt_before_memory_section(mock_client_class: MagicMock) -> None:
    """tool_prompt (STATIC) must appear before memory_section (VOLATILE) in assembled prompt.

    ADR-0081 D1: the byte layout must be monotone in mutation frequency so the
    KV-cache boundary sits after the static prefix and before the volatile tail.
    """
    mock_client = _make_mock_client()
    mock_client_class.return_value = mock_client

    session_manager = SessionManager()
    session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)
    trace_ctx = TraceContext.new_trace()
    ctx = ExecutionContext(
        session_id=session_id,
        trace_id=trace_ctx.trace_id,
        user_message="What do you know about Python?",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        memory_context=_MEMORY_CONTEXT,
    )

    await execute_task_safe(ctx, session_manager)

    call_kwargs = mock_client.respond.call_args_list[0].kwargs
    system_prompt: str = call_kwargs.get("system_prompt", "") or ""

    assert _TOOL_PROMPT_MARKER in system_prompt, "tool_prompt not found in assembled system_prompt"
    assert _MEMORY_MARKER in system_prompt, "memory_section not found in assembled system_prompt"

    tool_idx = system_prompt.index(_TOOL_PROMPT_MARKER)
    memory_idx = system_prompt.index(_MEMORY_MARKER)
    assert tool_idx < memory_idx, (
        f"tool_prompt (STATIC) must appear before memory_section (VOLATILE). "
        f"tool_idx={tool_idx}, memory_idx={memory_idx}. "
        f"This is the ADR-0081 D1 volatility-gradient invariant."
    )


@patch("personal_agent.llm_client.factory.get_llm_client")
@pytest.mark.asyncio
async def test_empty_memory_prompt_is_well_formed(mock_client_class: MagicMock) -> None:
    """When there is no memory_context, the assembled system_prompt must be well-formed.

    Verifies the empty-memory path produces a stable, non-empty prompt with no
    dangling headers or trailing whitespace churn.
    """
    mock_client = _make_mock_client()
    mock_client_class.return_value = mock_client

    session_manager = SessionManager()
    session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)
    trace_ctx = TraceContext.new_trace()
    ctx = ExecutionContext(
        session_id=session_id,
        trace_id=trace_ctx.trace_id,
        user_message="Hello",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        memory_context=None,
    )

    await execute_task_safe(ctx, session_manager)

    call_kwargs = mock_client.respond.call_args_list[0].kwargs
    system_prompt: str = call_kwargs.get("system_prompt", "") or ""

    assert system_prompt, "system_prompt must not be empty"
    assert not system_prompt.endswith("\n\n\n"), (
        "system_prompt must not have excessive trailing newlines"
    )
    assert _MEMORY_MARKER not in system_prompt, (
        "memory_section must not appear when memory_context is None"
    )
