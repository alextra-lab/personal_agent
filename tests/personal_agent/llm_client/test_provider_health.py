"""Per-provider availability checks (ADR-0121 §3, AC-5 / FRE-918).

Cloud providers: config-only check (declared ``auth_env`` secret present on
settings) — no live reachability probe, matching the existing
``/api/inference/status`` cloud branch and the ADR's own "endpoint reachable,
required secret present" framing for a vendor-managed API. Local providers:
a live SLM-tunnel health probe, reusing ``probe_slm_health``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.llm_client.models import ProviderDefinition
from personal_agent.llm_client.provider_health import (
    check_all_providers,
    is_provider_available,
)

_PKG = "personal_agent.llm_client.provider_health"


def _settings(**overrides: object) -> SimpleNamespace:
    base = {
        "anthropic_api_key": None,
        "openai_api_key": None,
        "voyage_api_key": None,
        "managed_embedding_token": None,
        "cf_access_client_id": None,
        "cf_access_client_secret": None,
        "slm_health_url": "https://slm.example.com/health",
        "slm_gpu_util_degraded_pct": 95.0,
        "slm_queue_depth_degraded": 4,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _snapshot(status: str):
    from personal_agent.observability.slm_health.snapshot import SlmHealthSnapshot

    return SlmHealthSnapshot(
        status=status,
        reachable=status != "down",
        probed_at=datetime.now(timezone.utc),
        trace_id="t",
        probe_latency_ms=12.0,
    )


# ── Cloud providers: secret presence only, no live probe ──────────────────────


@pytest.mark.asyncio
async def test_cloud_provider_available_when_secret_present():
    """A cloud provider with its auth_env credential set is available."""
    provider = ProviderDefinition(
        auth_env="anthropic_api_key", placement="cloud", max_concurrency=50
    )
    settings = _settings(anthropic_api_key="sk-live")
    assert await is_provider_available(provider, settings) is True


@pytest.mark.asyncio
async def test_cloud_provider_unavailable_when_secret_missing():
    """A cloud provider with no credential configured is unavailable."""
    provider = ProviderDefinition(
        auth_env="anthropic_api_key", placement="cloud", max_concurrency=50
    )
    settings = _settings(anthropic_api_key=None)
    assert await is_provider_available(provider, settings) is False


@pytest.mark.asyncio
async def test_cloud_provider_with_no_auth_env_is_always_available():
    """A cloud provider declaring no auth requirement is always available."""
    provider = ProviderDefinition(auth_env=None, placement="cloud", max_concurrency=50)
    settings = _settings()
    assert await is_provider_available(provider, settings) is True


@pytest.mark.asyncio
async def test_cloud_provider_does_not_make_a_network_call():
    """Cloud availability is a config check only — no probe_slm_health call."""
    provider = ProviderDefinition(auth_env="openai_api_key", placement="cloud", max_concurrency=50)
    settings = _settings(openai_api_key="sk-live")
    with patch(f"{_PKG}.probe_slm_health", new_callable=AsyncMock) as probe_mock:
        await is_provider_available(provider, settings)
    probe_mock.assert_not_awaited()


# ── Local providers: live SLM probe, "down" excludes, "up"/"degraded" don't ───


@pytest.mark.asyncio
async def test_local_provider_available_when_probe_up():
    """A local provider probed 'up' is available."""
    provider = ProviderDefinition(placement="local", max_concurrency=2)
    settings = _settings()
    with patch(f"{_PKG}.probe_slm_health", new_callable=AsyncMock, return_value=_snapshot("up")):
        assert await is_provider_available(provider, settings) is True


@pytest.mark.asyncio
async def test_local_provider_available_when_probe_degraded():
    """A local provider probed 'degraded' still counts as available — only 'down' excludes."""
    provider = ProviderDefinition(placement="local", max_concurrency=2)
    settings = _settings()
    with patch(
        f"{_PKG}.probe_slm_health", new_callable=AsyncMock, return_value=_snapshot("degraded")
    ):
        assert await is_provider_available(provider, settings) is True


@pytest.mark.asyncio
async def test_local_provider_unavailable_when_probe_down():
    """A local provider probed 'down' is unavailable."""
    provider = ProviderDefinition(placement="local", max_concurrency=2)
    settings = _settings()
    with patch(f"{_PKG}.probe_slm_health", new_callable=AsyncMock, return_value=_snapshot("down")):
        assert await is_provider_available(provider, settings) is False


# ── check_all_providers — the per-provider fan-out the read endpoint uses ─────


@pytest.mark.asyncio
async def test_check_all_providers_returns_one_entry_per_provider():
    """check_all_providers covers every declared provider, local and cloud."""
    from personal_agent.llm_client.models import ModelConfig, ModelDefinition

    config = ModelConfig(
        providers={
            "slm_local": ProviderDefinition(placement="local", max_concurrency=2),
            "anthropic": ProviderDefinition(
                auth_env="anthropic_api_key", placement="cloud", max_concurrency=50
            ),
        },
        models={
            "m": ModelDefinition(
                id="m",
                provider="slm_local",
                context_length=100,
                max_concurrency=1,
                default_timeout=10,
            )
        },
    )
    settings = _settings(anthropic_api_key="sk-live")
    with patch(f"{_PKG}.probe_slm_health", new_callable=AsyncMock, return_value=_snapshot("up")):
        result = await check_all_providers(config, settings)
    assert result == {"slm_local": True, "anthropic": True}
