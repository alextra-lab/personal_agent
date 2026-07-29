"""FRE-989 finding seven: the gateway streaming turn writes its ledger row.

``gateway/chat_api.py`` talks to the Anthropic SDK directly rather than through
``LiteLLMClient``, so it reserved and committed against the cost gate but wrote
no ``api_costs`` row. A paid turn moved the ``main_inference`` counter while
staying invisible in the store the audit names authoritative — which is exactly
the shape that made the FRE-987 incident unattributable.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from personal_agent.gateway.chat_api import (
    _commit_reservation_safe,
    _CLOUD_MODEL,
    _record_gateway_cost_safe,
    gateway_stream_usage,
)

# Keyed off the real constant: a model rename must not silently turn these
# pricing assertions into vacuous zero-vs-zero comparisons.
_PRICED_KEY = f"anthropic/{_CLOUD_MODEL}"


def _final_message(input_tokens: int, output_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    )


class TestGatewayStreamUsage:
    """Pricing a finished stream out of its usage block."""

    def test_prices_from_litellm_model_cost(self) -> None:
        with patch(
            "litellm.model_cost",
            {_PRICED_KEY: {}},
        ):
            cost, inp, out = gateway_stream_usage(_final_message(1000, 500))
        assert inp == 1000
        assert out == 500
        assert cost == Decimal("0.000000")  # no pricing entry → zero, not a crash

    def test_missing_usage_yields_zeros(self) -> None:
        assert gateway_stream_usage(None) == (Decimal("0"), 0, 0)
        assert gateway_stream_usage(SimpleNamespace(usage=None)) == (Decimal("0"), 0, 0)

    def test_real_pricing_is_applied(self) -> None:
        pricing = {
            _PRICED_KEY: {
                "input_cost_per_token": 0.000003,
                "output_cost_per_token": 0.000015,
            }
        }
        with patch("litellm.model_cost", pricing):
            cost, _inp, _out = gateway_stream_usage(_final_message(1000, 1000))
        # 1000 * 3e-6 + 1000 * 1.5e-5 = 0.018
        assert cost == Decimal("0.018000")

    def test_prices_from_the_BARE_model_id(self) -> None:
        """The real-world case: litellm carries no anthropic/-prefixed keys at all.

        Looking up only ``anthropic/<model>`` priced every gateway turn at
        exactly $0 — so the main_inference counter never moved for streamed
        chat and its cap was unreachable by this path (FRE-989).
        """
        bare_pricing = {
            _CLOUD_MODEL: {
                "input_cost_per_token": 0.000003,
                "output_cost_per_token": 0.000015,
            }
        }
        with patch("litellm.model_cost", bare_pricing):
            cost, _inp, _out = gateway_stream_usage(_final_message(1000, 1000))

        assert cost == Decimal("0.018000"), (
            "pricing must resolve via the bare id, not only the prefixed form"
        )

    def test_the_shipped_model_is_actually_priceable(self) -> None:
        """Guard against the model id drifting out of litellm's real table.

        No mock: if this fails, the deployed gateway is silently billing $0.
        """
        cost, _inp, _out = gateway_stream_usage(_final_message(1000, 1000))
        assert cost > Decimal("0"), (
            f"litellm has no pricing for {_CLOUD_MODEL!r} under either spelling; "
            "gateway turns would settle at $0 and the cap could never deny"
        )


class TestRecordGatewayCost:
    """The ledger write itself."""

    @pytest.mark.asyncio
    async def test_writes_an_api_costs_row_tagged_main_inference(self) -> None:
        """AC-3: the streamed turn is attributable by role in the ledger."""
        tracker = AsyncMock()
        session_uuid = uuid4()
        trace_id = str(uuid4())

        with (
            patch(
                "personal_agent.llm_client.cost_tracker.get_cost_tracker_service",
                return_value=tracker,
            ),
            patch("litellm.model_cost", {}),
        ):
            await _record_gateway_cost_safe(
                trace_id=trace_id,
                session_uuid=session_uuid,
                latency_ms=1234,
                final_message=_final_message(100, 50),
            )

        tracker.record_api_call.assert_awaited_once()
        kwargs = tracker.record_api_call.await_args.kwargs
        assert kwargs["purpose"] == "main_inference"
        assert kwargs["provider"] == "anthropic"
        assert kwargs["input_tokens"] == 100
        assert kwargs["output_tokens"] == 50
        assert kwargs["session_id"] == session_uuid
        assert str(kwargs["trace_id"]) == trace_id
        assert kwargs["latency_ms"] == 1234

    @pytest.mark.asyncio
    async def test_records_even_when_usage_is_absent(self) -> None:
        """A priceless call must still appear in the ledger, at zero — never be absent.

        An absent row and a zero row mean different things; only one of them is
        true, and silently omitting the row makes the ledger lie by omission.
        """
        tracker = AsyncMock()
        with (
            patch(
                "personal_agent.llm_client.cost_tracker.get_cost_tracker_service",
                return_value=tracker,
            ),
            patch("litellm.model_cost", {}),
        ):
            await _record_gateway_cost_safe(
                trace_id=str(uuid4()),
                session_uuid=uuid4(),
                latency_ms=10,
                final_message=None,
            )

        tracker.record_api_call.assert_awaited_once()
        assert tracker.record_api_call.await_args.kwargs["cost_usd"] == 0.0

    @pytest.mark.asyncio
    async def test_commit_falls_back_to_the_estimate_when_unpriceable(self) -> None:
        """Tokens burned but unpriceable must settle the estimate, not $0.

        Settling zero hands the headroom straight back for spend that did occur
        — the failure this ticket exists to remove.
        """
        gate = AsyncMock()
        with (
            patch("personal_agent.cost_gate.get_default_gate_or_none", return_value=gate),
            patch("litellm.model_cost", {}),
        ):
            await _commit_reservation_safe(
                reservation_id=uuid4(),
                trace_id=str(uuid4()),
                final_message=_final_message(1000, 500),
                estimate=Decimal("0.42"),
            )

        assert gate.commit.await_args.args[1] == Decimal("0.42")

    @pytest.mark.asyncio
    async def test_commit_without_an_estimate_is_safe(self) -> None:
        """The no-gate startup path passes estimate=None; that must not blow up.

        Guards the shape of the bug where ``reservation_amount`` was bound only
        inside the gate branch while the task launch read it unconditionally —
        an UnboundLocalError on every /chat in standalone gateway mode.
        """
        gate = AsyncMock()
        with (
            patch("personal_agent.cost_gate.get_default_gate_or_none", return_value=gate),
            patch("litellm.model_cost", {}),
        ):
            await _commit_reservation_safe(
                reservation_id=uuid4(),
                trace_id=str(uuid4()),
                final_message=_final_message(1000, 500),
                estimate=None,
            )

        assert gate.commit.await_args.args[1] == Decimal("0")

    @pytest.mark.asyncio
    async def test_a_ledger_failure_never_breaks_the_stream(self) -> None:
        """Best-effort contract: recording is not allowed to kill the user's turn."""
        tracker = AsyncMock()
        tracker.record_api_call.side_effect = RuntimeError("postgres is down")

        with (
            patch(
                "personal_agent.llm_client.cost_tracker.get_cost_tracker_service",
                return_value=tracker,
            ),
            patch("litellm.model_cost", {}),
        ):
            await _record_gateway_cost_safe(
                trace_id=str(uuid4()),
                session_uuid=uuid4(),
                latency_ms=10,
                final_message=_final_message(1, 1),
            )  # must not raise
