"""Scheduled sysgraph VACUUM (ANALYZE) maintenance (ADR-0105 D8, FRE-718).

The sysgraph tables see too little write volume for autovacuum's stock scale-factor thresholds to
ever trigger on their own (verified live: all 10 tables show `last_autovacuum`/`last_analyze` as
NULL) — AC-7 anticipates exactly this and asks for a scheduled sweep as the backstop proof-of-
maintenance path. This job connects its own `SysgraphRepository` (mirrors
`brainstem/jobs/outcome_ingestion.py`'s `run_outcome_ingestion`), runs `vacuum_analyze_all()`, and
records a durable, SQL-queryable completion marker (`record_maintenance_run`) so "did this last
succeed" is a single query rather than a log grep.
"""

from __future__ import annotations

from personal_agent.config.settings import get_settings
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


async def run_sysgraph_maintenance(trace_id: str) -> bool:
    """Execute one sysgraph maintenance pass (ADR-0105 D8/AC-7).

    Never raises — every failure is caught and logged so a scheduler tick never crashes. The
    return value, not an exception, is how a caller (the scheduler's daily-hour gate) tells a
    genuine failure apart from a completed pass, so it only marks the day done — skipping a
    retry until tomorrow's window — when this actually succeeded (FRE-718 code review: the
    scheduler previously advanced its "last run" date unconditionally, permanently skipping a
    day's maintenance whenever this function swallowed an internal failure).

    Args:
        trace_id: Correlation id for structured logs (ADR-0074 §I3).

    Returns:
        ``True`` if the pass completed (or there was nothing to do because the feature is
        disabled); ``False`` on a connect or maintenance failure — the caller should retry.
    """
    cfg = get_settings()
    if not cfg.sysgraph_maintenance_enabled:
        log.debug("sysgraph_maintenance_skipped_disabled", trace_id=trace_id)
        return True

    from personal_agent.sysgraph import SysgraphRepository

    repo = SysgraphRepository(cfg.sysgraph_database_url)
    try:
        await repo.connect()
    except Exception as exc:
        log.warning("sysgraph_maintenance_connect_failed", error=str(exc), trace_id=trace_id)
        return False

    try:
        results = await repo.vacuum_analyze_all()
        await repo.record_maintenance_run(results)
        successful = sum(1 for status in results.values() if status == "ok")
        log.info(
            "sysgraph_maintenance_completed",
            table_count=len(results),
            successful=successful,
            results=results,
            trace_id=trace_id,
        )
        return True
    except Exception as exc:
        log.warning("sysgraph_maintenance_failed", error=str(exc), trace_id=trace_id)
        return False
    finally:
        await repo.disconnect()
