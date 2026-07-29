"""Cost gating for DSPy jobs (FRE-989 finding eight).

DSPy calls providers through its own ``dspy.LM``, not through
:class:`~personal_agent.llm_client.litellm_client.LiteLLMClient`. Every budget
control this project has — reservation, cap enforcement, the ``api_costs``
ledger row that carries the budget role — lives in that client. So a cloud-bound
DSPy job reserved nothing and recorded nothing.

That was not theoretical. Captain's Log reflection resolves ``captains_log``,
whose ADR-0121 binding is ``claude_sonnet`` (cloud), and drove it through
``dspy.ChainOfThought``. It is the same role as the FRE-987 cost incident, and
its spend through this channel was invisible to the counters, the ledger and
every report built on either.

**Why job scope rather than per-LM-call.** ``dspy.LM.forward`` is synchronous,
and the reflection caller already runs it in a worker thread via
``asyncio.to_thread``; the gate is async. Reserving inside ``forward`` would
need an ``asyncio.run_coroutine_threadsafe`` bridge back into the caller's loop
— a real failure surface bought for no extra fidelity, since a DSPy predictor
call is one logical unit of work. ADR-0120 names this shape explicitly: keep the
``reserve``/``commit``/``refund`` primitive, re-applied at *job* scope.

**Where the actual cost comes from.** DSPy records it for us. ``BaseLM``'s
``_process_lm_response`` appends ``{"cost": <litellm response_cost>, "usage":
{...}}`` to ``lm.history`` per call, and ``configure_dspy_lm`` builds a fresh
``dspy.LM`` per job, so the history is exactly this job's calls. ``cost`` is
``None`` on a cache hit — which genuinely cost nothing, so it sums as zero.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from personal_agent.telemetry.trace import TraceContext

log = structlog.get_logger(__name__)

#: How ``actual_cost_usd`` was arrived at. Recorded because the three answers
#: are operationally different and a reader must be able to tell them apart:
#: a real metered cost, a locally-priced estimate, and "we could not tell".
CostSource = str

COST_SOURCE_DSPY_HISTORY: CostSource = "dspy_history"
COST_SOURCE_PRICING_FALLBACK: CostSource = "pricing_fallback"
COST_SOURCE_UNAVAILABLE: CostSource = "unavailable"


@dataclass
class DspyJobCost:
    """Mutable sink a synchronous DSPy job fills in for its async caller.

    The job runs in a worker thread and cannot await the gate; it deposits what
    it observed here, and the caller settles the reservation against it.

    Attributes:
        actual_cost_usd: Total observed cost for the job, in USD.
        input_tokens: Summed prompt tokens across the job's LM calls.
        output_tokens: Summed completion tokens across the job's LM calls.
        call_count: Number of LM calls DSPy actually made (a ``ChainOfThought``
            is not guaranteed to be exactly one).
        cost_source: Which of :data:`COST_SOURCE_DSPY_HISTORY`,
            :data:`COST_SOURCE_PRICING_FALLBACK` or
            :data:`COST_SOURCE_UNAVAILABLE` produced ``actual_cost_usd``.
    """

    actual_cost_usd: Decimal = field(default_factory=lambda: Decimal("0"))
    input_tokens: int = 0
    output_tokens: int = 0
    call_count: int = 0
    cost_source: CostSource = COST_SOURCE_UNAVAILABLE

    @property
    def observed(self) -> bool:
        """Whether any cost figure was actually established for this job."""
        return self.cost_source != COST_SOURCE_UNAVAILABLE


def collect_dspy_cost(lm: Any, sink: DspyJobCost, *, model: str | None = None) -> None:
    """Sum a finished DSPy job's cost out of the LM's own history into ``sink``.

    Falls back to local pricing when DSPy reported usage but no cost (its
    ``disable_history`` / provider-without-``response_cost`` cases). If neither
    is available the sink is left ``unavailable`` so the caller can commit the
    original estimate rather than silently settle at zero — an unpriced call and
    a free call must not look the same on the counters.

    Args:
        lm: The ``dspy.LM`` the job ran on.
        sink: The :class:`DspyJobCost` to populate.
        model: LiteLLM model string for the pricing fallback (e.g.
            ``"anthropic/claude-sonnet-4-6"``). ``None`` skips the fallback.
    """
    history = list(getattr(lm, "history", None) or [])
    if not history:
        return

    total = Decimal("0")
    any_priced = False
    for entry in history:
        sink.call_count += 1
        usage = entry.get("usage") or {}
        sink.input_tokens += int(usage.get("prompt_tokens", 0) or 0)
        sink.output_tokens += int(usage.get("completion_tokens", 0) or 0)
        cost = entry.get("cost")
        if cost is not None:
            any_priced = True
            total += Decimal(str(cost))

    if any_priced:
        sink.actual_cost_usd = total
        sink.cost_source = COST_SOURCE_DSPY_HISTORY
        return

    # DSPy saw calls but reported no cost on any of them. A cache-only job is
    # genuinely free; an unpriced provider is not. Price it ourselves if we can.
    if model is not None and (sink.input_tokens or sink.output_tokens):
        priced = _price_locally(model, sink.input_tokens, sink.output_tokens)
        if priced is not None:
            sink.actual_cost_usd = priced
            sink.cost_source = COST_SOURCE_PRICING_FALLBACK
            return

    if sink.call_count and not sink.input_tokens and not sink.output_tokens:
        # Calls with no tokens at all is what a fully-cached job looks like.
        sink.actual_cost_usd = Decimal("0")
        sink.cost_source = COST_SOURCE_DSPY_HISTORY


def _price_locally(model: str, input_tokens: int, output_tokens: int) -> Decimal | None:
    """Price a call from litellm's static cost table, or ``None`` if unknown."""
    try:
        import litellm  # noqa: PLC0415

        pricing = getattr(litellm, "model_cost", {}).get(model)
        if not pricing:
            return None
        input_price = Decimal(str(pricing.get("input_cost_per_token", "0")))
        output_price = Decimal(str(pricing.get("output_cost_per_token", "0")))
        return (
            Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price
        ).quantize(Decimal("0.000001"))
    except Exception:  # noqa: BLE001 — pricing is best-effort, never fatal
        return None


@asynccontextmanager
async def gated_dspy_job(
    *,
    budget_role: str,
    model: str,
    messages: Sequence[dict[str, Any]],
    max_tokens: int,
    trace_ctx: TraceContext,
) -> AsyncIterator[DspyJobCost]:
    """Reserve → run → commit-and-record, around a synchronous DSPy job.

    Reuses ``estimate_reservation_for_call`` so this channel is sized by the
    same estimator as every other paid call — a second estimator would be a
    second thing to drift.

    On success: commits the observed cost (or the original estimate when the
    cost could not be established) and writes the ``api_costs`` row that makes
    the job attributable. On any exception: refunds in full and re-raises, so a
    failed job does not hold headroom until the reaper sweeps it.

    ``BudgetDenied`` from the reservation propagates to the caller, which is the
    intended behaviour — for a ``nack`` role the caller redelivers, and for the
    reflection path it degrades to the already-gated manual client.

    Args:
        budget_role: Cost-gate lane to bill (resolve it via ``budget_role_for``).
        model: LiteLLM model string, e.g. ``"anthropic/claude-sonnet-4-6"``.
        messages: Prompt messages, for reservation sizing only.
        max_tokens: Output ceiling, for reservation sizing only.
        trace_ctx: Trace context carrying the identity the ledger row needs.

    Yields:
        The :class:`DspyJobCost` sink the job body must populate.

    Raises:
        BudgetDenied: If the reservation is refused.
    """
    from personal_agent.cost_gate import (  # noqa: PLC0415 — lazy to avoid cycle
        get_default_gate,
        load_budget_config,
    )
    from personal_agent.llm_client.cost_estimator import (  # noqa: PLC0415
        estimate_reservation_for_call,
    )

    gate = get_default_gate()
    estimate = estimate_reservation_for_call(
        role=budget_role,
        model=model,
        messages=list(messages),
        max_tokens=max_tokens,
        config=load_budget_config(),
        trace_id=trace_ctx.trace_id,
    )
    reservation_id = await gate.reserve(
        role=budget_role,
        amount=estimate,
        trace_id=UUID(trace_ctx.trace_id),
        session_id=UUID(trace_ctx.session_id) if trace_ctx.session_id else None,
        task_id=None,
    )

    sink = DspyJobCost()
    try:
        yield sink
    except BaseException:
        await gate.refund(reservation_id, trace_id=trace_ctx.trace_id)
        log.warning(
            "dspy_job_refunded",
            budget_role=budget_role,
            model=model,
            reservation_id=str(reservation_id),
            estimate_usd=float(estimate),
            trace_id=trace_ctx.trace_id,
        )
        raise

    # An unpriced job commits its estimate rather than zero: settling at zero
    # would quietly hand the headroom back for spend that did occur.
    settle = sink.actual_cost_usd if sink.observed else estimate
    await gate.commit(
        reservation_id,
        settle,
        trace_id=trace_ctx.trace_id,
        session_id=trace_ctx.session_id,
    )
    log.info(
        "dspy_job_settled",
        budget_role=budget_role,
        model=model,
        reservation_id=str(reservation_id),
        estimate_usd=float(estimate),
        settled_usd=float(settle),
        cost_source=sink.cost_source,
        call_count=sink.call_count,
        trace_id=trace_ctx.trace_id,
        session_id=trace_ctx.session_id,
    )

    from personal_agent.llm_client.cost_tracker import record_vendor_cost  # noqa: PLC0415

    await record_vendor_cost(
        provider=model.split("/", 1)[0],
        model=model.split("/", 1)[-1],
        tokens=sink.input_tokens,
        cost_usd=float(settle),
        trace_id=trace_ctx.trace_id,
        session_id=trace_ctx.session_id,
        purpose=budget_role,
        latency_ms=None,
        output_tokens=sink.output_tokens,
    )
