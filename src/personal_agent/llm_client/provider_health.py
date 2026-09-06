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
from dataclasses import dataclass
from typing import Any

import structlog

from personal_agent.config.config_guard import Finding
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


@dataclass(frozen=True)
class ServedModel:
    """One model record from a local provider's ``/v1/models`` response (ADR-0145 D5).

    ``context_length``/``quantization`` are ``None`` when the backend's response omits
    them, or reports them under a type other than the expected one — llama.cpp reports
    both today (:func:`check_served_catalog_drift`), but a future OpenAI-compatible
    backend that does not is a silent absence, not a mismatch on a field it never
    claimed.
    """

    id: str
    context_length: int | None
    quantization: str | None


async def fetch_served_models(
    base_url: str, *, trace_id: str | None = None
) -> dict[str, ServedModel]:
    """Return every model record a local provider's ``/v1/models`` endpoint reports as served.

    The single parsing path for that endpoint — :func:`fetch_served_model_ids` derives
    its id-only set from this function's result, rather than re-parsing the response
    itself.

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
        ``model id -> ServedModel``. Empty on ANY failure — a timeout, a non-2xx
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
        models: dict[str, ServedModel] = {}
        for entry in data:
            if not isinstance(entry, dict):
                raise ValueError("a 'data' entry is not an object")
            entry_id = entry.get("id")
            if not isinstance(entry_id, str) or not entry_id:
                raise ValueError("a 'data' entry has no non-empty string 'id'")
            models[entry_id] = ServedModel(
                id=entry_id,
                context_length=_as_int(entry.get("context_length")),
                quantization=_as_str(entry.get("quantization")),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "slm_served_models_probe_failed",
            url=url,
            trace_id=ctx_trace_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return {}
    return models


def _as_int(value: object) -> int | None:
    """Coerce a JSON field to ``int``, or ``None`` if absent/wrong-typed.

    ``bool`` is a subtype of ``int`` in Python, so it is excluded explicitly —
    a stray ``"context_length": true`` must not parse as ``1``.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_str(value: object) -> str | None:
    """Coerce a JSON field to ``str``, or ``None`` if absent/wrong-typed."""
    return value if isinstance(value, str) else None


async def fetch_served_model_ids(base_url: str, *, trace_id: str | None = None) -> frozenset[str]:
    """Return the model ids a local provider's ``/v1/models`` endpoint reports as served.

    This is the per-*model* liveness check :func:`check_all_providers`'s own
    docstring says it is not (FRE-1415): a local provider's declared catalog
    can be a strict superset of what the host currently serves — since
    2026-09-03 the Mac SLM host holds exactly one model at a time, while the
    catalog still declares several deployments under ``slm_local``.

    Args:
        base_url: See :func:`fetch_served_models`.
        trace_id: Optional trace id for log correlation.

    Returns:
        The served model ids — the key set of :func:`fetch_served_models`'s result.
        Empty on ANY failure, this function never raises (AC-5); see
        :func:`fetch_served_models` for the full failure/shape contract.
    """
    return frozenset((await fetch_served_models(base_url, trace_id=trace_id)).keys())


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


async def check_local_served_models(
    config: ModelConfig, *, trace_id: str | None = None
) -> dict[str, dict[str, ServedModel]]:
    """Return ``local provider key -> {model id -> ServedModel}`` for every LOCAL provider.

    The richer sibling of :func:`check_local_served_ids` — same fan-out-across-LOCAL-
    providers shape, but keeping ``context_length``/``quantization`` instead of
    discarding them, for :func:`check_served_catalog_drift` (ADR-0145 D5).

    Args:
        config: The loaded catalog.
        trace_id: Optional trace id threaded into each provider's probe.

    Returns:
        A mapping covering every local provider key. A local provider with no
        configured ``base_url`` maps to ``{}`` without probing — there is nowhere
        to ask, so it fails closed the same as a probe failure.
    """
    local_keys = [
        key for key, provider in config.providers.items() if provider.placement is Placement.LOCAL
    ]
    base_urls = [config.providers[key].base_url for key in local_keys]
    results = await asyncio.gather(
        *(
            fetch_served_models(base_url, trace_id=trace_id) if base_url else _empty_served_models()
            for base_url in base_urls
        )
    )
    return dict(zip(local_keys, results, strict=True))


async def _empty_served_models() -> dict[str, ServedModel]:
    """Return an empty served-model map — the no-``base_url`` fail-closed branch."""
    return {}


async def check_served_catalog_drift(
    config: ModelConfig, *, trace_id: str | None = None
) -> list[Finding]:
    """ADR-0145 D5 — compare each LOCAL deployment's declared facts against what is served.

    Widens :func:`check_local_served_ids`'s id-only membership check to
    ``context_length`` and ``quantization``, both of which have already drifted in
    the live catalog (``config/models.yaml``'s ``qwen3.8-flash-next-instruct`` declares
    ``quantization: "4bit"`` against a served ``"UD-IQ4_XS"``). A mismatch is reported,
    never enforced — the SLM host is the owner's Mac and is not always on, so nothing
    here may fail a boot (AC-4); that is why every finding is ``policy``, not
    ``safety``, severity.

    A deployment is skipped, producing no finding, when:

    * it is not bound to a LOCAL provider (:meth:`ModelConfig.placement_of`) — no
      served list exists for a cloud deployment.
    * its id is absent from the served set for its provider — either the host is
      unreachable right now (AC-5: :func:`fetch_served_models` fails closed to
      ``{}`` on any probe failure, indistinguishable here from a genuine "not
      currently loaded") or the id really is not served, which
      :func:`~personal_agent.config.model_loader.role_candidates` already fails
      closed on (FRE-1415) — a different, already-handled failure mode.
    * the served record leaves a dimension ``None`` (the backend's response did
      not carry it) — nothing to compare it against.

    Args:
        config: The loaded catalog.
        trace_id: Optional trace id threaded into each provider's probe.

    Returns:
        One ``Finding`` per drifted dimension (AC-1, AC-2); ``[]`` when every
        currently-served LOCAL deployment matches its declared facts (AC-3).
    """
    served_by_provider = await check_local_served_models(config, trace_id=trace_id)
    findings: list[Finding] = []
    for model_key, model_def in config.models.items():
        if config.placement_of(model_key) is not Placement.LOCAL:
            continue
        served = served_by_provider.get(model_def.provider or "", {}).get(model_def.id)
        if served is None:
            continue
        if served.context_length is not None and served.context_length != model_def.context_length:
            findings.append(
                Finding(
                    check="served_context_length_drift",
                    severity="policy",
                    message=(
                        f"deployment '{model_key}' (id {model_def.id!r}) declares "
                        f"context_length={model_def.context_length}, served value is "
                        f"{served.context_length}"
                    ),
                )
            )
        if served.quantization is not None and served.quantization != model_def.quantization:
            findings.append(
                Finding(
                    check="served_quantization_drift",
                    severity="policy",
                    message=(
                        f"deployment '{model_key}' (id {model_def.id!r}) declares "
                        f"quantization={model_def.quantization!r}, served value is "
                        f"{served.quantization!r}"
                    ),
                )
            )
    return findings


async def log_served_catalog_drift(*, trace_id: str | None = None) -> None:
    """Startup drift guard (ADR-0145 D5): warn-log catalog/served-model drift; never raises.

    Loads the catalog itself and delegates to :func:`check_served_catalog_drift`.
    Non-fatal by design, mirroring
    :func:`~personal_agent.config.model_loader.check_vision_capabilities`: a config
    gap, or a live probe finding one, must never down the gateway. Any failure to
    load the catalog is swallowed and logged here; a probe failure is already
    swallowed one layer down, inside :func:`fetch_served_models` (AC-5), and simply
    yields no findings for the deployments it covers.

    Args:
        trace_id: Optional trace correlation id. Startup has no request context,
            so this is normally None.
    """
    from personal_agent.config.model_loader import (
        load_model_config,  # noqa: PLC0415 — avoid import cycle
    )

    try:
        config = load_model_config()
        findings = await check_served_catalog_drift(config, trace_id=trace_id)
    except Exception as exc:  # noqa: BLE001 — startup diagnostic must never down the service
        log.warning(
            "served_catalog_drift_check_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            trace_id=trace_id,
        )
        return
    if findings:
        log.warning(
            "served_catalog_drift",
            findings=[str(finding) for finding in findings],
            trace_id=trace_id,
        )
