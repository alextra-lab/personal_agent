"""Tests for FRE-433 volatile-tail prompt layout."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.config import settings
from personal_agent.governance.models import Mode
from personal_agent.orchestrator import Channel
from personal_agent.orchestrator.executor import _render_memory_section, step_llm_call
from personal_agent.orchestrator.types import ExecutionContext
from personal_agent.telemetry.trace import TraceContext

_STATIC_PREFIX = "STATIC PREFIX"
_SKILL_BODY = "## FRE-433 SKILL BODY"
_MEMORY_CONTEXT = [
    {
        "type": "entity",
        "name": "Qwen",
        "entity_type": "Model",
        "description": "A local model used for cache testing",
        "mentions": 3,
    }
]


def _make_response() -> dict[str, object]:
    """Build a minimal successful LLM response."""
    return {
        "content": "Done.",
        "tool_calls": [],
        "response_id": None,
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        "raw": {},
        "cost_usd": 0.0,
    }


def _make_mock_llm() -> MagicMock:
    """Build an LLM mock that records its respond call."""
    mock_llm = MagicMock()
    mock_llm.respond = AsyncMock(return_value=_make_response())
    mock_llm.model_configs = {}
    return mock_llm


def _make_ctx() -> ExecutionContext:
    """Build an execution context with stable history and volatile inputs."""
    return ExecutionContext(
        session_id="fre433-session",
        trace_id="fre433-trace",
        user_message="current turn",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        messages=[
            {"role": "user", "content": "first turn"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "current turn"},
        ],
        memory_context=_MEMORY_CONTEXT,
        operator_stanza=_STATIC_PREFIX,
    )


async def _run_step(monkeypatch: pytest.MonkeyPatch, tail_layout: bool) -> MagicMock:
    """Run step_llm_call with deterministic volatile prompt fragments."""
    monkeypatch.setattr(settings, "cache_volatile_tail_layout", tail_layout)
    monkeypatch.setattr(settings, "prefer_primitives_enabled", True)
    monkeypatch.setattr(settings, "skill_routing_mode", "keyword")
    monkeypatch.setattr(settings, "skill_routing_model_key", "")
    monkeypatch.setattr(settings, "skill_nudge_enabled", False)

    mock_llm = _make_mock_llm()
    mock_session = MagicMock()
    mock_session.add_message = AsyncMock()
    mock_session.get_messages = AsyncMock(return_value=[])

    with (
        patch("personal_agent.orchestrator.skills.get_skill_block", return_value=_SKILL_BODY),
        patch("personal_agent.orchestrator.skills.get_all_skills", return_value={}),
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=mock_llm),
        patch(
            "personal_agent.orchestrator.executor.get_default_registry",
            return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
        ),
    ):
        await step_llm_call(_make_ctx(), mock_session, TraceContext.new_trace())

    return mock_llm


@pytest.mark.asyncio
async def test_fre433_tail_layout_flag_off_preserves_system_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag-off keeps volatile content inside system_prompt with no tail message."""
    mock_llm = await _run_step(monkeypatch, tail_layout=False)

    call_kwargs = mock_llm.respond.call_args.kwargs
    system_prompt = call_kwargs["system_prompt"]
    messages = call_kwargs["messages"]
    memory_section = _render_memory_section(_MEMORY_CONTEXT)

    assert system_prompt == f"{_STATIC_PREFIX}\n\n{_SKILL_BODY}\n{memory_section}"
    assert messages == [
        {"role": "user", "content": "first turn"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "current turn"},
    ]


@pytest.mark.asyncio
async def test_fre433_tail_layout_flag_on_moves_volatile_to_final_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag-on keeps system stable and appends volatile content after real history."""
    mock_llm = await _run_step(monkeypatch, tail_layout=True)

    call_kwargs = mock_llm.respond.call_args.kwargs
    system_prompt = call_kwargs["system_prompt"]
    messages = call_kwargs["messages"]
    memory_section = _render_memory_section(_MEMORY_CONTEXT)

    assert system_prompt == _STATIC_PREFIX
    assert messages[:-1] == [
        {"role": "user", "content": "first turn"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "current turn"},
    ]
    assert messages[-1] == {"role": "user", "content": f"{_SKILL_BODY}\n{memory_section}"}
