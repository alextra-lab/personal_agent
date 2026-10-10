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
from personal_agent.observability.first_token import first_token_clock_active, request_elapsed_ms
from personal_agent.observability.route_trace import (
    assemble_route_trace,
    assemble_sub_agent_route_trace,
    get_route_trace_ledger,
)
from personal_agent.observability.topology.es_projection import project_route_trace_to_es

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


def topology_label(strategy: str | None, reason: str | None, planner_decision: str | None) -> str:
    """Map a gateway decomposition and a planner decision to the topology label.

    The one rule shared by the seam (from the context) and the first-token update (from
    the stored row), so both give the same label (ADR-0154 D6, ADR-0152 D4). A planner
    that declined or failed on a ``planner_asked`` turn returned the turn to the tool
    loop, so the turn ran as ``primary`` whatever strategy the gateway routed it under.

    Args:
        strategy: The gateway decomposition strategy value (``single`` / ``hybrid`` /
            ``decompose`` / ``delegate``), or ``None`` when there is no decision.
        reason: The gateway decomposition reason.
        planner_decision: The planner decision (``declined`` / ``expanded`` / ``failed``),
            or ``None`` when the planner did not run.

    Returns:
        One of ``primary`` / ``hybrid_fanout`` / ``decompose`` / ``delegate``.
    """
    if reason == "planner_asked" and planner_decision in ("declined", "failed"):
        return "primary"
    if strategy is None:
        return "primary"
    return _STRATEGY_TO_TOPOLOGY.get(strategy, "primary")


def _resolve_topology(ctx: ExecutionContext) -> str:
    """Resolve the turn's execution-topology label from the gateway and planner decisions.

    On seam enter no planner decision exists, so the label is the gateway's intent. On seam
    exit the same call reads ``ctx.planner_run`` and gives the label the turn really had.

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
        decomposition = gateway_output.decomposition
        strategy = decomposition.strategy
    except AttributeError:
        return "primary"
    planner_run = getattr(ctx, "planner_run", None)
    return topology_label(
        str(getattr(strategy, "value", strategy)),
        getattr(decomposition, "reason", None),
        getattr(planner_run, "decision", None),
    )


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
            latency_total_ms=request_elapsed_ms(),
        )
        await ledger.write(row)
        # FRE-548: project the same in-hand row to the dedicated agent-topology-* ES index
        # (non-blocking, best-effort — cannot raise into the turn). While a first-token
        # clock is running, the service writes this document once, after it has filled
        # first_token_ms (ADR-0154 D6): two unordered full-document writes to one id could
        # land in the wrong order and erase the value.
        if not first_token_clock_active():
            project_route_trace_to_es(row, topology=topology)
    except Exception as e:
        log.warning(
            "route_trace_write_failed",
            trace_id=getattr(ctx, "trace_id", None),
            error=str(e),
        )
        return cost
    # Per-topology segment rows (FRE-517): written in a separate, isolated pass so a bad
    # segment can never corrupt the already-fetched authoritative cost this returns.
    await _write_segment_rows(ctx, topology)
    return cost


async def _write_segment_rows(ctx: ExecutionContext, topology: str) -> None:
    """Write one ``(trace_id, task_id)`` route-trace row per sub-agent (ADR-0088, FRE-517).

    Each segment is wrapped individually so one failed write never drops the rest, and the
    whole pass is best-effort so a telemetry failure can never break the turn. ``ON CONFLICT
    (trace_id, task_id)`` makes a re-run idempotent. ``CancelledError`` still propagates.

    Args:
        ctx: The completed turn's execution context (segments read from ``sub_agent_results``).
        topology: The resolved execution-topology label, threaded onto each segment's ES
            projection (FRE-548).
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
            seg_row = assemble_sub_agent_route_trace(ctx, sub)
            await ledger.write(seg_row)
            # FRE-548: project the segment row to ES (non-blocking, best-effort).
            project_route_trace_to_es(seg_row, topology=topology)
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
    On exit (including handled exceptions and cancellation): re-resolve the label with the
    planner's decision (ADR-0154 D6), write the direct durable route-trace row and publish
    ``turn.completed`` carrying the authoritative cost.

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
        # The planner may have declined or failed since enter; the row, the segments and
        # turn.completed carry the label the turn really had.
        topology = _resolve_topology(ctx)
        ctx.topology = topology
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
