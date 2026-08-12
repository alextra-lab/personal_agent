"""Daily kg_stats projection job (FRE-1210 T6.1 / ADR-0042 / FRE-161).

A separate job from the weekly ``freshness_review`` (``brainstem/jobs/freshness_review.py``),
not a cadence change to it. The ticket's requirement -- "weekly is too coarse for a
trend and the scan is cheap" -- applies to this Neo4j->Postgres projection specifically,
not to the whole staleness-tier/JSONL/Captain's-Log-proposal/bus-event pipeline that
``run_freshness_review`` also drives. That pipeline's ``MemoryStalenessReviewedEvent``
is keyed by ``iso_week`` and its downstream Captain's Log proposal fingerprint is
computed from ``(dominant_tier, iso_week)`` -- making the whole job run daily without
touching that contract would fire seven "weekly" events a week with colliding
fingerprints. Decoupling avoids that: this job shares only the Cypher aggregation and
Postgres writer (:mod:`memory.kg_stats_aggregate`) with the weekly job, not its
schedule, event contract, or Captain's Log proposals.

Gated on ``freshness_enabled`` -- the same flag ``freshness_review`` uses, since this
reads the same FRE-161 access-tracking properties. No separate enable flag; that would
be config sprawl the ticket doesn't ask for.
"""

from __future__ import annotations

from personal_agent.config.settings import get_settings
from personal_agent.memory.kg_stats_aggregate import aggregate_kg_stats, write_kg_stats
from personal_agent.memory.service import MemoryService
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

_DEFAULT_CRON_MINUTE = 0
_DEFAULT_CRON_HOUR = 4


def parse_kg_stats_projection_schedule(cron: str) -> tuple[int, int]:
    """Parse a 5-field crontab line into ``(minute, hour)`` -- this job runs every day.

    Unlike ``freshness_review``'s parser, there is no day-of-week to resolve:
    this job is daily by design, so only the minute/hour fields matter.

    Args:
        cron: Five whitespace-separated fields (minute hour dom month dow).

    Returns:
        ``(minute, hour)``. On parse failure, returns ``(0, 4)`` -- 04:00 UTC.
    """
    parts = cron.strip().split()
    if len(parts) != 5:
        return (_DEFAULT_CRON_MINUTE, _DEFAULT_CRON_HOUR)
    minute_s, hour_s, _dom, _month, _dow = parts
    try:
        return (int(minute_s), int(hour_s))
    except ValueError:
        return (_DEFAULT_CRON_MINUTE, _DEFAULT_CRON_HOUR)


async def run_kg_stats_projection(memory_service: MemoryService | None, trace_id: str) -> None:
    """Execute one kg_stats projection pass: aggregate from Neo4j, write to Postgres.

    Args:
        memory_service: Connected memory service, or ``None`` to skip.
        trace_id: Correlation id for structured logs (e.g. ``kg-stats-projection-2026-08-11``).
    """
    cfg = get_settings()
    if not cfg.freshness_enabled:
        log.debug("kg_stats_projection_skipped_disabled", trace_id=trace_id)
        return
    if memory_service is None or not memory_service.connected or memory_service.driver is None:
        log.warning("kg_stats_projection_skipped_no_memory", trace_id=trace_id)
        return

    rows = await aggregate_kg_stats(memory_service.driver, cfg)
    written = await write_kg_stats(rows, trace_id)
    log.info(
        "kg_stats_projection_completed",
        trace_id=trace_id,
        rows_computed=len(rows),
        rows_written=written,
    )
