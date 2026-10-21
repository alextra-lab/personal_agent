"""FRE-1631 — ``claude_sonnet_5_5`` as a picker-only catalog entry (AC-1, AC-2, AC-3).

Offline: ``LITELLM_LOCAL_MODEL_COST_MAP`` is in force, and every check asks
litellm 1.106.0's own request build what the declared mode becomes. Nothing is
sent. The entry binds no role, so ``claude_sonnet`` (Sonnet 5) keeps every
bound producer, including the three that force a tool.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import litellm
import pytest
import yaml
from litellm.exceptions import UnsupportedParamsError
from litellm.llms.anthropic.chat.transformation import AnthropicConfig
from litellm.utils import get_optional_params

from personal_agent.config import load_model_config
from personal_agent.config.config_guard import reasoning_wire_shape
from personal_agent.config.model_loader import resolve_role_model_key
from personal_agent.llm_client.pricing import register_model_pricing

_KEY = "claude_sonnet_5_5"
_MODEL_ID = "claude-sonnet-5-5"

# The three producers that force a tool (FRE-1621 note §6.2). Sonnet 5.5 returns
# 400 on a forced tool_choice, so none of them may resolve to the new key.
_FORCED_TOOL_ROLES = ("span_extraction", "entailment", "session_summary")

_TOOL: dict[str, Any] = {
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


def _build_request(effort: str, tool_choice: Any) -> dict[str, Any]:
    """The Anthropic request body litellm builds for one effort and one tool_choice."""
    optional = get_optional_params(
        model=_MODEL_ID,
        custom_llm_provider="anthropic",
        tools=[_TOOL],
        tool_choice=tool_choice,
        reasoning_effort=effort,
    )
    return AnthropicConfig().transform_request(
        model=_MODEL_ID,
        messages=[{"role": "user", "content": "hi"}],
        optional_params=dict(optional),
        litellm_params={},
        headers={},
    )


class TestModesOnTheWire:
    """AC-1: each declared mode sends the expected effort and adaptive thinking."""

    @pytest.mark.parametrize(
        ("mode", "effort"),
        [("default", "high"), ("worker", "low"), ("fast", "low"), ("deep", "high")],
    )
    def test_the_mode_declares_its_effort_and_sends_it(self, mode: str, effort: str) -> None:
        definition = load_model_config().models[_KEY]
        assert definition.modes[mode].effort == effort
        shape, error = reasoning_wire_shape(definition.id, "anthropic", {}, effort)
        assert error is None
        assert shape["thinking"]["type"] == "adaptive"
        assert shape["output_config"] == {"effort": effort}
        assert "budget_tokens" not in shape["thinking"]

    def test_the_entry_resolves_to_the_default_mode(self) -> None:
        definition = load_model_config().models[_KEY]
        assert definition.id == _MODEL_ID
        assert definition.provider == "anthropic"
        assert definition.default_mode == "default"

    @pytest.mark.parametrize("tool_choice", ["auto", "none"])
    @pytest.mark.parametrize("effort", ["low", "high"])
    def test_auto_and_none_tool_choice_pass_the_request_build(
        self, effort: str, tool_choice: str
    ) -> None:
        body = _build_request(effort, tool_choice)
        assert body["tool_choice"] == {"type": tool_choice}
        assert body["tools"][0]["name"] == "search_memory"
        assert body["thinking"]["type"] == "adaptive"
        assert body["output_config"] == {"effort": effort}
        assert "temperature" not in body

    @pytest.mark.parametrize(
        "tool_choice",
        ["required", {"type": "function", "function": {"name": "search_memory"}}],
    )
    def test_a_forced_tool_choice_is_refused_before_it_leaves_the_client(
        self, tool_choice: Any
    ) -> None:
        """Documents the limit: the producers send ``auto`` on this entry (FRE-1647)."""
        with pytest.raises(UnsupportedParamsError, match="does not support forced tool use"):
            _build_request("high", tool_choice)


class TestRegisteredPrices:
    """AC-2: the registered prices equal the official ones (pricing page, 2026-10-20)."""

    def test_the_catalog_prices(self) -> None:
        definition = load_model_config().models[_KEY]
        assert definition.input_cost_per_token == pytest.approx(2e-06)
        assert definition.output_cost_per_token == pytest.approx(1e-05)

    def test_litellm_bills_two_and_ten_per_mtok_after_registration(self) -> None:
        register_model_pricing(load_model_config(Path("config/models.yaml")))
        input_cost, output_cost = litellm.cost_per_token(
            model="anthropic/claude-sonnet-5-5",
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
        )
        assert input_cost == pytest.approx(2.0)
        assert output_cost == pytest.approx(10.0)

    def test_cache_reads_bill_at_ten_cents_per_mtok(self) -> None:
        """The catalog has no cache-read field; litellm's own map carries $0.10."""
        register_model_pricing(load_model_config(Path("config/models.yaml")))
        info = litellm.model_cost[_MODEL_ID]
        assert info["cache_read_input_token_cost"] == pytest.approx(1e-07)


class TestNoBinding:
    """AC-3: the entry is pickable, bound to no role."""

    def test_no_declared_role_resolves_to_the_new_key(self) -> None:
        roles = yaml.safe_load(Path("config/model_roles.yaml").read_text())["bindings"]
        resolved = {role: resolve_role_model_key(role) for role in roles}
        assert _KEY not in resolved.values(), resolved

    @pytest.mark.parametrize("role", _FORCED_TOOL_ROLES)
    def test_each_forced_tool_producer_stays_on_sonnet_5(self, role: str) -> None:
        assert resolve_role_model_key(role) == "claude_sonnet"

    def test_sonnet_5_keeps_its_own_entry(self) -> None:
        assert load_model_config().models["claude_sonnet"].id == "claude-sonnet-5"
