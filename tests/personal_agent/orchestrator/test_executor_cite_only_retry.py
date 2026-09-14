"""The cite-only retry and the withdrawn forcing, at the request (ADR-0151 D3, D5, FRE-1509).

``step_synthesis`` orders the retry. What makes it cite-only is the request ``step_llm_call``
then sends: ``tool_choice="none"`` with the tool list kept (ADR-0149 D6), and no extra tool
round. These tests drive the real ``step_llm_call`` and read the kwargs the client received.

The heavy tests assert the D5 withdrawal on the same seam: a heavy selection reaches no
request as a ``"required"`` pin or a retrieval directive, and records no forcing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.governance.models import Mode
from personal_agent.grounding.enforcement_selection import (
    EnforcementLevel,
    EnforcementSelection,
    EnforcementState,
    SelectionReason,
)
from personal_agent.grounding.verification import CheckOutcome, SpanVerification, TurnVerification
from personal_agent.llm_client.models import Dialect
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.types import ExecutionContext, TaskState

_QUERY = "How many people live in Paris?"
_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
}
_HEAVY_DIRECTIVE_OPENING = "Before you answer: retrieve first"


@pytest.fixture(autouse=True)
def _restore_executor_tool_globals() -> Any:
    import personal_agent.orchestrator.executor as _ex

    saved_registry = _ex._tool_registry
    saved_layer = _ex._tool_execution_layer
    # Unset, so step_llm_call resolves the patched registry rather than one an earlier
    # test cached.
    _ex._tool_registry = None
    yield
    _ex._tool_registry = saved_registry
    _ex._tool_execution_layer = saved_layer


def _ctx() -> ExecutionContext:
    ctx = ExecutionContext(
        session_id="session-cite-only",
        trace_id="trace-cite-only",
        user_message=_QUERY,
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        messages=[{"role": "user", "content": _QUERY}],
    )
    ctx.turn_started_at = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    return ctx


def _client(response: dict[str, Any]) -> MagicMock:
    client = MagicMock()
    client.model_configs = {}
    # A dialect that retains tools on a no-tool call, so the tool list stays (ADR-0149 D6).
    client.dialect_for_role.return_value = Dialect.LLAMACPP_QWEN
    client.respond = AsyncMock(return_value=response)
    return client


def _tool_call_response() -> dict[str, Any]:
    return {
        "content": "",
        "tool_calls": [
            {"id": "c1", "name": "web_search", "arguments": json.dumps({"query": "paris"})}
        ],
        "response_id": None,
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _answer_response() -> dict[str, Any]:
    return {
        "content": "Paris has 2.1 million residents.",
        "tool_calls": [],
        "response_id": None,
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


async def _call(ctx: ExecutionContext, client: MagicMock) -> dict[str, Any]:
    """Run the real ``step_llm_call`` once and return the kwargs the client received."""
    await _run(ctx, client)
    return dict(client.respond.call_args.kwargs)


async def _run(ctx: ExecutionContext, client: MagicMock) -> TaskState:
    """Run the real ``step_llm_call`` once and return the state it moves to."""
    from personal_agent.orchestrator.executor import step_llm_call
    from personal_agent.telemetry.trace import TraceContext

    session = MagicMock()
    session.add_message = AsyncMock()
    session.get_messages = AsyncMock(return_value=[])
    with (
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=client),
        patch(
            "personal_agent.orchestrator.executor.get_default_registry",
            return_value=MagicMock(
                get_tool_definitions_for_llm=MagicMock(return_value=[_TOOL_DEF])
            ),
        ),
    ):
        return await step_llm_call(ctx, session, TraceContext.new_trace())


def _add_tool_result(ctx: ExecutionContext) -> None:
    """What ``step_tool_execution`` leaves behind: one round and its result."""
    ctx.tool_iteration_count += 1
    ctx.messages.append(
        {"role": "tool", "tool_call_id": "c1", "name": "web_search", "content": "Paris: 2.1M."}
    )


def _prepare_retry(ctx: ExecutionContext) -> None:
    """What ``step_synthesis`` leaves behind when it orders the cite-only retry."""
    ctx.messages.append({"role": "assistant", "content": "Paris has 2.1 million residents."})
    ctx.messages.append({"role": "user", "content": "Cite each statement."})
    ctx.grounding_attempts = 1
    ctx.grounding_retry_pending = True


# ── AC-3 — the retry cannot retrieve ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ac3_the_retry_pins_none_keeps_the_tools_and_grants_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from personal_agent.config import settings

    monkeypatch.setattr(settings, "prefer_primitives_enabled", False)
    ctx = _ctx()
    first = await _call(ctx, _client(_tool_call_response()))
    _add_tool_result(ctx)
    _prepare_retry(ctx)
    grant_before = ctx.grounding_retrieval_grant
    messages_before = len(ctx.messages)

    retry = await _call(ctx, _client(_answer_response()))

    assert first["tools"] == [_TOOL_DEF]
    assert retry["tool_choice"] == "none"
    assert retry["tools"] == first["tools"]
    assert ctx.grounding_retrieval_grant == grant_before
    assert ctx.grounding_retry_pending is False
    # No tool-budget countdown or synthesis prompt was injected into the retry.
    added = [m for m in ctx.messages[messages_before:] if m.get("role") == "user"]
    assert added == []


@pytest.mark.asyncio
async def test_a_normal_pass_leaves_tool_choice_unpinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """The seeded negative: the same second pass without a retry pins nothing."""
    from personal_agent.config import settings

    monkeypatch.setattr(settings, "prefer_primitives_enabled", False)
    ctx = _ctx()
    await _call(ctx, _client(_tool_call_response()))
    _add_tool_result(ctx)

    second = await _call(ctx, _client(_answer_response()))

    assert second["tool_choice"] is None
    assert second["tools"] == [_TOOL_DEF]


def _tool_call_with_text_response() -> dict[str, Any]:
    """A model that ignores the pin: prose plus a tool call."""
    return {**_tool_call_response(), "content": "Paris has 2.1 million residents."}


@pytest.mark.asyncio
async def test_ac3_a_tool_call_on_the_retry_is_dropped_not_executed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-native strategy cannot carry the pin, and a backend may ignore it."""
    from personal_agent.config import settings

    monkeypatch.setattr(settings, "prefer_primitives_enabled", False)
    ctx = _ctx()
    await _call(ctx, _client(_tool_call_response()))
    _add_tool_result(ctx)
    _prepare_retry(ctx)
    rounds_before = ctx.tool_iteration_count

    state = await _run(ctx, _client(_tool_call_with_text_response()))

    assert state is TaskState.SYNTHESIS
    assert ctx.final_reply == "Paris has 2.1 million residents."
    assert "tool_calls" not in ctx.messages[-1]
    assert ctx.tool_iteration_count == rounds_before


@pytest.mark.asyncio
async def test_a_tool_call_on_a_normal_pass_is_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The seeded negative: the same response outside a retry goes to tool execution."""
    from personal_agent.config import settings

    monkeypatch.setattr(settings, "prefer_primitives_enabled", False)
    ctx = _ctx()
    await _call(ctx, _client(_tool_call_response()))
    _add_tool_result(ctx)

    state = await _run(ctx, _client(_tool_call_with_text_response()))

    assert state is TaskState.TOOL_EXECUTION
    assert "tool_calls" in ctx.messages[-1]


# ── AC-5 — nothing forces retrieval before generation ───────────────────────────────


def _heavy() -> EnforcementSelection:
    return EnforcementSelection(
        applied=EnforcementLevel.HEAVY,
        standing=EnforcementState(level=EnforcementLevel.HEAVY, demoted_at=None),
        reason=SelectionReason.UNMEASURED,
    )


@pytest.mark.asyncio
async def test_ac5_a_heavy_selection_forces_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from personal_agent.config import settings
    from personal_agent.orchestrator.executor import _record_grounding

    monkeypatch.setattr(settings, "prefer_primitives_enabled", False)
    monkeypatch.setattr(settings, "grounding_verification_mode", "enforce")
    ctx = _ctx()
    ctx.answering_model_key = "model-under-test"

    with patch(
        "personal_agent.orchestrator.executor._resolve_enforcement",
        new=AsyncMock(return_value=_heavy()),
    ):
        sent = await _call(ctx, _client(_answer_response()))

    assert ctx.grounding_enforcement is not None
    assert ctx.grounding_enforcement.applied is EnforcementLevel.HEAVY
    assert sent["tool_choice"] != "required"
    assert not any(
        _HEAVY_DIRECTIVE_OPENING in str(message.get("content")) for message in sent["messages"]
    )
    assert ctx.grounding_retrieval_grant == 0

    ctx.grounding_attempts = 1
    uncited = TurnVerification(
        spans=(
            SpanVerification(
                text="claim", start=0, end=5, identifier=None, outcome=CheckOutcome.UNCITED
            ),
        )
    )
    with patch("personal_agent.orchestrator.executor._record_compliance_observation") as observer:
        observer.return_value = "recorded"
        _record_grounding(ctx, uncited, "enforce")
    assert ctx.grounding_record is not None
    assert ctx.grounding_record.retrieval_forced is False
