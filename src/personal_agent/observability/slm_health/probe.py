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
in to an additional check: look up the currently-served model
(:func:`personal_agent.llm_client.provider_health.fetch_served_model_ids`)
and run a one-token completion against it. Failure degrades the status.
This is **opt-in and off by default** — only the scheduled probe tick passes
``base_url``; the per-request provider-availability check
(:func:`personal_agent.llm_client.provider_health.is_provider_available`)
does not, so its call frequency and behaviour are unchanged (ADR-0083
already rejected polling the SLM on every turn).
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
_GENERATION_TIMEOUT_S = 10.0
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
        generation_timeout_s: Timeout for the completion call (the
            served-model lookup carries its own fixed 3s timeout — see
            :func:`_probe_generation`).

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
    generation_probe_latency_ms: float | None = None
    if base_url:
        gen_start = time.monotonic()
        generation_ok, generation_error = await _probe_generation(
            base_url=base_url, timeout_s=generation_timeout_s, trace_id=trace_id
        )
        generation_probe_latency_ms = (time.monotonic() - gen_start) * 1000

    snapshot = _build_snapshot(
        body=body,
        probe_latency_ms=probe_latency_ms,
        probed_at=probed_at,
        trace_id=trace_id,
        gpu_util_degraded_pct=gpu_util_degraded_pct,
        queue_depth_degraded=queue_depth_degraded,
        generation_ok=generation_ok,
        generation_error=generation_error,
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
        trace_id=trace_id,
        component="slm_health",
    )
    return snapshot


async def _probe_generation(
    *, base_url: str, timeout_s: float, trace_id: str
) -> tuple[bool, str | None]:
    """Confirm the SLM backend can actually generate, not just answer ``/health`` (FRE-1474).

    Looks up the currently-served model id and runs a one-token completion
    against it. Reuses
    :func:`personal_agent.llm_client.provider_health.fetch_served_model_ids`
    for the served-model lookup rather than re-parsing ``/v1/models`` — the
    import is local because :mod:`personal_agent.llm_client.provider_health`
    imports this package at module scope, so a top-level import here would
    cycle.

    Args:
        base_url: The SLM's ``/v1`` API base.
        timeout_s: Timeout for the completion call. The served-model lookup
            has no timeout parameter of its own — it is fixed at
            :data:`personal_agent.llm_client.provider_health._SERVED_MODELS_TIMEOUT_S`
            (3s) regardless of what is passed here.
        trace_id: Probe's trace ID for log correlation.

    Returns:
        ``(True, None)`` on a successful completion; ``(False, <reason>)``
        for any failure — no model reported as served, a non-2xx completion
        response, a malformed body, or a transport error. Never raises.
    """
    from personal_agent.llm_client.provider_health import (  # noqa: PLC0415 — avoid import cycle
        fetch_served_model_ids,
    )

    served_ids = await fetch_served_model_ids(base_url, trace_id=trace_id)
    if not served_ids:
        return False, "generation check failed: no model reported as served"
    model_id = next(iter(served_ids))

    try:
        async with create_guarded_http_client(timeout=timeout_s) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                json={
                    "model": model_id,
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
            when it was not requested.
        generation_error: Explanation when ``generation_ok`` is ``False``.
        generation_probe_latency_ms: Round-trip latency of the generation
            check, when performed.

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
