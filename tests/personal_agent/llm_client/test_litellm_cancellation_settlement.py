"""FRE-973, FRE-1733: how a LiteLLM call that does not finish settles its reservation.

The turn-level wall-clock deadline in the orchestrator (executor.py step_llm_call) wraps
``llm_client.respond()`` in ``asyncio.wait_for``, which cancels the in-flight call by injecting
``asyncio.CancelledError``. That is a ``BaseException``, so the ``except Exception`` around
``litellm.acompletion`` does not catch it. Without its own branch, the reservation stays until the
reaper sweeps it at TTL.

A cancelled request may have reached the provider, which can still bill it, so the branch commits
the estimate. This can overcount a call that the provider did not bill. A call that fails with an
error refunds. The cancellation always propagates untouched (never wrapped into ``LLMClientError``,
which would mask it as a normal failure and break asyncio's cancellation contract).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.llm_client.types import LLMClientError

_ESTIMATE = Decimal("0.01")
_RESERVATION = "res-cancel-001"


async def _call_respond(failure: BaseException, *, gate: MagicMock) -> None:
    """Run LiteLLMClient.respond() where litellm.acompletion raises ``failure``."""
    from personal_agent.llm_client.litellm_client import LiteLLMClient
    from personal_agent.llm_client.types import ModelRole
    from tests._helpers.trace import make_test_ctx

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
        patch("litellm.acompletion", AsyncMock(side_effect=failure)),
        patch("personal_agent.cost_gate.get_default_gate", return_value=gate),
        patch(
            "personal_agent.cost_gate.load_budget_config",
            return_value=MagicMock(),
        ),
        patch(
            "personal_agent.llm_client.cost_estimator.estimate_reservation_for_call",
            return_value=_ESTIMATE,
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
        await client.respond(
            role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": "hello"}],
            trace_ctx=make_test_ctx("litellm_cancellation_settlement"),
        )


@pytest.fixture
def gate() -> MagicMock:
    mock_gate = MagicMock()
    mock_gate.reserve = AsyncMock(return_value=_RESERVATION)
    mock_gate.refund = AsyncMock()
    mock_gate.commit = AsyncMock()
    return mock_gate


class TestCancelledCall:
    @pytest.mark.asyncio
    async def test_a_cancelled_call_commits_its_estimate(self, gate: MagicMock) -> None:
        with pytest.raises(asyncio.CancelledError):
            await _call_respond(asyncio.CancelledError(), gate=gate)

        gate.commit.assert_awaited_once()
        assert gate.commit.call_args.args == (_RESERVATION, _ESTIMATE)
        gate.refund.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_commit_does_not_mask_the_cancellation(self, gate: MagicMock) -> None:
        """The cancellation must propagate as CancelledError, not LLMClientError.

        Wrapping it would break asyncio.wait_for's contract and misreport a deliberate deadline-stop
        as a model failure. ``pytest.raises`` is the proof, here with a commit that fails too.
        """
        gate.commit.side_effect = RuntimeError("database down")

        with pytest.raises(asyncio.CancelledError):
            await _call_respond(asyncio.CancelledError(), gate=gate)


class TestFailedCall:
    @pytest.mark.asyncio
    async def test_a_call_that_fails_with_an_error_keeps_its_refund(self, gate: MagicMock) -> None:
        with pytest.raises(LLMClientError):
            await _call_respond(RuntimeError("provider refused"), gate=gate)

        gate.refund.assert_awaited_once()
        assert gate.refund.call_args.args == (_RESERVATION,)
        gate.commit.assert_not_awaited()
