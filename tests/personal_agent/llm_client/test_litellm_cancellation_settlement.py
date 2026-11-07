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


# ── FRE-1683 (ADR-0159 D5.10): no cancellation point leaves the reservation active ────────────
#
# The dev-agent broker ends every call with asyncio.wait_for at 150 s, and a dev_agent reservation
# must never reach `expired`. Two cancellation points sat outside the FRE-1733 branch: the wait for
# an inference slot (after the reserve, before any request), and the final commit.


class _BlockedSlot:
    """A concurrency controller whose slot never frees."""

    def request_slot(self, **_kwargs: object) -> _BlockedSlot:
        return self

    async def __aenter__(self) -> None:
        await asyncio.Event().wait()

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _ok_response() -> MagicMock:
    response = MagicMock()
    message = MagicMock(content="hi", tool_calls=None, refusal=None)
    response.choices = [MagicMock(message=message, finish_reason="stop")]
    response.usage = MagicMock(
        prompt_tokens=3,
        completion_tokens=1,
        total_tokens=4,
        cache_read_input_tokens=None,
        cache_creation_input_tokens=None,
        prompt_tokens_details=None,
        completion_tokens_details=None,
    )
    response.id = "resp-1"
    response.system_fingerprint = None
    response.model = "claude-sonnet-4-6"
    response.model_dump = MagicMock(return_value={})
    return response


async def _respond_with(
    *, gate: MagicMock, acompletion: AsyncMock, controller: object | None = None
) -> None:
    """Run respond() with a chosen acompletion and, optionally, a chosen slot controller."""
    from personal_agent.llm_client.litellm_client import LiteLLMClient
    from personal_agent.llm_client.types import ModelRole
    from tests._helpers.trace import make_test_ctx

    tracker = AsyncMock()
    client = LiteLLMClient(
        model_id="claude-sonnet-4-6", provider="anthropic", max_tokens=256, budget_role="main_inference"
    )
    patches = [
        patch("litellm.acompletion", acompletion),
        patch("personal_agent.cost_gate.get_default_gate", return_value=gate),
        patch("personal_agent.cost_gate.load_budget_config", return_value=MagicMock()),
        patch(
            "personal_agent.llm_client.cost_estimator.estimate_reservation_for_call",
            return_value=_ESTIMATE,
        ),
        patch(
            "personal_agent.llm_client.cost_estimator.actual_cost_for_response",
            return_value=Decimal("0.002"),
        ),
        patch(
            "personal_agent.llm_client.history_sanitiser.sanitise_messages",
            side_effect=lambda msgs, trace_id: (msgs, []),
        ),
        patch(
            "personal_agent.llm_client.cost_tracker.get_cost_tracker_service", return_value=tracker
        ),
        patch(
            "personal_agent.config.settings.get_settings",
            return_value=MagicMock(anthropic_api_key="test-key", openai_api_key=None),
        ),
    ]
    if controller is not None:
        patches.append(
            patch(
                "personal_agent.llm_client.litellm_client.get_inference_concurrency_controller",
                return_value=controller,
            )
        )
    for p in patches:
        p.start()
    try:
        await client.respond(
            role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": "hello"}],
            trace_ctx=make_test_ctx("litellm_cancellation_settlement"),
        )
    finally:
        for p in reversed(patches):
            p.stop()


class TestEveryCancellationSettles:
    @pytest.mark.asyncio
    async def test_a_cancel_in_the_slot_wait_refunds(self, gate: MagicMock) -> None:
        """No request left the process, so nothing can be billed: refund at once, not at TTL."""
        acompletion = AsyncMock()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                _respond_with(gate=gate, acompletion=acompletion, controller=_BlockedSlot()),
                timeout=0.2,
            )

        acompletion.assert_not_awaited()
        gate.refund.assert_awaited_once()
        assert gate.refund.call_args.args == (_RESERVATION,)
        gate.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_cancel_during_the_final_commit_lets_the_commit_finish(
        self, gate: MagicMock
    ) -> None:
        """The provider answered and billed. A cancel during the commit must not abandon it."""
        started = asyncio.Event()
        release = asyncio.Event()
        finished: list[tuple[object, ...]] = []

        async def slow_commit(*args: object, **_kwargs: object) -> None:
            started.set()
            await release.wait()
            finished.append(args)

        gate.commit = AsyncMock(side_effect=slow_commit)
        task = asyncio.create_task(
            _respond_with(gate=gate, acompletion=AsyncMock(return_value=_ok_response()))
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

        assert finished == [(_RESERVATION, Decimal("0.002"))]
        gate.refund.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_second_cancel_during_the_cancel_branch_commit_lets_it_finish(
        self, gate: MagicMock
    ) -> None:
        """The FRE-1733 branch commits after a cancel. A second cancel must not abandon it."""
        started = asyncio.Event()
        release = asyncio.Event()
        finished: list[tuple[object, ...]] = []

        async def slow_commit(*args: object, **_kwargs: object) -> None:
            started.set()
            await release.wait()
            finished.append(args)

        async def hang(**_kwargs: object) -> None:
            await asyncio.Event().wait()

        gate.commit = AsyncMock(side_effect=slow_commit)
        task = asyncio.create_task(_respond_with(gate=gate, acompletion=AsyncMock(side_effect=hang)))
        await asyncio.sleep(0.05)
        task.cancel()  # first cancel: the call is in flight
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()  # second cancel: during the commit
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(3):
            await asyncio.sleep(0)

        assert finished == [(_RESERVATION, _ESTIMATE)]
        gate.refund.assert_not_awaited()
