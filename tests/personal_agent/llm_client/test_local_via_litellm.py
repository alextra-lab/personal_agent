"""ADR-0141 T2 / FRE-1365 — local placement dispatches through ``LiteLLMClient``.

Every test here dispatches through the **real** ``litellm.acompletion()`` and the
**real** factory door. Only the outbound transport is replaced, by patching
``httpx.AsyncHTTPTransport.handle_async_request`` — the transport the guarded
client built by ``create_guarded_http_client()`` actually uses on the
OpenAI-SDK route (ADR-0141 D2.1).

That placement is deliberate. AC-a of FRE-1365 fails if "the test hand-builds
the payload instead of exercising the dispatch path", so the request body every
assertion reads is the one litellm and the OpenAI SDK really put on the wire,
including the SDK's ``extra_body`` flattening — the behaviour whose absence on
the old raw-httpx path is the finding that forced ADR-0141.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from personal_agent.llm_client import litellm_client as litellm_client_module
from personal_agent.llm_client.models import (
    ModelConfig,
    ModelDefinition,
    Placement,
    ProviderDefinition,
    ToolCallingStrategy,
)
from personal_agent.llm_client.types import (
    LLMConnectionError,
    LLMRateLimit,
    LLMResponse,
    LLMServerError,
    LLMTimeout,
    ModelRole,
)
from personal_agent.security import DomainGuard
from personal_agent.telemetry.trace import SystemTraceContext

LOCAL_HOST = "slm.test.example"
LOCAL_BASE_URL = f"https://{LOCAL_HOST}/v1"

BUDGET_KEY = "local_budget"
DISABLE_KEY = "local_disable"


# ── Catalog fixtures — the primary's shape and the sub-agent's shape ──────


def _local_catalog() -> ModelConfig:
    """Two local deployments: one budget-declaring, one thinking-disabling.

    They mirror ``config/models.yaml``'s real primary and sub-agent entries in
    the fields this ticket is about. ADR-0141 D4 insists the two thinking
    shapes are distinct wire keys, not one — so they need distinct fixtures.
    """
    common: dict[str, Any] = {
        "provider": "slm_local",
        "context_length": 131072,
        "max_concurrency": 1,
        "endpoint": LOCAL_BASE_URL,
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repetition_penalty": 1.0,
        "tool_calling_strategy": ToolCallingStrategy.NATIVE,
    }
    return ModelConfig(
        providers={
            "slm_local": ProviderDefinition(
                base_url=LOCAL_BASE_URL,
                auth_env=None,
                placement=Placement.LOCAL,
                max_concurrency=2,
            )
        },
        models={
            # The primary's shape: a thinking budget, and NO max_tokens —
            # omit-means-unbounded (ADR-0141 D5).
            BUDGET_KEY: ModelDefinition(
                id="unsloth/qwen3.6-35-A3B",
                default_timeout=600,
                thinking_budget_tokens=32768,
                **common,
            ),
            # The sub-agent's shape: thinking hard-disabled, bounded output.
            DISABLE_KEY: ModelDefinition(
                id="unsloth/qwen3.6-35-A3B-Instruct",
                default_timeout=90,
                disable_thinking=True,
                max_tokens=2048,
                **common,
            ),
        },
    )


def _real_tracer() -> Any:
    """A genuine SDK tracer, so the model-call span has a recording context.

    Without one, ``model_call_span`` yields a non-recording span whose context
    is invalid: ``inject()`` then writes no ``traceparent`` at all and
    ``span_id`` formats as sixteen zeroes. An AC-f assertion that only checked
    header *presence* would pass on that; one that reads the real value needs
    the instrument to work first.
    """
    from opentelemetry.sdk.trace import TracerProvider

    return TracerProvider().get_tracer("fre1365-test")


def _permissive_guard() -> DomainGuard:
    """A pre-loaded DomainGuard that refuses nothing — never touches network or disk."""
    guard = DomainGuard(cache_path=Path("telemetry/security/_unused_test_blocklist.json"))
    guard._blocklist = frozenset()
    guard._last_loaded = datetime.now(timezone.utc)
    return guard


@pytest.fixture(autouse=True)
def _clear_guarded_client_caches() -> Iterator[None]:
    """Keep the module-level layer-2 client caches from leaking across tests."""
    yield
    litellm_client_module._guarded_httpx_clients.clear()
    litellm_client_module._guarded_async_http_handlers.clear()


# ── Recorded SSE streams ──────────────────────────────────────────────────


def _sse(*chunks: dict[str, Any]) -> bytes:
    body = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
    return body + b"data: [DONE]\n\n"


def _chunk(
    *,
    delta: dict[str, Any] | None = None,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    chunk: dict[str, Any] = {
        "id": "chatcmpl-local",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "unsloth/qwen3.6-35-A3B",
        "choices": []
        if delta is None and finish_reason is None
        else [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


USAGE_BLOCK: dict[str, Any] = {
    "prompt_tokens": 1120,
    "completion_tokens": 64,
    "total_tokens": 1184,
    "prompt_tokens_details": {"cached_tokens": 900},
}


def _plain_stream(content: str = "the answer") -> bytes:
    return _sse(
        _chunk(delta={"role": "assistant", "content": content}),
        _chunk(delta={}, finish_reason="stop"),
        _chunk(usage=USAGE_BLOCK),
    )


# ── Dispatch harness ──────────────────────────────────────────────────────


class Captured:
    """What the transport saw, and what ``respond()`` returned."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.urls: list[str] = []
        self.timeouts: list[Any] = []
        self.response: LLMResponse | None = None
        self.gate: MagicMock | None = None
        self.tracker: AsyncMock | None = None
        self.events: list[tuple[str, dict[str, Any]]] = []

    @property
    def body(self) -> dict[str, Any]:
        assert self.bodies, "no request reached the transport"
        return self.bodies[0]

    @property
    def header(self) -> dict[str, str]:
        assert self.headers, "no request reached the transport"
        return self.headers[0]

    def event(self, name: str) -> dict[str, Any]:
        for emitted, payload in self.events:
            if emitted == name:
                return payload
        raise AssertionError(f"no {name} among {[n for n, _ in self.events]}")

    def completed_event(self) -> dict[str, Any]:
        return self.event("model_call_completed")


async def _dispatch(
    *,
    model_key: str = BUDGET_KEY,
    stream: bytes | None = None,
    transport_error: Exception | None = None,
    status: int | None = None,
    session_id: str | None = "11111111-1111-4111-8111-111111111111",
    capture: Captured | None = None,
    **respond_kwargs: Any,
) -> Captured:
    """Run a local-placement call through the factory and real litellm dispatch.

    Args:
        model_key: Catalog key to acquire (drives which fixture shape is used).
        stream: SSE bytes the transport returns. Defaults to a plain reply.
        transport_error: Raised by the transport instead of returning a response.
        status: HTTP status to return instead of a 200 SSE stream.
        session_id: Session id on the trace context (``None`` omits it).
        capture: A caller-owned :class:`Captured` to fill in, so a test that
            expects the dispatch to raise can still read what the transport saw.
        **respond_kwargs: Passed through to ``respond()``.

    Returns:
        A :class:`Captured` holding the wire requests and the returned response.
    """
    from personal_agent.llm_client.factory import get_llm_client_for_key

    catalog = _local_catalog()
    captured = capture if capture is not None else Captured()

    async def _handle(_self: Any, request: httpx.Request) -> httpx.Response:
        captured.urls.append(str(request.url))
        captured.headers.append(dict(request.headers))
        captured.bodies.append(json.loads(request.content))
        captured.timeouts.append(request.extensions.get("timeout"))
        if transport_error is not None:
            raise transport_error
        if status is not None:
            return httpx.Response(
                status, json={"error": {"message": f"stub {status}"}}, request=request
            )
        return httpx.Response(
            200,
            content=stream if stream is not None else _plain_stream(),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    gate = MagicMock()
    gate.reserve = AsyncMock(return_value="reservation-should-not-happen")
    gate.commit = AsyncMock()
    gate.refund = AsyncMock()
    captured.gate = gate

    tracker = AsyncMock()
    tracker.connect = AsyncMock()
    tracker.record_api_call = AsyncMock()
    captured.tracker = tracker

    real_info = litellm_client_module.log.info

    def _record_info(event: str, **payload: Any) -> Any:
        captured.events.append((event, payload))
        return real_info(event, **payload)

    with ExitStack() as stack:
        stack.enter_context(
            patch("personal_agent.llm_client.factory.load_model_config", return_value=catalog)
        )
        stack.enter_context(patch("personal_agent.config.load_model_config", return_value=catalog))
        stack.enter_context(patch("personal_agent.cost_gate.get_default_gate", return_value=gate))
        stack.enter_context(
            patch("personal_agent.cost_gate.load_budget_config", return_value=MagicMock())
        )
        stack.enter_context(
            patch(
                "personal_agent.llm_client.cost_tracker.get_cost_tracker_service",
                return_value=tracker,
            )
        )
        stack.enter_context(patch.object(litellm_client_module.log, "info", _record_info))
        stack.enter_context(
            patch("personal_agent.telemetry.spans.get_tracer", return_value=_real_tracer())
        )
        stack.enter_context(patch.object(httpx.AsyncHTTPTransport, "handle_async_request", _handle))
        client = get_llm_client_for_key(model_key, budget_role="main_inference")
        client._egress_guard = _permissive_guard()
        captured.response = await client.respond(
            role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": "hello"}],
            trace_ctx=SystemTraceContext.new("fre1365", session_id=session_id),
            **respond_kwargs,
        )
    return captured


# ── AC-a — wire shape through the real dispatch path ──────────────────────


class TestWireShape:
    @pytest.mark.asyncio
    async def test_budget_shape_carries_every_non_standard_param_top_level(self) -> None:
        captured = await _dispatch(model_key=BUDGET_KEY)
        body = captured.body

        assert body["top_k"] == 20
        assert body["min_p"] == 0.0
        assert body["repetition_penalty"] == 1.0
        assert body["cache_prompt"] is True
        assert body["thinking_budget"] == 32768

    @pytest.mark.asyncio
    async def test_budget_shape_sends_no_thinking_disable(self) -> None:
        """The two thinking shapes are distinct keys, not one (ADR-0141 D4)."""
        body = (await _dispatch(model_key=BUDGET_KEY)).body
        assert "chat_template_kwargs" not in body

    @pytest.mark.asyncio
    async def test_disabling_shape_carries_chat_template_kwargs(self) -> None:
        body = (await _dispatch(model_key=DISABLE_KEY)).body
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        assert "thinking_budget" not in body

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model_key", [BUDGET_KEY, DISABLE_KEY])
    async def test_literal_extra_body_key_never_reaches_the_wire(self, model_key: str) -> None:
        """Pins the SDK flattening this ADR now depends on across upgrades."""
        assert "extra_body" not in (await _dispatch(model_key=model_key)).body

    @pytest.mark.asyncio
    async def test_no_max_tokens_when_the_catalog_omits_it(self) -> None:
        """ADR-0141 D5: the ``or 8192`` constructor fallback must not reach local."""
        assert "max_tokens" not in (await _dispatch(model_key=BUDGET_KEY)).body

    @pytest.mark.asyncio
    async def test_declared_max_tokens_is_still_sent(self) -> None:
        """Omit-means-unbounded is not omit-always — a declared cap still applies."""
        assert (await _dispatch(model_key=DISABLE_KEY)).body["max_tokens"] == 2048

    @pytest.mark.asyncio
    async def test_wire_model_is_the_bare_catalog_id_not_the_dispatch_prefix(self) -> None:
        """``openai/`` is litellm's routing prefix and must not leave the client."""
        assert (await _dispatch()).body["model"] == "unsloth/qwen3.6-35-A3B"

    @pytest.mark.asyncio
    async def test_request_goes_to_the_declared_local_endpoint(self) -> None:
        assert (await _dispatch()).urls[0] == f"{LOCAL_BASE_URL}/chat/completions"

    @pytest.mark.asyncio
    async def test_stream_and_usage_options_are_requested(self) -> None:
        """CF-524 avoidance carries over: bytes keep the proxy alive (ADR-0141 D7)."""
        body = (await _dispatch()).body
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}


# ── AC-b — streaming parity ───────────────────────────────────────────────


class TestStreamingParity:
    """A recorded pre-cutover SSE stream, replayed through the unified path.

    The comparison is against the aggregation the pre-cutover client performed
    on the *same* chunks — ``_aggregate_streaming_chunks`` +
    ``adapt_chat_completions_response``, called directly on the recorded
    payloads. That is what "the same LLMResponse usage block and tool-call set"
    means for this AC.
    """

    RECORDED: list[dict[str, Any]] = [
        _chunk(delta={"role": "assistant", "content": ""}),
        _chunk(
            delta={
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "search_memory", "arguments": '{"quer'},
                    }
                ]
            }
        ),
        _chunk(
            delta={"tool_calls": [{"index": 0, "function": {"arguments": 'y": "melon"}'}}]},
            finish_reason="tool_calls",
        ),
        _chunk(usage=USAGE_BLOCK),
    ]

    @pytest.mark.asyncio
    async def test_usage_block_and_tool_calls_match_the_pre_cutover_aggregation(self) -> None:
        from personal_agent.llm_client.adapters import (
            _aggregate_streaming_chunks,
            adapt_chat_completions_response,
        )

        expected = adapt_chat_completions_response(_aggregate_streaming_chunks(self.RECORDED))
        captured = await _dispatch(stream=_sse(*self.RECORDED))
        assert captured.response is not None

        assert captured.response["tool_calls"] == expected["tool_calls"]
        assert captured.response["usage"]["prompt_tokens"] == expected["usage"]["prompt_tokens"]
        assert (
            captured.response["usage"]["completion_tokens"]
            == expected["usage"]["completion_tokens"]
        )
        assert captured.response["usage"]["total_tokens"] == expected["usage"]["total_tokens"]

    @pytest.mark.asyncio
    async def test_split_tool_call_arguments_are_reassembled(self) -> None:
        captured = await _dispatch(stream=_sse(*self.RECORDED))
        assert captured.response is not None
        (call,) = captured.response["tool_calls"]
        assert call["name"] == "search_memory"
        assert call["id"] == "call_abc"
        assert json.loads(call["arguments"]) == {"query": "melon"}

    @pytest.mark.asyncio
    async def test_visible_content_survives_the_stream_intact(self) -> None:
        """Usage and tool calls matching is not enough — the answer must arrive."""
        captured = await _dispatch(stream=_plain_stream("the whole answer, unabridged"))
        assert captured.response is not None
        assert captured.response["content"] == "the whole answer, unabridged"


# ── AC-c — the cost gate is not on the local hot path ─────────────────────


class TestCostGateSkipped:
    @pytest.mark.asyncio
    async def test_local_call_never_reserves(self) -> None:
        """ADR-0141 D7: no reservation, no Postgres round-trip on the hot turn path."""
        captured = await _dispatch()
        assert captured.gate is not None
        captured.gate.reserve.assert_not_called()
        captured.gate.commit.assert_not_called()
        captured.gate.refund.assert_not_called()

    @pytest.mark.asyncio
    async def test_local_call_opens_no_cost_ledger_connection(self) -> None:
        """The obligation is "no Postgres round-trip", not "clean accounting".

        Asserting only that the gate was not called would pass while
        ``cost_tracker.connect()`` still ran ahead of it — the pool acquisition
        the hot turn path is supposed to avoid entirely.
        """
        captured = await _dispatch()
        assert captured.tracker is not None
        captured.tracker.connect.assert_not_called()
        captured.tracker.record_api_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_local_call_books_no_cost(self) -> None:
        captured = await _dispatch()
        assert captured.response is not None
        assert captured.response.get("cost_usd", 0.0) == 0.0


# ── AC-d — telemetry parity ───────────────────────────────────────────────


class TestTelemetryParity:
    @pytest.mark.asyncio
    async def test_completed_event_reports_the_catalog_provider_not_the_prefix(self) -> None:
        event = (await _dispatch()).completed_event()
        assert event["provider"] == "slm_local"
        assert event["model"] == "unsloth/qwen3.6-35-A3B"
        assert event["role"] == ModelRole.PRIMARY.value

    @pytest.mark.asyncio
    async def test_completed_event_carries_prompt_identity(self) -> None:
        event = (await _dispatch()).completed_event()
        for field in (
            "prompt_callsite",
            "prompt_component_ids",
            "prompt_static_prefix_hash",
            "prompt_dynamic_hash",
        ):
            assert event[field] is not None, f"{field} missing from model_call_completed"

    @pytest.mark.asyncio
    async def test_completed_event_carries_token_and_cache_counts(self) -> None:
        event = (await _dispatch()).completed_event()
        assert event["input_tokens"] == USAGE_BLOCK["prompt_tokens"]
        assert event["output_tokens"] == USAGE_BLOCK["completion_tokens"]
        # ADR-0141 D6.4 makes this the corroborating field for cache_prompt.
        assert event["cache_read_tokens"] == 900

    @pytest.mark.asyncio
    async def test_started_event_is_emitted_with_the_same_span(self) -> None:
        """The canonical pair, not just the completed half (ADR-0074 §I2)."""
        captured = await _dispatch()
        started = captured.event("model_call_started")
        assert started["provider"] == "slm_local"
        assert started["endpoint"] == LOCAL_BASE_URL
        assert started["span_id"] == captured.completed_event()["span_id"]

    @pytest.mark.asyncio
    async def test_failure_emits_the_model_call_error_event(self) -> None:
        """Parity: the raw-httpx path emitted MODEL_CALL_ERROR on every failure."""
        errors: list[tuple[str, dict[str, Any]]] = []
        real_error = litellm_client_module.log.error

        def _record_error(event: str, **payload: Any) -> Any:
            errors.append((event, payload))
            return real_error(event, **payload)

        with patch.object(litellm_client_module.log, "error", _record_error):
            with pytest.raises(LLMServerError):
                await _dispatch(status=500, max_retries=0)

        (payload,) = [p for name, p in errors if name == "model_call_error"]
        assert payload["error_type"] == "LLMServerError"
        assert payload["provider"] == "slm_local"
        assert payload["role"] == ModelRole.PRIMARY.value


# ── AC-e — reasoning preservation, both shapes ────────────────────────────


class TestReasoningPreservation:
    @pytest.mark.asyncio
    async def test_inline_think_tags_populate_reasoning_trace(self) -> None:
        stream = _sse(
            _chunk(
                delta={
                    "role": "assistant",
                    "content": "<think>weighing the options</think>the answer",
                }
            ),
            _chunk(delta={}, finish_reason="stop"),
            _chunk(usage=USAGE_BLOCK),
        )
        captured = await _dispatch(stream=stream)
        assert captured.response is not None
        assert captured.response["reasoning_trace"] == "weighing the options"
        assert captured.response["content"] == "the answer"
        assert "<think>" not in captured.response["content"]

    @pytest.mark.asyncio
    async def test_reasoning_content_field_populates_reasoning_trace(self) -> None:
        stream = _sse(
            _chunk(
                delta={
                    "role": "assistant",
                    "content": "the answer",
                    "reasoning_content": "weighing the options",
                }
            ),
            _chunk(delta={}, finish_reason="stop"),
            _chunk(usage=USAGE_BLOCK),
        )
        captured = await _dispatch(stream=stream)
        assert captured.response is not None
        assert captured.response["reasoning_trace"] == "weighing the options"
        assert captured.response["content"] == "the answer"


# ── AC-f — all four propagation headers ───────────────────────────────────


class TestPropagationHeaders:
    @pytest.mark.asyncio
    async def test_all_four_headers_reach_the_wire(self) -> None:
        captured = await _dispatch()
        headers = {k.lower(): v for k, v in captured.header.items()}
        span_id = captured.completed_event()["span_id"]

        # Values, not just names: a header set to a constant or to the OTel
        # all-zero sentinel would satisfy a presence check and prove nothing.
        assert headers["x-trace-id"] == captured.completed_event()["trace_id"]
        assert headers["x-span-id"] == span_id
        assert span_id != "0" * 16
        assert headers["x-session-id"] == "11111111-1111-4111-8111-111111111111"

        # W3C traceparent: version-traceid-spanid-flags, carrying THIS span.
        version, trace_id_hex, span_id_hex, _flags = headers["traceparent"].split("-")
        assert version == "00"
        assert len(trace_id_hex) == 32
        assert span_id_hex == span_id

    @pytest.mark.asyncio
    async def test_session_header_is_omitted_when_there_is_no_session(self) -> None:
        captured = await _dispatch(session_id=None)
        headers = {k.lower() for k in captured.header}
        assert "x-session-id" not in headers

    @pytest.mark.asyncio
    async def test_span_header_matches_the_emitted_span_id(self) -> None:
        captured = await _dispatch()
        headers = {k.lower(): v for k, v in captured.header.items()}
        assert headers["x-span-id"] == captured.completed_event()["span_id"]


# ── AC-g — text tool-call fallback ────────────────────────────────────────


class TestTextToolCallFallback:
    @pytest.mark.asyncio
    async def test_textual_tool_call_without_structured_calls_is_parsed(self) -> None:
        """Unconditional fallback — it fires regardless of the declared strategy.

        The fixture declares ``tool_calling_strategy: native``; ADR-0141 D7 keeps
        the fallback ungated precisely so a native-strategy model that emits a
        textual call still recovers.
        """
        textual = (
            '<tool_call>\n{"name": "search_memory", "arguments": {"query": "melon"}}\n</tool_call>'
        )
        stream = _sse(
            _chunk(delta={"role": "assistant", "content": textual}),
            _chunk(delta={}, finish_reason="stop"),
            _chunk(usage=USAGE_BLOCK),
        )
        captured = await _dispatch(stream=stream)
        assert captured.response is not None
        (call,) = captured.response["tool_calls"]
        assert call["name"] == "search_memory"
        assert json.loads(call["arguments"]) == {"query": "melon"}


# ── AC-h — error taxonomy ─────────────────────────────────────────────────


class TestErrorTaxonomy:
    @pytest.mark.asyncio
    async def test_refused_connection_maps_to_connection_error(self) -> None:
        """Litellm reports a refused connection as ``InternalServerError`` with
        ``status_code=500`` (measured against litellm 1.98.0), so a mapping that
        read the class or the status alone would answer ``LLMServerError`` here.
        """
        with pytest.raises(LLMConnectionError):
            await _dispatch(transport_error=httpx.ConnectError("Connection refused"), max_retries=0)

    @pytest.mark.asyncio
    async def test_read_timeout_maps_to_timeout(self) -> None:
        with pytest.raises(LLMTimeout):
            await _dispatch(transport_error=httpx.ReadTimeout("read timed out"), max_retries=0)

    @pytest.mark.asyncio
    async def test_429_maps_to_rate_limit(self) -> None:
        with pytest.raises(LLMRateLimit):
            await _dispatch(status=429, max_retries=0)

    @pytest.mark.asyncio
    async def test_500_maps_to_server_error(self) -> None:
        with pytest.raises(LLMServerError):
            await _dispatch(status=500, max_retries=0)

    @pytest.mark.asyncio
    async def test_503_maps_to_server_error(self) -> None:
        with pytest.raises(LLMServerError):
            await _dispatch(status=503, max_retries=0)


# ── Parity: retries, timeouts, sanitiser, tool strategy ───────────────────


class TestRetryAndTimeoutParity:
    @pytest.mark.asyncio
    async def test_max_retries_zero_issues_exactly_one_request(self) -> None:
        """The no-retry contract the background producers depend on.

        ``entity_extraction`` passes ``max_retries=0`` because "a timeout means
        the model is overloaded; retrying queues more work". A second retry
        layer underneath us would silently break that — and there is one: the
        OpenAI SDK retries twice by default. So this is asserted by counting
        requests at the transport, not by reading the parameter back.
        """
        captured = Captured()
        with pytest.raises(LLMServerError):
            await _dispatch(status=500, max_retries=0, capture=captured)
        assert len(captured.bodies) == 1, (
            f"expected exactly one request, transport saw {len(captured.bodies)}"
        )

    @pytest.mark.asyncio
    async def test_retries_are_bounded_and_do_not_multiply_silently(self) -> None:
        """One retry budget, not two stacked layers.

        Measured against litellm 1.98.0, ``num_retries=N`` costs ``2N + 1``
        transport requests. That bound is asserted rather than assumed: an
        upgrade that re-attaches the SDK's own default (which would make it
        ``3(N+1)``) has to fail here, not on a 600-second production turn.
        """
        captured = Captured()
        with pytest.raises(LLMServerError):
            await _dispatch(status=500, max_retries=1, capture=captured)
        assert len(captured.bodies) == 3

    @pytest.mark.asyncio
    async def test_role_timeout_is_the_read_budget_not_the_connect_budget(self) -> None:
        """The primary's 600s is a GENERATION budget (ADR-0141 D7).

        Handing litellm a bare float applies it to connect, write and pool as
        well — measured — so an unreachable SLM tunnel would hang a turn for
        ten minutes instead of failing in ten seconds.
        """
        captured = await _dispatch(model_key=BUDGET_KEY)
        timeout = captured.timeouts[0]
        assert timeout["read"] == 600.0
        assert timeout["connect"] == 10.0
        assert timeout["write"] == 10.0
        assert timeout["pool"] == 10.0

    @pytest.mark.asyncio
    async def test_call_site_timeout_overrides_the_declared_default(self) -> None:
        captured = await _dispatch(timeout_s=12.0)
        assert captured.timeouts[0]["read"] == 12.0
        assert captured.timeouts[0]["connect"] == 10.0

    @pytest.mark.asyncio
    async def test_history_sanitiser_runs_on_the_local_path(self) -> None:
        with patch(
            "personal_agent.llm_client.history_sanitiser.sanitise_messages",
            side_effect=lambda msgs, trace_id: (msgs, []),
        ) as sanitiser:
            await _dispatch()
        sanitiser.assert_called_once()


class TestFactoryCollapse:
    @pytest.mark.asyncio
    async def test_local_placement_returns_the_unified_client(self) -> None:
        from personal_agent.llm_client.factory import get_llm_client_for_key
        from personal_agent.llm_client.litellm_client import LiteLLMClient

        with patch(
            "personal_agent.llm_client.factory.load_model_config", return_value=_local_catalog()
        ):
            client = get_llm_client_for_key(BUDGET_KEY, budget_role="main_inference")
        assert isinstance(client, LiteLLMClient)
