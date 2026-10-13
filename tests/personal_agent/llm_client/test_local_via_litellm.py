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

import asyncio
import json
import sys
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import litellm
import pytest

from personal_agent.llm_client import litellm_client as litellm_client_module
from personal_agent.llm_client.models import (
    Dialect,
    ModelConfig,
    ModelDefinition,
    ModeSpec,
    Placement,
    ProviderDefinition,
    ToolCallingStrategy,
)
from personal_agent.llm_client.types import (
    GenerationProgress,
    LLMClientError,
    LLMConnectionError,
    LLMRateLimit,
    LLMResponse,
    LLMServerError,
    LLMTimeout,
    ModelRole,
)
from personal_agent.security import DomainGuard, EgressBlockedError
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
        "tool_calling_strategy": ToolCallingStrategy.NATIVE,
    }
    # ADR-0145 D3a: sampler/thinking values live only in modes: now, in the
    # dialect's own vocabulary. `repeat_penalty` is llama.cpp's real wire name
    # (FRE-1430 F2); `_local_extra_body` still sends the literal wire key
    # `repetition_penalty` regardless (unchanged — the rename is FRE-1438).
    common_mode = {
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repeat_penalty": 1.0,
    }
    return ModelConfig(
        providers={
            "slm_local": ProviderDefinition(
                base_url=LOCAL_BASE_URL,
                auth_env=None,
                placement=Placement.LOCAL,
                max_concurrency=2,
                dialect="llamacpp_qwen",
            )
        },
        models={
            # The primary's shape: thinking left on, no cap, and NO max_tokens —
            # omit-means-unbounded (ADR-0141 D5). `thinking_budget_tokens` used
            # to be declared here; it is gone (FRE-1430 F2 found it wire-inert;
            # dropped from the schema entirely, FRE-1423).
            BUDGET_KEY: ModelDefinition(
                id="unsloth/qwen3.6-35-A3B",
                default_timeout=600,
                default_mode="default",
                modes={"default": ModeSpec(enable_thinking=True, **common_mode)},
                **common,
            ),
            # The sub-agent's shape: thinking hard-disabled, bounded output.
            DISABLE_KEY: ModelDefinition(
                id="unsloth/qwen3.6-35-A3B-Instruct",
                default_timeout=90,
                max_tokens=2048,
                default_mode="default",
                modes={"default": ModeSpec(enable_thinking=False, **common_mode)},
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


def _guard(blocklist: frozenset[str]) -> DomainGuard:
    """A pre-loaded DomainGuard — never touches network or disk."""
    guard = DomainGuard(cache_path=Path("telemetry/security/_unused_test_blocklist.json"))
    guard._blocklist = blocklist
    guard._last_loaded = datetime.now(timezone.utc)
    return guard


def _permissive_guard() -> DomainGuard:
    """A DomainGuard that refuses nothing."""
    return _guard(frozenset())


def _blocking_guard() -> DomainGuard:
    """A DomainGuard that refuses the fixture deployments' own endpoint."""
    return _guard(frozenset({LOCAL_HOST}))


@pytest.fixture(autouse=True)
def _clear_guarded_client_caches() -> Iterator[None]:
    """Keep the module-level layer-2 client caches from leaking across tests."""
    yield
    litellm_client_module._guarded_httpx_clients.clear()
    litellm_client_module._guarded_async_http_handlers.clear()


@pytest.fixture(autouse=True)
def _reset_concurrency_singleton() -> Iterator[None]:
    """The re-homed controller (ADR-0141 D3) is process-global — reset it.

    Without this, whichever test dispatches first permanently seeds the
    singleton from ITS patched catalog, and every later test in this module
    (each with its own ``_local_catalog()``) would resolve the primary's
    deployment key against a stale registration.
    """
    from personal_agent.llm_client.concurrency import set_inference_concurrency_controller

    set_inference_concurrency_controller(None)
    yield
    set_inference_concurrency_controller(None)


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


async def _slow_sse(*chunks: dict[str, Any], delay: float) -> AsyncIterator[bytes]:
    """An SSE stream yielded one chunk at a time with a real await between them (FRE-1379).

    ``_sse()``/``_plain_stream()`` build a static bytes blob — httpx reads that
    all at once, with no real elapsed time between chunks, so a wall-clock
    timeout could never genuinely interrupt one mid-stream in a test. httpx
    reads an async-iterable ``content=`` lazily on iteration (verified: it does
    not eagerly materialise it at ``httpx.Response(...)`` construction time),
    so the ``asyncio.sleep`` between chunks here is real elapsed wall-clock
    time inside the test, not merely simulated.
    """
    for chunk in chunks:
        yield b"data: " + json.dumps(chunk).encode() + b"\n\n"
        await asyncio.sleep(delay)
    yield b"data: [DONE]\n\n"


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
    stream: bytes | AsyncIterator[bytes] | None = None,
    streams: list[bytes | AsyncIterator[bytes]] | None = None,
    transport_error: Exception | None = None,
    status: int | None = None,
    session_id: str | None = "11111111-1111-4111-8111-111111111111",
    capture: Captured | None = None,
    guard: DomainGuard | None = None,
    **respond_kwargs: Any,
) -> Captured:
    """Run a local-placement call through the factory and real litellm dispatch.

    Args:
        model_key: Catalog key to acquire (drives which fixture shape is used).
        stream: SSE bytes the transport returns, or an async byte iterator
            (e.g. :func:`_slow_sse`) for a test that needs real elapsed time
            between chunks. Defaults to a plain reply.
        streams: One body per request, in order (FRE-1602). The last entry
            answers any request past the end of the list. An async iterator is
            single-use, so a test that sends the same request twice needs one
            entry per attempt. Takes precedence over ``stream``.
        transport_error: Raised by the transport instead of returning a response.
        status: HTTP status to return instead of a 200 SSE stream.
        session_id: Session id on the trace context (``None`` omits it).
        guard: DomainGuard to inject; defaults to one that refuses nothing.
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
        if streams is not None:
            body_for_request = streams[min(len(captured.bodies), len(streams)) - 1]
        else:
            body_for_request = stream if stream is not None else _plain_stream()
        return httpx.Response(
            200,
            content=body_for_request,
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
    real_warning = litellm_client_module.log.warning
    real_error = litellm_client_module.log.error

    def _record_info(event: str, **payload: Any) -> Any:
        captured.events.append((event, payload))
        return real_info(event, **payload)

    def _record_warning(event: str, **payload: Any) -> Any:
        captured.events.append((event, payload))
        return real_warning(event, **payload)

    def _record_error(event: str, **payload: Any) -> Any:
        captured.events.append((event, payload))
        return real_error(event, **payload)

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
        stack.enter_context(patch.object(litellm_client_module.log, "warning", _record_warning))
        stack.enter_context(patch.object(litellm_client_module.log, "error", _record_error))
        stack.enter_context(
            patch("personal_agent.telemetry.spans.get_tracer", return_value=_real_tracer())
        )
        stack.enter_context(patch.object(httpx.AsyncHTTPTransport, "handle_async_request", _handle))
        client = get_llm_client_for_key(model_key, budget_role="main_inference")
        client._egress_guard = guard if guard is not None else _permissive_guard()
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
        assert "thinking_budget" not in body, (
            "thinking_budget is gone from the schema entirely (FRE-1430 F2 found "
            "it wire-inert on llama.cpp; FRE-1423 tracks the retirement) — "
            "enable_thinking is the only real local thinking lever now (F16)."
        )

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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model_key", [BUDGET_KEY, DISABLE_KEY])
    async def test_return_progress_is_a_top_level_key_of_the_serialized_body(
        self, model_key: str
    ) -> None:
        """FRE-1488 AC-1: the field is read off the bytes the transport received.

        Asserting on ``litellm_kwargs`` would pass with the field nested under a
        literal ``extra_body`` key, which is the ADR-0141 failure: the server
        ignores it and the pre-token byte is still only the 30 s ping.
        """
        body = (await _dispatch(model_key=model_key)).body
        assert body["return_progress"] is True
        assert "extra_body" not in body

    @pytest.mark.asyncio
    async def test_a_local_placement_on_another_dialect_sends_no_return_progress(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FRE-1488 AC-4: the field is the llama.cpp vocabulary, not every local call's.

        ``cache_prompt`` is the ``llamacpp_qwen`` block's other transport key. Its
        absence shows that block was not the one applied, so the missing
        ``return_progress`` is the dialect gate working and not a field that was
        dropped for another reason.
        """
        catalog = _local_catalog()
        catalog.providers["slm_local"] = catalog.providers["slm_local"].model_copy(
            update={"dialect": Dialect.OVH_QWEN}
        )
        monkeypatch.setattr(sys.modules[__name__], "_local_catalog", lambda: catalog)

        body = (await _dispatch(model_key=BUDGET_KEY)).body

        assert body["stream"] is True
        assert "cache_prompt" not in body
        assert "return_progress" not in body
        assert "extra_body" not in body


class TestSystemPromptAndParams:
    """Ported from the deleted tests/test_llm_client/test_client.py (ADR-0141 T4 /
    FRE-1367) — genuinely unique assertions not duplicated by TestWireShape above.
    """

    @pytest.mark.asyncio
    async def test_system_prompt_is_prepended_on_the_wire(self) -> None:
        body = (await _dispatch(system_prompt="You are a helpful assistant.")).body
        assert body["messages"][0] == {"role": "system", "content": "You are a helpful assistant."}

    @pytest.mark.asyncio
    async def test_model_default_temperature_reaches_the_wire(self) -> None:
        """No caller override — the catalog's declared temperature (0.6) is sent."""
        body = (await _dispatch()).body
        assert body["temperature"] == 0.6

    @pytest.mark.asyncio
    async def test_caller_temperature_overrides_the_model_default(self) -> None:
        body = (await _dispatch(temperature=0.1)).body
        assert body["temperature"] == 0.1

    @pytest.mark.asyncio
    async def test_response_format_reaches_the_wire(self) -> None:
        body = (await _dispatch(response_format={"type": "json_object"})).body
        assert body["response_format"] == {"type": "json_object"}


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

    @staticmethod
    def _engine_stream(*, fingerprint: str | None, wire_model: str) -> bytes:
        """A plain reply whose every chunk names an engine build and a wire model."""
        chunks = [
            _chunk(delta={"role": "assistant", "content": "the answer"}),
            _chunk(delta={}, finish_reason="stop"),
            _chunk(usage=USAGE_BLOCK),
        ]
        for chunk in chunks:
            chunk["model"] = wire_model
            if fingerprint is not None:
                chunk["system_fingerprint"] = fingerprint
        return _sse(*chunks)

    @pytest.mark.asyncio
    async def test_completed_event_names_the_engine_build(self) -> None:
        """FRE-1588 AC-1 (local path): llama.cpp's build reaches the event, via real litellm."""
        stream = self._engine_stream(fingerprint="b9999-test", wire_model="unsloth/qwen3.6-35-A3B")
        event = (await _dispatch(stream=stream)).completed_event()
        assert event["system_fingerprint"] == "b9999-test"

    @pytest.mark.asyncio
    async def test_completed_event_never_records_the_requested_model_as_the_reported_one(
        self,
    ) -> None:
        """FRE-1588 seeded negative.

        The engine answered as ``ENGINE-REPORTED-MODEL``. litellm replaces each
        streamed chunk's ``model`` with the requested id, so the event cannot
        know the engine's value. It must record null — not the requested id.
        """
        stream = self._engine_stream(fingerprint="b9999-test", wire_model="ENGINE-REPORTED-MODEL")
        event = (await _dispatch(stream=stream)).completed_event()
        assert event["response_model"] is None
        assert event["model"] == "unsloth/qwen3.6-35-A3B"  # the requested name, untouched

    @pytest.mark.asyncio
    async def test_completed_event_fingerprint_is_null_when_the_stream_has_none(self) -> None:
        """FRE-1588 AC-1: an engine that reports no build reads as null."""
        event = (await _dispatch()).completed_event()
        assert "system_fingerprint" in event
        assert event["system_fingerprint"] is None
        assert event["response_model"] is None

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
        # Ported from the deleted tests/test_llm_client/test_client.py
        # (test_respond_error_logs_session_id) — the error event must carry
        # session_id, not just trace_id, so a failed call is still joinable
        # to its session.
        assert payload["session_id"] == "11111111-1111-4111-8111-111111111111"


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


# ── FRE-1465 AC-3/AC-5 — the local dialect's reasoning-length telemetry ───


class TestReasoningContentCharsTelemetry:
    """``reasoning_content_chars`` on ``model_call_completed`` (AC-3), never the text (AC-5)."""

    @pytest.mark.asyncio
    async def test_thinking_on_reports_a_nonzero_char_count(self) -> None:
        """AC-3: BUDGET_KEY's mode leaves thinking on, so the trace has a nonzero length."""
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
        captured = await _dispatch(model_key=BUDGET_KEY, stream=stream)
        event = captured.completed_event()
        assert event["reasoning_content_chars"] == len("weighing the options")

    @pytest.mark.asyncio
    async def test_thinking_off_reports_a_zero_char_count(self) -> None:
        """DISABLE_KEY's mode hard-disables thinking — no trace, so the count is 0."""
        event = (await _dispatch(model_key=DISABLE_KEY)).completed_event()
        assert event["reasoning_content_chars"] == 0

    @pytest.mark.asyncio
    async def test_reasoning_text_itself_never_reaches_the_event(self) -> None:
        """AC-5: a character count only — the reasoning trace string never leaks."""
        stream = _sse(
            _chunk(
                delta={
                    "role": "assistant",
                    "content": "the answer",
                    "reasoning_content": "a secret chain of thought",
                }
            ),
            _chunk(delta={}, finish_reason="stop"),
            _chunk(usage=USAGE_BLOCK),
        )
        captured = await _dispatch(model_key=BUDGET_KEY, stream=stream)
        event = captured.completed_event()
        assert event["reasoning_content_chars"] == len("a secret chain of thought")
        assert "a secret chain of thought" not in repr(event)


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

    @pytest.mark.asyncio
    async def test_404_maps_to_client_error(self) -> None:
        """Ported from the deleted tests/test_llm_client/test_client.py — the
        generic catch-all branch of ``_map_local_dispatch_error`` (a 4xx that
        is neither 429 nor >=500 falls through to the base ``LLMClientError``).
        """
        with pytest.raises(LLMClientError):
            await _dispatch(status=404, max_retries=0)


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


class TestEgressGuardOnTheLocalRoute:
    """The seeded negative, dispatched through ``_respond_local`` itself.

    The FRE-1364 suite covers the OpenAI-SDK route through the cloud body of
    ``respond()``. That is the same injection *mechanism*, so ADR-0141 D2.2 is
    arguably satisfied by it — but an edit to ``_respond_local`` alone would
    trip none of those tests, and "the tests are the contract" is the whole
    point of the clause. So the local path gets its own.
    """

    @pytest.mark.asyncio
    async def test_layer1_blocks_a_blocklisted_endpoint_before_any_transport(self) -> None:
        captured = Captured()
        with pytest.raises(EgressBlockedError):
            await _dispatch(guard=_blocking_guard(), capture=captured)
        assert captured.bodies == [], "the request must never reach the transport"

    @pytest.mark.asyncio
    async def test_layer2_alone_blocks_when_layer1_is_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Layer 1 off — proves the injected hook guards this route on its own.

        The caller-facing type is asserted too: ``EgressBlockedError`` subclasses
        ``httpx.RequestError``, so a mapper that read the exception chain
        naively would report a network failure for a policy refusal.
        """
        monkeypatch.setattr(litellm_client_module, "check_egress_or_raise", lambda *a, **k: None)
        captured = Captured()
        with pytest.raises(EgressBlockedError):
            await _dispatch(guard=_blocking_guard(), capture=captured)
        assert captured.bodies == [], "the request must never reach the transport"


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


class TestRequestedKeyIsHonoured:
    """FRE-1343 — a local key binds the client to the deployment it names.

    Before ADR-0141 D1 the key-based door returned a bare ``LocalLLMClient()``
    that took no model key, so a standalone script got whatever the catalog
    resolved by default — the right-looking wrong answer. These tests read the
    ``model`` field of the request that reaches the wire, not the call's
    arguments: the ids differ between the two fixture deployments, so a client
    bound to anything but the requested key fails the equality.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", [BUDGET_KEY, DISABLE_KEY])
    async def test_wire_model_is_the_requested_keys_id_with_no_selection(self, key: str) -> None:
        from personal_agent.config.selection import get_current_selection

        assert get_current_selection(ModelRole.PRIMARY.value) is None, (
            "precondition: a standalone script has no per-turn selection"
        )

        captured = await _dispatch(model_key=key)

        assert captured.body["model"] == _local_catalog().models[key].id

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("requested", "selected"), [(BUDGET_KEY, DISABLE_KEY), (DISABLE_KEY, BUDGET_KEY)]
    )
    async def test_a_selection_for_another_key_does_not_displace_the_request(
        self, requested: str, selected: str
    ) -> None:
        from personal_agent.config.selection import (
            reset_current_selection,
            set_current_selection,
        )

        token = set_current_selection({ModelRole.PRIMARY.value: selected})
        try:
            captured = await _dispatch(model_key=requested)
        finally:
            reset_current_selection(token)

        assert captured.body["model"] == _local_catalog().models[requested].id

    def test_an_unknown_key_raises_instead_of_substituting(self) -> None:
        from personal_agent.llm_client.factory import get_llm_client_for_key

        with (
            patch(
                "personal_agent.llm_client.factory.load_model_config",
                return_value=_local_catalog(),
            ),
            pytest.raises(ValueError, match="Unknown model key 'not_in_catalog'"),
        ):
            get_llm_client_for_key("not_in_catalog", budget_role="main_inference")


# ── FRE-1379 AC-2/AC-3 — the generation budget bounds a real streaming call ─


class TestGenerationWallClockBudget:
    """The inner budget (``timeout_s``) now bounds wall-clock duration too.

    Before this, ``timeout_s`` only became the httpx read timeout (gap between
    bytes), which a steadily streaming response never trips — only the much
    larger outer hard deadline in ``sub_agent.py`` ever fired (the live
    incident this ticket exists for). Dispatched through the real
    ``LiteLLMClient``/``litellm.acompletion`` path; only the transport is
    faked, via the same harness every other test in this file uses.
    """

    @pytest.mark.asyncio
    async def test_wall_clock_budget_terminates_a_steadily_streaming_call(self) -> None:
        # Warm-up: the harness's first-ever local dispatch in a process pays a
        # ~0.7s one-time cost (litellm's cost-map lookup for an unmapped
        # model, tracer setup) unrelated to the mechanism under test — without
        # paying it here first, it would compete with the wall-clock budget
        # below and this test would assert on that overhead instead of on the
        # timeout. Measured: first dispatch ~0.79s, every one after ~0.05s.
        await _dispatch()

        slow_stream = _slow_sse(
            _chunk(delta={"role": "assistant", "content": "one "}),
            _chunk(delta={"content": "two "}),
            _chunk(delta={"content": "three "}),
            _chunk(delta={"content": "four "}),
            delay=0.08,
        )
        progress = GenerationProgress()

        start = time.monotonic()
        with pytest.raises(LLMTimeout) as exc_info:
            await _dispatch(
                stream=slow_stream, timeout_s=0.15, progress_sink=progress, max_retries=0
            )
        elapsed = time.monotonic() - start

        assert "0.1" in str(exc_info.value)  # names the wall-clock budget, not a read timeout
        # The proof that the budget actually intervened is in the CONTENT, not
        # wall-clock proximity (fragile in a shared/CI environment): cut off
        # before every chunk arrived (four chunks * 0.08s = 0.32s unbounded).
        assert progress.content != ""
        assert "four" not in progress.content
        assert progress.generation_started_monotonic is not None
        # Loose sanity bound only — did not run to completion.
        assert elapsed < 0.32

    @pytest.mark.asyncio
    async def test_stream_close_leaves_the_cached_pool_healthy_for_the_next_call(self) -> None:
        """Codex plan-review flagged this: an unclosed cancelled stream could
        leak into the cached guarded httpx.AsyncClient (process-lifetime pool)
        and wedge the next request through it.
        """
        slow_stream = _slow_sse(
            _chunk(delta={"role": "assistant", "content": "one "}),
            _chunk(delta={"content": "two "}),
            delay=0.05,
        )
        with pytest.raises(LLMTimeout):
            await _dispatch(stream=slow_stream, timeout_s=0.03)

        # A second, ordinary call through the same cached guarded client still
        # succeeds — the cancelled stream's cleanup did not wedge the pool.
        second = await _dispatch()
        assert second.response is not None
        assert second.response["content"] == "the answer"


# ── FRE-1488 AC-3 — prefill reports are not generation ────────────────────


def _progress_report(processed: int, *, shape: str) -> dict[str, Any]:
    """One llama-server ``prompt_progress`` report, in either shape litellm can hand on.

    ``role_delta`` carries a role-only delta, and litellm keeps the
    ``prompt_progress`` key on it. ``no_choices`` has an empty ``choices`` list,
    and litellm drops the key from it. The raw SSE carries the key in both.
    """
    report = (
        _chunk(delta={"role": "assistant", "content": None}) if shape == "role_delta" else _chunk()
    )
    report["prompt_progress"] = {
        "total": 8192,
        "cache": 0,
        "processed": processed,
        "time_ms": processed,
    }
    return report


_REPORT_SHAPES = ["role_delta", "no_choices"]


class TestPrefillReportsAreNotGeneration:
    """``return_progress`` puts reports on the stream before the first token.

    The streamed partial is the report a capped sub-agent hands back
    (ADR-0149 D3), and ``generation_started_monotonic`` is the clock its
    throughput estimate divides by. A report that reached either would corrupt
    both, silently.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shape", _REPORT_SHAPES)
    async def test_reports_before_the_answer_leave_the_partial_and_the_response_clean(
        self, shape: str
    ) -> None:
        stream = _sse(
            _progress_report(0, shape=shape),
            _progress_report(4096, shape=shape),
            _progress_report(8192, shape=shape),
            _chunk(delta={"role": "assistant", "content": "the answer"}),
            _chunk(delta={}, finish_reason="stop"),
            _chunk(usage=USAGE_BLOCK),
        )
        progress = GenerationProgress()

        captured = await _dispatch(stream=stream, progress_sink=progress)

        assert captured.response is not None
        assert captured.response["content"] == "the answer"
        assert progress.content == "the answer"
        assert "prompt_progress" not in progress.content
        assert progress.generation_started_monotonic is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shape", _REPORT_SHAPES)
    async def test_a_call_cut_during_prefill_has_no_content_and_no_generation_clock(
        self, shape: str
    ) -> None:
        # Warm-up, for the reason given in TestGenerationWallClockBudget.
        await _dispatch()

        reports_only = _slow_sse(
            *(_progress_report(step * 1024, shape=shape) for step in range(4)),
            delay=0.08,
        )
        progress = GenerationProgress()

        with pytest.raises(LLMTimeout):
            await _dispatch(
                stream=reports_only, timeout_s=0.15, progress_sink=progress, max_retries=0
            )

        assert progress.content == ""
        # Prefill is not generation. A clock set here would make a call that never
        # produced a token read as one that generated for as long as it prefilled.
        assert progress.generation_started_monotonic is None


# ── FRE-1602 — one retry of a local call whose stream broke mid-read ──────
#
# litellm retries a failure raised before the response starts (`2N + 1`
# requests, see TestRetryAndTimeoutParity) and never one raised while the
# stream is read. A Cloudflare tunnel drop is the second kind: the origin
# finishes the request and the gateway sees a reset body. These tests drive the
# real dispatch and break the stream at the transport.

_RESET_BODY = "peer closed connection without sending complete message body"
_PARTIAL_CHUNK = _chunk(delta={"role": "assistant", "content": "partial "})


async def _broken_sse(*chunks: dict[str, Any], error: Exception) -> AsyncIterator[bytes]:
    """Yield ``chunks`` as SSE, then fail the way a dropped connection does."""
    for chunk in chunks:
        yield b"data: " + json.dumps(chunk).encode() + b"\n\n"
    raise error


def _transport_break(shape: str) -> Exception:
    """The exception httpx raises, after its own mapping, for one reset shape."""
    if shape == "RemoteProtocolError":
        return httpx.RemoteProtocolError(_RESET_BODY)
    return httpx.ReadError("connection reset by peer")


@contextmanager
def _count_acompletion() -> Iterator[list[dict[str, Any]]]:
    """Record every ``litellm.acompletion`` call this layer makes.

    litellm's own retries happen inside one call, so the count is the number of
    attempts ``_respond_local`` made, whatever the transport saw.
    """
    real = litellm.acompletion
    calls: list[dict[str, Any]] = []

    async def _spy(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return await real(**kwargs)

    with patch.object(litellm, "acompletion", _spy):
        yield calls


class TestMidStreamTransportRetry:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("max_retries", [None, 1])
    @pytest.mark.parametrize("partial", [True, False])
    @pytest.mark.parametrize("shape", ["RemoteProtocolError", "ReadError"])
    async def test_a_mid_stream_transport_break_is_retried_once(
        self, shape: str, partial: bool, max_retries: int | None
    ) -> None:
        """AC-1 — the same request goes out a second time and its result is returned."""
        broken = _broken_sse(*([_PARTIAL_CHUNK] if partial else []), error=_transport_break(shape))

        captured = await _dispatch(streams=[broken, _plain_stream()], max_retries=max_retries)

        assert len(captured.bodies) == 2
        assert captured.bodies[0] == captured.bodies[1]
        assert captured.response is not None
        assert captured.response["content"] == "the answer"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("case", "dispatch_kwargs", "expected"),
        [
            ("http_400", lambda: {"status": 400}, LLMClientError),
            ("http_500", lambda: {"status": 500}, LLMServerError),
            (
                "mid_stream_read_timeout",
                lambda: {"streams": [_broken_sse(error=httpx.ReadTimeout("stalled"))]},
                LLMTimeout,
            ),
            (
                "mid_stream_non_transport_error",
                lambda: {"streams": [_broken_sse(_PARTIAL_CHUNK, error=ValueError("bad chunk"))]},
                LLMClientError,
            ),
        ],
    )
    async def test_only_transport_errors_are_sent_twice(
        self, case: str, dispatch_kwargs: Any, expected: type[Exception]
    ) -> None:
        """AC-2 — a failure that is not a transport break is sent once by this layer."""
        with _count_acompletion() as calls, pytest.raises(expected):
            await _dispatch(max_retries=1, **dispatch_kwargs())

        assert len(calls) == 1, case

    @pytest.mark.asyncio
    async def test_the_callers_wall_clock_timeout_is_not_retried(self) -> None:
        """AC-2 — the budget the caller set ends the call, not a second attempt."""
        await _dispatch()  # warm-up, for the reason given in TestGenerationWallClockBudget
        slow = _slow_sse(
            _chunk(delta={"role": "assistant", "content": "one "}),
            _chunk(delta={"content": "two "}),
            _chunk(delta={"content": "three "}),
            delay=0.08,
        )

        with _count_acompletion() as calls, pytest.raises(LLMTimeout):
            await _dispatch(stream=slow, timeout_s=0.15, max_retries=1)

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_a_cancel_by_the_caller_is_not_retried(self) -> None:
        """AC-2 — a cancelled call does not start a new attempt."""
        await _dispatch()  # warm-up
        slow = _slow_sse(
            _chunk(delta={"role": "assistant", "content": "one "}),
            _chunk(delta={"content": "two "}),
            delay=0.3,
        )
        captured = Captured()

        with _count_acompletion() as calls:
            task = asyncio.create_task(_dispatch(stream=slow, max_retries=1, capture=captured))
            async with asyncio.timeout(5):
                # The request is on the wire and the stream is being read. A dispatch that
                # fails first ends the wait, and awaiting the task below reports its error.
                while not captured.bodies and not task.done():
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert len(calls) == 1
        assert len(captured.bodies) == 1

    @pytest.mark.asyncio
    async def test_the_second_break_is_the_terminal_error(self) -> None:
        """AC-3 — one retry, then today's error. No third attempt."""
        captured = Captured()
        streams: list[bytes | AsyncIterator[bytes]] = [
            _broken_sse(_PARTIAL_CHUNK, error=httpx.RemoteProtocolError(_RESET_BODY))
            for _ in range(3)
        ]

        with pytest.raises(LLMConnectionError):
            await _dispatch(streams=streams, max_retries=1, capture=captured)

        names = [name for name, _ in captured.events]
        assert len(captured.bodies) == 2
        assert names.count("model_call_retried") == 1
        assert names.count("model_call_error") == 1, "only the terminal failure is an error"

    @pytest.mark.asyncio
    async def test_budget_zero_sends_one_request_on_a_mid_stream_break(self) -> None:
        """The ``max_retries=0`` contract of the background producers holds."""
        captured = Captured()

        with pytest.raises(LLMConnectionError):
            await _dispatch(
                streams=[
                    _broken_sse(error=httpx.RemoteProtocolError(_RESET_BODY)),
                    _plain_stream(),
                ],
                max_retries=0,
                capture=captured,
            )

        assert len(captured.bodies) == 1
        assert "model_call_retried" not in [name for name, _ in captured.events]

    @pytest.mark.asyncio
    async def test_the_retry_is_visible_in_telemetry(self) -> None:
        """AC-4 — a retried call is told apart from a clean one."""
        captured = await _dispatch(
            streams=[
                _broken_sse(_PARTIAL_CHUNK, error=httpx.ReadError("connection reset by peer")),
                _plain_stream(),
            ],
            max_retries=1,
        )

        retried = captured.event("model_call_retried")
        completed = captured.completed_event()
        assert retried["error_shape"] == "ReadError"
        assert retried["attempt"] == 2
        assert str(retried["trace_id"]) == str(completed["trace_id"]) != ""
        assert completed["attempts"] == 2

    @pytest.mark.asyncio
    async def test_a_clean_call_records_one_attempt(self) -> None:
        captured = await _dispatch()

        assert captured.completed_event()["attempts"] == 1
        assert "model_call_retried" not in [name for name, _ in captured.events]

    @pytest.mark.asyncio
    async def test_the_partial_of_the_first_attempt_does_not_reach_the_reply_or_the_sink(
        self,
    ) -> None:
        """AC-5 — the content and the progress sink hold only the second stream."""
        progress = GenerationProgress()

        captured = await _dispatch(
            streams=[
                _broken_sse(_PARTIAL_CHUNK, error=httpx.RemoteProtocolError(_RESET_BODY)),
                _plain_stream("the answer"),
            ],
            progress_sink=progress,
            max_retries=1,
        )

        assert captured.response is not None
        assert captured.response["content"] == "the answer"
        assert progress.content == "the answer"
        assert progress.generation_started_monotonic is not None

    @pytest.mark.asyncio
    async def test_the_generation_clock_of_the_first_attempt_does_not_outlive_it(self) -> None:
        """AC-5 — a retry cut during prefill has no content and no generation clock.

        The first attempt generates, so it sets the clock. The second attempt only
        prefills, so it must not inherit that clock: a sub-agent divides its
        output by the time since the clock started.
        """
        await _dispatch()  # warm-up, for the reason given in TestGenerationWallClockBudget
        prefill_only = _slow_sse(
            *(_progress_report(step * 1024, shape="no_choices") for step in range(6)),
            delay=0.08,
        )
        progress = GenerationProgress()

        with pytest.raises(LLMTimeout):
            await _dispatch(
                streams=[
                    _broken_sse(_PARTIAL_CHUNK, error=httpx.RemoteProtocolError(_RESET_BODY)),
                    prefill_only,
                ],
                timeout_s=0.3,
                progress_sink=progress,
                max_retries=1,
            )

        assert progress.content == ""
        assert progress.generation_started_monotonic is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"status": 502}, LLMServerError),
            ({"status": 503}, LLMServerError),
            ({"status": 504}, LLMTimeout),
            ({"transport_error": httpx.ConnectError("Connection refused")}, LLMConnectionError),
        ],
    )
    async def test_creation_time_failures_are_not_retried_again_by_this_layer(
        self, kwargs: dict[str, Any], expected: type[Exception]
    ) -> None:
        """AC-3 — litellm's budget already covers a failure before the response starts.

        ``2N + 1`` is 3 for ``N = 1``. A second layer on top would make it 6.
        """
        captured = Captured()

        with pytest.raises(expected):
            await _dispatch(max_retries=1, capture=captured, **kwargs)

        assert len(captured.bodies) == 3


class TestStreamTransportErrorShape:
    """The classifier names a reset or an incomplete body, and nothing else."""

    @pytest.mark.parametrize(
        ("error", "shape"),
        [
            (httpx.RemoteProtocolError(_RESET_BODY), "RemoteProtocolError"),
            (httpx.ReadError("connection reset by peer"), "ReadError"),
            (httpx.ReadTimeout("stalled"), None),
            (httpx.LocalProtocolError("bad request"), None),
            (
                EgressBlockedError(httpx.Request("POST", "https://x.test/v1"), "blocklisted", None),
                None,
            ),
            (ValueError("bad chunk"), None),
        ],
    )
    def test_names_the_shape_of_the_error_itself(
        self, error: BaseException, shape: str | None
    ) -> None:
        assert litellm_client_module._stream_transport_error_shape(error) == shape

    def test_finds_a_reset_wrapped_by_a_cause(self) -> None:
        try:
            try:
                raise httpx.ReadError("connection reset by peer")
            except httpx.ReadError as inner:
                raise RuntimeError("litellm wrapper") from inner
        except RuntimeError as wrapped:
            assert litellm_client_module._stream_transport_error_shape(wrapped) == "ReadError"

    def test_ignores_a_reset_the_raiser_suppressed(self) -> None:
        """``raise ... from None`` states that the earlier error is not the cause."""
        try:
            try:
                raise httpx.ReadError("connection reset by peer")
            except httpx.ReadError:
                raise ValueError("unparseable chunk") from None
        except ValueError as unrelated:
            assert litellm_client_module._stream_transport_error_shape(unrelated) is None
