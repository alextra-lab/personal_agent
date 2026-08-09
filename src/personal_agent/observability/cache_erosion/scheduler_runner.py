"""Convenience wrapper for invoking the cache-erosion probe from the scheduler.

Mirrors :mod:`personal_agent.observability.slm_health.scheduler_runner` in structure:
accepts the brainstem scheduler's already-open ES client (the probe's only
substrate — unlike joinability, cache-erosion cannot partially run without it),
computes the erosion report, and writes the result doc to Elasticsearch. Any
failure is logged and swallowed — the scheduler must not crash because a probe
tick failed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from personal_agent.config.settings import get_settings
from personal_agent.observability.cache_erosion.monitor import compute_erosion_report
from personal_agent.observability.cache_erosion.result import (
    CacheErosionResultDoc,
    from_report,
)
from personal_agent.observability.cache_erosion.sink import write_result
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.trace import SystemTraceContext

if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch

log = get_logger(__name__)


async def run_scheduled_cache_erosion_probe(
    *, es_client: "AsyncElasticsearch | None"
) -> CacheErosionResultDoc | None:
    """Run one cache-erosion probe tick from the brainstem scheduler.

    Args:
        es_client: Already-open ``AsyncElasticsearch`` client owned by the
            scheduler's :class:`DataLifecycleManager`. The probe's only
            substrate is ``agent-logs-*``, so a missing client means the probe
            cannot run at all this tick (unlike joinability, which can
            partially walk other substrates without ES).

    Returns:
        The :class:`CacheErosionResultDoc` for the run, or ``None`` if the
        probe is disabled or no ES client is available. A ``None`` return
        means "did not run this tick" — the caller must not advance its
        last-run timestamp on ``None``, only on a produced doc.
    """
    settings = get_settings()
    if not getattr(settings, "cache_erosion_probe_enabled", True):
        return None

    ctx = SystemTraceContext.new("cache_erosion_probe")
    if es_client is None:
        log.warning(
            "cache_erosion_probe_skipped_no_es_client",
            trace_id=ctx.trace_id,
            component="cache_erosion",
        )
        return None

    window_days = settings.cache_erosion_probe_window_days
    threshold = settings.cache_erosion_probe_threshold

    report = await compute_erosion_report(
        es_client,
        logs_prefix=settings.elasticsearch_index_prefix,
        window_days=window_days,
        threshold=threshold,
    )
    doc = from_report(report, window_days=window_days, trace_id=ctx.trace_id)

    log.info(
        "cache_erosion_probe_completed",
        any_eroded=doc.any_eroded,
        trace_id=ctx.trace_id,
        component="cache_erosion",
    )

    try:
        await write_result(es_client, doc, prefix=settings.cache_erosion_probe_index_prefix)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "cache_erosion_probe_es_write_failed",
            error=str(exc),
            trace_id=ctx.trace_id,
            component="cache_erosion",
        )

    return doc
