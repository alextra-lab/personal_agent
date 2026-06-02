"""Convenience wrapper for invoking the SLM-health probe from the scheduler.

Mirrors :mod:`personal_agent.observability.joinability.scheduler_runner` in
structure: accepts the brainstem scheduler's already-open ES client, probes the
SLM health endpoint (with CF Access headers from settings), updates the process
cache, and optionally writes the snapshot to Elasticsearch. Any failure is
logged and swallowed — the scheduler must not crash because a probe tick failed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from personal_agent.config.settings import get_settings
from personal_agent.observability.slm_health.cache import set_cached_snapshot
from personal_agent.observability.slm_health.probe import probe_slm_health
from personal_agent.observability.slm_health.sink import write_result
from personal_agent.observability.slm_health.snapshot import SlmHealthSnapshot
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.trace import SystemTraceContext

if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch

log = get_logger(__name__)


async def run_scheduled_slm_health_probe(
    *,
    es_client: "AsyncElasticsearch | None",
) -> SlmHealthSnapshot | None:
    """Run one SLM-health probe tick from the brainstem scheduler.

    Builds Cloudflare Access headers from settings, probes the SLM health URL,
    updates the process-global cache, and (when *es_client* is supplied) writes
    the snapshot to Elasticsearch. All failures are caught and logged; the
    function returns ``None`` on a catastrophic error.

    Args:
        es_client: Already-open ``AsyncElasticsearch`` client owned by the
            scheduler's :class:`DataLifecycleManager`. When ``None`` the probe
            still runs and the cache is updated, but the result is not persisted
            to ES.

    Returns:
        The :class:`~.snapshot.SlmHealthSnapshot` for the run, or ``None`` if
        the probe could not be launched (e.g. an unexpected exception building
        the request context).
    """
    settings = get_settings()
    if not settings.slm_health_probe_enabled:
        return None

    ctx = SystemTraceContext.new("slm_health_probe")

    # Build CF Access headers from settings (same pattern as client.py:400-405
    # and app.py:inference_status).
    cf_headers: dict[str, str] = {}
    if settings.cf_access_client_id and settings.cf_access_client_secret:
        cf_headers["CF-Access-Client-Id"] = settings.cf_access_client_id
        cf_headers["CF-Access-Client-Secret"] = settings.cf_access_client_secret

    try:
        snapshot = await probe_slm_health(
            url=settings.slm_health_url,
            cf_headers=cf_headers,
            timeout_s=3.0,
            trace_id=ctx.trace_id,
            gpu_util_degraded_pct=settings.slm_gpu_util_degraded_pct,
            queue_depth_degraded=settings.slm_queue_depth_degraded,
        )
    except Exception as exc:  # noqa: BLE001  # pragma: nocover
        log.warning(
            "slm_health_probe_launch_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            trace_id=ctx.trace_id,
            component="slm_health",
        )
        return None

    # Update process-global cache regardless of ES availability.
    set_cached_snapshot(snapshot)

    log.info(
        "slm_health_probe_scheduled_completed",
        status=snapshot.status,
        reachable=snapshot.reachable,
        probe_latency_ms=snapshot.probe_latency_ms,
        model_loaded=snapshot.model_loaded,
        gpu_util_pct=snapshot.gpu_util_pct,
        queue_depth=snapshot.queue_depth,
        trace_id=ctx.trace_id,
        component="slm_health",
    )

    if es_client is not None:
        try:
            await write_result(
                es_client,
                snapshot,
                prefix=settings.slm_health_index_prefix,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "slm_health_probe_es_write_failed",
                error=str(exc),
                trace_id=ctx.trace_id,
                component="slm_health",
            )

    return snapshot
