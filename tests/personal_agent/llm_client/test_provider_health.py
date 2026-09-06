"""Per-provider availability checks (ADR-0121 §3, AC-5 / FRE-918).

Cloud providers: config-only check (declared ``auth_env`` secret present on
settings) — no live reachability probe, matching the existing
``/api/inference/status`` cloud branch and the ADR's own "endpoint reachable,
required secret present" framing for a vendor-managed API. Local providers:
a live SLM-tunnel health probe, reusing ``probe_slm_health``.

``fetch_served_model_ids``/``check_local_served_ids`` (FRE-1415) are a second,
independent dimension: per-*model* served-id membership, layered on top of the
per-*provider* reachability above — a local provider can be reachable while
still serving only a subset of its declared catalog.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from personal_agent.llm_client.models import ModelConfig, ModelDefinition, ProviderDefinition
from personal_agent.llm_client.provider_health import (
    check_all_providers,
    check_local_served_ids,
    fetch_served_model_ids,
    is_provider_available,
)

_PKG = "personal_agent.llm_client.provider_health"


def _settings(**overrides: object) -> SimpleNamespace:
    base = {
        "anthropic_api_key": None,
        "openai_api_key": None,
        "voyage_api_key": None,
        "managed_embedding_token": None,
        "resolved_slm_health_url": "https://slm.example.com/health",
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


# ── fetch_served_model_ids — per-provider /v1/models probe (FRE-1415, AC-5) ───


def _mock_client(resp: MagicMock | None = None, *, raise_exc: Exception | None = None):
    """Patch ``httpx.AsyncClient`` so ``create_guarded_http_client`` returns a stub."""
    mock_client_cls = patch("httpx.AsyncClient")
    mock_cls = mock_client_cls.start()
    mock_client = AsyncMock()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    if raise_exc is not None:
        mock_client.get = AsyncMock(side_effect=raise_exc)
    else:
        mock_client.get = AsyncMock(return_value=resp)
    return mock_client_cls


def _response(*, status_code: int = 200, body: object = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    if 200 <= status_code < 300:
        resp.raise_for_status = MagicMock(return_value=None)
    else:
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("bad status", request=MagicMock(), response=resp)
        )
    if body is not None:
        resp.json.return_value = body
    else:
        resp.json.side_effect = ValueError("not JSON")
    return resp


@pytest.mark.asyncio
async def test_fetch_served_model_ids_parses_data_ids():
    """A well-formed OpenAI-style {"data": [...]} body yields the served ids."""
    body = {"data": [{"id": "unsloth/qwen3.8-flash-next"}, {"id": "unsloth/qwen3.6-35-A3B"}]}
    patcher = _mock_client(_response(body=body))
    try:
        result = await fetch_served_model_ids("https://slm.example.com/v1", trace_id="t")
    finally:
        patcher.stop()
    assert result == frozenset({"unsloth/qwen3.8-flash-next", "unsloth/qwen3.6-35-A3B"})


@pytest.mark.asyncio
async def test_fetch_served_model_ids_strips_url_trailing_slash():
    """Base URL trailing slash does not produce a double slash before 'models'."""
    patcher = _mock_client(_response(body={"data": []}))
    try:
        result = await fetch_served_model_ids("https://slm.example.com/v1/", trace_id="t")
    finally:
        patcher.stop()
    assert result == frozenset()


@pytest.mark.asyncio
async def test_fetch_served_model_ids_empty_data_is_genuinely_zero_served():
    """{"data": []} is a well-formed response meaning zero models served — not a failure."""
    patcher = _mock_client(_response(body={"data": []}))
    try:
        result = await fetch_served_model_ids("https://slm.example.com/v1", trace_id="t")
    finally:
        patcher.stop()
    assert result == frozenset()


@pytest.mark.asyncio
async def test_fetch_served_model_ids_never_raises_on_timeout():
    """A timeout fails closed to an empty frozenset, never propagates (AC-5)."""
    patcher = _mock_client(raise_exc=httpx.TimeoutException("timed out"))
    try:
        result = await fetch_served_model_ids("https://slm.example.com/v1", trace_id="t")
    finally:
        patcher.stop()
    assert result == frozenset()


@pytest.mark.asyncio
async def test_fetch_served_model_ids_never_raises_on_http_error():
    """A non-2xx status fails closed to an empty frozenset (AC-5)."""
    patcher = _mock_client(_response(status_code=503))
    try:
        result = await fetch_served_model_ids("https://slm.example.com/v1", trace_id="t")
    finally:
        patcher.stop()
    assert result == frozenset()


@pytest.mark.asyncio
async def test_fetch_served_model_ids_never_raises_on_malformed_json():
    """A non-JSON body fails closed to an empty frozenset (AC-5)."""
    patcher = _mock_client(_response(body=None))
    try:
        result = await fetch_served_model_ids("https://slm.example.com/v1", trace_id="t")
    finally:
        patcher.stop()
    assert result == frozenset()


@pytest.mark.asyncio
async def test_fetch_served_model_ids_missing_data_key_fails_closed():
    """A body with no 'data' key at all is malformed, not a genuine empty list (AC-5)."""
    patcher = _mock_client(_response(body={"unexpected": "shape"}))
    try:
        result = await fetch_served_model_ids("https://slm.example.com/v1", trace_id="t")
    finally:
        patcher.stop()
    assert result == frozenset()


@pytest.mark.asyncio
async def test_fetch_served_model_ids_data_not_a_list_fails_closed():
    """A non-list 'data' value is malformed (AC-5)."""
    patcher = _mock_client(_response(body={"data": "not-a-list"}))
    try:
        result = await fetch_served_model_ids("https://slm.example.com/v1", trace_id="t")
    finally:
        patcher.stop()
    assert result == frozenset()


@pytest.mark.asyncio
async def test_fetch_served_model_ids_entry_missing_id_fails_closed():
    """An entry with no (or a blank) 'id' field is malformed (AC-5)."""
    patcher = _mock_client(_response(body={"data": [{"not_id": "x"}]}))
    try:
        result = await fetch_served_model_ids("https://slm.example.com/v1", trace_id="t")
    finally:
        patcher.stop()
    assert result == frozenset()


# ── check_local_served_ids — per-LOCAL-provider fan-out session_api uses ──────


@pytest.mark.asyncio
async def test_check_local_served_ids_covers_only_local_providers():
    """Cloud providers never get a served-ids probe — they have no /v1/models concept."""
    config = ModelConfig(
        providers={
            "slm_local": ProviderDefinition(
                base_url="https://slm.example.com/v1", placement="local", max_concurrency=2
            ),
            "anthropic": ProviderDefinition(
                auth_env="anthropic_api_key", placement="cloud", max_concurrency=50
            ),
        },
        models={
            "m": ModelDefinition(
                id="unsloth/qwen3.8-flash-next",
                provider="slm_local",
                context_length=100,
                max_concurrency=1,
                default_timeout=10,
            )
        },
    )
    with patch(
        f"{_PKG}.fetch_served_model_ids",
        new_callable=AsyncMock,
        return_value=frozenset({"unsloth/qwen3.8-flash-next"}),
    ) as fetch_mock:
        result = await check_local_served_ids(config, trace_id="t")
    assert result == {"slm_local": frozenset({"unsloth/qwen3.8-flash-next"})}
    fetch_mock.assert_awaited_once_with("https://slm.example.com/v1", trace_id="t")


@pytest.mark.asyncio
async def test_check_local_served_ids_missing_base_url_fails_closed():
    """A local provider with no configured base_url probes to empty, not a crash."""
    config = ModelConfig(
        providers={
            "slm_local": ProviderDefinition(base_url=None, placement="local", max_concurrency=2),
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
    with patch(f"{_PKG}.fetch_served_model_ids", new_callable=AsyncMock) as fetch_mock:
        result = await check_local_served_ids(config, trace_id="t")
    assert result == {"slm_local": frozenset()}
    fetch_mock.assert_not_awaited()
