"""Unit tests for the SLM-health probe (FRE-399 / ADR-0083)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _make_response(
    *,
    status_code: int = 200,
    body: dict | None = None,
    text: str = "",
) -> MagicMock:
    """Build a mock httpx Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    if body is not None:
        resp.json.return_value = body
    else:
        resp.json.side_effect = Exception("not JSON")
    return resp


class TestProbeSlmHealth:
    """probe_slm_health returns a SlmHealthSnapshot regardless of outcome."""

    async def _call(self, resp: MagicMock, **kwargs) -> "SlmHealthSnapshot":
        from personal_agent.observability.slm_health.probe import probe_slm_health

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=resp)
            return await probe_slm_health(
                url="https://slm.example.com/health",
                cf_headers={},
                trace_id="test-trace-1",
                **kwargs,
            )

    @pytest.mark.asyncio
    async def test_liveness_only_body_returns_up(self) -> None:
        """A bare 200 OK with no JSON → up, all rich fields None."""
        snap = await self._call(_make_response(status_code=200))
        assert snap.status == "up"
        assert snap.reachable is True
        assert snap.model_loaded is None
        assert snap.gpu_util_pct is None
        assert snap.queue_depth is None

    @pytest.mark.asyncio
    async def test_rich_body_all_within_thresholds_returns_up(self) -> None:
        """Rich body with all fields below thresholds → up."""
        body = {
            "model_loaded": True,
            "gpu_util_pct": 60.0,
            "vram_used_mb": 10240,
            "vram_total_mb": 16384,
            "queue_depth": 1,
            "latency_ema_ms": 250.0,
            "model_id": "qwen3-14b",
        }
        snap = await self._call(_make_response(body=body))
        assert snap.status == "up"
        assert snap.model_loaded is True
        assert snap.gpu_util_pct == pytest.approx(60.0)
        assert snap.model_id == "qwen3-14b"
        assert snap.probe_latency_ms is not None

    @pytest.mark.asyncio
    async def test_model_not_loaded_returns_degraded(self) -> None:
        """model_loaded=False → degraded."""
        snap = await self._call(
            _make_response(body={"model_loaded": False, "gpu_util_pct": 10.0})
        )
        assert snap.status == "degraded"
        assert snap.model_loaded is False

    @pytest.mark.asyncio
    async def test_gpu_over_threshold_returns_degraded(self) -> None:
        """gpu_util_pct >= threshold → degraded."""
        snap = await self._call(
            _make_response(body={"gpu_util_pct": 98.0}),
            gpu_util_degraded_pct=95.0,
        )
        assert snap.status == "degraded"
        assert snap.gpu_util_pct == pytest.approx(98.0)

    @pytest.mark.asyncio
    async def test_gpu_exactly_at_threshold_returns_degraded(self) -> None:
        """gpu_util_pct exactly at threshold → degraded (inclusive)."""
        snap = await self._call(
            _make_response(body={"gpu_util_pct": 95.0}),
            gpu_util_degraded_pct=95.0,
        )
        assert snap.status == "degraded"

    @pytest.mark.asyncio
    async def test_queue_depth_over_threshold_returns_degraded(self) -> None:
        """queue_depth >= threshold → degraded."""
        snap = await self._call(
            _make_response(body={"queue_depth": 5}),
            queue_depth_degraded=4,
        )
        assert snap.status == "degraded"
        assert snap.queue_depth == 5

    @pytest.mark.asyncio
    async def test_403_returns_down_logs_auth_warning(self) -> None:
        """403 → down, reachable=False, auth warning logged."""
        import structlog.testing

        with structlog.testing.capture_logs() as logs:
            snap = await self._call(_make_response(status_code=403))
        assert snap.status == "down"
        assert snap.reachable is False
        assert any("inference_tunnel_auth_failed" in str(l) for l in logs)

    @pytest.mark.asyncio
    async def test_non_2xx_non_403_returns_down(self) -> None:
        """500 → down."""
        snap = await self._call(_make_response(status_code=500))
        assert snap.status == "down"
        assert snap.reachable is False
        assert snap.error is not None

    @pytest.mark.asyncio
    async def test_timeout_returns_down_never_raises(self) -> None:
        """TimeoutException → down, function does not raise."""
        from personal_agent.observability.slm_health.probe import probe_slm_health

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
            snap = await probe_slm_health(
                url="https://slm.example.com/health",
                cf_headers={},
                trace_id="test-timeout",
            )
        assert snap.status == "down"
        assert snap.reachable is False
        assert "timeout" in (snap.error or "")

    @pytest.mark.asyncio
    async def test_connection_error_returns_down_never_raises(self) -> None:
        """Connection error → down, function does not raise."""
        from personal_agent.observability.slm_health.probe import probe_slm_health

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=ConnectionRefusedError("refused"))
            snap = await probe_slm_health(
                url="https://slm.example.com/health",
                cf_headers={},
                trace_id="test-conn-err",
            )
        assert snap.status == "down"
        assert snap.reachable is False

    @pytest.mark.asyncio
    async def test_probe_latency_populated_on_success(self) -> None:
        """Successful probe populates probe_latency_ms."""
        snap = await self._call(_make_response(body={"model_loaded": True}))
        assert snap.probe_latency_ms is not None
        assert snap.probe_latency_ms >= 0.0

    @pytest.mark.asyncio
    async def test_probed_at_is_utc(self) -> None:
        """probed_at is always timezone-aware UTC."""
        snap = await self._call(_make_response(body={}))
        assert snap.probed_at.tzinfo is not None


class TestSlmHealthSnapshotDegradeReason:
    """SlmHealthSnapshot.degrade_reason() returns the right message."""

    def _snap(self, **kwargs) -> "SlmHealthSnapshot":
        from personal_agent.observability.slm_health.snapshot import SlmHealthSnapshot

        defaults = {
            "status": "up",
            "reachable": True,
            "probed_at": datetime.now(timezone.utc),
            "trace_id": "t",
        }
        defaults.update(kwargs)
        return SlmHealthSnapshot(**defaults)

    def test_up_returns_none(self) -> None:
        snap = self._snap(status="up")
        assert snap.degrade_reason() is None

    def test_down_with_error(self) -> None:
        snap = self._snap(status="down", reachable=False, error="timeout after 3s")
        reason = snap.degrade_reason()
        assert reason is not None
        assert "timeout" in reason

    def test_down_without_error(self) -> None:
        snap = self._snap(status="down", reachable=False)
        assert snap.degrade_reason() == "SLM unreachable"

    def test_degraded_model_not_loaded(self) -> None:
        snap = self._snap(status="degraded", reachable=True, model_loaded=False)
        assert snap.degrade_reason() == "model not loaded on SLM"

    def test_degraded_gpu_pinned(self) -> None:
        snap = self._snap(status="degraded", reachable=True, gpu_util_pct=98.3)
        reason = snap.degrade_reason()
        assert reason is not None
        assert "GPU" in reason

    def test_degraded_queue_saturated(self) -> None:
        snap = self._snap(status="degraded", reachable=True, queue_depth=7)
        reason = snap.degrade_reason()
        assert reason is not None
        assert "queue" in reason.lower() or "saturated" in reason.lower()

    def test_degraded_no_fields_returns_generic(self) -> None:
        snap = self._snap(status="degraded", reachable=True)
        assert snap.degrade_reason() == "SLM degraded"
