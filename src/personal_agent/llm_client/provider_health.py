"""Per-provider availability checks (ADR-0121 §3, AC-5 / FRE-918).

Replaces the "cloud is available iff the Anthropic key is present" proxy
(the old profile-keyed ``/api/inference/status`` cloud branch) with one check
per *declared provider* — the read API's candidate filter
(:func:`personal_agent.config.model_loader.role_candidates`) needs
availability keyed on the provider a deployment actually belongs to, not on a
two-valued profile.

Cloud placement is a **configuration** check: the provider's declared
``auth_env`` ``AppConfig`` field must hold a credential. No live reachability
probe — a per-provider network call on every config-read is chattiness this
ADR does not ask for, and secret presence is the same signal
``/api/inference/status`` already uses for its cloud branch.

Local placement is a **live** check: it reuses the same SLM-tunnel health
probe ``/api/inference/status`` calls, since the local provider's GPU really
can be down independent of any secret.
"""

from __future__ import annotations

import asyncio

import structlog

from personal_agent.config.settings import AppConfig
from personal_agent.llm_client.models import ModelConfig, Placement, ProviderDefinition
from personal_agent.observability.slm_health import probe_slm_health
from personal_agent.telemetry.trace import SystemTraceContext

log = structlog.get_logger(__name__)


async def is_provider_available(
    provider: ProviderDefinition, settings: AppConfig, *, trace_id: str | None = None
) -> bool:
    """Return whether a provider is currently available for dispatch.

    Args:
        provider: The provider definition to check.
        settings: Live ``AppConfig`` to resolve ``auth_env`` and SLM probe
            settings against.
        trace_id: Optional trace id for the local health probe's log
            correlation. A fresh one is generated when omitted.

    Returns:
        ``True`` if the provider is available for new requests right now.
        Cloud: the declared ``auth_env`` credential is present (or the
        provider declares no auth at all). Local: the SLM-tunnel probe status
        is not ``"down"`` — ``"up"`` and ``"degraded"`` both still serve.
    """
    if provider.placement is Placement.CLOUD:
        return provider.auth_env is None or bool(getattr(settings, provider.auth_env, None))

    ctx_trace_id = trace_id or SystemTraceContext.new("provider_health_probe").trace_id
    snapshot = await probe_slm_health(
        url=settings.resolved_slm_health_url,
        timeout_s=3.0,
        trace_id=ctx_trace_id,
        gpu_util_degraded_pct=settings.slm_gpu_util_degraded_pct,
        queue_depth_degraded=settings.slm_queue_depth_degraded,
    )
    return snapshot.status != "down"


async def check_all_providers(
    config: ModelConfig, settings: AppConfig, *, trace_id: str | None = None
) -> dict[str, bool]:
    """Return ``provider key -> available`` for every provider declared in the catalog.

    One health check per provider (ADR-0121 §3) — not a per-candidate liveness
    check. :func:`~personal_agent.config.model_loader.role_candidates` uses
    this map to filter a role's candidate deployments.

    Args:
        config: The loaded catalog.
        settings: Live ``AppConfig``.
        trace_id: Optional trace id threaded into each local probe.

    Returns:
        A mapping covering every key in ``config.providers``.
    """
    keys = list(config.providers)
    results = await asyncio.gather(
        *(is_provider_available(config.providers[key], settings, trace_id=trace_id) for key in keys)
    )
    return dict(zip(keys, results, strict=True))
