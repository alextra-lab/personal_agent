"""Background task that sweeps stale ``budget_reservations`` (ADR-0065 D1).

Runs every 30 seconds, calling :meth:`CostGate.reap_stale`. Catches stuck
reservations from callers that crashed between ``reserve()`` and
``commit()``/``refund()`` so headroom doesn't leak.

Started by the FastAPI lifespan hook with ``asyncio.create_task(run_reaper(...))``
and cancelled at shutdown.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import structlog

from personal_agent.cost_gate.gate import CostGate

log = structlog.get_logger(__name__)

DEFAULT_REAPER_INTERVAL_SECONDS = 30.0


async def run_reaper(
    gate: CostGate,
    *,
    interval_seconds: float = DEFAULT_REAPER_INTERVAL_SECONDS,
) -> None:
    """Sweep stale reservations on a fixed cadence until cancelled.

    Args:
        gate: Connected ``CostGate`` instance.
        interval_seconds: Seconds between sweeps. Defaults to 30s, which
            paired with the 90s reservation TTL bounds the worst-case stale
            headroom at ~120s.

    Cancellation:
        Cancel the task at shutdown. The function suppresses
        ``asyncio.CancelledError`` and exits cleanly.
    """
    log.info("cost_gate_reaper_started", interval_seconds=interval_seconds)
    try:
        while True:
            try:
                await gate.reap_stale()
            except Exception as exc:  # noqa: BLE001 — log + continue is the right thing here
                log.error("cost_gate_reaper_sweep_failed", error=str(exc), exc_info=True)
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        log.info("cost_gate_reaper_stopped")
        # Re-raise so the lifespan hook sees clean cancellation
        with suppress(asyncio.CancelledError):
            raise
