"""Scheduled session-store retention sweep (FRE-860 / ADR-0098 D4/D6).

Soft-prunes sessions inactive past ``settings.session_retention_days``: clears
``messages`` and stamps ``purged_at`` (see ``SessionRepository.prune_expired``).
Mirrors ``cost_gate/reaper.py``'s ``run_reaper`` shape — a standalone,
independently-testable sweep loop, started by the FastAPI lifespan hook with
``asyncio.create_task(run_session_retention_loop(...))`` and cancelled at
shutdown.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.config.settings import get_settings
from personal_agent.service.repositories.session_repository import SessionRepository
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

DbFactory = Callable[[], AsyncSession]


async def prune_expired_sessions(db_factory: DbFactory, retention_days: int | None = None) -> int:
    """Soft-prune sessions inactive past the retention window.

    Args:
        db_factory: Callable that returns an async context manager yielding
            an ``AsyncSession`` (typically ``AsyncSessionLocal``).
        retention_days: Override for the retention window; defaults to
            ``settings.session_retention_days``.

    Returns:
        Number of session rows pruned by this sweep.
    """
    days = retention_days if retention_days is not None else get_settings().session_retention_days
    async with db_factory() as db:
        repo = SessionRepository(db)
        count = await repo.prune_expired(days)
    if count:
        log.info("session_retention_pruned", count=count, retention_days=days)
    return count


async def run_session_retention_loop(
    db_factory: DbFactory,
    *,
    interval_seconds: float,
    retention_days: int | None = None,
) -> None:
    """Sweep expired sessions on a fixed cadence until cancelled.

    Sweeps immediately on start, then on every subsequent tick — a service
    that restarts more often than ``interval_seconds`` (the default is a full
    day) would otherwise never run a single sweep.

    Args:
        db_factory: Callable that returns an async context manager yielding
            an ``AsyncSession`` (typically ``AsyncSessionLocal``).
        interval_seconds: Seconds between sweeps.
        retention_days: Override for the retention window; defaults to
            ``settings.session_retention_days``.

    Cancellation:
        Cancel the task at shutdown. ``CancelledError`` propagates after the
        stop is logged, so the lifespan hook sees a genuinely cancelled task.
    """
    log.info("session_retention_loop_started", interval_seconds=interval_seconds)
    try:
        while True:
            try:
                await prune_expired_sessions(db_factory, retention_days)
            except Exception as exc:  # noqa: BLE001 — log + continue is the right thing here
                log.error("session_retention_sweep_failed", error=str(exc), exc_info=True)
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        log.info("session_retention_loop_stopped")
        raise
