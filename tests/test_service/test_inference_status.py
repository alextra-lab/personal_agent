"""Tests for the inference_status endpoint function."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# Import the endpoint function directly — same pattern as test_chat_hydration.py.
# This avoids spinning up the full FastAPI lifespan (DB, ES, etc.).
from personal_agent.service.app import inference_status


@pytest.mark.asyncio
async def test_status_up_when_health_returns_200() -> None:
    """Returns {"local": "up", "latency_ms": N} when slm_server /health returns 200."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()

    with (
        patch("personal_agent.service.app.settings") as mock_settings,
        patch("personal_agent.service.app.httpx.AsyncClient") as mock_http,
    ):
        mock_settings.cf_access_client_id = "test-id"
        mock_settings.cf_access_client_secret = "test-secret"
        mock_http_instance = AsyncMock()
        mock_http_instance.get = AsyncMock(return_value=mock_response)
        mock_http.return_value.__aenter__ = AsyncMock(return_value=mock_http_instance)
        mock_http.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await inference_status()

    assert result["local"] == "up"
    assert isinstance(result["latency_ms"], int)
    assert result["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_status_down_on_connect_timeout() -> None:
    """Returns {"local": "down", "latency_ms": None} on ConnectTimeout."""
    with (
        patch("personal_agent.service.app.settings") as mock_settings,
        patch("personal_agent.service.app.httpx.AsyncClient") as mock_http,
    ):
        mock_settings.cf_access_client_id = None
        mock_settings.cf_access_client_secret = None
        mock_http_instance = AsyncMock()
        mock_http_instance.get = AsyncMock(side_effect=httpx.ConnectTimeout("timed out"))
        mock_http.return_value.__aenter__ = AsyncMock(return_value=mock_http_instance)
        mock_http.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await inference_status()

    assert result["local"] == "down"
    assert result["latency_ms"] is None


@pytest.mark.asyncio
async def test_status_down_on_502() -> None:
    """Returns {"local": "down"} when cloudflared returns 502 (slm_server not running)."""
    mock_response = MagicMock()
    mock_response.status_code = 502
    mock_response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "502", request=MagicMock(), response=mock_response
        )
    )

    with (
        patch("personal_agent.service.app.settings") as mock_settings,
        patch("personal_agent.service.app.httpx.AsyncClient") as mock_http,
    ):
        mock_settings.cf_access_client_id = None
        mock_settings.cf_access_client_secret = None
        mock_http_instance = AsyncMock()
        mock_http_instance.get = AsyncMock(return_value=mock_response)
        mock_http.return_value.__aenter__ = AsyncMock(return_value=mock_http_instance)
        mock_http.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await inference_status()

    assert result["local"] == "down"
    assert result["latency_ms"] is None


@pytest.mark.asyncio
async def test_status_down_on_403_logs_warning() -> None:
    """Returns {"local": "down"} and logs a warning when CF returns 403."""
    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "403", request=MagicMock(), response=mock_response
        )
    )

    with (
        patch("personal_agent.service.app.settings") as mock_settings,
        patch("personal_agent.service.app.log") as mock_log,
        patch("personal_agent.service.app.httpx.AsyncClient") as mock_http,
    ):
        mock_settings.cf_access_client_id = "bad-id"
        mock_settings.cf_access_client_secret = "bad-secret"
        mock_http_instance = AsyncMock()
        mock_http_instance.get = AsyncMock(return_value=mock_response)
        mock_http.return_value.__aenter__ = AsyncMock(return_value=mock_http_instance)
        mock_http.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await inference_status()

    assert result["local"] == "down"
    mock_log.warning.assert_called_once()
    call_args = mock_log.warning.call_args
    assert "inference_tunnel_auth_failed" in call_args[0]
