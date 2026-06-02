"""Tests for the enriched /api/inference/status endpoint (FRE-399 / ADR-0083).

Calls the endpoint function directly (no TestClient/lifespan) — same pattern as
tests/test_service/test_inference_status.py.

inference_status does a local import:
    from personal_agent.observability.slm_health import probe_slm_health, set_cached_snapshot

So patches must target the package-level name
(personal_agent.observability.slm_health.probe_slm_health) not the sub-module.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.service.app import inference_status

_PKG = "personal_agent.observability.slm_health"


def _make_snapshot(status: str = "up", **kwargs) -> "SlmHealthSnapshot":
    from personal_agent.observability.slm_health.snapshot import SlmHealthSnapshot

    defaults: dict = {
        "status": status,
        "reachable": status != "down",
        "probed_at": datetime.now(timezone.utc),
        "trace_id": "test-endpoint",
        "probe_latency_ms": 42.0,
    }
    defaults.update(kwargs)
    return SlmHealthSnapshot(**defaults)  # type: ignore[arg-type]


async def _call(snap, *, profile: str = "local") -> dict:
    """Invoke inference_status() with a stubbed probe."""
    with (
        patch("personal_agent.service.app.settings") as mock_settings,
        patch(f"{_PKG}.probe_slm_health", new=AsyncMock(return_value=snap)),
        patch(f"{_PKG}.set_cached_snapshot"),
    ):
        mock_settings.cf_access_client_id = "cid"
        mock_settings.cf_access_client_secret = "csec"
        mock_settings.slm_health_url = "https://slm.example.com/health"
        mock_settings.slm_gpu_util_degraded_pct = 95.0
        mock_settings.slm_queue_depth_degraded = 4
        mock_settings.anthropic_api_key = None
        return await inference_status(profile=profile)


class TestInferenceStatusEnriched:
    """inference_status enriches its response and preserves backward compat."""

    @pytest.mark.asyncio
    async def test_backward_compatible_keys_present(self) -> None:
        """status, profile, local, latency_ms must always be present."""
        snap = _make_snapshot("up")
        result = await _call(snap)
        assert "status" in result
        assert "profile" in result
        assert "local" in result
        assert "latency_ms" in result

    @pytest.mark.asyncio
    async def test_up_response(self) -> None:
        snap = _make_snapshot("up", probe_latency_ms=55.0)
        result = await _call(snap)
        assert result["status"] == "up"
        assert result["local"] == "up"
        assert result["profile"] == "local"
        assert result["latency_ms"] == 55

    @pytest.mark.asyncio
    async def test_degraded_maps_local_to_degraded(self) -> None:
        """degraded SLM must surface as local='degraded', not 'down'."""
        snap = _make_snapshot("degraded", gpu_util_pct=98.0)
        result = await _call(snap)
        assert result["status"] == "degraded"
        assert result["local"] == "degraded"

    @pytest.mark.asyncio
    async def test_down_response(self) -> None:
        snap = _make_snapshot("down", reachable=False, probe_latency_ms=None)
        result = await _call(snap)
        assert result["status"] == "down"
        assert result["local"] == "down"
        assert result["latency_ms"] is None

    @pytest.mark.asyncio
    async def test_enriched_fields_present_for_rich_snapshot(self) -> None:
        snap = _make_snapshot(
            "up",
            gpu_util_pct=60.0,
            queue_depth=1,
            model_loaded=True,
        )
        result = await _call(snap)
        assert result["gpu_util_pct"] == pytest.approx(60.0)
        assert result["queue_depth"] == 1
        assert result["model_loaded"] is True
        assert result["degrade_reason"] is None  # "up" → no reason

    @pytest.mark.asyncio
    async def test_enriched_fields_none_for_liveness_only(self) -> None:
        """Liveness-only snapshot (all rich fields None) → enriched keys present but None."""
        snap = _make_snapshot("up")
        result = await _call(snap)
        assert result["gpu_util_pct"] is None
        assert result["queue_depth"] is None
        assert result["model_loaded"] is None
        assert result["degrade_reason"] is None

    @pytest.mark.asyncio
    async def test_cloud_profile_skips_probe(self) -> None:
        """cloud branch must not call probe_slm_health at all."""
        with (
            patch("personal_agent.service.app.settings") as mock_settings,
            patch(
                f"{_PKG}.probe_slm_health",
                new=AsyncMock(side_effect=AssertionError("should not be called")),
            ),
        ):
            mock_settings.anthropic_api_key = "sk-test"
            result = await inference_status(profile="cloud")

        assert result["profile"] == "cloud"
        assert result["status"] in ("up", "down")

    @pytest.mark.asyncio
    async def test_degrade_reason_present_when_degraded(self) -> None:
        snap = _make_snapshot("degraded", model_loaded=False)
        result = await _call(snap)
        assert result["degrade_reason"] is not None
        assert "model" in result["degrade_reason"].lower()
