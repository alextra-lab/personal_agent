"""The ADR-0088 execution-topology emission seam (FRE-513).

``observe_topology`` is the mandatory context manager every topology runs inside; on enter
it publishes ``turn.topology_entered`` and on exit it writes the durable route-trace row
**directly** (bus-independent — ADR-0088 D8) and publishes ``turn.completed``.
``report_degradation`` is the single sanctioned "did less" signal (D5).

Two sinks, deliberately separated (D6): the durable route-trace ledger write survives a
bus outage; the ``stream:turn.observed`` publish is best-effort and only drives the live
projector. Every sink call is wrapped so a telemetry failure can never break the turn;
``asyncio.CancelledError`` is **not** swallowed, so turn cancellation still propagates
after the durable row is attempted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

from personal_agent.config import settings
from personal_agent.events import get_event_bus
from personal_agent.events.models import (
    STREAM_TURN_OBSERVED,
    TopologyEnteredEvent,
    TurnCompletedEvent,
    TurnDegradedEvent,
)
from personal_agent.observability.route_trace import (
    assemble_route_trace,
    assemble_sub_agent_route_trace,
    get_route_trace_ledger,
)

if TYPE_CHECKING:
    from personal_agent.orchestrator.types import ExecutionContext

log = structlog.get_logger(__name__)

# Identity-required events the seam publishes to stream:turn.observed. The union name
# ends in ``Event`` so the ADR-0074 identity lint (scripts/check_identity_threaded.py)
# can see, through the _publish helper, that every payload is a typed Event whose
# trace_id/session_id are mandatory at the type level.
TurnObservedEvent = TopologyEnteredEvent | TurnDegradedEvent | TurnCompletedEvent

# ADR-0088 D7 runtime guard: the topology active in the current async context. Set by
# observe_topology on enter and reset on exit; ``contextvars`` propagates it into every
# awaited coroutine (including sub-agents on the same call stack). A model call whose
# context shows ``None`` ran outside the seam — a checkable contract violation.
_active_topology: ContextVar[str | None] = ContextVar("active_topology", default=None)


def current_topology() -> str | None:
    """Return the execution topology active in the current async context, or ``None``.

    ``None`` means no ``observe_topology`` is active on this call stack — model work seen
    with ``None`` is an out-of-seam violation (ADR-0088 D7).
    """
    return _active_topology.get()


# Map the gateway decomposition strategy to the ADR-0088 D1 topology vocabulary.
_STRATEGY_TO_TOPOLOGY: dict[str, str] = {
    "single": "primary",
    "hybrid": "hybrid_fanout",
    "decompose": "decompose",
    "delegate": "delegate",
}


def _resolve_topology(ctx: ExecutionContext) -> str:
    """Resolve the turn's execution-topology label from the gateway decision.

    Args:
        ctx: The turn's execution context (``gateway_output`` may be absent).

    Returns:
        One of ``primary`` / ``hybrid_fanout`` / ``decompose`` / ``delegate``; defaults to
        ``primary`` when no gateway decomposition decision is available.
    """
    gateway_output = getattr(ctx, "gateway_output", None)
    if gateway_output is None:
        return "primary"
    try:
        strategy = gateway_output.decomposition.strategy
    except AttributeError:
        return "primary"
    value = getattr(strategy, "value", strategy)
    return _STRATEGY_TO_TOPOLOGY.get(str(value), "primary")


async def _publish(event: TurnObservedEvent, *, trace_id: str | None) -> None:
    """Publish a turn-observed event best-effort (the live sink — ADR-0088 D6 sink 2).

    Args:
        event: The event to publish to ``stream:turn.observed``.
        trace_id: Trace identifier for failure telemetry correlation.
    """
    try:
        await get_event_bus().publish(
            STREAM_TURN_OBSERVED, event, maxlen=settings.turn_observed_stream_maxlen
        )
    except Exception:
        log.debug("turn_observed_publish_failed", trace_id=trace_id, event_type=event.event_type)


async def _write_durable_row(ctx: ExecutionContext, topology: str) -> float:
    """Write the direct durable route-trace row (ADR-0088 D6 sink 1) and return its cost.

    Best-effort: any failure is logged and swallowed so a ledger problem never breaks the
    turn. ``CancelledError`` is not caught here, so it still propagates.

    Args:
        ctx: The completed turn's execution context.
        topology: The resolved execution-topology label.

    Returns:
        ``SUM(api_costs WHERE trace_id)`` for the turn (``0.0`` on any failure).
    """
    cost = 0.0
    try:
        ledger = get_route_trace_ledger()
        trace_uuid = UUID(str(ctx.trace_id))
        cost, in_tok, out_tok = await ledger.fetch_authoritative_cost(trace_uuid)
        row = assemble_route_trace(
            ctx,
            authoritative_cost_usd=cost,
            input_tokens=in_tok,
            output_tokens=out_tok,
            store_preview=settings.route_trace_store_preview,
            preview_chars=settings.route_trace_preview_chars,
            topology=topology,
        )
        await ledger.write(row)
    except Exception as e:
        log.warning(
            "route_trace_write_failed",
            trace_id=getattr(ctx, "trace_id", None),
            error=str(e),
        )
        return cost
    # Per-topology segment rows (FRE-517): written in a separate, isolated pass so a bad
    # segment can never corrupt the already-fetched authoritative cost this returns.
    await _write_segment_rows(ctx)
    return cost


async def _write_segment_rows(ctx: ExecutionContext) -> None:
    """Write one ``(trace_id, task_id)`` route-trace row per sub-agent (ADR-0088, FRE-517).

    Each segment is wrapped individually so one failed write never drops the rest, and the
    whole pass is best-effort so a telemetry failure can never break the turn. ``ON CONFLICT
    (trace_id, task_id)`` makes a re-run idempotent. ``CancelledError`` still propagates.

    Args:
        ctx: The completed turn's execution context (segments read from ``sub_agent_results``).
    """
    subs = getattr(ctx, "sub_agent_results", None) or []
    if not subs:
        return
    ledger = get_route_trace_ledger()
    for sub in subs:
        # A segment row is keyed by (trace_id, task_id); a sub without a task_id would
        # collide with the turn-level NULL key, so skip it (real SubAgentResults always
        # carry a UUID — this only guards malformed/partial stand-ins).
        if getattr(sub, "task_id", None) is None:
            continue
        try:
            await ledger.write(assemble_sub_agent_route_trace(ctx, sub))
        except Exception as e:
            log.warning(
                "route_trace_segment_write_failed",
                trace_id=getattr(ctx, "trace_id", None),
                task_id=str(getattr(sub, "task_id", "")),
                error=str(e),
            )


@asynccontextmanager
async def observe_topology(ctx: ExecutionContext) -> AsyncIterator[None]:
    """Wrap a turn's execution topology in the ADR-0088 emission seam (D2).

    On enter: resolve + stamp ``ctx.topology`` and publish ``turn.topology_entered``.
    On exit (including handled exceptions and cancellation): write the direct durable
    route-trace row and publish ``turn.completed`` carrying the authoritative cost.

    Args:
        ctx: The turn's execution context.

    Yields:
        ``None`` — the wrapped topology runs inside the ``async with`` body.
    """
    topology = _resolve_topology(ctx)
    ctx.topology = topology
    trace_id = str(getattr(ctx, "trace_id", "")) or None
    session_id = str(getattr(ctx, "session_id", "")) or None

    token = _active_topology.set(topology)
    if trace_id and session_id:
        await _publish(
            TopologyEnteredEvent(trace_id=trace_id, session_id=session_id, topology=topology),
            trace_id=trace_id,
        )
    try:
        yield
    finally:
        _active_topology.reset(token)
        cost = await _write_durable_row(ctx, topology)
        if trace_id and session_id:
            await _publish(
                TurnCompletedEvent(
                    trace_id=trace_id,
                    session_id=session_id,
                    topology=topology,
                    cost_authoritative_usd=cost,
                ),
                trace_id=trace_id,
            )


async def report_degradation(
    *,
    trace_id: str,
    session_id: str,
    where: str,
    reason: str,
    severity: str = "warning",
    expected: str | None = None,
    actual: str | None = None,
) -> None:
    """Emit the single sanctioned "did less" signal (ADR-0088 D5).

    Publishes ``turn.degraded`` to ``stream:turn.observed`` so the live projector raises a
    visible ``degraded`` state with reason onto ``turn_status``. Best-effort on the live
    sink. Every topology that does less than intended (planner schema-fail → tool-less
    fallback, artifact strip-and-deliver, budget-trimmed memory, discarded sub-agent
    result) routes through this one call.

    Args:
        trace_id: Trace identifier (ADR-0074 identity).
        session_id: Session identifier (ADR-0074 identity).
        where: Topology / call-site that degraded.
        reason: Human-readable degradation reason.
        severity: ``info`` | ``warning`` | ``critical`` (defaults to ``warning``).
        expected: What the topology intended, when expressible.
        actual: What it did instead, when expressible.
    """
    normalized = severity if severity in ("info", "warning", "critical") else "warning"
    await _publish(
        TurnDegradedEvent(
            trace_id=trace_id,
            session_id=session_id,
            where=where,
            reason=reason,
            severity=normalized,  # type: ignore[arg-type]
            expected=expected,
            actual=actual,
        ),
        trace_id=trace_id,
    )
    log.info(
        "turn_degraded",
        trace_id=trace_id,
        session_id=session_id,
        where=where,
        reason=reason,
        severity=normalized,
    )
