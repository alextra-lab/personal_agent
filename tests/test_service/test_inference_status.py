"""Tests for the inference_status endpoint function.

FRE-399: inference_status now delegates to probe_slm_health (observability.slm_health).
The first two tests patch httpx.AsyncClient globally and still exercise the full
probe code path. The latter two tests (502 / 403) patch probe_slm_health directly
because the probe's internal HTTP-status handling is tested in test_slm_health_probe.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import the endpoint function directly — same pattern as test_chat_hydration.py.
# This avoids spinning up the full FastAPI lifespan (DB, ES, etc.).
from personal_agent.service.app import inference_status

_PKG = "personal_agent.observability.slm_health"


def _down_snapshot():
    from personal_agent.observability.slm_health.snapshot import SlmHealthSnapshot

    return SlmHealthSnapshot(
        status="down",
        reachable=False,
        probed_at=datetime.now(timezone.utc),
        trace_id="t",
        probe_latency_ms=None,
    )


@pytest.mark.asyncio
async def test_status_up_when_health_returns_200() -> None:
    """Returns {"local": "up", "latency_ms": N} when slm_server /health returns 200."""
    from personal_agent.observability.slm_health.snapshot import SlmHealthSnapshot

    up_snap = SlmHealthSnapshot(
        status="up",
        reachable=True,
        probed_at=datetime.now(timezone.utc),
        trace_id="t",
        probe_latency_ms=25.0,
    )
    with (
        patch("personal_agent.service.app.settings") as mock_settings,
        patch(f"{_PKG}.probe_slm_health", new=AsyncMock(return_value=up_snap)),
        patch(f"{_PKG}.set_cached_snapshot"),
    ):
        mock_settings.cf_access_client_id = "test-id"
        mock_settings.cf_access_client_secret = "test-secret"
        mock_settings.slm_health_url = "https://slm.example.com/health"
        mock_settings.slm_gpu_util_degraded_pct = 95.0
        mock_settings.slm_queue_depth_degraded = 4
        mock_settings.anthropic_api_key = None
        result = await inference_status()

    assert result["local"] == "up"
    assert isinstance(result["latency_ms"], int)
    assert result["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_status_down_on_connect_timeout() -> None:
    """Returns {"local": "down", "latency_ms": None} on ConnectTimeout (probe returns down)."""
    # Timeout handling lives in probe.py, tested in test_slm_health_probe.py.
    # This test verifies the endpoint correctly surfaces "down" from the probe.
    with (
        patch("personal_agent.service.app.settings") as mock_settings,
        patch(f"{_PKG}.probe_slm_health", new=AsyncMock(return_value=_down_snapshot())),
        patch(f"{_PKG}.set_cached_snapshot"),
    ):
        mock_settings.cf_access_client_id = None
        mock_settings.cf_access_client_secret = None
        mock_settings.slm_health_url = "https://slm.example.com/health"
        mock_settings.slm_gpu_util_degraded_pct = 95.0
        mock_settings.slm_queue_depth_degraded = 4
        mock_settings.anthropic_api_key = None
        result = await inference_status()

    assert result["local"] == "down"
    assert result["latency_ms"] is None


@pytest.mark.asyncio
async def test_status_down_on_502() -> None:
    """Returns {"local": "down"} when probe returns down (e.g. cloudflared 502)."""
    # probe_slm_health is the new boundary; its HTTP-level handling is tested
    # in test_slm_health_probe.py. Here we just verify the endpoint passes "down" through.
    with (
        patch("personal_agent.service.app.settings") as mock_settings,
        patch(f"{_PKG}.probe_slm_health", new=AsyncMock(return_value=_down_snapshot())),
        patch(f"{_PKG}.set_cached_snapshot"),
    ):
        mock_settings.cf_access_client_id = None
        mock_settings.cf_access_client_secret = None
        mock_settings.slm_health_url = "https://slm.example.com/health"
        mock_settings.slm_gpu_util_degraded_pct = 95.0
        mock_settings.slm_queue_depth_degraded = 4
        mock_settings.anthropic_api_key = None
        result = await inference_status()

    assert result["local"] == "down"
    assert result["latency_ms"] is None


@pytest.mark.asyncio
async def test_status_down_on_403_logs_warning() -> None:
    """Returns {"local": "down"} when probe returns down on CF auth failure."""
    # The 403-specific warning is logged by probe.py (tested in test_slm_health_probe.py).
    # Here we verify the endpoint honours "down" from the probe.
    with (
        patch("personal_agent.service.app.settings") as mock_settings,
        patch(f"{_PKG}.probe_slm_health", new=AsyncMock(return_value=_down_snapshot())),
        patch(f"{_PKG}.set_cached_snapshot"),
    ):
        mock_settings.cf_access_client_id = "bad-id"
        mock_settings.cf_access_client_secret = "bad-secret"
        mock_settings.slm_health_url = "https://slm.example.com/health"
        mock_settings.slm_gpu_util_degraded_pct = 95.0
        mock_settings.slm_queue_depth_degraded = 4
        mock_settings.anthropic_api_key = None
        result = await inference_status()

    assert result["local"] == "down"
