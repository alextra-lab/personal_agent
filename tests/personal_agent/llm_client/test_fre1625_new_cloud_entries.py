"""FRE-1625 — gpt-6-luna, gpt-6.1-sol and claude_haiku_5_5 on litellm 1.106.0 (AC-1, AC-3).

The eight ticket tests, offline. litellm reads its cost map once, at import; on
1.106.0 the bundled map already carries all four new models, so these results do
not depend on the remote fetch. ``test_bundled_map_carries_the_new_models`` pins
that premise rather than assuming it.

No test here reaches the network: the test-7 group replaces
``httpx.AsyncClient.send``, the one method every httpx client and transport (the
guarded client, litellm's own handlers, its aiohttp transport) sends through,
and asserts the request it caught.

Test 7 deviates from the ticket text by owner decision (2026-10-20): litellm
1.106.0's async Responses bridge drops the guarded client, and the translation is
proven at litellm's own boundary (7a). FRE-1627 gives ``LiteLLMClient`` its own
guarded Responses route, so a Sol tool call now dispatches through it (7b).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import litellm
import pytest
import yaml
from litellm.main import responses_api_bridge_check
from litellm.utils import get_optional_params
from pydantic import ValidationError

from personal_agent.config import load_model_config
from personal_agent.config.config_guard import reasoning_wire_shape
from personal_agent.config.model_loader import resolve_role_model_key
from personal_agent.llm_client.models import ModelConfig
from personal_agent.llm_client.pricing import register_model_pricing
from personal_agent.llm_client.types import (
    DialectParameterRejected,
    LLMClientError,
    LLMResponse,
    ModelRole,
)
from personal_agent.security import _guard_request_hook
from tests._helpers.trace import make_test_ctx

_NEW_KEYS = ("gpt-6-luna", "gpt-6.1-sol", "claude_haiku_5_5")

_FUNCTION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search_memory",
        "description": "Search stored memory.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}

# The documented Responses API shape for one function call (reasoning item first).
# Built from the API reference, not recorded live: the FRE-1573 hold forbids a
# live model call, and this shape is what litellm's bridge must translate.
_SOL_RESPONSES_BODY: dict[str, Any] = {
    "id": "resp_fre1625",
    "object": "response",
    "created_at": 1760900000,
    "status": "completed",
    "model": "gpt-6.1-sol-2026-09-29",
    "output": [
        {"type": "reasoning", "id": "rs_fre1625", "summary": []},
        {
            "type": "function_call",
            "id": "fc_fre1625",
            "call_id": "call_fre1625",
            "name": "search_memory",
            "arguments": '{"query": "paris"}',
            "status": "completed",
        },
    ],
    "usage": {
        "input_tokens": 50,
        "output_tokens": 20,
        "total_tokens": 70,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 8},
    },
}


def _config_dict() -> dict[str, Any]:
    """The real catalog as a plain dict, for a mutated re-validation."""
    return load_model_config().model_dump()


def test_the_remote_cost_map_is_off() -> None:
    """The ticket's premise: ``LITELLM_LOCAL_MODEL_COST_MAP`` is in force, so no fetch ran."""
    from litellm.litellm_core_utils.get_model_cost_map import _cost_map_source_info

    assert _cost_map_source_info.is_env_forced is True
    assert _cost_map_source_info.source == "local"


def test_bundled_map_carries_the_new_models() -> None:
    """The premise of the offline tests: litellm's own map knows all four models."""
    for model in ("gpt-6-luna", "gpt-6.1-sol", "claude-haiku-5-5", "claude-sonnet-5-5"):
        assert model in litellm.model_cost, model


class TestLunaParameters:
    """Tests 1 and 2: Luna's temperature pin works only at effort ``none``."""

    def test_1_effort_none_with_temperature_passes_both(self) -> None:
        """Litellm 1.98.0 raised UnsupportedParamsError here; 1.106.0 passes both."""
        params = get_optional_params(
            model="gpt-6-luna",
            custom_llm_provider="openai",
            reasoning_effort="none",
            temperature=0.0,
        )
        assert params["reasoning_effort"] == "none"
        assert params["temperature"] == 0.0

    def test_2_effort_high_with_temperature_is_refused_at_catalog_load(self) -> None:
        """The openai_gpt5 mode validator refuses the pair before any call."""
        data = _config_dict()
        data["models"]["gpt-6-luna"]["modes"]["default"] = {
            "reasoning_effort": "high",
            "temperature": 0.0,
        }
        with pytest.raises(ValidationError, match="only at effort 'none'"):
            ModelConfig.model_validate(data)


def test_3_haiku_5_5_low_maps_to_adaptive_thinking() -> None:
    """Haiku 5.5 rejects budget thinking; its effort must become output_config."""
    shape, error = reasoning_wire_shape("claude-haiku-5-5", "anthropic", {}, "low")
    assert error is None
    assert shape["thinking"]["type"] == "adaptive"
    assert shape["output_config"] == {"effort": "low"}
    assert "budget_tokens" not in shape["thinking"]


@pytest.mark.parametrize(
    ("litellm_model", "prompt_tokens", "expected_per_mtok"),
    [
        # Base rates, below each model's long-context tier — the catalog's own values.
        ("openai/gpt-6-luna", 10_000, (0.10, 0.50)),
        ("anthropic/claude-haiku-5-5", 10_000, (0.10, 0.50)),
        ("openai/gpt-6.1-sol", 10_000, (2.0, 10.0)),
        # Above the tier the official long-context rates apply. They come from
        # litellm's map and survive register_model_pricing, which sets base rates only.
        ("openai/gpt-6-luna", 300_000, (0.20, 0.75)),
        ("anthropic/claude-haiku-5-5", 150_000, (0.50, 2.50)),
        ("openai/gpt-6.1-sol", 300_000, (4.0, 15.0)),
    ],
)
def test_4_registered_prices_match_the_official_prices(
    litellm_model: str, prompt_tokens: int, expected_per_mtok: tuple[float, float]
) -> None:
    """USD per MTok in and out, after the catalog's own registration."""
    register_model_pricing(load_model_config(Path("config/models.yaml")))
    completion_tokens = 10_000
    input_cost, output_cost = litellm.cost_per_token(
        model=litellm_model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
    )
    per_mtok = (
        input_cost * 1_000_000 / prompt_tokens,
        output_cost * 1_000_000 / completion_tokens,
    )
    assert per_mtok == pytest.approx(expected_per_mtok)


class TestResponsesRoute:
    """Tests 5 and 6: which wire API a tool call takes on litellm 1.106.0."""

    def test_5_luna_at_effort_none_with_a_tool_stays_on_chat(self) -> None:
        """Luna's tool calls depend on Chat Completions at effort none."""
        info, model = responses_api_bridge_check(
            model="gpt-6-luna",
            custom_llm_provider="openai",
            tools=[_FUNCTION_TOOL],
            reasoning_effort="none",
        )
        assert info.get("mode") == "chat"
        assert model == "gpt-6-luna"

    def test_6_sol_at_effort_medium_with_a_tool_is_bridged_to_responses(self) -> None:
        """Sol takes tool calls only on the Responses API."""
        info, model = responses_api_bridge_check(
            model="gpt-6.1-sol",
            custom_llm_provider="openai",
            tools=[_FUNCTION_TOOL],
            reasoning_effort="medium",
        )
        assert info.get("mode") == "responses"
        assert model == "gpt-6.1-sol"


_LUNA_CHAT_BODY: dict[str, Any] = {
    "id": "chatcmpl-fre1625",
    "object": "chat.completion",
    "created": 1760900000,
    "model": "gpt-6-luna",
    "choices": [
        {
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_luna1625",
                        "type": "function",
                        "function": {"name": "search_memory", "arguments": '{"query": "paris"}'},
                    }
                ],
            },
        }
    ],
    "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
}


@dataclass
class _Sent:
    """One request the fake send caught, and the request hooks of the client that sent it."""

    request: httpx.Request
    request_hooks: int
    guarded: bool = False


@pytest.fixture
def _caught_requests() -> Iterator[list[_Sent]]:
    """Answer every httpx send offline; record what was sent and by which client."""
    caught: list[_Sent] = []

    async def _fake_send(
        self: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        hooks = self.event_hooks.get("request", [])
        guarded = any(getattr(hook, "func", None) is _guard_request_hook for hook in hooks)
        caught.append(_Sent(request, len(hooks), guarded))
        body = _SOL_RESPONSES_BODY if request.url.path.endswith("/responses") else _LUNA_CHAT_BODY
        return httpx.Response(200, json=body, request=request)

    with patch.object(httpx.AsyncClient, "send", _fake_send):
        yield caught


@pytest.mark.asyncio
async def test_7a_the_bridge_translates_a_sol_responses_tool_call(
    _caught_requests: list[_Sent],
) -> None:
    """Litellm's bridge turns a Responses function_call into a chat tool call.

    Called at litellm's own boundary: on 1.106.0 the async bridge drops the
    ``client=`` that carries egress-guard layer 2, so ``LiteLLMClient`` refuses
    a bridged request (test 7b, owner decision 2026-10-20). This keeps the
    translation itself proven for the follow-up that gives the bridge a guarded
    client. It also guards litellm #46045: on 1.106.0 with mcp 1.x, every
    ``completion()`` with ``tools`` raised ImportError before sending.
    """
    response = await litellm.acompletion(
        model="openai/gpt-6.1-sol",
        messages=[{"role": "user", "content": "what do you know about paris?"}],
        tools=[_FUNCTION_TOOL],
        reasoning_effort="medium",
        api_key="k",
    )

    assert len(_caught_requests) == 1
    sent = _caught_requests[0].request
    assert sent.url.path.endswith("/responses")
    assert json.loads(sent.content)["reasoning"]["effort"] == "medium"

    tool_calls = response.choices[0].message.tool_calls
    assert len(tool_calls) == 1
    assert tool_calls[0].function.name == "search_memory"
    assert json.loads(tool_calls[0].function.arguments) == {"query": "paris"}
    assert tool_calls[0].id == "call_fre1625"


def _mock_gate() -> MagicMock:
    """A cost gate whose reserve/commit/refund calls the test can inspect."""
    gate = MagicMock()
    gate.reserve = AsyncMock(return_value="res-fre1625")
    gate.commit = AsyncMock()
    gate.refund = AsyncMock()
    return gate


async def _respond(
    model_key: str, mock_gate: MagicMock | None = None, **call: Any
) -> tuple[LLMResponse, MagicMock]:
    """Dispatch through the trusted-key door with the cost gate and tracker mocked."""
    from personal_agent.llm_client.factory import get_llm_client_for_key

    if mock_gate is None:
        mock_gate = _mock_gate()
    client = get_llm_client_for_key(model_key, budget_role="captains_log")
    with (
        patch("litellm.completion_cost", return_value=0.001),
        patch("personal_agent.cost_gate.get_default_gate", return_value=mock_gate),
        patch("personal_agent.cost_gate.load_budget_config", return_value=MagicMock()),
        patch(
            "personal_agent.llm_client.cost_estimator.estimate_reservation_for_call",
            return_value=Decimal("0.01"),
        ),
        patch(
            "personal_agent.llm_client.cost_tracker.get_cost_tracker_service",
            return_value=AsyncMock(),
        ),
        patch(
            "personal_agent.config.settings.get_settings",
            return_value=MagicMock(anthropic_api_key="k", openai_api_key="k"),
        ),
    ):
        response = await client.respond(
            role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": "what do you know about paris?"}],
            trace_ctx=make_test_ctx("fre1625"),
            **call,
        )
    return response, mock_gate


class TestLiteLLMClientRoute:
    """Test 7 through ``LiteLLMClient``: the guard decides, not litellm's heuristics."""

    @pytest.mark.asyncio
    async def test_7b_a_sol_tool_call_dispatches_through_the_guarded_responses_route(
        self, _caught_requests: list[_Sent]
    ) -> None:
        """FRE-1627 AC-1: the request reaches /v1/responses on a client with the guard hook."""
        response, gate = await _respond("gpt-6.1-sol", tools=[_FUNCTION_TOOL])

        assert len(_caught_requests) == 1
        sent = _caught_requests[0]
        assert sent.request.url.path == "/v1/responses"
        assert sent.guarded, "the egress guard's request hook must ride on the client that sends"
        body = json.loads(sent.request.content)
        assert body["reasoning"]["effort"] == "medium"
        assert body["store"] is False
        assert body["tools"][0]["name"] == "search_memory"
        gate.reserve.assert_awaited_once()
        gate.commit.assert_awaited_once()

        assert [(c["id"], c["name"]) for c in response["tool_calls"]] == [
            ("call_fre1625", "search_memory")
        ]
        assert json.loads(response["tool_calls"][0]["arguments"]) == {"query": "paris"}
        assert response["finish_reason"] == "tool_calls"
        assert response["usage"]["reasoning_tokens"] == 8

    @pytest.mark.asyncio
    async def test_7c_a_luna_tool_call_at_effort_none_goes_to_chat_through_the_guard(
        self, _caught_requests: list[_Sent]
    ) -> None:
        """The positive control: the guarded client sends, and the tool call returns."""
        response, mock_gate = await _respond("gpt-6-luna", tools=[_FUNCTION_TOOL])

        assert len(_caught_requests) == 1
        sent = _caught_requests[0]
        assert sent.request.url.path.endswith("/chat/completions")
        assert sent.guarded, "layer 2 must ride on the client that sends"
        assert json.loads(sent.request.content)["reasoning_effort"] == "none"
        mock_gate.reserve.assert_awaited_once()

        tool_calls = response["tool_calls"]
        assert [(c["id"], c["name"]) for c in tool_calls] == [("call_luna1625", "search_memory")]
        assert json.loads(tool_calls[0]["arguments"]) == {"query": "paris"}

    @pytest.mark.asyncio
    async def test_a_sol_call_without_tools_stays_on_chat_through_the_guard(
        self, _caught_requests: list[_Sent]
    ) -> None:
        """The fail-closed check refuses only what the bridge would take."""
        await _respond("gpt-6.1-sol")

        assert len(_caught_requests) == 1
        sent = _caught_requests[0]
        assert sent.request.url.path.endswith("/chat/completions")
        assert sent.request_hooks > 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "call",
        [{"reasoning_effort": "none"}, {"temperature": 0.2}, {"top_p": 0.9}],
    )
    async def test_a_call_site_override_cannot_leave_sols_domain(
        self, call: dict[str, Any], _caught_requests: list[_Sent]
    ) -> None:
        """The catalog refuses these in a mode; the call site must not reintroduce them."""
        with pytest.raises(DialectParameterRejected, match="gpt-6.1-sol"):
            await _respond("gpt-6.1-sol", **call)
        assert _caught_requests == []


class TestSolCatalogRefusals:
    """Test 8: Sol's reasoning cannot be off, so neither none nor sampling may load."""

    def test_8a_effort_none_is_refused(self) -> None:
        """Litellm accepts it client-side; only the catalog can refuse it."""
        data = _config_dict()
        data["models"]["gpt-6.1-sol"]["modes"]["default"] = {"reasoning_effort": "none"}
        with pytest.raises(ValidationError, match="gpt-6.1-sol"):
            ModelConfig.model_validate(data)

    @pytest.mark.parametrize(
        "mode",
        [
            {"reasoning_effort": "medium", "temperature": 0.0},
            {"temperature": 0.0},
            {"top_p": 0.9},
        ],
    )
    def test_8b_a_sampler_is_refused(self, mode: dict[str, Any]) -> None:
        """Sampling is legal only at effort none, which Sol cannot reach."""
        data = _config_dict()
        data["models"]["gpt-6.1-sol"]["modes"]["worker"] = mode
        with pytest.raises(ValidationError, match="gpt-6.1-sol"):
            ModelConfig.model_validate(data)

    def test_the_shipped_sol_entry_loads(self) -> None:
        """The refusals above are of mutations, not of the entry itself."""
        definition = load_model_config().models["gpt-6.1-sol"]
        assert definition.resolve_mode().reasoning_effort == "medium"
        assert definition.resolve_mode("worker").reasoning_effort == "low"


class TestLocalPathRefusesTheBridgeToo:
    """Codex review (FRE-1625): ``_respond_local`` is a second dispatch site.

    The global flag and a ``responses/`` id prefix both make litellm route an
    ``openai/*`` call to the Responses API, whose async bridge drops the guarded
    client. Both need an environment or catalog change, so neither is reachable
    today, but the refusal must not depend on which client path a model takes.
    """

    @pytest.mark.asyncio
    async def test_the_global_responses_flag_is_refused_on_the_local_stream_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.personal_agent.llm_client.test_local_via_litellm import (
            BUDGET_KEY,
            Captured,
            _dispatch,
        )

        monkeypatch.setattr(litellm, "route_all_chat_openai_to_responses", True)
        captured = Captured()
        with pytest.raises(LLMClientError, match="Responses API bridge"):
            await _dispatch(model_key=BUDGET_KEY, capture=captured)
        assert captured.urls == [], "the request must not leave"

    @pytest.mark.asyncio
    async def test_without_the_flag_the_local_stream_path_still_dispatches(self) -> None:
        """The positive control: the same harness sends when the route is chat."""
        from tests.personal_agent.llm_client.test_local_via_litellm import BUDGET_KEY, _dispatch

        captured = await _dispatch(model_key=BUDGET_KEY)
        assert len(captured.urls) == 1


class TestFastModes:
    """ADR-0157 D5: the Fast level's mode on each new entry, at the bottom of its range."""

    @pytest.mark.parametrize(
        ("key", "field", "expected"),
        [
            ("gpt-6-luna", "reasoning_effort", "none"),
            ("gpt-6.1-sol", "reasoning_effort", "low"),
            ("claude_haiku_5_5", "effort", "low"),
        ],
    )
    def test_fast_mode_is_declared_at_the_bottom_of_the_range(
        self, key: str, field: str, expected: str
    ) -> None:
        """The mode exists, sets its dialect's lever, and keeps default_mode unchanged."""
        definition = load_model_config().models[key]
        fast = definition.resolve_mode("fast")
        assert getattr(fast, field) == expected
        assert definition.default_mode == "default"

    def test_sol_fast_cannot_turn_reasoning_off_or_sample(self) -> None:
        """Sol's domain has no none, so Fast stops at low and carries no sampler."""
        fast = load_model_config().models["gpt-6.1-sol"].resolve_mode("fast")
        assert fast.reasoning_effort != "none"
        assert fast.temperature is None
        assert fast.top_p is None


class TestNoBindingChange:
    """AC-3: the entries are pickable, not bound."""

    def test_no_role_resolves_to_a_new_key(self) -> None:
        """Every declared role resolves to a pre-existing key."""
        roles = yaml.safe_load(Path("config/model_roles.yaml").read_text())["bindings"]
        resolved = {role: resolve_role_model_key(role) for role in roles}
        assert not set(resolved.values()) & set(_NEW_KEYS), resolved
