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
from typing import Any

import structlog

from personal_agent.config.settings import AppConfig
from personal_agent.llm_client.models import ModelConfig, Placement, ProviderDefinition
from personal_agent.observability.slm_health import probe_slm_health
from personal_agent.security import create_guarded_http_client
from personal_agent.telemetry.trace import SystemTraceContext

log = structlog.get_logger(__name__)

_SERVED_MODELS_TIMEOUT_S = 3.0


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


async def fetch_served_model_ids(base_url: str, *, trace_id: str | None = None) -> frozenset[str]:
    """Return the model ids a local provider's ``/v1/models`` endpoint reports as served.

    This is the per-*model* liveness check :func:`check_all_providers`'s own
    docstring says it is not (FRE-1415): a local provider's declared catalog
    can be a strict superset of what the host currently serves — since
    2026-09-03 the Mac SLM host holds exactly one model at a time, while the
    catalog still declares several deployments under ``slm_local``.

    Args:
        base_url: The provider's own resolved base URL (its
            :attr:`~personal_agent.llm_client.models.ProviderDefinition.base_url`,
            already carrying its ``/v1`` suffix and, on the cloud profile,
            already rewritten from the placeholder tunnel host to the real
            Caddy egress base by :func:`personal_agent.config.model_loader._resolve_slm_endpoints`).
            Never a deployment-wide setting — each local provider is probed at
            its own address, so a second local backend gets its own served set.
        trace_id: Optional trace id for log correlation.

    Returns:
        The served model ids. Empty on ANY failure — a timeout, a non-2xx
        status, or a response that does not match the expected
        ``{"data": [{"id": ...}, ...]}`` shape (missing/non-list ``data``, a
        non-object entry, or an entry with no non-empty string ``id``) — this
        function never raises. Empty is also the genuine, well-formed answer
        for ``{"data": []}`` (a host reachable but serving nothing); the two
        cases are deliberately not distinguished here because
        :func:`personal_agent.config.model_loader.role_candidates` treats them
        identically — "no id confirmed served" — never falling through to
        "everything available" (AC-5).
    """
    ctx_trace_id = trace_id or SystemTraceContext.new("slm_served_models_probe").trace_id
    url = f"{base_url.rstrip('/')}/models"
    try:
        async with create_guarded_http_client(timeout=_SERVED_MODELS_TIMEOUT_S) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        body: Any = resp.json()
        if not isinstance(body, dict):
            raise ValueError("response body is not a JSON object")
        data = body.get("data")
        if not isinstance(data, list):
            raise ValueError("'data' is missing or not a list")
        ids: set[str] = set()
        for entry in data:
            if not isinstance(entry, dict):
                raise ValueError("a 'data' entry is not an object")
            entry_id = entry.get("id")
            if not isinstance(entry_id, str) or not entry_id:
                raise ValueError("a 'data' entry has no non-empty string 'id'")
            ids.add(entry_id)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "slm_served_models_probe_failed",
            url=url,
            trace_id=ctx_trace_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return frozenset()
    return frozenset(ids)


async def check_local_served_ids(
    config: ModelConfig, *, trace_id: str | None = None
) -> dict[str, frozenset[str]]:
    """Return ``local provider key -> served model ids`` for every LOCAL provider.

    Cloud providers are absent from the result — they expose no per-model
    served list, and :func:`personal_agent.config.model_loader.role_candidates`
    only consults this mapping for :attr:`~personal_agent.llm_client.models.Placement.LOCAL`
    deployments (AC-3).

    Args:
        config: The loaded catalog.
        trace_id: Optional trace id threaded into each provider's probe.

    Returns:
        A mapping covering every local provider key. A local provider with no
        configured ``base_url`` maps to an empty frozenset without probing —
        there is nowhere to ask, so it fails closed the same as a probe failure.
    """
    local_keys = [
        key for key, provider in config.providers.items() if provider.placement is Placement.LOCAL
    ]
    base_urls = [config.providers[key].base_url for key in local_keys]
    results = await asyncio.gather(
        *(
            fetch_served_model_ids(base_url, trace_id=trace_id) if base_url else _empty_served_ids()
            for base_url in base_urls
        )
    )
    return dict(zip(local_keys, results, strict=True))


async def _empty_served_ids() -> frozenset[str]:
    """Return an empty served-id set — the no-``base_url`` fail-closed branch."""
    return frozenset()
