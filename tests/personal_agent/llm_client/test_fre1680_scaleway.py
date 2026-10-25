"""FRE-1680 — Scaleway Generative APIs as a picker-only cloud provider.

Offline. No test here reaches the network: dispatch tests replace
``httpx.AsyncClient.send`` (the one method every httpx client sends through, the
guarded client included) and assert the request it caught.

The wire facts come from master's probe on the ticket (2026-10-24): one OpenAI-style
dialect fits both models, ``reasoning_effort`` is the only thinking lever, and
``chat_template_kwargs`` and ``thinking`` have no effect. Only the value ``low`` was
measured, so the AC-3 proof on the deployed path covers ``medium`` and ``high``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import litellm
import pytest
import yaml
from pydantic import ValidationError

from personal_agent.config import load_model_config
from personal_agent.config.model_loader import resolve_role_model_key, role_candidates
from personal_agent.llm_client.models import (
    DIALECT_FIELDS,
    DIALECT_VALUE_DOMAINS,
    Dialect,
    ModelConfig,
    Placement,
)
from personal_agent.llm_client.pricing import register_model_pricing
from personal_agent.llm_client.types import DialectParameterRejected, ModelRole
from personal_agent.security import _guard_request_hook
from personal_agent.telemetry.trace import SystemTraceContext

_GLM = "glm-5.3-scaleway"
_DEEPSEEK = "deepseek-v4.1-flash-scaleway"
_NEW_KEYS = (_GLM, _DEEPSEEK)
_SECRET = "scw-test-secret-not-real"

#: USD per million tokens (input, output): the owner's console EUR prices x 1.18, the
#: rate the OVH chat entry uses. DeepSeek's cached input price (EUR 0.12) is not modelled.
_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    _GLM: (1.80 * 1.18, 5.50 * 1.18),
    _DEEPSEEK: (0.60 * 1.18, 1.80 * 1.18),
}
_LITELLM_MODEL = {_GLM: "scaleway/glm-5.3", _DEEPSEEK: "scaleway/deepseek-v4.1-flash"}

_PROMPT_TOKENS = 1_000
_COMPLETION_TOKENS = 200


def _config_dict() -> dict[str, Any]:
    """The real catalog as a plain dict, for a mutated re-validation."""
    return load_model_config().model_dump()


# ── The catalog ────────────────────────────────────────────────────────────


class TestCatalogEntries:
    def test_the_provider_is_a_cloud_provider_with_its_own_credential(self) -> None:
        provider = load_model_config().providers["scaleway"]
        assert provider.base_url == "https://api.scaleway.ai/v1"
        assert provider.auth_env == "scaleway_secret_key"
        assert provider.placement is Placement.CLOUD
        assert provider.dialect is Dialect.SCALEWAY

    @pytest.mark.parametrize(
        ("key", "model_id"), [(_GLM, "glm-5.3"), (_DEEPSEEK, "deepseek-v4.1-flash")]
    )
    def test_each_model_is_declared_on_the_scaleway_provider(self, key: str, model_id: str) -> None:
        definition = load_model_config().models[key]
        assert definition.provider == "scaleway"
        assert definition.id == model_id
        assert definition.context_length == 262144
        assert definition.supports_function_calling is True

    @pytest.mark.parametrize(("key", "default"), [(_GLM, "low"), (_DEEPSEEK, "high")])
    def test_modes_use_the_only_lever_the_probe_found(self, key: str, default: str) -> None:
        """DeepSeek's default is high: master's probe got a 400 on its medium.

        GLM's default is low (FRE-1699): at medium, its reasoning filled the 16,384 cap.
        """
        definition = load_model_config().models[key]
        assert definition.default_mode == "default"
        assert definition.resolve_mode().reasoning_effort == default
        assert definition.resolve_mode("worker").reasoning_effort == "low"
        assert definition.resolve_mode("fast").reasoning_effort == "low"

    @pytest.mark.parametrize("key", _NEW_KEYS)
    def test_the_picker_only_models_declare_no_sampler_the_probe_did_not_need(
        self, key: str
    ) -> None:
        mode = load_model_config().models[key].resolve_mode()
        assert mode.temperature is None
        assert mode.top_p is None


class TestDeepseekEffortDomain:
    """Master's probe of the deployed API (2026-10-24): deepseek-v4.1-flash returns 400 on medium."""

    def test_the_deepseek_domain_omits_medium_and_the_glm_domain_does_not(self) -> None:
        models = load_model_config().models
        assert models[_DEEPSEEK].reasoning_effort_domain == frozenset({"low", "high"})
        assert models[_GLM].reasoning_effort_domain == frozenset({"low", "medium", "high"})

    @pytest.mark.parametrize("mode_name", ["default", "worker", "fast"])
    def test_a_deepseek_mode_at_medium_is_refused_at_catalog_load(self, mode_name: str) -> None:
        data = _config_dict()
        data["models"][_DEEPSEEK]["modes"][mode_name] = {"reasoning_effort": "medium"}
        with pytest.raises(ValidationError, match=_DEEPSEEK):
            ModelConfig.model_validate(data)

    @pytest.mark.asyncio
    async def test_a_call_site_medium_is_refused_before_any_request(
        self, _caught: list[_Sent]
    ) -> None:
        """The runtime gate: an override cannot reintroduce the value the catalog refuses."""
        with pytest.raises(DialectParameterRejected, match="medium"):
            await _respond(_DEEPSEEK, reasoning_effort="medium")
        assert _caught == []


class TestDialectVocabulary:
    def test_accepted_fields_are_the_documented_parameters_minus_frequency_penalty(self) -> None:
        assert DIALECT_FIELDS[Dialect.SCALEWAY] == frozenset(
            {"reasoning_effort", "temperature", "top_p", "presence_penalty", "seed"}
        )

    def test_the_effort_domain_has_no_off_value(self) -> None:
        """The probe found no off switch, so ``none`` is not declarable."""
        domain = DIALECT_VALUE_DOMAINS[Dialect.SCALEWAY]["reasoning_effort"]
        assert domain == frozenset({"low", "medium", "high"})

    @pytest.mark.parametrize(
        "mode",
        [
            {"reasoning_effort": "medium", "frequency_penalty": 0.5},
            {"reasoning_effort": "medium", "enable_thinking": False},
            {"reasoning_effort": "medium", "effort": "low"},
            {"reasoning_effort": "none"},
            {"reasoning_effort": "xhigh"},
        ],
    )
    def test_a_mode_outside_the_vocabulary_is_refused_at_catalog_load(
        self, mode: dict[str, Any]
    ) -> None:
        data = _config_dict()
        data["models"][_GLM]["modes"]["default"] = mode
        with pytest.raises(ValidationError, match=_GLM):
            ModelConfig.model_validate(data)


# ── AC-4 and the picker (AC-1) ─────────────────────────────────────────────


class TestPickerOnly:
    def test_no_role_resolves_to_a_new_key(self) -> None:
        """AC-4: the entries are pickable, not bound. A swap is the owner's decision."""
        roles = yaml.safe_load(Path("config/model_roles.yaml").read_text())["bindings"]
        resolved = {role: resolve_role_model_key(role) for role in roles}
        assert not {r: k for r, k in resolved.items() if k in _NEW_KEYS}, resolved

    def test_no_role_binding_names_a_new_key(self) -> None:
        text = Path("config/model_roles.yaml").read_text()
        for key in _NEW_KEYS:
            assert key not in text

    def _candidates(self, *, scaleway_up: bool) -> list[str]:
        config = load_model_config()
        availability = {name: name != "scaleway" or scaleway_up for name in config.providers}
        return role_candidates(
            "primary", config, availability, served_model_ids={"slm_local": frozenset()}
        )

    def test_both_models_list_when_the_credential_is_set(self) -> None:
        assert set(_NEW_KEYS) <= set(self._candidates(scaleway_up=True))

    def test_neither_model_lists_when_the_credential_is_unset(self) -> None:
        assert not set(_NEW_KEYS) & set(self._candidates(scaleway_up=False))

    @pytest.mark.asyncio
    async def test_availability_follows_the_secret_setting(self) -> None:
        from personal_agent.llm_client.provider_health import is_provider_available

        provider = load_model_config().providers["scaleway"]
        assert await is_provider_available(provider, MagicMock(scaleway_secret_key=_SECRET))
        assert not await is_provider_available(provider, MagicMock(scaleway_secret_key=None))


# ── AC-2: prices ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", _NEW_KEYS)
def test_registered_prices_match_the_owners_price_table(key: str) -> None:
    register_model_pricing(load_model_config())
    input_cost, output_cost = litellm.cost_per_token(
        model=_LITELLM_MODEL[key], prompt_tokens=10_000, completion_tokens=10_000
    )
    assert (input_cost * 100, output_cost * 100) == pytest.approx(_PRICES_PER_MTOK[key])


# ── The wire (AC-1, AC-2, AC-3) ────────────────────────────────────────────


def _chat_body(model: str) -> dict[str, Any]:
    """A Scaleway-shaped reply: the ``reasoning`` field and the usage details the probe saw."""
    return {
        "id": "chatcmpl-fre1680",
        "object": "chat.completion",
        "created": 1761320000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "Paris is the capital of France.",
                    "reasoning": "The user asks about Paris.",
                },
            }
        ],
        "usage": {
            "prompt_tokens": _PROMPT_TOKENS,
            "completion_tokens": _COMPLETION_TOKENS,
            "total_tokens": _PROMPT_TOKENS + _COMPLETION_TOKENS,
            "prompt_tokens_details": {"cached_tokens": 640, "created_cache_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 6},
        },
    }


@dataclass
class _Sent:
    request: httpx.Request
    guarded: bool


@pytest.fixture
def _caught() -> Iterator[list[_Sent]]:
    """Answer every httpx send offline; record what was sent and by which client."""
    caught: list[_Sent] = []

    async def _fake_send(
        self: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        hooks = self.event_hooks.get("request", [])
        guarded = any(getattr(hook, "func", None) is _guard_request_hook for hook in hooks)
        caught.append(_Sent(request, guarded))
        body = json.loads(request.content)
        return httpx.Response(200, json=_chat_body(body["model"]), request=request)

    with patch.object(httpx.AsyncClient, "send", _fake_send):
        yield caught


async def _respond(
    model_key: str, *, mode: str | None = None, **call: Any
) -> tuple[MagicMock, MagicMock]:
    """Dispatch through the trusted-key door. Only the gate and the tracker are mocked.

    ``litellm.completion_cost`` is NOT patched: AC-2 needs the real cost the registered
    prices produce.
    """
    from personal_agent.llm_client.factory import get_llm_client_for_key

    register_model_pricing(load_model_config())
    gate = MagicMock()
    gate.reserve = AsyncMock(return_value="res-fre1680")
    gate.commit = AsyncMock()
    gate.refund = AsyncMock()
    tracker = AsyncMock()
    client = get_llm_client_for_key(model_key, budget_role="captains_log", mode=mode)
    with (
        patch("personal_agent.cost_gate.get_default_gate", return_value=gate),
        patch("personal_agent.cost_gate.load_budget_config", return_value=MagicMock()),
        patch(
            "personal_agent.llm_client.cost_estimator.estimate_reservation_for_call",
            return_value=Decimal("0.01"),
        ),
        patch(
            "personal_agent.llm_client.cost_tracker.get_cost_tracker_service", return_value=tracker
        ),
        patch(
            "personal_agent.config.settings.get_settings",
            return_value=MagicMock(scaleway_secret_key=_SECRET),
        ),
    ):
        await client.respond(
            role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": "what is the capital of France?"}],
            **call,
            # A session id is required: without one the client skips the ledger row.
            trace_ctx=SystemTraceContext.new("fre1680", session_id=str(uuid4())),
        )
    return gate, tracker


class TestDispatch:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", _NEW_KEYS)
    async def test_the_request_goes_to_scaleway_with_the_secret_on_a_guarded_client(
        self, key: str, _caught: list[_Sent]
    ) -> None:
        gate, _ = await _respond(key)

        assert len(_caught) == 1
        sent = _caught[0]
        assert sent.request.url.host == "api.scaleway.ai"
        assert sent.request.url.path.endswith("/chat/completions")
        assert sent.request.headers["authorization"] == f"Bearer {_SECRET}"
        assert sent.guarded, "the egress guard's request hook must ride on the client that sends"
        assert json.loads(sent.request.content)["model"] == _LITELLM_MODEL[key].split("/", 1)[1]
        gate.reserve.assert_awaited_once()
        gate.commit.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("key", "mode", "effort"),
        [
            (_GLM, None, "low"),
            (_GLM, "worker", "low"),
            (_DEEPSEEK, None, "high"),
            (_DEEPSEEK, "worker", "low"),
        ],
    )
    async def test_the_effort_lever_reaches_the_wire_and_no_other_lever_does(
        self, key: str, mode: str | None, effort: str, _caught: list[_Sent]
    ) -> None:
        """AC-3 offline: the dialect's one lever is sent. The levers the probe found inert are not."""
        await _respond(key, mode=mode)

        body = json.loads(_caught[0].request.content)
        assert body["reasoning_effort"] == effort
        for inert in ("chat_template_kwargs", "thinking", "extra_body", "frequency_penalty"):
            assert inert not in body, inert

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", _NEW_KEYS)
    async def test_the_ledger_records_provider_model_and_the_priced_cost(
        self, key: str, _caught: list[_Sent]
    ) -> None:
        """AC-2: the row names scaleway and carries the cost the price table gives.

        Cached input is billed at the full input price: the catalog has no cached-price field.
        """
        _, tracker = await _respond(key)

        tracker.record_api_call.assert_awaited_once()
        row = tracker.record_api_call.await_args.kwargs
        in_price, out_price = _PRICES_PER_MTOK[key]
        expected = (_PROMPT_TOKENS * in_price + _COMPLETION_TOKENS * out_price) / 1_000_000
        assert row["provider"] == "scaleway"
        assert row["model"] == _LITELLM_MODEL[key]
        assert row["cost_usd"] > 0
        assert row["cost_usd"] == pytest.approx(expected, abs=1e-6)  # the ledger rounds to 6 places
        assert row["input_tokens"] == _PROMPT_TOKENS
        assert row["output_tokens"] == _COMPLETION_TOKENS
        assert row["cache_read_input_tokens"] == 640


class TestToolsPassLitellmsParameterCheck:
    """FRE-1692 AC-1: tools reach the wire through litellm's real parameter check.

    litellm 1.106.0's scaleway table lists neither ``tools`` nor ``tool_choice`` for these
    two models, so it raises ``UnsupportedParamsError`` before it sends anything. The
    fixture replaces only the HTTP send, so that check runs here as it does live.
    """

    _TOOLS = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Return the weather for a city.",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", _NEW_KEYS)
    async def test_a_request_with_tools_reaches_the_wire(
        self, key: str, _caught: list[_Sent]
    ) -> None:
        await _respond(key, tools=self._TOOLS)

        assert len(_caught) == 1
        body = json.loads(_caught[0].request.content)
        assert body["tools"] == self._TOOLS
        assert "reasoning_effort" in body

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", _NEW_KEYS)
    async def test_a_request_with_a_forced_tool_choice_reaches_the_wire(
        self, key: str, _caught: list[_Sent]
    ) -> None:
        choice = {"type": "function", "function": {"name": "get_weather"}}
        await _respond(key, tools=self._TOOLS, tool_choice=choice)

        body = json.loads(_caught[0].request.content)
        assert body["tool_choice"] == choice

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", _NEW_KEYS)
    async def test_a_tool_request_keeps_the_guard_the_secret_and_the_ledger_row(
        self, key: str, _caught: list[_Sent]
    ) -> None:
        """AC-2 holds on the tool path, not only on the plain one."""
        _, tracker = await _respond(key, tools=self._TOOLS)

        sent = _caught[0]
        assert sent.guarded
        assert sent.request.headers["authorization"] == f"Bearer {_SECRET}"
        row = tracker.record_api_call.await_args.kwargs
        assert row["provider"] == "scaleway"
        assert row["model"] == _LITELLM_MODEL[key]
        assert row["cost_usd"] > 0
