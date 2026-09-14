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

    @pytest.mark.asyncio
    async def test_model_loaded_false_no_longer_degrades(self) -> None:
        """FRE-1474 AC-3: model_loaded is unobtainable from /health, so it no
        longer drives the degraded classification — even when a future body
        does carry it, without a generation check requested this stays up.
        """
        snap = await self._call(_make_response(body={"model_loaded": False, "gpu_util_pct": 10.0}))
        assert snap.status == "up"
        assert snap.model_loaded is False


class TestProbeSlmHealthGeneration:
    """FRE-1474: an opt-in generation-capability check folded into the tick."""

    async def _call_with_generation(
        self,
        health_resp: MagicMock,
        *,
        served_ids: frozenset[str] = frozenset({"test-model"}),
        completion_resp: MagicMock | Exception | None = None,
        base_url: str = "https://slm.example.com/v1",
        primary_resolution: tuple[tuple[str, str] | None, str | None] = (
            ("slm_local", "test-model"),
            None,
        ),
        active_count: int = 0,
    ) -> "SlmHealthSnapshot":
        from personal_agent.observability.slm_health.probe import probe_slm_health

        if completion_resp is None:
            completion_resp = _make_response(
                status_code=200, body={"choices": [{"message": {"content": "pong"}}]}
            )

        fetch_mock = AsyncMock(return_value=served_ids)

        with (
            patch("httpx.AsyncClient") as mock_client_cls,
            patch(
                "personal_agent.llm_client.provider_health.fetch_served_model_ids",
                new=fetch_mock,
            ),
            patch(
                "personal_agent.observability.slm_health.probe._resolve_primary_local_deployment",
                return_value=primary_resolution,
            ),
            patch(
                "personal_agent.observability.slm_health.probe._provider_active_count",
                return_value=active_count,
            ),
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=health_resp)
            if isinstance(completion_resp, Exception):
                mock_client.post = AsyncMock(side_effect=completion_resp)
            else:
                mock_client.post = AsyncMock(return_value=completion_resp)
            snap = await probe_slm_health(
                url="https://slm.example.com/health",
                trace_id="test-trace-gen",
                base_url=base_url,
            )
            self._last_post_mock = mock_client.post
            return snap

    @pytest.mark.asyncio
    async def test_ac1_backend_unreachable_while_health_answers_degrades(self) -> None:
        """AC-1: /health 200, generation completely fails → degraded, not up."""
        snap = await self._call_with_generation(
            _make_response(status_code=200),
            completion_resp=httpx.ConnectError("backend unreachable"),
        )
        assert snap.status in {"degraded", "down"}
        assert snap.status != "up"
        assert snap.reachable is True
        assert snap.generation_ok is False

    @pytest.mark.asyncio
    async def test_ac1_completion_5xx_degrades(self) -> None:
        """AC-1 variant: the completion call itself answers with a server error."""
        snap = await self._call_with_generation(
            _make_response(status_code=200),
            completion_resp=_make_response(status_code=503),
        )
        assert snap.status != "up"
        assert snap.generation_ok is False

    @pytest.mark.asyncio
    async def test_no_served_model_skips_not_degrades(self) -> None:
        """FRE-1474 master gate: nothing served is ambiguous, not a failure — skip."""
        snap = await self._call_with_generation(
            _make_response(status_code=200),
            served_ids=frozenset(),
        )
        assert snap.status == "up"
        assert snap.generation_ok is None
        assert snap.generation_skip_reason is not None
        assert "no model reported as served" in snap.generation_skip_reason

    @pytest.mark.asyncio
    async def test_primary_not_served_skips_not_degrades(self) -> None:
        """FRE-1474 master gate: a swapped-in pack (primary absent from the
        served set) must not read degraded — e.g. a manual swap for a
        model-testing study.
        """
        snap = await self._call_with_generation(
            _make_response(status_code=200),
            served_ids=frozenset({"some-other-pack"}),
        )
        assert snap.status == "up"
        assert snap.generation_ok is None
        assert snap.generation_skip_reason is not None
        assert "not currently served" in snap.generation_skip_reason

    @pytest.mark.asyncio
    async def test_busy_backend_skips_not_degrades_and_makes_no_completion_call(
        self,
    ) -> None:
        """FRE-1474 master gate: a healthy backend busy with a real request
        must not read degraded — the check skips before the network call.
        """
        snap = await self._call_with_generation(
            _make_response(status_code=200),
            active_count=2,
        )
        assert snap.status == "up"
        assert snap.generation_ok is None
        assert snap.generation_skip_reason is not None
        assert "busy" in snap.generation_skip_reason
        assert "active=2" in snap.generation_skip_reason
        self._last_post_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_does_not_set_latency_or_error(self) -> None:
        """A skip measures nothing and is not a probe error (snapshot.error
        stays None on a skip — only a definitive failure sets it).
        """
        snap = await self._call_with_generation(
            _make_response(status_code=200),
            active_count=1,
        )
        assert snap.generation_probe_latency_ms is None
        assert snap.error is None

    @pytest.mark.asyncio
    async def test_probes_primary_model_not_an_arbitrary_served_id(self) -> None:
        """FRE-1474 master gate: with multiple served models (e.g. a reranker
        beside the chat model), the probe targets the catalog's primary
        deployment specifically, not an arbitrary served id.
        """
        snap = await self._call_with_generation(
            _make_response(status_code=200),
            served_ids=frozenset({"chat-model", "rerank-model"}),
            primary_resolution=(("slm_local", "chat-model"), None),
        )
        assert snap.status == "up"
        assert snap.generation_ok is True
        self._last_post_mock.assert_awaited_once()
        posted_json = self._last_post_mock.await_args.kwargs["json"]
        assert posted_json["model"] == "chat-model"

    @pytest.mark.asyncio
    async def test_successful_generation_stays_up(self) -> None:
        """A real completion succeeding keeps status up and records generation_ok."""
        snap = await self._call_with_generation(_make_response(status_code=200))
        assert snap.status == "up"
        assert snap.generation_ok is True
        assert snap.generation_probe_latency_ms is not None

    @pytest.mark.asyncio
    async def test_ac2_ten_consecutive_healthy_probes_no_false_alarm(self) -> None:
        """AC-2: a healthy server produces up across >= 10 consecutive probes."""
        for _ in range(10):
            snap = await self._call_with_generation(_make_response(status_code=200))
            assert snap.status == "up"
            assert snap.generation_ok is True

    @pytest.mark.asyncio
    async def test_no_base_url_skips_generation_check(self) -> None:
        """Without base_url (the default), no generation check is attempted —
        provider_health.is_provider_available's call path is unaffected.
        """
        from personal_agent.observability.slm_health.probe import probe_slm_health

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=_make_response(status_code=200))
            snap = await probe_slm_health(
                url="https://slm.example.com/health", trace_id="test-trace-no-gen"
            )
        assert snap.generation_ok is None
        assert snap.generation_probe_latency_ms is None


class TestResolvePrimaryLocalDeployment:
    """FRE-1474 master gate: exercise the helper against a real ModelConfig,
    not a mocked shape — a bounced-three-times PR should encode API-shape
    assumptions as tests, not leave them to manual review.
    """

    def _config(self, *, roles: dict) -> "ModelConfig":
        from personal_agent.llm_client.models import (
            ModelConfig,
            ModelDefinition,
            ModeSpec,
            Placement,
            ProviderDefinition,
        )

        trivial_modes = {"default": ModeSpec()}
        models = {
            "local-chat": ModelDefinition(
                id="local-chat-model",
                context_length=8192,
                max_concurrency=1,
                default_timeout=30,
                provider="slm_local",
                dialect="llamacpp_qwen",
                modes=trivial_modes,
                default_mode="default",
            ),
            "cloud-chat": ModelDefinition(
                id="cloud-chat-model",
                context_length=8192,
                max_concurrency=1,
                default_timeout=30,
                provider="anthropic",
                dialect="anthropic_adaptive",
                modes=trivial_modes,
                default_mode="default",
            ),
        }
        return ModelConfig(
            providers={
                "slm_local": ProviderDefinition(placement=Placement.LOCAL, max_concurrency=2),
                "anthropic": ProviderDefinition(placement=Placement.CLOUD, max_concurrency=50),
            },
            models=models,
            roles=roles,
        )

    def test_resolves_local_primary(self) -> None:
        from personal_agent.llm_client.models import RoleBinding
        from personal_agent.observability.slm_health.probe import (
            _resolve_primary_local_deployment,
        )

        config = self._config(roles={"primary": RoleBinding(deployment="local-chat")})
        with patch("personal_agent.config.model_loader.load_model_config", return_value=config):
            resolved, reason = _resolve_primary_local_deployment()

        assert resolved == ("slm_local", "local-chat-model")
        assert reason is None

    def test_cloud_primary_is_not_resolved(self) -> None:
        from personal_agent.llm_client.models import RoleBinding
        from personal_agent.observability.slm_health.probe import (
            _resolve_primary_local_deployment,
        )

        config = self._config(roles={"primary": RoleBinding(deployment="cloud-chat")})
        with patch("personal_agent.config.model_loader.load_model_config", return_value=config):
            resolved, reason = _resolve_primary_local_deployment()

        assert resolved is None
        assert reason is not None
        assert "LOCAL" in reason

    def test_no_primary_role_binding(self) -> None:
        from personal_agent.observability.slm_health.probe import (
            _resolve_primary_local_deployment,
        )

        config = self._config(roles={})
        with patch("personal_agent.config.model_loader.load_model_config", return_value=config):
            resolved, reason = _resolve_primary_local_deployment()

        assert resolved is None
        assert reason is not None
        assert "primary" in reason

    def test_catalog_load_failure(self) -> None:
        from personal_agent.observability.slm_health.probe import (
            _resolve_primary_local_deployment,
        )

        with patch(
            "personal_agent.config.model_loader.load_model_config",
            side_effect=RuntimeError("catalog unreachable"),
        ):
            resolved, reason = _resolve_primary_local_deployment()

        assert resolved is None
        assert reason is not None
        assert "catalog unreachable" in reason


class TestProviderActiveCount:
    """FRE-1474 master gate: exercise the helper against a real
    InferenceConcurrencyController, not a mocked shape.
    """

    @pytest.mark.asyncio
    async def test_reflects_a_real_in_flight_slot(self) -> None:
        from personal_agent.llm_client.concurrency import (
            InferenceConcurrencyController,
            set_inference_concurrency_controller,
        )
        from personal_agent.observability.slm_health.probe import _provider_active_count

        controller = InferenceConcurrencyController()
        controller.register_provider("slm_local", max_concurrency=2)
        controller.register_model(role="test-primary", max_concurrency=2, provider="slm_local")

        set_inference_concurrency_controller(controller)
        try:
            assert _provider_active_count("slm_local") == 0
            async with controller.request_slot("test-primary"):
                assert _provider_active_count("slm_local") == 1
            assert _provider_active_count("slm_local") == 0
        finally:
            set_inference_concurrency_controller(None)

    def test_unregistered_provider_reads_zero(self) -> None:
        from personal_agent.llm_client.concurrency import (
            InferenceConcurrencyController,
            set_inference_concurrency_controller,
        )
        from personal_agent.observability.slm_health.probe import _provider_active_count

        set_inference_concurrency_controller(InferenceConcurrencyController())
        try:
            assert _provider_active_count("never-registered") == 0
        finally:
            set_inference_concurrency_controller(None)


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

    def test_degraded_generation_failed(self) -> None:
        """FRE-1474: generation_ok=False is reported ahead of the other reasons."""
        snap = self._snap(
            status="degraded",
            reachable=True,
            generation_ok=False,
            error="generation check failed: no model reported as served",
        )
        reason = snap.degrade_reason()
        assert reason is not None
        assert "generate" in reason.lower()

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
