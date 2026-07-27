"""FRE-971: LiteLLMClient.respond must never end a dispatched request on assistant.

Anthropic (via litellm) rejects such a request with: "This model does not
support assistant message prefill. The conversation must end with a user
message." A within-session compression recap with an empty tail can leave
``ctx.messages`` ending on a lone assistant turn; ``sanitise_messages`` (the
choke point both ``LiteLLMClient`` and ``LocalLLMClient`` call before every
dispatch) must close the request out on user/tool before it reaches litellm.

Unlike ``test_litellm_emit_payload.py``'s ``_call_respond`` helper, which
patches ``sanitise_messages`` out to a no-op, these tests leave it real so
the actual wiring is exercised end to end.
"""

# ruff: noqa: D103

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.llm_client.litellm_client import LiteLLMClient
from personal_agent.llm_client.types import ModelRole
from tests._helpers.trace import make_test_ctx


def _make_mock_response() -> MagicMock:
    """Build a minimal litellm ModelResponse mock with realistic usage."""
    usage = MagicMock()
    usage.prompt_tokens = 100
    usage.completion_tokens = 50
    usage.total_tokens = 150
    usage.cache_read_input_tokens = None
    usage.cache_creation_input_tokens = None
    usage.prompt_tokens_details = None

    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "sure, continuing"
    response.choices[0].message.tool_calls = None
    response.usage = usage
    response.id = "resp_abc123"
    return response


async def _respond_and_capture(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    """Run LiteLLMClient.respond() for an Anthropic provider, real sanitiser.

    Everything except sanitise_messages is mocked. Returns the ``messages``
    kwarg the (mocked) litellm.acompletion call actually received.
    """
    mock_response = _make_mock_response()

    mock_gate = MagicMock()
    mock_gate.reserve = AsyncMock(return_value="res-001")
    mock_gate.commit = AsyncMock()

    mock_tracker = AsyncMock()
    mock_tracker.connect = AsyncMock()
    mock_tracker.disconnect = AsyncMock()
    mock_tracker.record_api_call = AsyncMock()

    fake_acompletion = AsyncMock(return_value=mock_response)

    client = LiteLLMClient(
        model_id="claude-sonnet-5",
        provider="anthropic",
        max_tokens=256,
        budget_role="main_inference",
    )

    with (
        patch("litellm.acompletion", fake_acompletion),
        patch("litellm.completion_cost", return_value=0.001),
        patch("personal_agent.cost_gate.get_default_gate", return_value=mock_gate),
        patch("personal_agent.cost_gate.load_budget_config", return_value=MagicMock()),
        patch(
            "personal_agent.llm_client.cost_estimator.estimate_reservation_for_call",
            return_value=Decimal("0.01"),
        ),
        patch(
            "personal_agent.llm_client.cost_tracker.get_cost_tracker_service",
            return_value=mock_tracker,
        ),
        patch(
            "personal_agent.config.settings.get_settings",
            return_value=MagicMock(anthropic_api_key="test-key", openai_api_key=None),
        ),
    ):
        await client.respond(
            role=ModelRole.PRIMARY,
            messages=messages,
            trace_ctx=make_test_ctx("litellm_trailing_role_guard"),
        )

    _, kwargs = fake_acompletion.call_args
    return kwargs["messages"]


@pytest.mark.asyncio
async def test_anthropic_primary_trailing_assistant_recap_ends_on_user() -> None:
    """Reproduce FRE-971: a compression recap with an empty tail ends on assistant."""
    messages = [
        {"role": "user", "content": "original task"},
        {"role": "assistant", "content": "CUMULATIVE NARRATIVE recap of the tool loop"},
    ]
    dispatched = await _respond_and_capture(messages)
    assert dispatched[-1]["role"] == "user"
    assert dispatched[-1]["content"] == "Continue with the user's request."


@pytest.mark.asyncio
async def test_anthropic_primary_trailing_tool_result_is_untouched() -> None:
    """A normal re-plan state (ends on a tool result) must reach litellm unmodified."""
    messages = [
        {"role": "user", "content": "run the check"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]
    dispatched = await _respond_and_capture(messages)
    assert dispatched[-1]["role"] == "tool"
    assert len(dispatched) == len(messages)
