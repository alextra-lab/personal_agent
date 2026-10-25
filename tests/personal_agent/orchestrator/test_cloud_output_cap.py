"""FRE-1697 — a cloud call that stops at the output cap does not end the turn as completed.

Trace bff76b1d (owner turn on a Scaleway GLM deployment, output cap 16,384 tokens):

- 13:04:34Z: 16,384 output tokens, all reasoning, no text. The turn ended ``completed`` with no
  answer and no reason.
- 13:00:22Z: a tool call was cut by the cap. The tool result said only that parameters were missing.

These tests stub the provider response, so they make no paid call. They drive
``_finalize_llm_call_success``, where the response and its tool calls are handled, and
``execute_task_safe`` for the reply and the capture. The registry outcome is tested in
``tests/personal_agent/service/test_request_tasks.py``.

- AC-1: reasoning only at the cap ends the turn with a message that names the limit, outcome failed.
- AC-2: a cut tool call gets a tool result that says the output was cut.
- AC-3: the event for such a stop records the cause.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

import personal_agent.orchestrator.executor as ex
from personal_agent.error_classification import ClassifiedError, classify_error
from personal_agent.governance.models import Mode
from personal_agent.llm_client.models import Placement, ToolCallingStrategy
from personal_agent.llm_client.types import LLMOutputCapReached, LLMResponse, ModelRole
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.types import ExecutionContext, TaskState
from tests._helpers.trace import make_test_ctx

_CAP = 16384
_DEPLOYMENT = "glm-5.3-scaleway"
_EVENT = "primary_output_cap_reached"


@pytest.fixture(autouse=True)
def _restore_executor_state() -> Iterator[None]:
    """``step_llm_call`` builds the tool registry and layer lazily; leave none behind."""
    saved_registry, saved_layer = ex._tool_registry, ex._tool_execution_layer
    yield
    ex._tool_registry, ex._tool_execution_layer = saved_registry, saved_layer


def _ctx() -> ExecutionContext:
    ctx = ExecutionContext(
        session_id="00000000-0000-4000-8000-000000001697",
        trace_id="trace-fre1697",
        user_message="draft me an artifact",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        messages=[{"role": "user", "content": "draft me an artifact"}],
    )
    ctx.answering_model_key = _DEPLOYMENT
    ctx.user_id = uuid4()
    return ctx


def _response(
    *,
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "length",
    completion_tokens: int = _CAP,
    reasoning_tokens: int | None = _CAP,
) -> LLMResponse:
    usage: dict[str, Any] = {
        "prompt_tokens": 36631,
        "completion_tokens": completion_tokens,
        "total_tokens": 36631 + completion_tokens,
    }
    if reasoning_tokens is not None:
        usage["reasoning_tokens"] = reasoning_tokens
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls or [],
        "reasoning_trace": None,
        "usage": usage,
        "response_id": None,
        "raw": {},
        "finish_reason": finish_reason,
    }


def _cut_tool_call() -> dict[str, Any]:
    """An ``artifact_draft`` call that the cap cut before any parameter was written."""
    return {"id": "call_1", "name": "artifact_draft", "arguments": "{}"}


async def _finalize(
    ctx: ExecutionContext,
    response: LLMResponse,
    *,
    output_cap: int | None = _CAP,
    events: list[tuple[str, dict[str, Any]]] | None = None,
) -> TaskState:
    recorded = events if events is not None else []

    def _capture(event: str, **payload: Any) -> None:
        recorded.append((event, payload))

    with (
        patch.object(ex, "_report_turn_progress", AsyncMock()),
        patch.object(ex.log, "warning", _capture),
        patch.object(ex.log, "info", _capture),
    ):
        return await ex._finalize_llm_call_success(
            ctx,
            response,
            model_role=ModelRole.PRIMARY,
            trace_ctx=make_test_ctx("fre1697"),
            span_id="span-fre1697",
            cite_only_retry=False,
            tool_strategy=ToolCallingStrategy.NATIVE,
            step_start_time=time.time(),
            generation_bound=None,
            output_cap=output_cap,
        )


def _cap_events(events: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [payload for name, payload in events if name == _EVENT]


async def _no_emit(_ctx: ExecutionContext, _classified: ClassifiedError) -> None:
    return None


# ── AC-1 — reasoning only at the cap ──────────────────────────────────────


class TestReasoningOnlyAtTheCapFailsTheTurn:
    @pytest.mark.asyncio
    async def test_the_turn_fails_and_the_cut_reply_does_not_enter_history(self) -> None:
        ctx = _ctx()

        state = await _finalize(ctx, _response())

        assert state is TaskState.FAILED
        assert isinstance(ctx.error, LLMOutputCapReached)
        assert ctx.classified_error is not None
        assert ctx.classified_error.category == "output_cap"
        assert "16,384" in ctx.classified_error.reason
        assert all(m.get("role") != "assistant" for m in ctx.messages)
        assert ctx.final_reply is None

    @pytest.mark.asyncio
    async def test_whitespace_content_counts_as_no_text(self) -> None:
        state = await _finalize(_ctx(), _response(content="  \n"))

        assert state is TaskState.FAILED

    @pytest.mark.asyncio
    async def test_the_visible_reply_names_the_limit_and_the_capture_is_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reply and the capture outcome that the owner and a query see."""
        ctx = _ctx()

        async def fake_execute_task(ctx_in: ExecutionContext, _sm: object) -> ExecutionContext:
            ctx_in.state = await _finalize(ctx_in, _response())
            return ctx_in

        monkeypatch.setattr(ex, "execute_task", fake_execute_task)
        monkeypatch.setattr(ex, "_emit_classified_error", _no_emit)
        written: list[Any] = []
        with patch("personal_agent.captains_log.capture.write_capture", side_effect=written.append):
            result = await ex.execute_task_safe(ctx, session_manager=None)  # type: ignore[arg-type]

        assert "output limit" in result["reply"]
        assert "16,384" in result["reply"]
        assert len(written) == 1
        assert written[0].outcome == "failed"
        assert written[0].assistant_response == result["reply"]

    @pytest.mark.asyncio
    async def test_the_result_carries_the_category_for_the_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _ctx()

        async def fake_execute_task(ctx_in: ExecutionContext, _sm: object) -> ExecutionContext:
            ctx_in.state = await _finalize(ctx_in, _response())
            return ctx_in

        monkeypatch.setattr(ex, "execute_task", fake_execute_task)
        monkeypatch.setattr(ex, "_emit_classified_error", _no_emit)
        with patch("personal_agent.captains_log.capture.write_capture"):
            result = await ex.execute_task_safe(ctx, session_manager=None)  # type: ignore[arg-type]

        assert result["error_category"] == "output_cap"

    @pytest.mark.asyncio
    async def test_gathered_tool_results_stay_in_the_reply(self) -> None:
        ctx = _ctx()
        ctx.tool_results = [{"tool_name": "web_search", "success": True}]

        state = await _finalize(ctx, _response())

        reply = ctx.final_reply or ""
        assert state is TaskState.FAILED
        assert "web_search: success" in reply
        assert "16,384" in reply
        assert "output limit" in reply

    @pytest.mark.asyncio
    async def test_an_unknown_cap_still_says_output_limit(self) -> None:
        error = LLMOutputCapReached("cut", cap=None, tokens=900)

        classified = classify_error(error)

        assert classified.category == "output_cap"
        assert "output limit" in classified.reason
        assert "None" not in classified.reason


# ── AC-2 — a cut tool call ────────────────────────────────────────────────


def _tool_messages(ctx: ExecutionContext) -> list[dict[str, Any]]:
    return [m for m in ctx.messages if m.get("role") == "tool"]


class TestACutToolCallTellsTheModelItsOutputWasCut:
    @pytest.mark.asyncio
    async def test_the_first_cut_answers_every_call_and_returns_to_the_model(self) -> None:
        ctx = _ctx()
        calls = [
            _cut_tool_call(),
            {"id": "call_2", "name": "web_search", "arguments": '{"q": "cut'},
        ]

        state = await _finalize(ctx, _response(tool_calls=calls))

        assert state is TaskState.LLM_CALL
        assert ctx.error is None
        assert ctx.turn_stopped_early is False
        assistant = ctx.messages[-3]
        assert assistant["role"] == "assistant"
        tool_messages = _tool_messages(ctx)
        assert [m["tool_call_id"] for m in tool_messages] == [
            tc["id"] for tc in assistant["tool_calls"]
        ], "every call in history has its own result"
        assert [m["name"] for m in tool_messages] == ["artifact_draft", "web_search"]

    @pytest.mark.asyncio
    async def test_the_tool_result_says_the_output_was_cut_not_only_that_parameters_are_missing(
        self,
    ) -> None:
        ctx = _ctx()

        await _finalize(ctx, _response(tool_calls=[_cut_tool_call()]))

        (message,) = _tool_messages(ctx)
        payload = json.loads(message["content"])
        text = json.dumps(payload)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "output_cut"
        assert payload["error"]["retryable"] is True
        assert "cut" in text
        assert "output limit" in text
        assert "did not run" in text
        assert "missing" not in text, "the cause is the cut, not the missing parameters"

    @pytest.mark.asyncio
    async def test_text_beside_a_cut_call_is_still_a_cut(self) -> None:
        ctx = _ctx()

        state = await _finalize(
            ctx, _response(content="Let me draft that.", tool_calls=[_cut_tool_call()])
        )

        assert state is TaskState.LLM_CALL
        assert len(_tool_messages(ctx)) == 1
        assert ctx.messages[-2]["content"] == "Let me draft that."

    @pytest.mark.asyncio
    async def test_a_second_cut_ends_the_turn(self) -> None:
        ctx = _ctx()
        events: list[tuple[str, dict[str, Any]]] = []

        first = await _finalize(ctx, _response(tool_calls=[_cut_tool_call()]), events=events)
        second = await _finalize(ctx, _response(tool_calls=[_cut_tool_call()]), events=events)

        assert first is TaskState.LLM_CALL
        assert second is TaskState.FAILED
        assert isinstance(ctx.error, LLMOutputCapReached)
        assert [e["cause"] for e in _cap_events(events)] == ["tool_call_cut", "tool_call_cut"]
        assert len(_tool_messages(ctx)) == 1, "only the first cut is answered with a tool result"

    @pytest.mark.asyncio
    async def test_a_cut_call_does_not_reach_tool_execution(self) -> None:
        ctx = _ctx()

        state = await _finalize(ctx, _response(tool_calls=[_cut_tool_call()]))

        assert state is not TaskState.TOOL_EXECUTION

    @pytest.mark.asyncio
    async def test_a_complete_call_still_goes_to_tool_execution(self) -> None:
        ctx = _ctx()

        state = await _finalize(
            ctx,
            _response(
                finish_reason="tool_calls",
                completion_tokens=300,
                reasoning_tokens=100,
                tool_calls=[{"id": "call_1", "name": "web_search", "arguments": '{"q": "x"}'}],
            ),
        )

        assert state is TaskState.TOOL_EXECUTION
        assert _tool_messages(ctx) == []


# ── AC-3 — the event records the cause ────────────────────────────────────


class TestTheEventRecordsTheCause:
    @pytest.mark.asyncio
    async def test_reasoning_only_records_its_cause_and_sizes(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []

        await _finalize(_ctx(), _response(reasoning_tokens=14062), events=events)

        emitted = _cap_events(events)
        assert len(emitted) == 1
        assert emitted[0]["cause"] == "reasoning_only"
        assert emitted[0]["deployment"] == _DEPLOYMENT
        assert emitted[0]["output_cap"] == _CAP
        assert emitted[0]["tokens_generated"] == _CAP
        assert emitted[0]["reasoning_tokens"] == 14062
        assert emitted[0]["trace_id"] == "trace-fre1697"

    @pytest.mark.asyncio
    async def test_a_cut_tool_call_records_its_cause_and_never_its_text(self) -> None:
        call = {"id": "call_1", "name": "artifact_draft", "arguments": "SECRET-ARGUMENT"}
        events: list[tuple[str, dict[str, Any]]] = []

        await _finalize(_ctx(), _response(tool_calls=[call]), events=events)

        emitted = _cap_events(events)
        assert len(emitted) == 1
        assert emitted[0]["cause"] == "tool_call_cut"
        assert "SECRET-" not in repr(events)

    @pytest.mark.asyncio
    async def test_a_cite_only_cut_is_not_misread_as_reasoning_only(self) -> None:
        """The cite-only retry drops tool calls before it runs; the cause reads the raw ones."""
        events: list[tuple[str, dict[str, Any]]] = []
        ctx = _ctx()

        with (
            patch.object(ex, "_report_turn_progress", AsyncMock()),
            patch.object(ex.log, "warning", lambda e, **p: events.append((e, p))),
            patch.object(ex.log, "info", lambda e, **p: events.append((e, p))),
        ):
            state = await ex._finalize_llm_call_success(
                ctx,
                _response(tool_calls=[_cut_tool_call()]),
                model_role=ModelRole.PRIMARY,
                trace_ctx=make_test_ctx("fre1697"),
                span_id="span-fre1697",
                cite_only_retry=True,
                tool_strategy=ToolCallingStrategy.NATIVE,
                step_start_time=time.time(),
                generation_bound=None,
                output_cap=_CAP,
            )

        assert state is TaskState.FAILED
        assert [e["cause"] for e in _cap_events(events)] == ["tool_call_cut"]
        assert _tool_messages(ctx) == [], "a cite-only retry never owns a tool result"

    @pytest.mark.asyncio
    async def test_a_missing_reasoning_count_is_recorded_as_none(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []

        await _finalize(_ctx(), _response(reasoning_tokens=None), events=events)

        assert _cap_events(events)[0]["reasoning_tokens"] is None


# ── Scope — what does not change ──────────────────────────────────────────


class TestOnlyTheTwoCausesChange:
    @pytest.mark.asyncio
    async def test_a_partial_answer_at_the_cap_keeps_the_old_path(self) -> None:
        ctx = _ctx()
        events: list[tuple[str, dict[str, Any]]] = []

        state = await _finalize(ctx, _response(content="A partial answer"), events=events)

        assert state is TaskState.SYNTHESIS
        assert ctx.final_reply == "A partial answer"
        assert ctx.error is None
        assert _cap_events(events) == []

    @pytest.mark.asyncio
    async def test_a_normal_stop_is_untouched(self) -> None:
        ctx = _ctx()
        events: list[tuple[str, dict[str, Any]]] = []

        state = await _finalize(
            ctx,
            _response(content="done", finish_reason="stop", completion_tokens=50),
            events=events,
        )

        assert state is TaskState.SYNTHESIS
        assert ctx.error is None
        assert _cap_events(events) == []

    @pytest.mark.asyncio
    async def test_a_length_stop_with_no_cap_keeps_the_old_path(self) -> None:
        """A local client has no ``output_cap``: its own FRE-1562 path decides."""
        ctx = _ctx()

        state = await _finalize(ctx, _response(), output_cap=None)

        assert ctx.error is None
        assert state is TaskState.SYNTHESIS


# ── The production callers pass the cap ───────────────────────────────────


def _mock_client(respond: AsyncMock, placement: Placement, max_tokens: int | None) -> MagicMock:
    client = MagicMock()
    client.respond = respond
    client.model_configs = {}
    client.placement = placement
    client.max_tokens = max_tokens
    return client


def _step_patches(client: MagicMock) -> Any:
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch("personal_agent.orchestrator.skills.assemble_skill_catalog", return_value="")
    )
    stack.enter_context(
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=client)
    )
    stack.enter_context(
        patch.object(
            ex,
            "get_default_registry",
            return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
        )
    )
    stack.enter_context(patch.object(ex, "_report_turn_progress", AsyncMock()))
    return stack


class TestStepLlmCallPassesTheCap:
    @pytest.mark.asyncio
    async def test_a_cloud_client_reasoning_only_reply_fails_the_turn(self) -> None:
        ctx = _ctx()
        client = _mock_client(AsyncMock(return_value=_response()), Placement.CLOUD, _CAP)

        with _step_patches(client):
            state = await ex.step_llm_call(ctx, MagicMock(), make_test_ctx("fre1697"))

        assert state is TaskState.FAILED
        assert "16,384" in (ctx.classified_error.reason if ctx.classified_error else "")

    @pytest.mark.asyncio
    async def test_the_context_window_retry_passes_the_cap_too(self) -> None:
        from litellm.exceptions import ContextWindowExceededError

        ctx = _ctx()
        ctx.messages = [
            {"role": "user", "content": "go"},
            {"role": "tool", "tool_call_id": "a", "content": "old " * 200},
            {"role": "tool", "tool_call_id": "b", "content": "new result"},
        ]
        respond = AsyncMock(
            side_effect=[
                ContextWindowExceededError("too big", model="m", llm_provider="openai"),
                _response(),
            ]
        )
        client = _mock_client(respond, Placement.CLOUD, _CAP)

        with _step_patches(client):
            state = await ex.step_llm_call(ctx, MagicMock(), make_test_ctx("fre1697"))

        assert respond.await_count == 2, "the retry path was not exercised"
        assert state is TaskState.FAILED

    @pytest.mark.asyncio
    async def test_a_local_client_keeps_its_own_bound_path(self) -> None:
        ctx = _ctx()
        ctx.length_stop_retries = 1
        client = _mock_client(AsyncMock(return_value=_response()), Placement.LOCAL, 12288)

        with _step_patches(client):
            state = await ex.step_llm_call(ctx, MagicMock(), make_test_ctx("fre1697"))

        assert state is TaskState.SYNTHESIS
        assert ctx.error is None
        assert ctx.turn_stopped_early is True


class TestCloudOutputCap:
    def test_a_cloud_client_reports_its_max_tokens(self) -> None:
        client = _mock_client(AsyncMock(), Placement.CLOUD, _CAP)

        assert ex._cloud_output_cap(client) == _CAP

    def test_a_local_client_reports_none(self) -> None:
        client = _mock_client(AsyncMock(), Placement.LOCAL, 12288)

        assert ex._cloud_output_cap(client) is None

    def test_a_cloud_client_without_a_cap_reports_none(self) -> None:
        client = _mock_client(AsyncMock(), Placement.CLOUD, None)

        assert ex._cloud_output_cap(client) is None
