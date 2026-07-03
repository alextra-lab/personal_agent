"""FRE-766 — reasoning_effort passthrough + reasoning-token capture in LiteLLMClient.

The extraction model×reasoning benchmark needs the discrete GPT-5 effort ladder
(low/medium/high/xhigh) to actually reach the provider. Before FRE-766 the
``reasoning_effort`` param on ``LiteLLMClient.respond()`` was declared and documented
"passed through" but never added to the ``litellm.acompletion`` kwargs — silently
dropped. These tests pin the wiring (forwarded when set, absent when ``None``) and the
defensive reasoning-token capture (object and dict shapes, ``None`` when absent).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.llm_client.litellm_client import LiteLLMClient
from personal_agent.llm_client.types import ModelRole
from personal_agent.telemetry.trace import SystemTraceContext


def _response(*, reasoning_tokens: Any = "UNSET", dict_shape: bool = False) -> MagicMock:
    """Build a minimal litellm ModelResponse mock.

    Args:
        reasoning_tokens: When not the sentinel ``"UNSET"``, attach a
            ``completion_tokens_details`` carrying this ``reasoning_tokens`` value
            (use an object with the attribute; dict-shape covered separately).
    """
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 20
    usage.total_tokens = 30
    usage.cache_read_input_tokens = None
    usage.cache_creation_input_tokens = None
    usage.prompt_tokens_details = None
    if reasoning_tokens == "UNSET":
        usage.completion_tokens_details = None
    elif dict_shape:
        usage.completion_tokens_details = {"reasoning_tokens": reasoning_tokens}
    else:
        details = MagicMock()
        details.reasoning_tokens = reasoning_tokens
        usage.completion_tokens_details = details
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "{}"
    response.choices[0].message.tool_calls = None
    response.usage = usage
    response.id = "resp_reasoning"
    return response


def _patches(acompletion_mock: AsyncMock) -> list[Any]:
    """The shared patch stack that isolates respond() from the cost substrate."""
    mock_gate = MagicMock()
    mock_gate.reserve = AsyncMock(return_value="res-1")
    mock_gate.commit = AsyncMock()
    return [
        patch("litellm.acompletion", acompletion_mock),
        patch("litellm.completion_cost", return_value=0.0),
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
            "personal_agent.llm_client.cost_tracker.CostTrackerService",
            return_value=AsyncMock(),
        ),
        patch(
            "personal_agent.config.settings.get_settings",
            return_value=MagicMock(anthropic_api_key=None, openai_api_key="k"),
        ),
    ]


async def _run(*, reasoning_effort: str | None, response: MagicMock) -> tuple[dict[str, Any], Any]:
    """Call respond() with the given effort; return (acompletion kwargs, response dict)."""
    acompletion = AsyncMock(return_value=response)
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in _patches(acompletion):
            stack.enter_context(p)
        client = LiteLLMClient(
            model_id="gpt-5.4-mini",
            provider="openai",
            max_tokens=64,
            budget_role="entity_extraction",
        )
        result = await client.respond(
            role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": "hi"}],
            reasoning_effort=reasoning_effort,
            trace_ctx=SystemTraceContext.new(
                "entity_extraction", session_id="00000000-0000-0000-0000-000000000001"
            ),
        )
    return acompletion.call_args.kwargs, result


@pytest.mark.asyncio
class TestReasoningEffortWiring:
    """The effort value must actually reach litellm.acompletion (was dropped)."""

    async def test_effort_forwarded_when_set(self) -> None:
        """respond(reasoning_effort='high') puts reasoning_effort='high' on the call."""
        kwargs, _ = await _run(reasoning_effort="high", response=_response())
        assert kwargs.get("reasoning_effort") == "high"

    async def test_effort_absent_when_none(self) -> None:
        """respond() with no effort must NOT add reasoning_effort (prod default path)."""
        kwargs, _ = await _run(reasoning_effort=None, response=_response())
        assert "reasoning_effort" not in kwargs


class TestModelDefinitionReasoningEffort:
    """FRE-766: ModelDefinition carries a validated reasoning_effort (config-owned)."""

    @staticmethod
    def _base(**extra: Any) -> Any:
        from personal_agent.llm_client.models import ModelDefinition

        return ModelDefinition(
            id="gpt-5.4", context_length=128000, max_concurrency=10, default_timeout=60, **extra
        )

    def test_defaults_none(self) -> None:
        """Absent reasoning_effort defaults None (backend default; prod mini path)."""
        assert self._base().reasoning_effort is None

    def test_accepts_the_ladder(self) -> None:
        """Each GPT-5 effort rung validates."""
        for effort in ("low", "medium", "high", "xhigh"):
            assert self._base(reasoning_effort=effort).reasoning_effort == effort

    def test_rejects_off_ladder(self) -> None:
        """An off-ladder value is rejected at construction (not silently kept)."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            self._base(reasoning_effort="ultra")


@pytest.mark.asyncio
class TestReasoningTokenCapture:
    """Defensive reasoning-token capture — object shape, and None when absent."""

    async def test_reasoning_tokens_captured_from_object(self) -> None:
        """completion_tokens_details.reasoning_tokens surfaces in usage (object shape)."""
        _, result = await _run(reasoning_effort="high", response=_response(reasoning_tokens=128))
        assert result["usage"].get("reasoning_tokens") == 128

    async def test_reasoning_tokens_captured_from_dict(self) -> None:
        """completion_tokens_details as a dict is parsed too (defensive; codex coverage)."""
        _, result = await _run(
            reasoning_effort="high", response=_response(reasoning_tokens=99, dict_shape=True)
        )
        assert result["usage"].get("reasoning_tokens") == 99

    async def test_reasoning_tokens_absent_is_not_error(self) -> None:
        """No completion_tokens_details → no reasoning_tokens key, no crash."""
        _, result = await _run(reasoning_effort=None, response=_response())
        assert "reasoning_tokens" not in result["usage"]
