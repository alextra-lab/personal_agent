"""FRE-1368 — the thinking canary (ADR-0141 D6.1).

The canary dispatches through the **real** factory door and the **real**
``litellm.acompletion()``. Only the outbound transport is replaced, by patching
``httpx.AsyncHTTPTransport.handle_async_request`` (the pattern of
``tests/personal_agent/llm_client/test_local_via_litellm.py``). The stub behaves like
llama-server: it streams reasoning unless the request body carries
``chat_template_kwargs.enable_thinking == false``.

That placement is what makes AC-a decidable. A canary that read a hand-built payload
could not be shown to fail when the control evaporates in transit.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from personal_agent.llm_client import adapters as adapters_module
from personal_agent.llm_client import litellm_client as litellm_client_module
from personal_agent.llm_client.concurrency import (
    InferenceSlotTimeout,
    set_inference_concurrency_controller,
)
from personal_agent.llm_client.models import (
    ModelConfig,
    ModelDefinition,
    ModeSpec,
    Placement,
    ProviderDefinition,
    RoleBinding,
    ToolCallingStrategy,
)
from personal_agent.observability.slm_health import thinking_canary as canary_module
from personal_agent.observability.slm_health.cache import (
    clear_cache,
    get_cached_snapshot,
    set_cached_snapshot,
)
from personal_agent.observability.slm_health.snapshot import SlmHealthSnapshot
from personal_agent.observability.slm_health.thinking_canary import (
    ThinkingCanaryResult,
    run_thinking_canary,
)
from personal_agent.security import DomainGuard

LOCAL_BASE_URL = "https://slm.test.example/v1"
PRIMARY_KEY = "local_primary"
MODEL_ID = "unsloth/canary-test"
ALTERNATIVE_KEY = "local_alternative"
ALTERNATIVE_MODEL_ID = "unsloth/alternative-test"


# ── Fixtures ──────────────────────────────────────────────────────────────


def _config(
    *,
    default_thinking: bool = True,
    worker_thinking: bool = False,
    sub_agent_deployment: str = "inherit",
    sub_agent_mode: str | None = "worker",
    with_selectable_alternative: bool = False,
) -> ModelConfig:
    """One local deployment with a thinking default mode and a ``worker`` mode.

    ``with_selectable_alternative`` adds a second local deployment that the open ``primary``
    role may select, so a test can tell the catalog primary from a turn's selection.
    """
    sampling = {"temperature": 0.6, "top_p": 0.95, "top_k": 20}
    primary_def = ModelDefinition(
        id=MODEL_ID,
        provider="slm_local",
        context_length=131072,
        max_concurrency=1,
        endpoint=LOCAL_BASE_URL,
        tool_calling_strategy=ToolCallingStrategy.NATIVE,
        default_timeout=600,
        default_mode="default",
        modes={
            "default": ModeSpec(enable_thinking=default_thinking, **sampling),
            "worker": ModeSpec(enable_thinking=worker_thinking, **sampling),
        },
    )
    models = {PRIMARY_KEY: primary_def}
    if with_selectable_alternative:
        models[ALTERNATIVE_KEY] = primary_def.model_copy(update={"id": ALTERNATIVE_MODEL_ID})
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
        models=models,
        roles={
            "primary": RoleBinding(deployment=PRIMARY_KEY, open=True),
            "sub_agent": RoleBinding(
                deployment=sub_agent_deployment, mode=sub_agent_mode, open=True
            ),
        },
    )


def _permissive_guard() -> DomainGuard:
    guard = DomainGuard(cache_path=Path("telemetry/security/_unused_test_blocklist.json"))
    guard._blocklist = frozenset()
    guard._last_loaded = datetime.now(timezone.utc)
    return guard


def _sse(*chunks: dict[str, Any]) -> bytes:
    body = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
    return body + b"data: [DONE]\n\n"


def _chunk(
    delta: dict[str, Any] | None = None,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    chunk: dict[str, Any] = {
        "id": "chatcmpl-canary",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": MODEL_ID,
        "choices": []
        if delta is None and finish_reason is None
        else [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


_USAGE = {"prompt_tokens": 20, "completion_tokens": 12, "total_tokens": 32}


def _llama_server_stream(*, thinking: bool, shape: str, finish: str) -> bytes:
    """What llama-server streams for the canary prompt."""
    if not thinking:
        return _sse(
            _chunk({"role": "assistant", "content": "42"}),
            _chunk({}, finish_reason=finish),
            _chunk(usage=_USAGE),
        )
    if shape == "reasoning_content":
        deltas = [
            {"role": "assistant", "reasoning_content": "17 plus 25 is 42."},
            {"content": "42"} if finish == "stop" else {},
        ]
    else:  # inline, unclosed when the generation stops at the length cap
        text = "<think>17 plus 25 is 42.</think>42" if finish == "stop" else "<think>17 plus"
        deltas = [{"role": "assistant", "content": text}]
    return _sse(
        *(_chunk(d) for d in deltas), _chunk({}, finish_reason=finish), _chunk(usage=_USAGE)
    )


class Wire:
    """The requests the stub transport saw."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []


@pytest.fixture(autouse=True)
def _isolation() -> Iterator[None]:
    clear_cache()
    set_inference_concurrency_controller(None)
    yield
    clear_cache()
    set_inference_concurrency_controller(None)
    litellm_client_module._guarded_httpx_clients.clear()
    litellm_client_module._guarded_async_http_handlers.clear()


def _run_env(
    *,
    config: ModelConfig | None = None,
    shape: str = "reasoning_content",
    finish: str = "stop",
    busy: int = 0,
    served: set[str] | None = None,
    transport_error: Exception | None = None,
    dialect_wrapper: Callable[[Callable[..., Any]], Callable[..., Any]] | None = None,
    adapt_wrapper: Callable[[Callable[..., Any]], Callable[..., Any]] | None = None,
) -> tuple[ExitStack, Wire]:
    """Patch the catalog, the transport and the served-model lookups. Caller closes the stack."""
    catalog = config if config is not None else _config()
    wire = Wire()

    async def _handle(_self: Any, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        wire.bodies.append(body)
        if transport_error is not None:
            raise transport_error
        thinking = (body.get("chat_template_kwargs") or {}).get("enable_thinking", True)
        return httpx.Response(
            200,
            content=_llama_server_stream(thinking=thinking, shape=shape, finish=finish),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    real_info = litellm_client_module.log.info

    def _record(event: str, **payload: Any) -> Any:
        wire.events.append((event, payload))
        return real_info(event, **payload)

    stack = ExitStack()
    for target in (
        "personal_agent.llm_client.factory.load_model_config",
        "personal_agent.config.load_model_config",
        "personal_agent.config.model_loader.load_model_config",
    ):
        stack.enter_context(patch(target, return_value=catalog))
    stack.enter_context(
        patch(
            "personal_agent.llm_client.provider_health.fetch_served_model_ids",
            new=AsyncMock(return_value=served if served is not None else {MODEL_ID}),
        )
    )
    stack.enter_context(
        patch(
            "personal_agent.observability.slm_health.probe._provider_active_count",
            return_value=busy,
        )
    )
    stack.enter_context(
        patch.object(litellm_client_module, "get_domain_guard", return_value=_permissive_guard())
    )
    stack.enter_context(patch.object(litellm_client_module.log, "info", _record))
    stack.enter_context(patch.object(httpx.AsyncHTTPTransport, "handle_async_request", _handle))
    if dialect_wrapper is not None:
        stack.enter_context(
            patch.object(
                litellm_client_module,
                "_dialect_params",
                dialect_wrapper(litellm_client_module._dialect_params),
            )
        )
    if adapt_wrapper is not None:
        stack.enter_context(
            patch.object(
                adapters_module,
                "adapt_chat_completions_response",
                adapt_wrapper(adapters_module.adapt_chat_completions_response),
            )
        )
    return stack, wire


async def _canary(**env: Any) -> tuple[ThinkingCanaryResult, Wire]:
    stack, wire = _run_env(**env)
    with stack:
        result = await run_thinking_canary(base_url=LOCAL_BASE_URL, trace_id="canary-test")
    return result, wire


# ── The healthy path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_healthy_path_is_green() -> None:
    result, wire = await _canary()

    assert result.ok is True
    assert result.error is None and result.skip_reason is None
    assert result.on_reasoning_chars and result.on_reasoning_chars > 0
    assert result.off_reasoning_chars == 0
    assert len(wire.bodies) == 2
    on_body, off_body = wire.bodies
    assert "chat_template_kwargs" not in on_body
    assert off_body["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.asyncio
async def test_both_arms_are_bounded_and_background() -> None:
    """Each arm is capped in tokens and time, makes one request, and yields to user turns."""
    from personal_agent.llm_client.priority import InferencePriority

    real_respond = litellm_client_module.LiteLLMClient.respond
    calls: list[dict[str, Any]] = []

    async def _spy(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return await real_respond(self, *args, **kwargs)

    stack, wire = _run_env()
    with stack, patch.object(litellm_client_module.LiteLLMClient, "respond", _spy):
        await run_thinking_canary(base_url=LOCAL_BASE_URL, trace_id="canary-test")

    assert [b["max_tokens"] for b in wire.bodies] == [256, 64]
    assert len(calls) == 2
    for call in calls:
        assert call["timeout_s"] == 45.0
        assert call["max_retries"] == 0
        assert call["priority"] is InferencePriority.BACKGROUND
        assert call["priority_timeout"] == 2.0


# ── AC-a: able to fail in both directions ─────────────────────────────────


def _drop_disable_control(real: Callable[..., Any]) -> Callable[..., Any]:
    """The disable control evaporates in transit: the dialect block loses it."""

    def _wrapped(dialect: Any, mode: Any) -> dict[str, Any]:
        params = real(dialect, mode)
        params.get("extra_body", {}).pop("chat_template_kwargs", None)
        return params

    return _wrapped


def _drop_reasoning(real: Callable[..., Any]) -> Callable[..., Any]:
    """The reasoning mapping evaporates: the adapter returns no reasoning trace."""

    def _wrapped(response_data: dict[str, Any]) -> Any:
        adapted = real(response_data)
        adapted["reasoning_trace"] = None
        return adapted

    return _wrapped


@pytest.mark.asyncio
async def test_stripped_disable_control_turns_the_off_arm_red() -> None:
    result, wire = await _canary(dialect_wrapper=_drop_disable_control)

    assert all("chat_template_kwargs" not in body for body in wire.bodies)
    assert result.ok is False
    assert result.off_reasoning_chars and result.off_reasoning_chars > 0
    assert result.error is not None and "off arm" in result.error
    assert "on arm" not in result.error


@pytest.mark.asyncio
async def test_stripped_reasoning_mapping_turns_the_on_arm_red() -> None:
    result, _ = await _canary(adapt_wrapper=_drop_reasoning)

    assert result.ok is False
    assert result.on_reasoning_chars == 0
    assert result.error is not None and "on arm" in result.error
    assert "positive control" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["reasoning_content", "inline_think"])
async def test_on_arm_reads_reasoning_when_generation_stops_at_the_length_cap(shape: str) -> None:
    result, _ = await _canary(shape=shape, finish="length")

    assert result.ok is True
    assert result.on_reasoning_chars and result.on_reasoning_chars > 0


# ── A skip, or an error, is not a verdict ─────────────────────────────────


@pytest.mark.asyncio
async def test_a_dispatch_error_is_inconclusive_not_red() -> None:
    result, _ = await _canary(transport_error=httpx.ConnectError("refused"))

    assert result.ok is None
    assert result.error is not None
    assert result.on_reasoning_chars is None and result.off_reasoning_chars is None


@pytest.mark.asyncio
async def test_a_slot_timeout_is_inconclusive_not_red() -> None:
    stack, _ = _run_env()
    with (
        stack,
        patch.object(
            litellm_client_module.LiteLLMClient,
            "respond",
            new=AsyncMock(side_effect=InferenceSlotTimeout("busy")),
        ),
    ):
        result = await run_thinking_canary(base_url=LOCAL_BASE_URL, trace_id="canary-test")

    assert result.ok is None
    assert result.error is not None and "slot" in result.error


@pytest.mark.asyncio
async def test_a_busy_provider_skips_without_a_request() -> None:
    result, wire = await _canary(busy=1)

    assert result.ok is None
    assert result.skip_reason is not None and "busy" in result.skip_reason
    assert wire.bodies == []


@pytest.mark.asyncio
async def test_a_primary_that_is_not_served_skips() -> None:
    result, wire = await _canary(served={"some/other-model"})

    assert result.ok is None
    assert result.skip_reason is not None and "not currently served" in result.skip_reason
    assert wire.bodies == []


@pytest.mark.asyncio
async def test_a_primary_default_mode_without_thinking_skips() -> None:
    """A zero read against a backend that never thinks proves nothing (ADR-0141 D6.1)."""
    result, wire = await _canary(config=_config(default_thinking=False))

    assert result.ok is None
    assert result.skip_reason is not None and "does not enable thinking" in result.skip_reason
    assert wire.bodies == []


@pytest.mark.asyncio
async def test_a_sub_agent_mode_that_does_not_disable_thinking_skips() -> None:
    result, wire = await _canary(config=_config(worker_thinking=True))

    assert result.ok is None
    assert result.skip_reason is not None and "does not disable thinking" in result.skip_reason
    assert wire.bodies == []


@pytest.mark.asyncio
async def test_a_sub_agent_without_a_mode_skips() -> None:
    result, wire = await _canary(config=_config(sub_agent_mode=None))

    assert result.ok is None
    assert result.skip_reason is not None and "declares no mode" in result.skip_reason
    assert wire.bodies == []


# ── The target and the telemetry ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_turn_selection_does_not_change_the_target() -> None:
    """A valid, open selection moves a real turn; it must not move the canary."""
    from personal_agent.config import selection

    stack, wire = _run_env(config=_config(with_selectable_alternative=True))
    token = selection.set_current_selection({"primary": ALTERNATIVE_KEY})
    try:
        with stack:
            result = await run_thinking_canary(base_url=LOCAL_BASE_URL, trace_id="canary-test")
    finally:
        selection.reset_current_selection(token)

    assert result.ok is True
    assert [body["model"] for body in wire.bodies] == [MODEL_ID, MODEL_ID]


@pytest.mark.asyncio
async def test_canary_calls_carry_the_canary_callsite() -> None:
    _, wire = await _canary()

    completed = [p for name, p in wire.events if name == "model_call_completed"]
    assert len(completed) == 2
    assert {c["prompt_callsite"] for c in completed} == {
        "slm_health.thinking_canary.on",
        "slm_health.thinking_canary.off",
    }


@pytest.mark.asyncio
async def test_the_canary_never_publishes_a_verdict() -> None:
    """Run alone, the canary leaves an empty cache empty and a stored verdict as it was."""
    await _canary()
    assert get_cached_snapshot(ttl=60.0) is None

    stored = _snapshot("up")
    assert set_cached_snapshot(stored) is True
    await _canary(shape="inline_think")
    assert get_cached_snapshot(ttl=60.0) is stored


# ── The scheduled tick: the cache stays a verdict ─────────────────────────


def _snapshot(status: str = "up") -> SlmHealthSnapshot:
    return SlmHealthSnapshot(
        status=status,  # type: ignore[arg-type]
        reachable=status != "down",
        probed_at=datetime.now(timezone.utc),
        trace_id="tick-test",
    )


def _settings(*, canary: bool) -> MagicMock:
    cfg = MagicMock()
    cfg.slm_health_probe_enabled = True
    cfg.slm_thinking_canary_enabled = canary
    cfg.slm_health_generation_check_enabled = False
    cfg.resolved_slm_base_url = "https://slm.test.example"
    cfg.resolved_slm_health_url = "https://slm.test.example/health"
    cfg.slm_gpu_util_degraded_pct = 95.0
    cfg.slm_queue_depth_degraded = 4
    cfg.slm_health_index_prefix = "agent-monitors-slm-health"
    return cfg


_RED = ThinkingCanaryResult(
    ok=False, error="off arm: 31 reasoning chars with thinking disabled", off_reasoning_chars=31
)


@pytest.mark.asyncio
async def test_a_red_canary_leaves_the_cached_verdict_untouched() -> None:
    """Master condition, 2026-10-24: do not write the SLM-health verdict from the canary."""
    from personal_agent.observability.slm_health import scheduler_runner

    snap = _snapshot("up")
    write = AsyncMock()
    with (
        patch.object(scheduler_runner, "get_settings", return_value=_settings(canary=True)),
        patch.object(scheduler_runner, "probe_slm_health", new=AsyncMock(return_value=snap)),
        patch.object(scheduler_runner, "run_thinking_canary", new=AsyncMock(return_value=_RED)),
        patch.object(scheduler_runner, "write_result", new=write),
    ):
        returned = await scheduler_runner.run_scheduled_slm_health_probe(es_client=AsyncMock())

    cached = get_cached_snapshot(ttl=60.0)
    assert cached is snap
    assert cached.status == "up"
    assert cached.thinking_canary_ok is None and cached.thinking_canary_error is None
    written = write.await_args.args[1]
    assert written.status == "up"
    assert written.thinking_canary_ok is False
    assert written.thinking_canary_error == _RED.error
    assert written.thinking_canary_off_reasoning_chars == 31
    assert returned is written


@pytest.mark.asyncio
async def test_canary_off_by_default_changes_nothing() -> None:
    from personal_agent.observability.slm_health import scheduler_runner

    snap = _snapshot("up")
    canary = AsyncMock(return_value=_RED)
    with (
        patch.object(scheduler_runner, "get_settings", return_value=_settings(canary=False)),
        patch.object(scheduler_runner, "probe_slm_health", new=AsyncMock(return_value=snap)),
        patch.object(scheduler_runner, "run_thinking_canary", new=canary),
        patch.object(scheduler_runner, "write_result", new=AsyncMock()),
    ):
        returned = await scheduler_runner.run_scheduled_slm_health_probe(es_client=AsyncMock())

    canary.assert_not_awaited()
    assert returned is snap


@pytest.mark.asyncio
async def test_canary_does_not_run_against_an_unreachable_backend() -> None:
    from personal_agent.observability.slm_health import scheduler_runner

    snap = _snapshot("down")
    canary = AsyncMock(return_value=_RED)
    with (
        patch.object(scheduler_runner, "get_settings", return_value=_settings(canary=True)),
        patch.object(scheduler_runner, "probe_slm_health", new=AsyncMock(return_value=snap)),
        patch.object(scheduler_runner, "run_thinking_canary", new=canary),
        patch.object(scheduler_runner, "write_result", new=AsyncMock()),
    ):
        returned = await scheduler_runner.run_scheduled_slm_health_probe(es_client=None)

    canary.assert_not_awaited()
    assert returned is snap


@pytest.mark.asyncio
async def test_a_canary_that_raises_does_not_break_the_tick() -> None:
    from personal_agent.observability.slm_health import scheduler_runner

    snap = _snapshot("up")
    with (
        patch.object(scheduler_runner, "get_settings", return_value=_settings(canary=True)),
        patch.object(scheduler_runner, "probe_slm_health", new=AsyncMock(return_value=snap)),
        patch.object(
            scheduler_runner,
            "run_thinking_canary",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch.object(scheduler_runner, "write_result", new=AsyncMock()) as write,
    ):
        returned = await scheduler_runner.run_scheduled_slm_health_probe(es_client=AsyncMock())

    assert returned.status == "up"
    assert returned.thinking_canary_ok is None
    assert (
        returned.thinking_canary_error is not None
        and "RuntimeError" in returned.thinking_canary_error
    )
    write.assert_awaited_once()


def test_the_cache_still_orders_on_the_probe_snapshot() -> None:
    """A canary field must not change the cache's ordering rule (FRE-1435)."""
    first = _snapshot("up")
    assert set_cached_snapshot(first) is True
    assert set_cached_snapshot(first) is False
