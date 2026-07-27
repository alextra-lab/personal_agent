"""FRE-973: a cancelled LiteLLM call must refund its cost-gate reservation.

The turn-level wall-clock deadline in the orchestrator (executor.py
step_llm_call) wraps ``llm_client.respond()`` in ``asyncio.wait_for``, which
cancels the in-flight call by injecting ``asyncio.CancelledError`` wherever
it's awaiting. ``asyncio.CancelledError`` is a ``BaseException``, not an
``Exception`` — the existing ``except Exception as e:`` around
``litellm.acompletion`` would silently miss it, leaving the reservation
minted at ``gate.reserve()`` un-refunded until the cost-gate reaper sweeps it
at TTL. This test proves the dedicated ``except asyncio.CancelledError:``
branch refunds and re-raises the cancellation untouched (never wrapped into
``LLMClientError``, which would mask it as a normal failure and break
asyncio's cancellation propagation contract).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


async def _call_respond_cancelled() -> tuple[MagicMock, AsyncMock]:
    """Run LiteLLMClient.respond() where litellm.acompletion is cancelled mid-flight.

    Returns the mock gate and mock tracker so the caller can assert on
    refund call state.
    """
    from personal_agent.llm_client.litellm_client import LiteLLMClient
    from personal_agent.llm_client.types import ModelRole
    from tests._helpers.trace import make_test_ctx

    mock_gate = MagicMock()
    mock_gate.reserve = AsyncMock(return_value="res-cancel-001")
    mock_gate.refund = AsyncMock()

    mock_tracker = AsyncMock()
    mock_tracker.connect = AsyncMock()
    mock_tracker.disconnect = AsyncMock()

    client = LiteLLMClient(
        model_id="claude-sonnet-4-6",
        provider="anthropic",
        max_tokens=256,
        budget_role="main_inference",
    )

    with (
        patch("litellm.acompletion", AsyncMock(side_effect=asyncio.CancelledError())),
        patch("personal_agent.cost_gate.get_default_gate", return_value=mock_gate),
        patch(
            "personal_agent.cost_gate.load_budget_config",
            return_value=MagicMock(),
        ),
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
            return_value=MagicMock(anthropic_api_key="test-key", openai_api_key=None),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "hello"}],
                trace_ctx=make_test_ctx("litellm_cancellation_refund"),
            )

    return mock_gate, mock_tracker


class TestCancellationRefundsReservation:
    @pytest.mark.asyncio
    async def test_cancellation_refunds_reservation(self) -> None:
        mock_gate, _ = await _call_respond_cancelled()

        assert mock_gate.refund.called, "reservation must be refunded on cancellation"
        refund_kwargs = mock_gate.refund.call_args
        # First positional arg is the reservation_id returned by reserve().
        assert refund_kwargs.args[0] == "res-cancel-001"

    @pytest.mark.asyncio
    async def test_cancellation_is_not_wrapped_into_llm_client_error(self) -> None:
        """The cancellation must propagate as CancelledError, not LLMClientError —
        wrapping it would break asyncio.wait_for's cancellation contract (the
        caller's wait_for expects a clean CancelledError, not an application
        exception) and would misreport a deliberate deadline-stop as a model
        failure.
        """
        # _call_respond_cancelled already asserts pytest.raises(asyncio.CancelledError)
        # internally; a successful return here is the proof.
        await _call_respond_cancelled()
