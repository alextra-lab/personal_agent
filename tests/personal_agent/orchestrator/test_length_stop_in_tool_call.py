"""FRE-1676 — a local call cut at the output limit inside a tool call gets one more chance.

FRE-1562 ends the turn when a local primary call stops with ``finish_reason: length``. When the
cut falls inside a tool call, the turn used to end with the work discarded and nothing logged
about what was cut. These tests drive ``_finalize_llm_call_success`` with such a response:

- AC-1: the log records tool name, argument length and reasoning length, and never the text.
- AC-2: the first such stop returns to the model once with a note. The second ends the turn
  with the FRE-1562 reply.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import personal_agent.orchestrator.executor as ex
from personal_agent.governance.models import Mode
from personal_agent.llm_client.models import ToolCallingStrategy
from personal_agent.llm_client.types import LLMResponse, ModelRole
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.types import ExecutionContext, TaskState
from tests._helpers.trace import make_test_ctx

_BOUND = 12288
_SECRET_ARGUMENT = "SECRET-PAGE-BODY-" + "<p>lesson</p>" * 400
_SECRET_REASONING = "SECRET-REASONING-" + "think " * 100


def _ctx() -> ExecutionContext:
    ctx = ExecutionContext(
        session_id="00000000-0000-4000-8000-000000001676",
        trace_id="trace-fre1676",
        user_message="write me a beginner lesson",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        messages=[{"role": "user", "content": "write me a beginner lesson"}],
    )
    ctx.answering_model_key = "qwen3.8-flash-next"
    return ctx


def _cut_response() -> LLMResponse:
    """A local call that stopped at the bound inside a ``bash`` tool call."""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "name": "bash",
                # The client hands over the raw argument string; a cut one is not valid JSON.
                "arguments": '{"command": "cat > page.html <<EOF ' + _SECRET_ARGUMENT,
            }
        ],
        "reasoning_trace": _SECRET_REASONING,
        "usage": {"prompt_tokens": 58435, "completion_tokens": _BOUND, "total_tokens": 70723},
        "response_id": None,
        "raw": {},
        "finish_reason": "length",
    }


async def _finalize(
    ctx: ExecutionContext, response: LLMResponse, events: list[tuple[str, dict[str, Any]]]
) -> TaskState:
    def _capture(event: str, **payload: Any) -> None:
        events.append((event, payload))

    with (
        patch.object(ex, "_report_turn_progress", AsyncMock()),
        patch.object(ex.log, "warning", _capture),
        patch.object(ex.log, "info", _capture),
    ):
        return await ex._finalize_llm_call_success(
            ctx,
            response,
            model_role=ModelRole.PRIMARY,
            trace_ctx=make_test_ctx("fre1676"),
            span_id="span-fre1676",
            cite_only_retry=False,
            tool_strategy=ToolCallingStrategy.NATIVE,
            step_start_time=time.time(),
            generation_bound=_BOUND,
        )


def _cut_events(events: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [p for name, p in events if name == "primary_tool_call_cut_at_bound"]


class TestTheCutIsMeasurable:
    """AC-1."""

    @pytest.mark.asyncio
    async def test_the_log_names_the_tool_and_sizes_but_never_the_text(self) -> None:
        response = _cut_response()
        events: list[tuple[str, dict[str, Any]]] = []

        await _finalize(_ctx(), response, events)

        cut = _cut_events(events)
        assert len(cut) == 1
        assert cut[0]["tool_name"] == "bash"
        assert cut[0]["argument_length"] == len(response["tool_calls"][0]["arguments"])
        assert cut[0]["reasoning_length"] == len(_SECRET_REASONING)
        assert cut[0]["trace_id"] == "trace-fre1676"
        assert "SECRET-" not in repr(events), "argument or reasoning text reached the log"

    @pytest.mark.asyncio
    async def test_a_dict_argument_is_sized_by_its_json(self) -> None:
        response = _cut_response()
        response["tool_calls"][0]["arguments"] = {"command": "x" * 500}
        events: list[tuple[str, dict[str, Any]]] = []

        await _finalize(_ctx(), response, events)

        assert _cut_events(events)[0]["argument_length"] == len('{"command": "' + "x" * 500 + '"}')

    @pytest.mark.asyncio
    async def test_a_length_stop_with_no_tool_call_logs_no_cut_event(self) -> None:
        response = _cut_response()
        response["tool_calls"] = []
        response["content"] = "a runaway reply"
        events: list[tuple[str, dict[str, Any]]] = []

        await _finalize(_ctx(), response, events)

        assert _cut_events(events) == []


class TestOneChanceThenTheTurnEnds:
    """AC-2."""

    @pytest.mark.asyncio
    async def test_the_first_stop_returns_to_the_model_once_with_a_note(self) -> None:
        ctx = _ctx()

        state = await _finalize(ctx, _cut_response(), [])

        assert state is TaskState.LLM_CALL
        assert ctx.turn_stopped_early is False
        assert ctx.final_reply is None
        note = ctx.messages[-1]
        assert note["role"] == "user"
        text = note["content"]
        assert "output limit" in text
        assert "smaller" in text
        assert "artifact_draft" in text
        assert all(m.get("role") != "assistant" for m in ctx.messages), (
            "the cut tool call must not enter history"
        )
        assert "SECRET-" not in repr(ctx.messages)

    @pytest.mark.asyncio
    async def test_the_second_stop_ends_the_turn_with_the_fre1562_reply(self) -> None:
        ctx = _ctx()
        events: list[tuple[str, dict[str, Any]]] = []

        first = await _finalize(ctx, _cut_response(), events)
        second = await _finalize(ctx, _cut_response(), events)

        assert first is TaskState.LLM_CALL
        assert second is TaskState.SYNTHESIS
        assert ctx.turn_stopped_early is True
        assert "ran too long" in (ctx.final_reply or "")
        assert sum(1 for m in ctx.messages if "output limit" in str(m.get("content"))) == 1, (
            "only one note is sent"
        )
        assert len(_cut_events(events)) == 2, "both cuts are measured"

    @pytest.mark.asyncio
    async def test_a_length_stop_with_no_tool_call_still_ends_the_turn_at_once(self) -> None:
        response = _cut_response()
        response["tool_calls"] = []
        response["content"] = "a runaway reply"
        ctx = _ctx()

        state = await _finalize(ctx, response, [])

        assert state is TaskState.SYNTHESIS
        assert ctx.turn_stopped_early is True
        assert "ran too long" in (ctx.final_reply or "")
