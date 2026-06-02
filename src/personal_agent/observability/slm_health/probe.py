"""SLM-health probe — HTTP liveness + optional rich telemetry (FRE-399 / ADR-0083).

:func:`probe_slm_health` makes one HTTP ``GET`` to the SLM health URL and
returns a :class:`~.snapshot.SlmHealthSnapshot`. It is **defensive by design**:

* A liveness-only ``200 OK`` (today's ``/health`` response) → ``up`` with all
  rich fields ``None``.
* A richer structured JSON response → fills in GPU util / VRAM / queue depth /
  model-loaded and computes the ``degraded`` verdict when any threshold fires.
* A ``403 Forbidden`` (expired CF Access token) → ``down`` + auth warning.
* Any exception or non-2xx status → ``down`` snapshot.

The function **never raises** — it returns a ``down`` snapshot on any failure.
This contract lets it be called from the brainstem scheduler and the FastAPI
endpoint without try/except at the call site.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import httpx

from personal_agent.observability.slm_health.snapshot import SlmHealthSnapshot
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

_DEFAULT_TIMEOUT_S = 3.0


async def probe_slm_health(
    *,
    url: str,
    cf_headers: Mapping[str, str],
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    trace_id: str,
    gpu_util_degraded_pct: float = 95.0,
    queue_depth_degraded: int = 4,
) -> SlmHealthSnapshot:
    """Probe the SLM health endpoint and return a frozen snapshot.

    Args:
        url: Full URL of the SLM ``/health`` endpoint.
        cf_headers: Cloudflare Access headers (``CF-Access-Client-Id`` /
            ``CF-Access-Client-Secret``). Pass an empty dict when the SLM is
            accessed without CF Access (e.g. in tests or local-only mode).
        timeout_s: HTTP connection + read timeout in seconds.
        trace_id: Probe's trace ID for log correlation.
        gpu_util_degraded_pct: GPU utilisation threshold that triggers
            ``"degraded"`` status.
        queue_depth_degraded: Queue depth threshold that triggers
            ``"degraded"`` status.

    Returns:
        A :class:`~.snapshot.SlmHealthSnapshot` — always, even on failure.
        The caller must never assume a raised exception.
    """
    probed_at = datetime.now(timezone.utc)
    start = time.monotonic()

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(url, headers=dict(cf_headers))
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

    # 403 = expired / invalid CF Access service token
    if resp.status_code == 403:
        log.warning(
            "inference_tunnel_auth_failed",
            status=403,
            hint="Rotate CF_ACCESS_CLIENT_ID/SECRET via terraform apply",
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

    snapshot = _build_snapshot(
        body=body,
        probe_latency_ms=probe_latency_ms,
        probed_at=probed_at,
        trace_id=trace_id,
        gpu_util_degraded_pct=gpu_util_degraded_pct,
        queue_depth_degraded=queue_depth_degraded,
    )
    log.info(
        "slm_health_probe_completed",
        status=snapshot.status,
        reachable=snapshot.reachable,
        probe_latency_ms=round(probe_latency_ms, 1),
        model_loaded=snapshot.model_loaded,
        gpu_util_pct=snapshot.gpu_util_pct,
        queue_depth=snapshot.queue_depth,
        trace_id=trace_id,
        component="slm_health",
    )
    return snapshot


def _build_snapshot(
    *,
    body: dict[str, Any],
    probe_latency_ms: float,
    probed_at: datetime,
    trace_id: str,
    gpu_util_degraded_pct: float,
    queue_depth_degraded: int,
) -> SlmHealthSnapshot:
    """Parse the SLM health response body and compute the aggregated status.

    Field lookup uses defensive ``body.get(key)`` with ``None`` fallback so any
    absent field keeps ``None`` without raising. The status logic mirrors the
    four-level observability degraded-vs-down distinction:

    * ``down`` = not reachable (handled before this function).
    * ``degraded`` = reachable but model not loaded OR a threshold exceeded.
    * ``up`` = reachable, model loaded (or unknown), all thresholds OK.

    Args:
        body: Parsed JSON response from the SLM. May be empty (liveness-only).
        probe_latency_ms: Round-trip probe wall-clock time.
        probed_at: Probe initiation timestamp (UTC).
        trace_id: Probe trace ID.
        gpu_util_degraded_pct: GPU threshold.
        queue_depth_degraded: Queue depth threshold.

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
    if model_loaded is False:
        degraded = True
    if gpu_util_pct is not None and gpu_util_pct >= gpu_util_degraded_pct:
        degraded = True
    if queue_depth is not None and queue_depth >= queue_depth_degraded:
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
