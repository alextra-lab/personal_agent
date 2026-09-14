"""SLM-health probe — HTTP liveness + optional rich telemetry (FRE-399 / ADR-0083).

:func:`probe_slm_health` makes one HTTP ``GET`` to the SLM health URL and
returns a :class:`~.snapshot.SlmHealthSnapshot`. It is **defensive by design**:

* A liveness-only ``200 OK`` (today's ``/health`` response) → ``up`` with all
  rich fields ``None``.
* A richer structured JSON response → fills in GPU util / VRAM / queue depth
  and computes the ``degraded`` verdict when any threshold fires.
* A ``403 Forbidden`` (expired CF Access token) → ``down`` + auth warning.
* Any exception or non-2xx status → ``down`` snapshot.

The function **never raises** — it returns a ``down`` snapshot on any failure.
This contract lets it be called from the brainstem scheduler and the FastAPI
endpoint without try/except at the call site.

**Generation check (FRE-1474).** ``/health`` answering ``200`` only proves the
SLM's front door is up, not that the model backend behind it can generate — a
live incident showed ``/health`` reading ``up`` on both sides of a ~20s window
where every real generation call got a 503. Passing ``base_url`` opts a call
in to an additional check: run a one-token completion against the catalog's
local ``primary`` deployment. Failure degrades the status. This is **opt-in
and off by default** — only the scheduled probe tick passes ``base_url``
(gated further by ``settings.slm_health_generation_check_enabled``, itself
default ``False`` — master gate 2026-09-14, FRE-1517); the per-request
provider-availability check
(:func:`personal_agent.llm_client.provider_health.is_provider_available`)
never passes ``base_url``, so its call frequency and behaviour are unchanged
(ADR-0083 already rejected polling the SLM on every turn).

**The check skips rather than degrades (master gate, FRE-1474) when:**

* this gateway's own local provider has an in-flight request (a serial local
  backend can take minutes on a large real call — waiting behind it and
  timing out would flap a healthy-but-busy backend to ``degraded``, exactly
  the false alarm AC-2 guards against);
* the catalog's primary deployment id is not currently served (e.g. the
  owner manually swapped in a different pack for a model-testing study).

A skip is not a verdict either way: ``generation_ok`` stays ``None`` and
``generation_skip_reason`` explains why, but ``status`` is unaffected by it.
This cannot see load from a **different** process (e.g. the eval gateway) —
the concurrency check is process-local — so a raised completion timeout
(``_GENERATION_TIMEOUT_S``) gives headroom against that residual case rather
than eliminating it.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from personal_agent.observability.slm_health.snapshot import SlmHealthSnapshot
from personal_agent.security import create_guarded_http_client
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

_DEFAULT_TIMEOUT_S = 3.0
# 30s (was 10s): headroom over ordinary prefill/queueing jitter for the
# residual cross-process-traffic case the busy-check can't see (FRE-1474
# master gate) — well short of the 155-365s a genuinely wedged local backend
# has shown, so a real stall still trips a failure when this gateway's own
# semaphore is idle.
_GENERATION_TIMEOUT_S = 30.0
_GENERATION_MAX_TOKENS = 1
_ERROR_MAX_LEN = 200


async def probe_slm_health(
    *,
    url: str,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    trace_id: str,
    gpu_util_degraded_pct: float = 95.0,
    queue_depth_degraded: int = 4,
    base_url: str | None = None,
    generation_timeout_s: float = _GENERATION_TIMEOUT_S,
) -> SlmHealthSnapshot:
    """Probe the SLM health endpoint and return a frozen snapshot.

    Args:
        url: Full URL of the SLM ``/health`` endpoint. On deployments behind
            Cloudflare this addresses the internal Caddy egress block, which
            injects the Access service token (ADR-0132 D1) — this probe never
            constructs or forwards a credential.
        timeout_s: HTTP connection + read timeout in seconds.
        trace_id: Probe's trace ID for log correlation.
        gpu_util_degraded_pct: GPU utilisation threshold that triggers
            ``"degraded"`` status.
        queue_depth_degraded: Queue depth threshold that triggers
            ``"degraded"`` status.
        base_url: The SLM's ``/v1`` API base (FRE-1474). When given, a
            generation-capability check runs after a successful ``/health``
            response. ``None`` (the default) skips it entirely.
        generation_timeout_s: Timeout for the completion call. The
            served-model-existence check has no timeout of its own — it is
            fixed at
            :data:`personal_agent.llm_client.provider_health._SERVED_MODELS_TIMEOUT_S`
            (3s) regardless of what is passed here.

    Returns:
        A :class:`~.snapshot.SlmHealthSnapshot` — always, even on failure.
        The caller must never assume a raised exception.
    """
    probed_at = datetime.now(timezone.utc)
    start = time.monotonic()

    try:
        async with create_guarded_http_client(timeout=timeout_s) as client:
            resp = await client.get(url)
    except httpx.TimeoutException as exc:
        log.warning(
            "slm_health_probe_timeout",
            url=url,
            trace_id=trace_id,
            error=str(exc),
            component="slm_health",
        )
        return SlmHealthSnapshot(
            status="down",
            reachable=False,
            probed_at=probed_at,
            trace_id=trace_id,
            error=f"timeout after {timeout_s}s",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "slm_health_probe_error",
            url=url,
            trace_id=trace_id,
            error=str(exc),
            error_type=type(exc).__name__,
            component="slm_health",
        )
        return SlmHealthSnapshot(
            status="down",
            reachable=False,
            probed_at=probed_at,
            trace_id=trace_id,
            error=str(exc),
        )

    probe_latency_ms = (time.monotonic() - start) * 1000

    # 403 = expired / invalid CF Access service token. Since ADR-0132 D1 the
    # token is held by Caddy, not this process, so the rotation target is the
    # Caddy-only env source — not anything the application can see.
    if resp.status_code == 403:
        log.warning(
            "inference_tunnel_auth_failed",
            status=403,
            hint=(
                "Rotate the CF Access service token via terraform apply, then update "
                "the Caddy-only env source (.env.caddy) and recreate the caddy container"
            ),
            trace_id=trace_id,
            component="slm_health",
        )
        return SlmHealthSnapshot(
            status="down",
            reachable=False,
            probe_latency_ms=probe_latency_ms,
            probed_at=probed_at,
            trace_id=trace_id,
            error="CF Access auth failed (403)",
        )

    if not resp.is_success:
        log.warning(
            "slm_health_probe_http_error",
            status_code=resp.status_code,
            url=url,
            trace_id=trace_id,
            component="slm_health",
        )
        return SlmHealthSnapshot(
            status="down",
            reachable=False,
            probe_latency_ms=probe_latency_ms,
            probed_at=probed_at,
            trace_id=trace_id,
            error=f"HTTP {resp.status_code}",
        )

    # Parse any structured fields the SLM exposes; gracefully ignore absent keys.
    body: dict[str, Any] = {}
    try:
        body = resp.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:  # noqa: BLE001
        pass  # liveness-only body — all rich fields stay None

    generation_ok: bool | None = None
    generation_error: str | None = None
    generation_skip_reason: str | None = None
    generation_probe_latency_ms: float | None = None
    if base_url:
        gen_start = time.monotonic()
        generation_ok, generation_reason = await _probe_generation(
            base_url=base_url, timeout_s=generation_timeout_s, trace_id=trace_id
        )
        if generation_ok is None:
            # Skipped — a busy local backend or an unresolvable primary is not
            # a verdict either way, and a skip measures nothing worth timing.
            generation_skip_reason = generation_reason
        else:
            generation_probe_latency_ms = (time.monotonic() - gen_start) * 1000
            if generation_ok is False:
                generation_error = generation_reason

    snapshot = _build_snapshot(
        body=body,
        probe_latency_ms=probe_latency_ms,
        probed_at=probed_at,
        trace_id=trace_id,
        gpu_util_degraded_pct=gpu_util_degraded_pct,
        queue_depth_degraded=queue_depth_degraded,
        generation_ok=generation_ok,
        generation_error=generation_error,
        generation_skip_reason=generation_skip_reason,
        generation_probe_latency_ms=generation_probe_latency_ms,
    )
    log.info(
        "slm_health_probe_completed",
        status=snapshot.status,
        reachable=snapshot.reachable,
        probe_latency_ms=round(probe_latency_ms, 1),
        model_loaded=snapshot.model_loaded,
        gpu_util_pct=snapshot.gpu_util_pct,
        queue_depth=snapshot.queue_depth,
        generation_ok=snapshot.generation_ok,
        generation_skip_reason=snapshot.generation_skip_reason,
        trace_id=trace_id,
        component="slm_health",
    )
    return snapshot


def _provider_active_count(provider_key: str) -> int:
    """Return *provider_key*'s current in-flight count, or ``0`` on any failure.

    Wraps :func:`personal_agent.llm_client.concurrency.get_inference_concurrency_controller`
    — never raises, so a concurrency-controller lookup failure cannot break
    :func:`probe_slm_health`'s own never-raises contract. ``0`` is the
    permissive default: a lookup failure falls back to running the
    generation check as before this existed, rather than silently disabling
    AC-1 detection whenever the controller is unavailable.

    Args:
        provider_key: Provider name as registered in the catalog (e.g.
            ``"slm_local"``).

    Returns:
        The provider's active in-flight count, or ``0`` if unregistered or
        the lookup fails.
    """
    from personal_agent.llm_client.concurrency import (  # noqa: PLC0415 — deferred: only needed by the generation check
        get_inference_concurrency_controller,
    )

    try:
        status = get_inference_concurrency_controller().get_status()
        return status["providers"].get(provider_key, {}).get("active", 0)
    except Exception:  # noqa: BLE001
        return 0


def _resolve_primary_local_deployment() -> tuple[tuple[str, str] | None, str | None]:
    """Return the catalog's local ``primary`` deployment's (provider, model id).

    The generation check targets this specific deployment rather than an
    arbitrary served id (FRE-1474 master gate) — once more than one model is
    served (e.g. a reranker beside the chat model), an arbitrary pick can
    send a chat completion to a non-chat model and report a false failure.

    Args:
        (none)

    Returns:
        ``((provider_key, model_id), None)`` on success. ``(None, <reason>)``
        when the catalog fails to load, declares no ``primary`` role, or that
        role does not resolve to a LOCAL deployment — each a distinct,
        specific reason rather than a bare ``None``, since these are
        different failure shapes (a broken catalog vs. an intentionally
        cloud-bound primary) that a caller or reader should be able to tell
        apart.
    """
    from personal_agent.config.model_loader import (  # noqa: PLC0415 — deferred: only needed by the generation check
        load_model_config,
    )
    from personal_agent.llm_client.models import Placement  # noqa: PLC0415

    try:
        config = load_model_config()
    except Exception as exc:  # noqa: BLE001
        reason = f"{type(exc).__name__}: {exc}"[:_ERROR_MAX_LEN]
        return None, f"catalog failed to load: {reason}"

    primary_binding = config.roles.get("primary")
    if primary_binding is None:
        return None, "catalog declares no 'primary' role binding"

    deployment_key = primary_binding.deployment
    if config.placement_of(deployment_key) is not Placement.LOCAL:
        return None, f"primary deployment {deployment_key!r} is not LOCAL placement"

    model_def = config.models.get(deployment_key)
    if model_def is None or not model_def.provider:
        return None, f"primary deployment {deployment_key!r} has no provider"

    return (model_def.provider, model_def.id), None


async def _probe_generation(
    *, base_url: str, timeout_s: float, trace_id: str
) -> tuple[bool | None, str | None]:
    """Confirm the SLM backend can actually generate, not just answer ``/health`` (FRE-1474).

    Targets the catalog's local ``primary`` deployment specifically (see
    :func:`_resolve_primary_local_deployment`) and skips — rather than fails
    — when this gateway's own local provider is busy (see
    :func:`_provider_active_count`) or that deployment is not currently
    served, both master-gate requirements: a healthy-but-busy backend, or a
    manually swapped-in pack (e.g. during a model-testing study), must not
    read ``degraded``.

    :func:`personal_agent.llm_client.provider_health.fetch_served_model_ids`
    is imported locally because
    :mod:`personal_agent.llm_client.provider_health` imports this package at
    module scope, so a top-level import here would cycle. The concurrency
    and catalog imports in the two helpers above have no such cycle but are
    deferred alongside it for consistency — all three are only needed on
    this opt-in path.

    Note: the busy-check races an acquisition between it and the completion
    POST below (only actually holding a slot would close that window, which
    this probe deliberately does not do); the raised completion timeout
    (``_GENERATION_TIMEOUT_S``) is the accepted mitigation, not a fix.

    Args:
        base_url: The SLM's ``/v1`` API base.
        timeout_s: Timeout for the completion call. The served-model-existence
            check has no timeout of its own — it is fixed at
            :data:`personal_agent.llm_client.provider_health._SERVED_MODELS_TIMEOUT_S`
            (3s) regardless of what is passed here.
        trace_id: Probe's trace ID for log correlation.

    Returns:
        ``(True, None)`` on a successful completion; ``(False, <reason>)``
        for a definitive failure — a non-2xx completion response, a
        malformed body, or a transport error; ``(None, <reason>)`` when
        skipped — the primary deployment is unresolvable, its provider is
        busy, or it is not currently served. Never raises.
    """
    primary, resolve_reason = _resolve_primary_local_deployment()
    if primary is None:
        return None, f"generation check skipped: {resolve_reason}"
    provider_key, primary_model_id = primary

    active = _provider_active_count(provider_key)
    if active > 0:
        return None, f"generation check skipped: {provider_key} busy (active={active})"

    from personal_agent.llm_client.provider_health import (  # noqa: PLC0415 — avoid import cycle
        fetch_served_model_ids,
    )

    served_ids = await fetch_served_model_ids(base_url, trace_id=trace_id)
    if not served_ids:
        return None, "generation check skipped: no model reported as served"
    if primary_model_id not in served_ids:
        return (
            None,
            f"generation check skipped: primary deployment {primary_model_id!r} "
            "not currently served",
        )

    try:
        async with create_guarded_http_client(timeout=timeout_s) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                json={
                    "model": primary_model_id,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": _GENERATION_MAX_TOKENS,
                    "stream": False,
                },
            )
    except Exception as exc:  # noqa: BLE001
        reason = f"{type(exc).__name__}: {exc}"[:_ERROR_MAX_LEN]
        log.warning(
            "slm_health_generation_check_failed",
            base_url=base_url,
            trace_id=trace_id,
            error=reason,
            component="slm_health",
        )
        return False, f"generation check failed: {reason}"

    if not resp.is_success:
        log.warning(
            "slm_health_generation_check_failed",
            base_url=base_url,
            trace_id=trace_id,
            status_code=resp.status_code,
            component="slm_health",
        )
        return False, f"generation check failed: HTTP {resp.status_code}"

    try:
        completion_body = resp.json()
    except Exception:  # noqa: BLE001
        return False, "generation check failed: response was not JSON"

    choices = completion_body.get("choices") if isinstance(completion_body, dict) else None
    if not isinstance(choices, list) or not choices:
        return False, "generation check failed: completion response had no choices"

    return True, None


def _build_snapshot(
    *,
    body: dict[str, Any],
    probe_latency_ms: float,
    probed_at: datetime,
    trace_id: str,
    gpu_util_degraded_pct: float,
    queue_depth_degraded: int,
    generation_ok: bool | None = None,
    generation_error: str | None = None,
    generation_skip_reason: str | None = None,
    generation_probe_latency_ms: float | None = None,
) -> SlmHealthSnapshot:
    """Parse the SLM health response body and compute the aggregated status.

    Field lookup uses defensive ``body.get(key)`` with ``None`` fallback so any
    absent field keeps ``None`` without raising. The status logic mirrors the
    four-level observability degraded-vs-down distinction:

    * ``down`` = not reachable (handled before this function).
    * ``degraded`` = reachable but the generation check failed (FRE-1474), OR
      a rich-body threshold is exceeded.
    * ``up`` = reachable, generation confirmed (or not checked), all
      thresholds OK.

    ``model_loaded`` is still parsed defensively — a future SLM ``/health``
    enrichment could populate it — but no longer drives ``degraded`` on its
    own (FRE-1474 AC-3): today's SLM never reports it, so that branch could
    never fire.

    Args:
        body: Parsed JSON response from the SLM. May be empty (liveness-only).
        probe_latency_ms: Round-trip probe wall-clock time.
        probed_at: Probe initiation timestamp (UTC).
        trace_id: Probe trace ID.
        gpu_util_degraded_pct: GPU threshold.
        queue_depth_degraded: Queue depth threshold.
        generation_ok: Result of the opt-in generation check, or ``None``
            when it was not requested or was skipped.
        generation_error: Explanation when ``generation_ok`` is ``False``.
        generation_skip_reason: Explanation when the check was requested but
            skipped (``generation_ok`` is ``None`` because of a busy backend
            or an unresolvable/unserved primary deployment, not because the
            check was never requested).
        generation_probe_latency_ms: Round-trip latency of the generation
            check, when it actually ran (``None`` on a skip).

    Returns:
        A :class:`~.snapshot.SlmHealthSnapshot` with ``reachable=True``.
    """
    # --- extract rich fields (all optional) ---
    model_loaded: bool | None = body.get("model_loaded")
    gpu_util_pct: float | None = _as_float(body.get("gpu_util_pct"))
    vram_used_mb: float | None = _as_float(body.get("vram_used_mb"))
    vram_total_mb: float | None = _as_float(body.get("vram_total_mb"))
    queue_depth: int | None = _as_int(body.get("queue_depth"))
    latency_ema_ms: float | None = _as_float(body.get("latency_ema_ms"))
    model_id: str | None = body.get("model_id") or None

    # --- compute status ---
    degraded = False
    if gpu_util_pct is not None and gpu_util_pct >= gpu_util_degraded_pct:
        degraded = True
    if queue_depth is not None and queue_depth >= queue_depth_degraded:
        degraded = True
    if generation_ok is False:
        degraded = True

    status: str = "degraded" if degraded else "up"

    return SlmHealthSnapshot(
        status=status,  # type: ignore[arg-type]
        reachable=True,
        model_loaded=model_loaded,
        gpu_util_pct=gpu_util_pct,
        vram_used_mb=vram_used_mb,
        vram_total_mb=vram_total_mb,
        queue_depth=queue_depth,
        latency_ema_ms=latency_ema_ms,
        model_id=model_id,
        probe_latency_ms=probe_latency_ms,
        probed_at=probed_at,
        trace_id=trace_id,
        generation_ok=generation_ok,
        generation_skip_reason=generation_skip_reason,
        generation_probe_latency_ms=generation_probe_latency_ms,
        error=generation_error,
    )


def _as_float(value: object) -> float | None:
    """Coerce a JSON value to float, returning ``None`` on failure."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    """Coerce a JSON value to int, returning ``None`` on failure."""
    if value is None:
        return None
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def new_probe_trace_id() -> str:
    """Return a fresh trace-like ID for an ad-hoc probe (not a system context)."""
    return f"slm-health-{uuid.uuid4().hex[:12]}"
