"""SLM-health snapshot data model (FRE-399 Layer 3 / ADR-0083).

:class:`SlmHealthSnapshot` is the canonical frozen document for one SLM-health
probe result. It is written to Elasticsearch (via :mod:`sink`), cached in
process memory (via :mod:`cache`), and returned by the enriched
``/api/inference/status`` endpoint.

The schema is intentionally **additive / nullable-on-arrival**: the current
Mac SLM ``/health`` response is a liveness-only ``200 OK`` with an opaque body.
All rich fields (GPU util, VRAM, queue depth, model-loaded) default to ``None``
and are populated only once the Mac-side enrichment ticket lands, so the monitor
ships and operates correctly today without waiting for that child ticket.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class SlmHealthSnapshot(BaseModel):
    """One SLM-health probe result — liveness + optional rich telemetry.

    Attributes:
        status: Coarse health verdict: ``"up"`` (reachable, all thresholds OK),
            ``"degraded"`` (reachable but a threshold exceeded or model not
            loaded), or ``"down"`` (unreachable / auth error).
        reachable: Whether the HTTP probe received a non-error response.
        model_loaded: Whether the inference model is loaded and ready. ``None``
            when the SLM does not yet expose this field.
        gpu_util_pct: GPU utilisation in percent (0–100). ``None`` when not
            exposed.
        vram_used_mb: VRAM consumed in MiB. ``None`` when not exposed.
        vram_total_mb: Total VRAM in MiB. ``None`` when not exposed.
        queue_depth: Number of requests queued at the SLM. ``None`` when not
            exposed.
        latency_ema_ms: Exponential moving average of recent inference latency
            in milliseconds, as reported by the SLM. ``None`` when not exposed.
        model_id: Active model identifier string. ``None`` when not exposed.
        probe_latency_ms: Round-trip latency of the probe HTTP call itself, in
            milliseconds. ``None`` when the probe did not complete.
        probed_at: UTC timestamp when the probe was initiated.
        trace_id: Probe's own :class:`~personal_agent.telemetry.trace.SystemTraceContext`
            trace ID — the probe is itself joinable.
        error: Error string when the probe failed. ``None`` on success.
        kind: Fixed sentinel ``"system:slm_health_probe"`` for index routing.
    """

    model_config = ConfigDict(frozen=True)

    status: Literal["up", "degraded", "down"]
    reachable: bool
    model_loaded: bool | None = None
    gpu_util_pct: float | None = None
    vram_used_mb: float | None = None
    vram_total_mb: float | None = None
    queue_depth: int | None = None
    latency_ema_ms: float | None = None
    model_id: str | None = None
    probe_latency_ms: float | None = None
    probed_at: datetime
    trace_id: str
    error: str | None = None
    kind: str = "system:slm_health_probe"

    def degrade_reason(self) -> str | None:
        """Return a human-readable reason string when status is degraded/down.

        Checks rich fields in priority order and returns the most actionable
        reason. Returns ``None`` when status is ``"up"`` or when no rich fields
        are populated to explain the degradation.

        Returns:
            A short phrase suitable for appending to a classified error message,
            e.g. ``"model not loaded on SLM"`` or ``"GPU pinned (98.3%)"``; or
            ``None`` when the status is healthy or unexplained.
        """
        if self.status == "up":
            return None
        if self.status == "down":
            if self.error:
                return f"SLM unreachable ({self.error})"
            return "SLM unreachable"
        # degraded
        if self.model_loaded is False:
            return "model not loaded on SLM"
        if self.gpu_util_pct is not None and self.gpu_util_pct >= 95.0:
            return f"GPU pinned ({self.gpu_util_pct:.1f}%)"
        if self.queue_depth is not None and self.queue_depth >= 4:
            return f"SLM queue saturated (depth={self.queue_depth})"
        return "SLM degraded"
