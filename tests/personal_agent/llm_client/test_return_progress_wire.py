"""FRE-1488 — ``return_progress`` is a llama.cpp field and reaches only that dialect.

``_respond_local`` sends it on the local ``llamacpp_qwen`` call, where it is the
only thing that puts a byte on the wire during prefill. The positive case and the
local-placement negatives live in ``test_local_via_litellm.py``. This module holds
the cloud branch, and every assertion reads the **serialized** request body the
transport received, not the keyword arguments handed to ``litellm.acompletion``:
a transformation after that boundary is exactly where a stray field would appear
(ADR-0141).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import ExitStack
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

from personal_agent.llm_client.factory import get_llm_client_for_key
from personal_agent.llm_client.litellm_client import LiteLLMClient
from personal_agent.llm_client.models import (
    ModelConfig,
    ModelDefinition,
    ModeSpec,
    Placement,
    ProviderDefinition,
    ToolCallingStrategy,
)
from personal_agent.llm_client.types import ModelRole
from tests._helpers.litellm_capability import pinned_litellm_capabilities
from tests._helpers.trace import make_test_ctx

_OPENAI_REPLY: dict[str, Any] = {
    "id": "resp_fre1488",
    "object": "chat.completion",
    "created": 0,
    "model": "test-model",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

_ANTHROPIC_REPLY: dict[str, Any] = {
    "id": "msg_fre1488",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 5},
}


@pytest.fixture(autouse=True)
def _pin_capabilities() -> Iterator[None]:
    """Litellm's GitHub-fetched capability map must not decide these results."""
    with pinned_litellm_capabilities():
        yield


def _side_effect_patches() -> list[Any]:
    """Patches that isolate dispatch from cost, credentials and telemetry."""
    gate = MagicMock()
    gate.reserve = AsyncMock(return_value="res-fre1488")
    gate.commit = AsyncMock()
    gate.refund = AsyncMock()
    tracker = AsyncMock()
    tracker.connect = AsyncMock()
    tracker.record_api_call = AsyncMock()
    return [
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
    ]


def _transport_patches(seen: list[dict[str, Any]], reply: dict[str, Any]) -> list[Any]:
    """Replace both HTTP routes litellm uses, recording each request's JSON body.

    The OpenAI SDK route ends in ``httpx.AsyncHTTPTransport``. Anthropic's and
    ovhcloud's routes post through litellm's own ``AsyncHTTPHandler``. Patching
    both lets one helper serve every provider the tests dispatch to.
    """

    async def _handle(_self: Any, request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=reply, request=request)

    async def _post(self: Any, url: str, headers: Any = None, **kwargs: Any) -> httpx.Response:
        body = kwargs.get("json")
        seen.append(body if body is not None else json.loads(kwargs["data"]))
        return httpx.Response(200, json=reply, request=httpx.Request("POST", url))

    return [
        patch.object(httpx.AsyncHTTPTransport, "handle_async_request", _handle),
        patch.object(AsyncHTTPHandler, "post", _post),
    ]


@pytest.fixture(autouse=True)
def _clear_guarded_client_caches() -> Iterator[None]:
    """Keep the module-level layer-2 client caches from leaking across tests."""
    from personal_agent.llm_client import litellm_client as module

    yield
    module._guarded_httpx_clients.clear()
    module._guarded_async_http_handlers.clear()


def _assert_field_absent(body: dict[str, Any]) -> None:
    assert "return_progress" not in body
    nested = body.get("extra_body")
    assert not (isinstance(nested, dict) and "return_progress" in nested)


class TestNoOtherDialectIsAffected:
    """AC-4 — the serialized body of each cloud dialect carries no ``return_progress``."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("model_key", "reply"),
        [
            ("gpt-5.4-mini", _OPENAI_REPLY),  # openai_gpt5, OpenAI SDK route
            ("claude_sonnet", _ANTHROPIC_REPLY),  # anthropic_adaptive, AsyncHTTPHandler route
            ("qwen3.8-27b-ovh", _OPENAI_REPLY),  # ovh_qwen
        ],
    )
    async def test_the_cloud_body_has_no_return_progress(
        self, model_key: str, reply: dict[str, Any]
    ) -> None:
        seen: list[dict[str, Any]] = []
        client = get_llm_client_for_key(model_key, budget_role="captains_log")
        with ExitStack() as stack:
            for p in [*_side_effect_patches(), *_transport_patches(seen, reply)]:
                stack.enter_context(p)
            await client.respond(
                role=ModelRole.SESSION_SUMMARY,
                messages=[{"role": "user", "content": "go"}],
                trace_ctx=make_test_ctx("fre1488"),
            )

        assert seen, "no request reached the transport — the test proves nothing"
        _assert_field_absent(seen[0])


class TestNonStreamingPathIsDecided:
    """AC-5 — a non-streaming call never carries the stream-only field.

    The dialect is a property of the provider, placement is another. A cloud
    placement that declares ``llamacpp_qwen`` runs the non-streaming branch and
    builds its parameter block from the same dialect. It must not receive a field
    the server documents as stream-only.
    """

    @pytest.mark.asyncio
    async def test_a_cloud_placement_declaring_llamacpp_qwen_sends_no_return_progress(
        self,
    ) -> None:
        catalog = ModelConfig(
            providers={
                "openai": ProviderDefinition(
                    base_url="https://llama.example.test/v1",
                    auth_env="openai_api_key",
                    placement=Placement.CLOUD,
                    max_concurrency=2,
                    dialect="llamacpp_qwen",
                )
            },
            models={},
        )
        definition = ModelDefinition(
            id="llama-behind-a-cloud-placement",
            provider="openai",
            context_length=32768,
            max_concurrency=1,
            default_timeout=60,
            default_mode="default",
            modes={"default": ModeSpec(temperature=0.6, top_k=20)},
            tool_calling_strategy=ToolCallingStrategy.NATIVE,
        )
        client = LiteLLMClient(
            model_id=definition.id,
            provider="openai",
            max_tokens=256,
            budget_role="captains_log",
            placement=Placement.CLOUD,
            model_def=definition,
            model_key="llama_behind_cloud",
        )
        seen: list[dict[str, Any]] = []
        with ExitStack() as stack:
            for p in [
                *_side_effect_patches(),
                *_transport_patches(seen, _OPENAI_REPLY),
                patch("personal_agent.config.load_model_config", return_value=catalog),
            ]:
                stack.enter_context(p)
            await client.respond(
                role=ModelRole.SESSION_SUMMARY,
                messages=[{"role": "user", "content": "go"}],
                trace_ctx=make_test_ctx("fre1488"),
            )

        assert seen, "no request reached the transport — the test proves nothing"
        body = seen[0]
        # The dialect block was applied (so the field's absence is a decision, not
        # a consequence of the dialect never being read) and the call is not a stream.
        assert body["cache_prompt"] is True
        assert body["top_k"] == 20
        assert body.get("stream") is not True
        _assert_field_absent(body)
