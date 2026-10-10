"""FRE-1562 — a runaway local primary generation stops at a bound and ends the turn honestly.

The chain under test is the real one: the catalog's ``max_tokens`` -> the real
``LiteLLMClient`` local dispatch (only the outbound httpx transport is replaced) ->
``_finalize_llm_call_success``. The stub transport behaves like llama-server: it streams one
token per chunk and stops with ``finish_reason: length`` when the request's ``max_tokens`` is
reached. A request that carries no ``max_tokens`` would stream until the stub's hard cap, which
raises, so a missing bound fails the test instead of passing vacuously.

Observed normal maximum (ES ``agent-logs-*``, ``model_call_completed``, ``role: primary``,
``provider: slm_local``, last 30 days): 8,654 output tokens. See
``tests/personal_agent/config/test_local_primary_generation_bound.py`` for the query.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import personal_agent.orchestrator.executor as ex
from personal_agent.config.model_loader import load_model_config
from personal_agent.governance.models import Mode
from personal_agent.llm_client import litellm_client as litellm_client_module
from personal_agent.llm_client.models import ToolCallingStrategy
from personal_agent.llm_client.types import LLMResponse, ModelRole
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.types import ExecutionContext, TaskState
from personal_agent.security import DomainGuard
from personal_agent.telemetry.trace import SystemTraceContext
from tests._helpers.litellm_capability import pinned_litellm_capabilities
from tests._helpers.trace import make_test_ctx

_CATALOG_PATH = Path(__file__).resolve().parents[3] / "config" / "models.yaml"
_LOCAL_KEY = "qwen3.8-flash-next"
_BOUND = 12288
#: Largest completed local primary call in the last 30 days (see the module docstring).
_OBSERVED_NORMAL_MAX = 8654
#: The stub server's hard cap: a request with no bound streams this far, then the stub raises.
_STUB_UNBOUNDED_CAP = 40000


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    from personal_agent.llm_client.concurrency import set_inference_concurrency_controller

    set_inference_concurrency_controller(None)
    saved_registry, saved_layer = ex._tool_registry, ex._tool_execution_layer
    yield
    set_inference_concurrency_controller(None)
    ex._tool_registry, ex._tool_execution_layer = saved_registry, saved_layer
    litellm_client_module._guarded_httpx_clients.clear()
    litellm_client_module._guarded_async_http_handlers.clear()


# ── The stub server ───────────────────────────────────────────────────────


def _sse_line(chunk: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(chunk).encode() + b"\n\n"


def _token_stream(*, tokens: int, finish_reason: str) -> bytes:
    """A llama-server style SSE stream: one token per chunk, then the stop chunk and usage."""
    base = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m"}
    parts = [
        _sse_line(
            {
                **base,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
        )
    ]
    token_chunk = _sse_line(
        {**base, "choices": [{"index": 0, "delta": {"content": "w"}, "finish_reason": None}]}
    )
    parts.extend([token_chunk] * tokens)
    parts.append(
        _sse_line({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]})
    )
    parts.append(
        _sse_line(
            {
                **base,
                "choices": [],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": tokens,
                    "total_tokens": 1000 + tokens,
                },
            }
        )
    )
    parts.append(b"data: [DONE]\n\n")
    return b"".join(parts)


class _Wire:
    """What the transport saw."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []


async def _respond_from_real_catalog(
    *,
    model_key: str,
    stub_tokens: int | None,
    stub_finish: str = "stop",
    wire: _Wire,
) -> tuple[Any, LLMResponse]:
    """Dispatch one call through the real factory and real local path against the stub.

    Args:
        model_key: Catalog deployment to build the client for.
        stub_tokens: Tokens the stub emits when it is NOT bound-limited, or ``None`` to
            emulate a model that never stops (it honours the request's ``max_tokens``, like
            llama-server, and stops with ``length`` there).
        stub_finish: ``finish_reason`` of a self-terminated stream.
        wire: Filled with the request bodies.

    Returns:
        The client and its response.
    """
    from personal_agent.llm_client.factory import get_llm_client_for_key

    catalog = load_model_config(_CATALOG_PATH)

    async def _handle(_self: Any, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        wire.bodies.append(body)
        if stub_tokens is not None:
            content = _token_stream(tokens=stub_tokens, finish_reason=stub_finish)
        else:
            bound = body.get("max_tokens")
            if bound is None:
                raise AssertionError(
                    f"the request carried no max_tokens: the stub would stream {_STUB_UNBOUNDED_CAP}+ "
                    "tokens and never stop"
                )
            content = _token_stream(tokens=bound, finish_reason="length")
        return httpx.Response(
            200,
            content=content,
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    guard = DomainGuard(cache_path=Path("telemetry/security/_unused_test_blocklist.json"))
    guard._blocklist = frozenset()
    from datetime import datetime, timezone

    guard._last_loaded = datetime.now(timezone.utc)

    with ExitStack() as stack:
        stack.enter_context(
            patch("personal_agent.llm_client.factory.load_model_config", return_value=catalog)
        )
        stack.enter_context(patch("personal_agent.config.load_model_config", return_value=catalog))
        stack.enter_context(patch.object(httpx.AsyncHTTPTransport, "handle_async_request", _handle))
        client = get_llm_client_for_key(model_key, budget_role="main_inference")
        client._egress_guard = guard
        response = await client.respond(
            role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": "hello"}],
            trace_ctx=SystemTraceContext.new("fre1562", session_id=None),
        )
    return client, response


# ── The executor side ─────────────────────────────────────────────────────


def _ctx() -> ExecutionContext:
    ctx = ExecutionContext(
        session_id="00000000-0000-4000-8000-000000001562",
        trace_id="trace-fre1562",
        user_message="write the long thing",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        messages=[{"role": "user", "content": "write the long thing"}],
    )
    ctx.answering_model_key = _LOCAL_KEY
    return ctx


async def _finalize(
    ctx: ExecutionContext,
    response: LLMResponse,
    *,
    generation_bound: int | None,
    events: list[tuple[str, dict[str, Any]]] | None = None,
) -> TaskState:
    recorded = events if events is not None else []

    def _capture(level_events: list[tuple[str, dict[str, Any]]]) -> Any:
        def _inner(event: str, **payload: Any) -> None:
            level_events.append((event, payload))

        return _inner

    with (
        patch.object(ex, "_report_turn_progress", AsyncMock()),
        patch.object(ex.log, "warning", _capture(recorded)),
        patch.object(ex.log, "info", _capture(recorded)),
    ):
        return await ex._finalize_llm_call_success(
            ctx,
            response,
            model_role=ModelRole.PRIMARY,
            trace_ctx=make_test_ctx("fre1562"),
            span_id="span-fre1562",
            cite_only_retry=False,
            tool_strategy=ToolCallingStrategy.NATIVE,
            step_start_time=time.time(),
            generation_bound=generation_bound,
        )


def _bound_events(events: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [payload for name, payload in events if name == "primary_generation_hit_bound"]


# ── AC-2 — a runaway stops at the bound ───────────────────────────────────


class TestARunawayStopsAtTheBound:
    @pytest.mark.asyncio
    async def test_the_request_carries_the_bound_and_the_stream_ends_with_length(self) -> None:
        wire = _Wire()
        _, response = await _respond_from_real_catalog(
            model_key=_LOCAL_KEY, stub_tokens=None, wire=wire
        )

        assert wire.bodies[0]["max_tokens"] == _BOUND
        assert response["finish_reason"] == "length"
        assert response["usage"]["completion_tokens"] == _BOUND
        assert len(response["content"]) == _BOUND

    @pytest.mark.asyncio
    async def test_the_turn_ends_with_the_honest_message_and_one_event(self) -> None:
        wire = _Wire()
        client, response = await _respond_from_real_catalog(
            model_key=_LOCAL_KEY, stub_tokens=None, wire=wire
        )
        # A partial tool call on a cut-off reply must never be executed.
        response["tool_calls"] = [
            {"id": "call_1", "name": "write_file", "arguments": {"content": "cut off mid-str"}}
        ]
        ctx = _ctx()
        events: list[tuple[str, dict[str, Any]]] = []

        state = await _finalize(
            ctx, response, generation_bound=ex._local_generation_bound(client), events=events
        )

        assert state is TaskState.SYNTHESIS
        reply = ctx.final_reply or ""
        assert "ran too long" in reply
        assert "12,288" in reply
        assert "timed out" not in reply
        assert "request was large" not in reply
        assert ctx.turn_stopped_early is True
        assert all(m.get("role") != "assistant" for m in ctx.messages), (
            "the runaway reply must not enter history"
        )
        emitted = _bound_events(events)
        assert len(emitted) == 1
        assert emitted[0]["deployment"] == _LOCAL_KEY
        assert emitted[0]["bound"] == _BOUND
        assert emitted[0]["tokens_generated"] == _BOUND
        assert emitted[0]["trace_id"] == "trace-fre1562"

    @pytest.mark.asyncio
    async def test_gathered_tool_results_are_kept_in_the_reply(self) -> None:
        client, response = await _respond_from_real_catalog(
            model_key=_LOCAL_KEY, stub_tokens=None, wire=_Wire()
        )
        ctx = _ctx()
        ctx.tool_results = [{"tool_name": "web_search", "success": True}]

        await _finalize(ctx, response, generation_bound=ex._local_generation_bound(client))

        reply = ctx.final_reply or ""
        assert "ran too long" in reply
        assert "web_search: success" in reply


# ── AC-3 — a normal answer is not cut ─────────────────────────────────────


class TestANormalAnswerIsNotCut:
    @pytest.mark.asyncio
    async def test_an_answer_of_the_observed_maximum_completes_unchanged(self) -> None:
        wire = _Wire()
        client, response = await _respond_from_real_catalog(
            model_key=_LOCAL_KEY, stub_tokens=_OBSERVED_NORMAL_MAX, wire=wire
        )
        assert response["finish_reason"] == "stop"
        assert response["content"] == "w" * _OBSERVED_NORMAL_MAX

        ctx = _ctx()
        events: list[tuple[str, dict[str, Any]]] = []
        state = await _finalize(
            ctx, response, generation_bound=ex._local_generation_bound(client), events=events
        )

        assert state is TaskState.SYNTHESIS
        assert ctx.final_reply == "w" * _OBSERVED_NORMAL_MAX
        assert ctx.turn_stopped_early is False
        assert ctx.messages[-1] == {"role": "assistant", "content": "w" * _OBSERVED_NORMAL_MAX}
        assert _bound_events(events) == []

    @pytest.mark.asyncio
    async def test_a_tool_call_reply_still_goes_to_tool_execution(self) -> None:
        client, response = await _respond_from_real_catalog(
            model_key=_LOCAL_KEY,
            stub_tokens=_OBSERVED_NORMAL_MAX,
            stub_finish="tool_calls",
            wire=_Wire(),
        )
        response["tool_calls"] = [{"id": "call_1", "name": "web_search", "arguments": {"q": "x"}}]

        state = await _finalize(
            _ctx(), response, generation_bound=ex._local_generation_bound(client)
        )

        assert state is TaskState.TOOL_EXECUTION


# ── AC-4 — only local primaries change ────────────────────────────────────


def _cloud_response(finish_reason: str) -> LLMResponse:
    return {
        "role": "assistant",
        "content": "a cloud reply",
        "tool_calls": [],
        "reasoning_trace": None,
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "response_id": None,
        "raw": {},
        "finish_reason": finish_reason,
    }


class TestOnlyLocalPrimariesChange:
    def test_a_local_client_reports_its_bound_and_a_cloud_client_reports_none(self) -> None:
        from personal_agent.llm_client.factory import get_llm_client_for_key

        catalog = load_model_config(_CATALOG_PATH)
        with (
            patch("personal_agent.llm_client.factory.load_model_config", return_value=catalog),
            patch("personal_agent.config.load_model_config", return_value=catalog),
        ):
            local = get_llm_client_for_key(_LOCAL_KEY, budget_role="main_inference")
            cloud = get_llm_client_for_key("claude_sonnet", budget_role="main_inference")

        assert ex._local_generation_bound(local) == _BOUND
        assert ex._local_generation_bound(cloud) is None

    @pytest.mark.asyncio
    async def test_a_cloud_length_reply_follows_the_unchanged_path(self) -> None:
        ctx = _ctx()
        events: list[tuple[str, dict[str, Any]]] = []

        state = await _finalize(
            ctx, _cloud_response("length"), generation_bound=None, events=events
        )

        assert state is TaskState.SYNTHESIS
        assert ctx.final_reply == "a cloud reply"
        assert ctx.turn_stopped_early is False
        assert _bound_events(events) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("key", "max_tokens_before"), [("gpt-5.4-mini", 8192), ("claude_sonnet", 128000)]
    )
    async def test_cloud_requests_carry_the_max_tokens_they_always_did(
        self, key: str, max_tokens_before: int
    ) -> None:
        from personal_agent.llm_client.factory import get_llm_client_for_key

        captured: dict[str, Any] = {}

        async def _fake_acompletion(**kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            usage = MagicMock(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                cache_read_input_tokens=None,
                cache_creation_input_tokens=None,
                prompt_tokens_details=None,
            )
            response = MagicMock()
            response.choices = [MagicMock()]
            response.choices[0].message.content = "ok"
            response.choices[0].message.tool_calls = None
            response.usage = usage
            response.id = "resp_fre1562"
            return response

        gate = MagicMock()
        gate.reserve = AsyncMock(return_value="res-fre1562")
        gate.commit = AsyncMock()
        tracker = AsyncMock()

        with (
            pinned_litellm_capabilities(),
            patch("litellm.acompletion", side_effect=_fake_acompletion),
            patch("litellm.completion_cost", return_value=0.001),
            patch("personal_agent.cost_gate.get_default_gate", return_value=gate),
            patch("personal_agent.cost_gate.load_budget_config", return_value=MagicMock()),
            patch(
                "personal_agent.llm_client.cost_estimator.estimate_reservation_for_call",
                return_value=Decimal("0.01"),
            ),
            patch(
                "personal_agent.llm_client.history_sanitiser.sanitise_messages",
                side_effect=lambda msgs, trace_id: (msgs, []),
            ),
            patch(
                "personal_agent.llm_client.cost_tracker.get_cost_tracker_service",
                return_value=tracker,
            ),
            patch(
                "personal_agent.config.settings.get_settings",
                return_value=MagicMock(anthropic_api_key="k", openai_api_key="k"),
            ),
        ):
            client = get_llm_client_for_key(key, budget_role="captains_log")
            await client.respond(
                role=ModelRole.SESSION_SUMMARY,
                messages=[{"role": "user", "content": "summarise"}],
                trace_ctx=make_test_ctx("fre1562_cloud"),
            )

        assert captured["max_tokens"] == max_tokens_before


# ── The production callers forward the bound (codex plan review, FRE-1562) ─


def _step_patches(mock_llm: MagicMock) -> ExitStack:
    """Patch set that drives ``step_llm_call`` up to the ``respond()`` boundary."""
    stack = ExitStack()
    for target, value in (
        ("personal_agent.orchestrator.skills.get_skill_bodies", ("", ())),
        ("personal_agent.orchestrator.skills.assemble_skill_index", ""),
        ("personal_agent.orchestrator.skills.assemble_skill_index_directive", ""),
        ("personal_agent.orchestrator.skills.assemble_skill_usage_directives", ""),
        ("personal_agent.orchestrator.skills.get_all_skills", {}),
        ("personal_agent.llm_client.factory.get_llm_client", mock_llm),
    ):
        stack.enter_context(patch(target, return_value=value))
    stack.enter_context(
        patch.object(
            ex,
            "get_default_registry",
            return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
        )
    )
    stack.enter_context(patch.object(ex, "_report_turn_progress", AsyncMock()))
    return stack


def _local_mock_client(respond: AsyncMock) -> MagicMock:
    from personal_agent.llm_client.models import Placement

    client = MagicMock()
    client.respond = respond
    client.model_configs = {}
    client.placement = Placement.LOCAL
    client.max_tokens = _BOUND
    return client


def _length_response() -> LLMResponse:
    return {
        "role": "assistant",
        "content": "w" * 50,
        "tool_calls": [{"id": "call_1", "name": "web_search", "arguments": {"q": "cut"}}],
        "reasoning_trace": None,
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": _BOUND,
            "total_tokens": 1000 + _BOUND,
        },
        "response_id": None,
        "raw": {},
        "finish_reason": "length",
    }


class TestStepLlmCallForwardsTheBound:
    """Both production callers of the finalizer pass the bound; the deployment comes from ctx."""

    @pytest.mark.asyncio
    async def test_the_normal_call_ends_the_turn_and_names_the_deployment(self) -> None:
        ctx = _ctx()
        ctx.answering_model_key = None  # step_llm_call must set it itself
        client = _local_mock_client(AsyncMock(return_value=_length_response()))
        events: list[tuple[str, dict[str, Any]]] = []

        with (
            _step_patches(client),
            patch.object(ex.log, "warning", lambda e, **p: events.append((e, p))),
        ):
            state = await ex.step_llm_call(ctx, MagicMock(), make_test_ctx("fre1562"))

        assert state is TaskState.SYNTHESIS
        assert "ran too long" in (ctx.final_reply or "")
        emitted = _bound_events(events)
        assert len(emitted) == 1
        assert emitted[0]["deployment"] == _LOCAL_KEY
        assert emitted[0]["bound"] == _BOUND
        assert emitted[0]["tokens_generated"] == _BOUND

    @pytest.mark.asyncio
    async def test_the_context_window_retry_forwards_the_bound_too(self) -> None:
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
                _length_response(),
            ]
        )
        client = _local_mock_client(respond)
        events: list[tuple[str, dict[str, Any]]] = []

        with (
            _step_patches(client),
            patch.object(ex.log, "warning", lambda e, **p: events.append((e, p))),
        ):
            state = await ex.step_llm_call(ctx, MagicMock(), make_test_ctx("fre1562"))

        assert respond.await_count == 2, "the retry path was not exercised"
        assert state is TaskState.SYNTHESIS
        assert len(_bound_events(events)) == 1
        assert ctx.turn_stopped_early is True

    @pytest.mark.asyncio
    async def test_a_cloud_client_length_reply_is_not_stopped(self) -> None:
        from personal_agent.llm_client.models import Placement

        ctx = _ctx()
        client = _local_mock_client(AsyncMock(return_value=_length_response()))
        client.placement = Placement.CLOUD
        events: list[tuple[str, dict[str, Any]]] = []

        with (
            _step_patches(client),
            patch.object(ex.log, "warning", lambda e, **p: events.append((e, p))),
        ):
            state = await ex.step_llm_call(ctx, MagicMock(), make_test_ctx("fre1562"))

        assert state is TaskState.TOOL_EXECUTION
        assert _bound_events(events) == []
        assert ctx.turn_stopped_early is False
