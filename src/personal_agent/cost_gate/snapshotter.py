"""Background task that snapshots ``budget_counters`` to ES (FRE-547 / ADR-0065).

Runs every 60 seconds, calling :meth:`CostGate.snapshot_counters`, so the
Cost & Budget dashboard can render cap utilization (``running_total`` vs
``cap_usd``) — counter state that lives only in Postgres and is otherwise
invisible to Kibana.

Started by the FastAPI lifespan hook with
``asyncio.create_task(run_counter_snapshotter(...))`` and cancelled at shutdown,
mirroring the cost-gate reaper.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import structlog

from personal_agent.cost_gate.gate import CostGate

log = structlog.get_logger(__name__)

DEFAULT_SNAPSHOT_INTERVAL_SECONDS = 60.0


async def run_counter_snapshotter(
    gate: CostGate,
    *,
    interval_seconds: float = DEFAULT_SNAPSHOT_INTERVAL_SECONDS,
) -> None:
    """Snapshot budget counters to ES on a fixed cadence until cancelled.

    Sleeps *before* the first emit on purpose: the snapshotter task is spawned
    in the FastAPI lifespan before the Elasticsearch log handler is attached,
    so emitting immediately would route the first batch only to file/console —
    never to ES. The interval sleep guarantees the handler is wired before the
    first emit.

    Args:
        gate: Connected ``CostGate`` instance.
        interval_seconds: Seconds between snapshots. Defaults to 60s.

    Cancellation:
        Cancel the task at shutdown. The function suppresses
        ``asyncio.CancelledError`` and exits cleanly.
    """
    log.info("cost_gate_snapshotter_started", interval_seconds=interval_seconds)
    try:
        while True:
            await asyncio.sleep(interval_seconds)  # sleep-first: ES handler attached by now
            try:
                await gate.snapshot_counters()
            except Exception as exc:  # noqa: BLE001 — log + continue is the right thing here
                log.error("cost_gate_snapshot_failed", error=str(exc), exc_info=True)
    except asyncio.CancelledError:
        log.info("cost_gate_snapshotter_stopped")
        # Re-raise so the lifespan hook sees clean cancellation
        with suppress(asyncio.CancelledError):
            raise
