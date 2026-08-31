"""FRE-1341 — `check_gateway_freshness` / `assert_gateway_fresh` against a fake gateway.

Uses `httpx.MockTransport` (established pattern — see `tests/test_eval/test_fre817_embed_ovh.py`)
so no live network or Docker is required. `compute_build_fingerprint` is monkeypatched to a
fixed value so these tests aren't coupled to this repo's actual on-disk content.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from scripts.eval import gateway_freshness
from scripts.eval.gateway_freshness import (
    GatewayStaleError,
    assert_gateway_fresh,
    check_gateway_freshness,
)


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


@pytest.fixture(autouse=True)
def _fixed_local_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gateway_freshness, "compute_build_fingerprint", lambda repo_root: "local-abc123"
    )


def _health_handler(build_fingerprint: object) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "healthy", "build_fingerprint": build_fingerprint}
        )

    return httpx.MockTransport(handler)


class TestCheckGatewayFreshness:
    @pytest.mark.asyncio
    async def test_matching_fingerprint_is_fresh(self, tmp_path: Path) -> None:
        async with _client(_health_handler("local-abc123")) as client:
            result = await check_gateway_freshness(client, "http://localhost:9002", tmp_path)
        assert result.fresh is True
        assert result.running_fingerprint == "local-abc123"
        assert result.expected_fingerprint == "local-abc123"

    @pytest.mark.asyncio
    async def test_mismatched_fingerprint_is_stale(self, tmp_path: Path) -> None:
        async with _client(_health_handler("stale-old999")) as client:
            result = await check_gateway_freshness(client, "http://localhost:9002", tmp_path)
        assert result.fresh is False
        assert result.running_fingerprint == "stale-old999"
        assert result.expected_fingerprint == "local-abc123"

    @pytest.mark.asyncio
    async def test_missing_build_fingerprint_is_stale_not_silently_fresh(
        self, tmp_path: Path
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "healthy"})

        async with _client(httpx.MockTransport(handler)) as client:
            result = await check_gateway_freshness(client, "http://localhost:9002", tmp_path)
        assert result.fresh is False
        assert result.running_fingerprint is None

    @pytest.mark.asyncio
    async def test_unknown_marker_is_stale_not_silently_fresh(self, tmp_path: Path) -> None:
        async with _client(_health_handler("unknown")) as client:
            result = await check_gateway_freshness(client, "http://localhost:9002", tmp_path)
        assert result.fresh is False

    @pytest.mark.asyncio
    async def test_unreachable_gateway_is_stale_not_silently_fresh(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        async with _client(httpx.MockTransport(handler)) as client:
            result = await check_gateway_freshness(client, "http://localhost:9002", tmp_path)
        assert result.fresh is False
        assert result.running_fingerprint is None

    @pytest.mark.asyncio
    async def test_non_2xx_is_stale_not_silently_fresh(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"status": "unhealthy"})

        async with _client(httpx.MockTransport(handler)) as client:
            result = await check_gateway_freshness(client, "http://localhost:9002", tmp_path)
        assert result.fresh is False


class TestAssertGatewayFresh:
    @pytest.mark.asyncio
    async def test_fresh_returns_result(self, tmp_path: Path) -> None:
        async with _client(_health_handler("local-abc123")) as client:
            result = await assert_gateway_fresh(client, "http://localhost:9002", tmp_path)
        assert result.fresh is True

    @pytest.mark.asyncio
    async def test_stale_raises_naming_both_fingerprints_and_rebuild_command(
        self, tmp_path: Path
    ) -> None:
        async with _client(_health_handler("stale-old999")) as client:
            with pytest.raises(GatewayStaleError) as exc_info:
                await assert_gateway_fresh(client, "http://localhost:9002", tmp_path)
        message = str(exc_info.value)
        assert "stale-old999" in message
        assert "local-abc123" in message
        assert "docker compose" in message
        assert "build seshat-gateway-control seshat-gateway-treatment" in message
