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


# NOTE: the head-layout test_tool_prompt_before_memory_section was removed with the
# cache_frozen_layout_enabled flag (FRE-941). The replacement below drives the same
# real execute_task_safe pipeline under the (now sole) frozen layout.


@patch("personal_agent.llm_client.factory.get_llm_client")
@pytest.mark.asyncio
async def test_memory_stays_out_of_static_prefix_under_frozen_layout(
    mock_client_class: MagicMock,
) -> None:
    """ADR-0081 D1/D2 cache-boundary invariant, exercised through the real pipeline.

    Populated memory_context must ride the volatile user-turn block, never the
    STATIC system_prompt — otherwise every turn perturbs the byte-stable prefix
    and the KV-cache forward-extension property the frozen layout exists for is
    silently lost. Complements the unit-level test_frozen_layout.py (which only
    exercises ``_inline_volatile_into_last_user_message`` in isolation with
    synthetic strings) with an end-to-end check through ``execute_task_safe``.
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
    sent_messages = call_kwargs.get("messages") or []

    assert _MEMORY_MARKER not in system_prompt, (
        "memory_section leaked into the STATIC system_prompt — this breaks the "
        "ADR-0081 D1 cache-boundary invariant (byte-stable prefix)."
    )
    last_user_content = next(
        m["content"] for m in reversed(sent_messages) if m.get("role") == "user"
    )
    assert _MEMORY_MARKER in last_user_content, (
        "memory_section must ride the volatile current user turn under the "
        "frozen layout (ADR-0081 D2)."
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
