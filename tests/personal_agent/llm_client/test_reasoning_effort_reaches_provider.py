"""FRE-1007 AC-2/AC-3 — the declaration reaches the request, not just the config.

The discriminating criterion on the ticket is that the declared configuration is
"visible in the request actually sent to the provider, not merely present in
configuration". Asserting that a kwarg reached ``litellm.acompletion`` would not
establish that: litellm still has to transform it, and what it becomes is per
**model** — ``claude-sonnet-5`` maps effort onto adaptive thinking plus
``output_config``, while ``claude-haiku-4-5`` takes litellm's legacy path and
rewrites ``max_tokens``. So these tests carry the captured kwargs the rest of the
way through ``get_optional_params`` and assert the provider-shaped payload.

They also enter through the **digest producer's own door** —
``get_llm_client_for_key``, the trusted-key path that resolves ``config.models[key]``
and never consults a Layer-3 binding (``second_brain/session_summary.py``). That is
the door FRE-1007 says the digest would have failed, so it is the one worth proving.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests._helpers.litellm_capability import pinned_litellm_capabilities

from personal_agent.config import load_model_config
from personal_agent.config.config_guard import reasoning_wire_shape
from personal_agent.llm_client.types import ModelRole
from tests._helpers.trace import make_test_ctx


@pytest.fixture(autouse=True)
def _pin_capabilities() -> Iterator[None]:
    """See the guard test: litellm's capability map must not decide these results."""
    with pinned_litellm_capabilities():
        yield


def _make_mock_response() -> MagicMock:
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    usage.total_tokens = 15
    usage.cache_read_input_tokens = None
    usage.cache_creation_input_tokens = None
    usage.prompt_tokens_details = None

    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "ok"
    response.choices[0].message.tool_calls = None
    response.usage = usage
    response.id = "resp_fre1007"
    return response


async def _capture_litellm_kwargs(model_key: str) -> dict[str, Any]:
    """Dispatch through the digest's own key door and capture litellm's kwargs."""
    from personal_agent.llm_client.factory import get_llm_client_for_key

    captured: dict[str, Any] = {}

    async def _fake_acompletion(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return _make_mock_response()

    mock_gate = MagicMock()
    mock_gate.reserve = AsyncMock(return_value="res-fre1007")
    mock_gate.commit = AsyncMock()

    mock_tracker = AsyncMock()
    mock_tracker.connect = AsyncMock()
    mock_tracker.record_api_call = AsyncMock()

    client = get_llm_client_for_key(model_key, budget_role="captains_log")

    with (
        patch("litellm.acompletion", side_effect=_fake_acompletion),
        patch("litellm.completion_cost", return_value=0.001),
        patch("personal_agent.cost_gate.get_default_gate", return_value=mock_gate),
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
            return_value=mock_tracker,
        ),
        patch(
            "personal_agent.config.settings.get_settings",
            return_value=MagicMock(anthropic_api_key="k", openai_api_key="k"),
        ),
    ):
        await client.respond(
            role=ModelRole.SESSION_SUMMARY,
            messages=[{"role": "user", "content": "summarise"}],
            trace_ctx=make_test_ctx("fre1007_reasoning"),
        )
    return captured


class TestDeclarationReachesTheRequest:
    """AC-2 — visible in the request actually sent, through the digest's own door."""

    @pytest.mark.asyncio
    async def test_declared_effort_is_sent_without_the_caller_passing_it(self) -> None:
        """No call site passes reasoning_effort — the declaration alone must carry it."""
        captured = await _capture_litellm_kwargs("claude_sonnet")
        assert captured["reasoning_effort"] == "high"

    @pytest.mark.asyncio
    async def test_it_survives_transformation_into_the_providers_own_shape(self) -> None:
        """The check that separates a wired field from an effective one."""
        captured = await _capture_litellm_kwargs("claude_sonnet")
        shape, error = reasoning_wire_shape(
            "claude-sonnet-5", "anthropic", {}, captured["reasoning_effort"]
        )
        assert error is None
        assert shape["output_config"] == {"effort": "high"}
        assert shape["thinking"] == {"type": "adaptive"}


class TestNonAnthropicVocabulary:
    """AC-3 — a non-Anthropic binding, expressed in that provider's own vocabulary."""

    def test_openai_binding_transforms_from_the_real_catalog(self) -> None:
        """Driven from the loaded catalog, not from hardcoded transformer inputs."""
        config = load_model_config()
        definition = config.models["gpt-5.4-mini"]
        assert definition.reasoning_effort is not None, "FRE-1007: must be declared"

        shape, error = reasoning_wire_shape(
            definition.id,
            definition.provider or "",
            {"temperature": definition.temperature},
            definition.reasoning_effort,
        )
        assert error is None
        # OpenAI's vocabulary: a flat parameter, no thinking block anywhere.
        assert shape["reasoning_effort"] == "none"
        assert "thinking" not in shape
        assert "output_config" not in shape

    def test_the_same_declared_field_takes_two_different_wire_shapes(self) -> None:
        """One catalog field, two providers, two genuinely different requests."""
        config = load_model_config()
        anthropic_shape, _ = reasoning_wire_shape(
            config.models["claude_sonnet"].id, "anthropic", {}, "high"
        )
        openai_shape, _ = reasoning_wire_shape(
            config.models["gpt-5.4-mini"].id, "openai", {"temperature": 0.0}, "none"
        )
        assert set(anthropic_shape) & {"thinking", "output_config"}
        assert "reasoning_effort" not in anthropic_shape
        assert "reasoning_effort" in openai_shape
