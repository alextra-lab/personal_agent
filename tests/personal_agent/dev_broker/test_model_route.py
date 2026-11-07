# ruff: noqa: D102
"""The broker's model route (ADR-0159 D5.1, D5.6-D5.11; FRE-1683 AC-2, AC-4)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from decimal import ROUND_UP, Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from personal_agent.dev_broker.dispatch import ModelRequest
from personal_agent.dev_broker.model_route import ModelRoute, build_model_app

from personal_agent.config import load_model_config
from personal_agent.cost_gate.types import BudgetDenied
from personal_agent.dev_broker.config import BrokerConfig, load_broker_config
from personal_agent.dev_broker.ledger import (
    Cancelled,
    CostUnknown,
    Failed,
    GateDenied,
    Refusal,
    Settled,
    TimedOut,
)
from personal_agent.dev_broker.tokens import hash_token
from personal_agent.llm_client.types import LLMResponse
from personal_agent.telemetry.trace import TraceContext

from .fakes import FakeClock, FakeDispatcher, FakeLedger, llm_response

pytestmark = pytest.mark.asyncio

_TOKEN = "job-token-1"
_MODEL = "deepseek-v4.1-flash-scaleway"
_NOON = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
_MESSAGES = [{"role": "user", "content": "build a page"}]


class _Setup:
    def __init__(
        self, config: BrokerConfig | None = None, dispatcher: FakeDispatcher | None = None
    ):
        self.config = config or load_broker_config()
        self.ledger = FakeLedger(hash_token(_TOKEN), _MODEL)
        self.dispatcher = dispatcher or FakeDispatcher()
        self.clock = FakeClock(_NOON)
        self.route = ModelRoute(
            config=self.config,
            ledger=self.ledger,
            dispatcher=self.dispatcher,
            catalog=load_model_config().models,
            clock=self.clock,
        )


@pytest_asyncio.fixture
async def setup() -> AsyncIterator[_Setup]:
    yield _Setup()


async def _post(
    setup: _Setup, body: dict[str, Any] | bytes, *, token: str | None = _TOKEN
) -> httpx.Response:
    app = build_model_app(setup.route)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://broker"
    ) as client:
        return await client.post(
            "/v1/chat/completions",
            content=content,
            headers={**headers, "Content-Type": "application/json"},
        )


def _body(**extra: Any) -> dict[str, Any]:
    return {"model": _MODEL, "messages": _MESSAGES, **extra}


class TestAuthentication:
    async def test_no_token_is_401(self, setup: _Setup) -> None:
        response = await _post(setup, _body(), token=None)
        assert response.status_code == 401
        assert setup.dispatcher.calls == []

    async def test_unknown_token_is_401(self, setup: _Setup) -> None:
        response = await _post(setup, _body(), token="forged")
        assert response.status_code == 401
        assert setup.ledger.reserved == []

    async def test_another_model_is_403(self, setup: _Setup) -> None:
        response = await _post(setup, {**_body(), "model": "glm-5.3-scaleway"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "wrong_model"

    async def test_a_model_outside_the_config_is_403(self, setup: _Setup) -> None:
        response = await _post(setup, {**_body(), "model": "claude_sonnet"})
        assert response.status_code == 403
        assert setup.ledger.reserved == []


class TestRequestValidation:
    async def test_a_body_over_the_limit_is_413(self) -> None:
        setup = _Setup(load_broker_config().model_copy(update={"max_request_bytes": 64}))
        response = await _post(setup, _body(padding="x" * 200))
        assert response.status_code == 413
        assert setup.ledger.reserved == []

    async def test_a_body_that_is_not_json_is_400(self, setup: _Setup) -> None:
        response = await _post(setup, b"not json")
        assert response.status_code == 400

    async def test_messages_must_be_a_list(self, setup: _Setup) -> None:
        response = await _post(setup, {"model": _MODEL, "messages": "hi"})
        assert response.status_code == 400


class TestWorstCase:
    async def test_the_reservation_is_the_byte_bound_and_the_clamp_at_list_price(
        self, setup: _Setup
    ) -> None:
        tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
        await _post(setup, _body(tools=tools, max_tokens=500))

        entry = load_model_config().models[_MODEL]
        payload = json.dumps({"messages": _MESSAGES, "tools": tools}, ensure_ascii=False)
        bound = len(payload.encode("utf-8")) + setup.config.prompt_overhead_tokens
        expected = (
            Decimal(bound) * Decimal(str(entry.input_cost_per_token))
            + Decimal(500) * Decimal(str(entry.output_cost_per_token))
        ).quantize(Decimal("0.000001"), rounding=ROUND_UP)
        amount, prompt_bound, max_out, _ = setup.ledger.reserved[0]
        assert (amount, prompt_bound, max_out) == (expected, bound, 500)

    @pytest.mark.parametrize(
        ("requested", "clamped"),
        [
            ({}, 8192),
            ({"max_tokens": 100}, 100),
            ({"max_tokens": 100_000}, 8192),
            ({"max_completion_tokens": 300}, 300),
            ({"max_tokens": -5}, 8192),
        ],
    )
    async def test_output_tokens_are_clamped(
        self, setup: _Setup, requested: dict[str, int], clamped: int
    ) -> None:
        await _post(setup, _body(**requested))
        request, _ = setup.dispatcher.calls[0]
        assert request.max_tokens == clamped
        assert setup.ledger.reserved[0][2] == clamped


class TestDispatch:
    async def test_a_call_settles_at_the_reported_cost(self, setup: _Setup) -> None:
        response = await _post(setup, _body(temperature=0.2, top_k=9))

        assert response.status_code == 200
        data = response.json()
        assert data["object"] == "chat.completion"
        assert data["model"] == _MODEL
        assert data["choices"][0]["message"] == {"role": "assistant", "content": "hello"}
        assert data["choices"][0]["finish_reason"] == "stop"
        assert data["usage"] == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
        [(_, outcome)] = setup.ledger.settled
        assert outcome == Settled(Decimal("0.0004"), 12, 5)

    async def test_the_call_runs_under_the_jobs_trace_and_session(self, setup: _Setup) -> None:
        await _post(setup, _body())
        request, trace_ctx = setup.dispatcher.calls[0]
        assert request.model_key == _MODEL
        assert trace_ctx.trace_id == setup.ledger.grant.trace_id.hex
        assert trace_ctx.session_id == str(setup.ledger.grant.session_id)

    async def test_only_the_known_fields_are_forwarded(self, setup: _Setup) -> None:
        await _post(setup, _body(temperature=0.2, top_k=9, tool_choice="auto"))
        request, _ = setup.dispatcher.calls[0]
        assert request == ModelRequest(
            model_key=_MODEL,
            messages=tuple(_MESSAGES),
            tools=None,
            tool_choice="auto",
            temperature=0.2,
            max_tokens=8192,
        )

    async def test_tool_calls_are_returned_in_openai_shape(self) -> None:
        call = {"id": "call_1", "name": "write", "arguments": '{"path": "a"}'}

        async def handler(_r: ModelRequest, _t: TraceContext) -> LLMResponse:
            return llm_response(content="", tool_calls=[call], finish_reason="tool_calls")

        setup = _Setup(dispatcher=FakeDispatcher(handler))
        data = (await _post(setup, _body())).json()

        message = data["choices"][0]["message"]
        assert message["content"] is None
        assert message["tool_calls"] == [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "write", "arguments": '{"path": "a"}'},
            }
        ]
        assert data["choices"][0]["finish_reason"] == "tool_calls"

    @pytest.mark.parametrize(
        "response",
        [
            llm_response(usage={}),
            llm_response(usage={"prompt_tokens": 3}),
            llm_response(cost_usd=0.0),
        ],
    )
    async def test_a_call_without_a_cost_keeps_the_worst_case(self, response: LLMResponse) -> None:
        """AC-2: missing usage settles as CostUnknown, which keeps the worst case."""

        async def handler(_r: ModelRequest, _t: TraceContext) -> LLMResponse:
            return response

        setup = _Setup(dispatcher=FakeDispatcher(handler))
        assert (await _post(setup, _body())).status_code == 200
        assert [outcome for _, outcome in setup.ledger.settled] == [CostUnknown()]

    async def test_a_refusal_at_the_cap_is_403_and_never_dispatches(self, setup: _Setup) -> None:
        setup.ledger.refuse_reserve = Refusal("cap_reached")
        response = await _post(setup, _body())
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "cap_reached"
        assert setup.dispatcher.calls == []


class TestFailures:
    async def test_a_call_past_the_limit_is_ended_and_kept_at_the_worst_case(self) -> None:
        async def hang(_r: ModelRequest, _t: TraceContext) -> LLMResponse:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        config = load_broker_config().model_copy(update={"call_timeout_seconds": 0.05})
        setup = _Setup(config, FakeDispatcher(hang))

        response = await _post(setup, _body())

        assert response.status_code == 504
        assert [outcome for _, outcome in setup.ledger.settled] == [TimedOut()]

    async def test_a_cost_gate_denial_releases_the_reservation(self) -> None:
        async def deny(_r: ModelRequest, _t: TraceContext) -> LLMResponse:
            raise BudgetDenied(
                role="_total",
                time_window="weekly",
                current_spend=Decimal("10"),
                cap=Decimal("10"),
                window_resets_at=_NOON,
                denial_reason="cap_exceeded",
            )

        setup = _Setup(dispatcher=FakeDispatcher(deny))
        assert (await _post(setup, _body())).status_code == 503
        assert [outcome for _, outcome in setup.ledger.settled] == [GateDenied()]

    async def test_a_provider_error_releases_the_reservation(self) -> None:
        async def fail(_r: ModelRequest, _t: TraceContext) -> LLMResponse:
            raise RuntimeError("provider 500")

        setup = _Setup(dispatcher=FakeDispatcher(fail))
        response = await _post(setup, _body())
        assert response.status_code == 502
        assert "provider 500" not in response.text
        assert [outcome for _, outcome in setup.ledger.settled] == [Failed()]

    async def test_a_cancelled_request_keeps_the_worst_case(self) -> None:
        started = asyncio.Event()

        async def hang(_r: ModelRequest, _t: TraceContext) -> LLMResponse:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        setup = _Setup(dispatcher=FakeDispatcher(hang))
        task = asyncio.create_task(
            setup.route.complete(authorization=f"Bearer {_TOKEN}", body=_body())
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert [outcome for _, outcome in setup.ledger.settled] == [Cancelled()]


class TestBoundaryHold:
    async def test_a_call_100s_before_a_boundary_starts_after_it(self) -> None:
        """AC-4: the reserve and the dispatch both happen after 00:00 UTC."""
        midnight = datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc)
        seen: list[datetime] = []
        setup: _Setup

        async def record(_r: ModelRequest, _t: TraceContext) -> LLMResponse:
            seen.append(setup.clock.now())
            return llm_response()

        setup = _Setup(dispatcher=FakeDispatcher(record))
        setup.clock.current = midnight - timedelta(seconds=100)

        assert (await _post(setup, _body())).status_code == 200

        assert setup.clock.sleeps, "the call did not wait"
        assert setup.ledger.reserved[0][3] >= midnight
        assert seen[0] >= midnight

    async def test_a_call_far_from_a_boundary_does_not_wait(self, setup: _Setup) -> None:
        await _post(setup, _body())
        assert setup.clock.sleeps == []


class TestStreaming:
    async def _events(self, response: httpx.Response) -> list[Any]:
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = [line for line in response.text.split("\n") if line.startswith("data: ")]
        return [line[6:] if line == "data: [DONE]" else json.loads(line[6:]) for line in lines]

    async def test_a_streamed_answer_ends_with_done(self, setup: _Setup) -> None:
        events = await self._events(await _post(setup, _body(stream=True)))

        assert events[-1] == "[DONE]"
        chunks = events[:-1]
        assert all(c["object"] == "chat.completion.chunk" for c in chunks)
        assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": "hello"}
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
        assert not any("usage" in c for c in chunks)

    async def test_usage_is_streamed_when_asked(self, setup: _Setup) -> None:
        body = _body(stream=True, stream_options={"include_usage": True})
        events = await self._events(await _post(setup, body))
        usage_chunk = events[-2]
        assert usage_chunk["choices"] == []
        assert usage_chunk["usage"]["total_tokens"] == 17

    async def test_tool_calls_are_streamed_with_an_index(self) -> None:
        calls = [
            {"id": "c1", "name": "a", "arguments": "{}"},
            {"id": "c2", "name": "b", "arguments": '{"x": 1}'},
        ]

        async def handler(_r: ModelRequest, _t: TraceContext) -> LLMResponse:
            return llm_response(content="", tool_calls=calls, finish_reason="tool_calls")

        setup = _Setup(dispatcher=FakeDispatcher(handler))
        events = await self._events(await _post(setup, _body(stream=True)))
        deltas = [
            c["choices"][0]["delta"]["tool_calls"][0]
            for c in events[:-1]
            if c["choices"] and "tool_calls" in c["choices"][0]["delta"]
        ]
        assert [(d["index"], d["id"], d["function"]["name"]) for d in deltas] == [
            (0, "c1", "a"),
            (1, "c2", "b"),
        ]
