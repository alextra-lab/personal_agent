"""FRE-989 finding eight: the cloud DSPy channel reserves, settles and records.

Before this, ``dspy.LM`` called providers directly — every budget control this
project has lives in ``LiteLLMClient``, so a cloud DSPy job reserved nothing and
wrote no ledger row. Captain's Log reflection ran on ``captains_log``
(``claude_sonnet``, cloud) through that channel: the same role as the FRE-987
cost incident, spending invisibly.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from personal_agent.llm_client.dspy_gate import (
    COST_SOURCE_DSPY_HISTORY,
    COST_SOURCE_PRICING_FALLBACK,
    COST_SOURCE_UNAVAILABLE,
    DspyJobCost,
    collect_dspy_cost,
    gated_dspy_job,
)

_MODEL = "anthropic/claude-sonnet-4-6"


def _lm(history: list[dict[str, object]]) -> SimpleNamespace:
    return SimpleNamespace(history=history)


def _entry(cost: float | None, prompt: int = 0, completion: int = 0) -> dict[str, object]:
    return {"cost": cost, "usage": {"prompt_tokens": prompt, "completion_tokens": completion}}


class TestCollectDspyCost:
    """Reading a finished job's cost out of DSPy's own history."""

    def test_sums_priced_calls(self) -> None:
        sink = DspyJobCost()
        collect_dspy_cost(_lm([_entry(0.01, 100, 50), _entry(0.02, 200, 25)]), sink, model=_MODEL)

        assert sink.actual_cost_usd == Decimal("0.03")
        assert sink.input_tokens == 300
        assert sink.output_tokens == 75
        assert sink.call_count == 2
        assert sink.cost_source == COST_SOURCE_DSPY_HISTORY

    def test_a_chain_of_thought_may_be_more_than_one_call(self) -> None:
        """call_count is observed, not assumed to be 1."""
        sink = DspyJobCost()
        collect_dspy_cost(_lm([_entry(0.01), _entry(0.01), _entry(0.01)]), sink, model=_MODEL)
        assert sink.call_count == 3

    def test_fully_cached_job_settles_at_zero(self) -> None:
        """A usage block reporting no work done is positively free."""
        sink = DspyJobCost()
        collect_dspy_cost(_lm([_entry(None, 0, 0)]), sink, model=_MODEL)

        assert sink.actual_cost_usd == Decimal("0")
        assert sink.cost_source == COST_SOURCE_DSPY_HISTORY
        assert sink.observed is True

    def test_missing_usage_block_is_unavailable_not_free(self) -> None:
        """ "We could not tell" must not collapse into "it cost nothing".

        An entry with no usage block at all is indistinguishable from a cache
        hit only if you squint. Treating it as free releases the full
        reservation for a call that may well have been billed.
        """
        sink = DspyJobCost()
        with patch("litellm.model_cost", {}):
            collect_dspy_cost(_lm([{"cost": None}]), sink, model=_MODEL)

        assert sink.cost_source == COST_SOURCE_UNAVAILABLE
        assert sink.observed is False

    def test_unpriced_but_used_falls_back_to_local_pricing(self) -> None:
        """Tokens with no reported cost are priced locally, not assumed free."""
        pricing = {_MODEL: {"input_cost_per_token": 0.000003, "output_cost_per_token": 0.000015}}
        sink = DspyJobCost()
        with patch("litellm.model_cost", pricing):
            collect_dspy_cost(_lm([_entry(None, 1000, 1000)]), sink, model=_MODEL)

        assert sink.cost_source == COST_SOURCE_PRICING_FALLBACK
        assert sink.actual_cost_usd == Decimal("0.018000")

    def test_local_pricing_finds_the_bare_model_id(self) -> None:
        """Litellm keys Anthropic models by the BARE id — there are no anthropic/ keys.

        A single-key lookup on the prefixed form returns nothing, which made
        this whole fallback dead code for its only caller.
        """
        bare = _MODEL.split("/", 1)[-1]
        pricing = {bare: {"input_cost_per_token": 0.000003, "output_cost_per_token": 0.000015}}
        sink = DspyJobCost()
        with patch("litellm.model_cost", pricing):
            collect_dspy_cost(_lm([_entry(None, 1000, 1000)]), sink, model=_MODEL)

        assert sink.cost_source == COST_SOURCE_PRICING_FALLBACK
        assert sink.actual_cost_usd == Decimal("0.018000")

    def test_unpriceable_job_stays_unavailable(self) -> None:
        """No cost and no pricing must not masquerade as free."""
        sink = DspyJobCost()
        with patch("litellm.model_cost", {}):
            collect_dspy_cost(_lm([_entry(None, 1000, 1000)]), sink, model=_MODEL)

        assert sink.cost_source == COST_SOURCE_UNAVAILABLE
        assert sink.observed is False

    def test_mixed_priced_and_unpriced_keeps_both_contributions(self) -> None:
        """A partially-priced job must not silently drop the unpriced call.

        Summing only the priced entries while still counting every entry's
        tokens loses real spend: the job reads as $0.03 while 5,900 tokens
        vanish from the total.
        """
        sink = DspyJobCost()
        with patch("litellm.model_cost", {}):
            collect_dspy_cost(
                _lm([_entry(0.03, 1000, 100), _entry(None, 5000, 900)]), sink, model=_MODEL
            )

        assert sink.actual_cost_usd == Decimal("0.03")  # the floor, not the answer
        assert sink.input_tokens == 6000
        assert sink.output_tokens == 1000
        assert sink.cost_source == COST_SOURCE_UNAVAILABLE, (
            "a partially-priced job must not claim to be fully observed"
        )

    def test_collect_is_idempotent(self) -> None:
        """It reads as a setter, so calling it twice must not double the job."""
        sink = DspyJobCost()
        lm = _lm([_entry(0.01, 100, 50)])
        collect_dspy_cost(lm, sink, model=_MODEL)
        collect_dspy_cost(lm, sink, model=_MODEL)

        assert sink.call_count == 1
        assert sink.input_tokens == 100
        assert sink.actual_cost_usd == Decimal("0.01")

    def test_empty_history_leaves_sink_unobserved(self) -> None:
        sink = DspyJobCost()
        collect_dspy_cost(_lm([]), sink, model=_MODEL)
        assert sink.call_count == 0
        assert sink.observed is False


@pytest.fixture
def trace_ctx() -> SimpleNamespace:
    return SimpleNamespace(trace_id=str(uuid4()), session_id=str(uuid4()))


@pytest.fixture
def gate() -> AsyncMock:
    gate = AsyncMock()
    gate.reserve = AsyncMock(return_value=uuid4())
    return gate


class TestGatedDspyJob:
    """The reserve → run → settle lifecycle around a synchronous job."""

    @pytest.mark.asyncio
    async def test_reserves_settles_and_records(
        self, gate: AsyncMock, trace_ctx: SimpleNamespace
    ) -> None:
        """AC-2/AC-3 for the DSPy channel: gated on the way in, ledgered on the way out."""
        recorder = AsyncMock()
        with (
            patch("personal_agent.cost_gate.get_default_gate", return_value=gate),
            patch("personal_agent.llm_client.cost_tracker.record_vendor_cost", recorder),
        ):
            async with gated_dspy_job(
                budget_role="captains_log",
                model=_MODEL,
                messages=[{"role": "user", "content": "reflect on this turn"}],
                max_tokens=512,
                trace_ctx=trace_ctx,
            ) as sink:
                sink.actual_cost_usd = Decimal("0.0421")
                sink.input_tokens = 1200
                sink.output_tokens = 300
                sink.call_count = 1
                sink.cost_source = COST_SOURCE_DSPY_HISTORY

        gate.reserve.assert_awaited_once()
        assert gate.reserve.await_args.kwargs["role"] == "captains_log"

        gate.commit.assert_awaited_once()
        assert gate.commit.await_args.args[1] == Decimal("0.0421")

        recorder.assert_awaited_once()
        kwargs = recorder.await_args.kwargs
        assert kwargs["purpose"] == "captains_log"
        assert kwargs["provider"] == "anthropic"
        assert kwargs["cost_usd"] == pytest.approx(0.0421)
        assert kwargs["tokens"] == 1200
        assert kwargs["output_tokens"] == 300

    @pytest.mark.asyncio
    async def test_failure_before_any_spend_refunds_and_reraises(
        self, gate: AsyncMock, trace_ctx: SimpleNamespace
    ) -> None:
        """A job that failed without spending returns its headroom immediately."""
        recorder = AsyncMock()
        with (
            patch("personal_agent.cost_gate.get_default_gate", return_value=gate),
            patch("personal_agent.llm_client.cost_tracker.record_vendor_cost", recorder),
            pytest.raises(RuntimeError, match="dspy blew up"),
        ):
            async with gated_dspy_job(
                budget_role="captains_log",
                model=_MODEL,
                messages=[{"role": "user", "content": "x"}],
                max_tokens=512,
                trace_ctx=trace_ctx,
            ):
                raise RuntimeError("dspy blew up")

        gate.refund.assert_awaited_once()
        gate.commit.assert_not_awaited()
        recorder.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failure_AFTER_spend_commits_rather_than_refunds(
        self, gate: AsyncMock, trace_ctx: SimpleNamespace
    ) -> None:
        """Real money already spent must not be refunded off the counter.

        The most likely DSPy failure is a POST-call one — the predictor returned
        and parsing its result failed, which is exactly why the manual fallback
        exists. The provider has already billed. Refunding here would erase real
        spend from the counter, and the fallback would then spend again.
        """
        recorder = AsyncMock()
        with (
            patch("personal_agent.cost_gate.get_default_gate", return_value=gate),
            patch("personal_agent.llm_client.cost_tracker.record_vendor_cost", recorder),
            pytest.raises(ValueError, match="parse failed"),
        ):
            async with gated_dspy_job(
                budget_role="captains_log",
                model=_MODEL,
                messages=[{"role": "user", "content": "x"}],
                max_tokens=512,
                trace_ctx=trace_ctx,
            ) as sink:
                sink.actual_cost_usd = Decimal("0.12")
                sink.cost_source = COST_SOURCE_DSPY_HISTORY
                raise ValueError("parse failed")

        gate.refund.assert_not_awaited()
        gate.commit.assert_awaited_once()
        assert gate.commit.await_args.args[1] == Decimal("0.12")
        recorder.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_commit_failure_never_propagates(
        self, gate: AsyncMock, trace_ctx: SimpleNamespace
    ) -> None:
        """A ledger hiccup must not become a duplicated LLM charge.

        If settlement raised, the caller's error handling would read it as "the
        reflection failed" and run its fallback — a second paid call for a turn
        the provider already billed.
        """
        gate.commit = AsyncMock(side_effect=RuntimeError("postgres blip"))
        recorder = AsyncMock()
        with (
            patch("personal_agent.cost_gate.get_default_gate", return_value=gate),
            patch("personal_agent.llm_client.cost_tracker.record_vendor_cost", recorder),
        ):
            async with gated_dspy_job(
                budget_role="captains_log",
                model=_MODEL,
                messages=[{"role": "user", "content": "x"}],
                max_tokens=512,
                trace_ctx=trace_ctx,
            ) as sink:
                sink.actual_cost_usd = Decimal("0.05")
                sink.cost_source = COST_SOURCE_DSPY_HISTORY
            # must not raise

    @pytest.mark.asyncio
    async def test_unobserved_cost_commits_at_least_the_estimate(
        self, gate: AsyncMock, trace_ctx: SimpleNamespace
    ) -> None:
        """Settling an unpriced job at zero would hand back headroom for real spend."""
        recorder = AsyncMock()
        with (
            patch("personal_agent.cost_gate.get_default_gate", return_value=gate),
            patch("personal_agent.llm_client.cost_tracker.record_vendor_cost", recorder),
        ):
            async with gated_dspy_job(
                budget_role="captains_log",
                model=_MODEL,
                messages=[{"role": "user", "content": "x"}],
                max_tokens=512,
                trace_ctx=trace_ctx,
            ) as sink:
                assert sink.observed is False  # job established nothing

        settled = gate.commit.await_args.args[1]
        assert settled > Decimal("0"), "an unpriced job must not settle at zero"

    @pytest.mark.asyncio
    async def test_partially_priced_job_settles_at_least_its_floor(
        self, gate: AsyncMock, trace_ctx: SimpleNamespace
    ) -> None:
        """max(floor, estimate) — a known-partial cost must never under-count."""
        recorder = AsyncMock()
        with (
            patch("personal_agent.cost_gate.get_default_gate", return_value=gate),
            patch("personal_agent.llm_client.cost_tracker.record_vendor_cost", recorder),
        ):
            async with gated_dspy_job(
                budget_role="captains_log",
                model=_MODEL,
                messages=[{"role": "user", "content": "x"}],
                max_tokens=512,
                trace_ctx=trace_ctx,
            ) as sink:
                sink.actual_cost_usd = Decimal("9.99")  # priced floor, but incomplete
                sink.cost_source = COST_SOURCE_UNAVAILABLE

        assert gate.commit.await_args.args[1] == Decimal("9.99")

    @pytest.mark.asyncio
    async def test_ledger_row_names_the_canonical_prefixed_model(
        self, gate: AsyncMock, trace_ctx: SimpleNamespace
    ) -> None:
        """api_costs.model must match model_call_completed (ADR-0121 T4/AC-8).

        Migration 0021 exists because a bare id splits one model's spend across
        two keys in get_cost_by_model().
        """
        recorder = AsyncMock()
        with (
            patch("personal_agent.cost_gate.get_default_gate", return_value=gate),
            patch("personal_agent.llm_client.cost_tracker.record_vendor_cost", recorder),
        ):
            async with gated_dspy_job(
                budget_role="captains_log",
                model=_MODEL,
                messages=[{"role": "user", "content": "x"}],
                max_tokens=512,
                trace_ctx=trace_ctx,
            ) as sink:
                sink.actual_cost_usd = Decimal("0.01")
                sink.cost_source = COST_SOURCE_DSPY_HISTORY

        assert recorder.await_args.kwargs["model"] == _MODEL
        assert recorder.await_args.kwargs["provider"] == "anthropic"

    @pytest.mark.asyncio
    async def test_budget_denied_propagates_and_nothing_is_recorded(
        self, trace_ctx: SimpleNamespace
    ) -> None:
        """A denial must reach the caller so a nack role can redeliver."""
        from personal_agent.cost_gate import BudgetDenied

        denied_gate = AsyncMock()
        denied_gate.reserve = AsyncMock(
            side_effect=BudgetDenied(
                role="captains_log",
                time_window="daily",
                current_spend=Decimal("5.00"),
                cap=Decimal("5.00"),
                window_resets_at=__import__("datetime").datetime.now(),
            )
        )
        recorder = AsyncMock()

        with (
            patch("personal_agent.cost_gate.get_default_gate", return_value=denied_gate),
            patch("personal_agent.llm_client.cost_tracker.record_vendor_cost", recorder),
            pytest.raises(BudgetDenied),
        ):
            async with gated_dspy_job(
                budget_role="captains_log",
                model=_MODEL,
                messages=[{"role": "user", "content": "x"}],
                max_tokens=512,
                trace_ctx=trace_ctx,
            ):
                pytest.fail("body must not run when the reservation is denied")

        recorder.assert_not_awaited()
        denied_gate.commit.assert_not_awaited()
