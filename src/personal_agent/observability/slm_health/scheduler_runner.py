"""Convenience wrapper for invoking the SLM-health probe from the scheduler.

Mirrors :mod:`personal_agent.observability.joinability.scheduler_runner` in
structure: accepts the brainstem scheduler's already-open ES client, probes the
SLM health endpoint (with CF Access headers from settings), updates the process
cache, and optionally writes the snapshot to Elasticsearch. Any failure is
logged and swallowed — the scheduler must not crash because a probe tick failed.

This is the **only** caller that opts the probe into the FRE-1474 generation
check (passing ``base_url``): the scheduled tick's existing cadence
(``slm_health_probe_interval_seconds``, 300s by default) absorbs the extra
cost, rather than adding a new schedule. The per-request provider-availability
check (:func:`personal_agent.llm_client.provider_health.is_provider_available`)
does not probe: it reads the verdict this tick publishes (FRE-1435).

While the published verdict is ``down``, the scheduler probes at the shorter
``slm_health_probe_down_interval_seconds`` (ADR-0144 D4 rule 4), see
:func:`slm_health_probe_interval_s`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from personal_agent.config.settings import get_settings
from personal_agent.observability.slm_health.cache import set_cached_snapshot, verdict_is_down
from personal_agent.observability.slm_health.probe import probe_slm_health
from personal_agent.observability.slm_health.sink import write_result
from personal_agent.observability.slm_health.snapshot import SlmHealthSnapshot
from personal_agent.observability.slm_health.thinking_canary import (
    ThinkingCanaryResult,
    run_thinking_canary,
)
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.spans import close_root_span, open_root_span
from personal_agent.telemetry.trace import SystemTraceContext

if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch
    from opentelemetry.trace import Tracer

log = get_logger(__name__)


def slm_health_probe_interval_s(*, normal_s: float, down_s: float) -> float:
    """Return the probe interval the published verdict calls for (ADR-0144 D4 rule 4).

    The scheduler checks this once per lifecycle tick (60 s), so a recovered origin is seen
    within one interval plus one tick.

    Args:
        normal_s: The interval while the verdict is not ``down``.
        down_s: The shorter interval while the verdict is ``down``.

    Returns:
        The smaller of *down_s* and *normal_s* while an uncleared ``down`` is published, else
        *normal_s*. The ``min`` keeps a ``down`` from ever probing more slowly than normal.
    """
    return min(down_s, normal_s) if verdict_is_down() else normal_s


async def _run_canary(base_url: str, *, trace_id: str) -> ThinkingCanaryResult:
    """Run the thinking canary, turning any failure into an error result (FRE-1368).

    :func:`run_thinking_canary` never raises. This guard keeps the tick safe if that
    contract is ever broken, and it is what the scheduler-level test pins.

    Args:
        base_url: The SLM's ``/v1`` API base.
        trace_id: The tick's trace ID.

    Returns:
        The canary's result, or one carrying only the error.
    """
    try:
        return await run_thinking_canary(base_url=base_url, trace_id=trace_id)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "slm_thinking_canary_launch_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            trace_id=trace_id,
            component="slm_health",
        )
        return ThinkingCanaryResult(error=f"{type(exc).__name__}: {exc}"[:200])


async def run_scheduled_slm_health_probe(
    *,
    es_client: "AsyncElasticsearch | None",
    tracer: "Tracer | None" = None,
) -> SlmHealthSnapshot | None:
    """Run one SLM-health probe tick from the brainstem scheduler.

    Builds Cloudflare Access headers from settings, probes the SLM health URL,
    updates the process-global cache, and (when *es_client* is supplied) writes
    the snapshot to Elasticsearch. All failures are caught and logged; the
    function returns ``None`` on a catastrophic error.

    Opens a root span for the tick (ADR-0129 D3, FRE-1069) — only when the probe
    actually runs; a disabled probe produces no span, since nothing happened to
    trace.

    Args:
        es_client: Already-open ``AsyncElasticsearch`` client owned by the
            scheduler's :class:`DataLifecycleManager`. When ``None`` the probe
            still runs and the cache is updated, but the result is not persisted
            to ES.
        tracer: Tracer to open the root span with. Defaults to the process-wide
            tracer; tests inject their own tracer bound to an in-memory exporter.

    Returns:
        The :class:`~.snapshot.SlmHealthSnapshot` for the run, or ``None`` if
        the probe could not be launched (e.g. an unexpected exception building
        the request context).
    """
    settings = get_settings()
    if not settings.slm_health_probe_enabled:
        return None

    span, token, cv_tokens = open_root_span("slm_health_probe", tracer=tracer)
    try:
        ctx = SystemTraceContext.new("slm_health_probe")

        # No Cloudflare Access headers are built here (ADR-0132 D1): the health URL
        # resolves to the internal Caddy egress block on deployments behind
        # Cloudflare, and Caddy injects the service token.
        # resolved_slm_base_url is an origin that may or may not already
        # carry /v1 (mirrors resolved_slm_health_url's own normalization).
        origin = settings.resolved_slm_base_url.rstrip("/").removesuffix("/v1")
        slm_v1_base_url = f"{origin}/v1"
        generation_base_url: str | None = (
            slm_v1_base_url if settings.slm_health_generation_check_enabled else None
        )

        try:
            snapshot = await probe_slm_health(
                url=settings.resolved_slm_health_url,
                timeout_s=3.0,
                trace_id=ctx.trace_id,
                gpu_util_degraded_pct=settings.slm_gpu_util_degraded_pct,
                queue_depth_degraded=settings.slm_queue_depth_degraded,
                base_url=generation_base_url,
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

        # Update process-global cache regardless of ES availability. A probe that
        # started before a newer observation is rejected (ADR-0144 D4 rule 2).
        accepted = set_cached_snapshot(snapshot)

        log.info(
            "slm_health_probe_scheduled_completed",
            status=snapshot.status,
            verdict_accepted=accepted,
            reachable=snapshot.reachable,
            probe_latency_ms=snapshot.probe_latency_ms,
            model_loaded=snapshot.model_loaded,
            gpu_util_pct=snapshot.gpu_util_pct,
            queue_depth=snapshot.queue_depth,
            trace_id=ctx.trace_id,
            component="slm_health",
        )

        # FRE-1368: the thinking canary runs only after the verdict is cached, and its
        # result goes onto a copy for Elasticsearch. The cache never holds a canary field,
        # so a red canary cannot change what `is_provider_available` reads (master,
        # 2026-10-24). An unreachable backend has nothing to measure.
        published = snapshot
        if settings.slm_thinking_canary_enabled and snapshot.reachable:
            published = snapshot.model_copy(
                update=(await _run_canary(slm_v1_base_url, trace_id=ctx.trace_id)).snapshot_fields()
            )

        if es_client is not None:
            try:
                await write_result(
                    es_client,
                    published,
                    prefix=settings.slm_health_index_prefix,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "slm_health_probe_es_write_failed",
                    error=str(exc),
                    trace_id=ctx.trace_id,
                    component="slm_health",
                )

        return published
    finally:
        close_root_span(span, token, cv_tokens)
