"""Tests for the commit-time served-envelope probe (FRE-512 / ADR-0089 D5).

The probe issues one real GET through the edge after every artifact commit and
emits ``artifact_envelope_integrity``. It is never load-bearing: no exception,
timeout, or denial may escape into the commit path — and it never reads the
response body (the D1/D5 scope boundary).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import structlog.testing

from tests.observability.artifact_envelope.test_verifier import GOOD_CSP

IDENTITY: dict[str, Any] = {
    "public_url": "https://artifacts.frenchforet.com/aaaabbbb-0000-0000-0000-000000000000",
    "artifact_id": "aaaabbbb-0000-0000-0000-000000000000",
    "slug": "test-artifact",
    "content_type": "text/html; charset=utf-8",
    "trace_id": "trace-512",
    "session_id": "session-512",
    "user_id": "user-512",
}


def _response(status_code: int, headers: list[tuple[str, str]]) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = httpx.Headers(headers)
    resp.aread = AsyncMock()
    return resp


def _good_response() -> MagicMock:
    return _response(
        200,
        [
            ("Content-Security-Policy", GOOD_CSP),
            ("Content-Type", "text/html; charset=utf-8"),
            ("X-Content-Type-Options", "nosniff"),
        ],
    )


def _client_with(resp_or_exc: Any) -> MagicMock:
    """Mock httpx.AsyncClient whose .stream() yields resp_or_exc."""
    client = AsyncMock()
    stream_cm = MagicMock()
    if isinstance(resp_or_exc, Exception):
        stream_cm.__aenter__ = AsyncMock(side_effect=resp_or_exc)
    else:
        stream_cm.__aenter__ = AsyncMock(return_value=resp_or_exc)
    stream_cm.__aexit__ = AsyncMock(return_value=False)
    client.stream = MagicMock(return_value=stream_cm)
    return client


async def _run(resp_or_exc: Any) -> tuple[list[dict[str, Any]], MagicMock]:
    from personal_agent.observability.artifact_envelope.probe import probe_served_envelope

    client = _client_with(resp_or_exc)
    with patch("httpx.AsyncClient") as client_cls:
        client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        with structlog.testing.capture_logs() as logs:
            await probe_served_envelope(**IDENTITY)
    return logs, client


def _event(logs: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [e for e in logs if e["event"] == "artifact_envelope_integrity"]
    assert len(matches) == 1, f"expected exactly one envelope event, got {logs}"
    return matches[0]


class TestProbeHappyPath:
    @pytest.mark.asyncio
    async def test_verified_serve_emits_info_event_with_identity(self) -> None:
        logs, _ = await _run(_good_response())
        event = _event(logs)
        assert event["log_level"] == "info"
        assert event["probe_status"] == "verified"
        assert event["envelope_ok"] is True
        assert event["trace_id"] == "trace-512"
        assert event["session_id"] == "session-512"
        assert event["user_id"] == "user-512"
        assert event["artifact_id"] == IDENTITY["artifact_id"]
        assert event["slug"] == "test-artifact"
        assert event["http_status"] == 200
        assert event["probe_duration_ms"] >= 0

    @pytest.mark.asyncio
    async def test_body_is_never_read(self) -> None:
        """The probe consumes headers only — the scope boundary in action."""
        resp = _good_response()
        await _run(resp)
        resp.aread.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_with_no_redirect_follow(self) -> None:
        _, client = await _run(_good_response())
        args, kwargs = client.stream.call_args
        assert args[0] == "GET"
        assert args[1] == IDENTITY["public_url"]


class TestProbeAlarm:
    @pytest.mark.asyncio
    async def test_directive_missing_serve_emits_error_naming_it(self) -> None:
        """The D5 alarm condition: a bare delivery is loud."""
        mutated = GOOD_CSP.replace("connect-src 'none'; ", "")
        resp = _response(
            200,
            [
                ("Content-Security-Policy", mutated),
                ("Content-Type", "text/html; charset=utf-8"),
                ("X-Content-Type-Options", "nosniff"),
            ],
        )
        logs, _ = await _run(resp)
        event = _event(logs)
        assert event["log_level"] == "error"
        assert event["envelope_ok"] is False
        assert "connect-src" in event["missing_directives"]
        assert "csp_directive_missing" in event["envelope_failures"]

    @pytest.mark.asyncio
    async def test_csp_absent_serve_emits_error(self) -> None:
        resp = _response(200, [("Content-Type", "text/html; charset=utf-8")])
        logs, _ = await _run(resp)
        event = _event(logs)
        assert event["log_level"] == "error"
        assert event["csp_present"] is False
        assert "missing_csp" in event["envelope_failures"]


class TestProbeDegradedPaths:
    @pytest.mark.asyncio
    async def test_access_denied_emits_warning_not_alarm(self) -> None:
        """Until the service token is authorized on the artifacts Access app,
        the probe must be visible-but-distinct from the CSP alarm.
        """
        resp = _response(
            302,
            [("Location", "https://frenchforest.cloudflareaccess.com/cdn-cgi/access/login/x")],
        )
        logs, _ = await _run(resp)
        event = _event(logs)
        assert event["log_level"] == "warning"
        assert event["probe_status"] == "unverified_access_denied"
        assert event["http_status"] == 302

    @pytest.mark.asyncio
    async def test_timeout_emits_probe_failed_and_never_raises(self) -> None:
        logs, _ = await _run(httpx.TimeoutException("timed out"))
        event = _event(logs)
        assert event["log_level"] == "warning"
        assert event["probe_status"] == "probe_failed"
        assert "timed out" in event["error_message"]

    @pytest.mark.asyncio
    async def test_unexpected_exception_never_raises(self) -> None:
        logs, _ = await _run(RuntimeError("boom"))
        event = _event(logs)
        assert event["probe_status"] == "probe_failed"
        assert "boom" in event["error_message"]


class TestServiceTokenHeaders:
    @pytest.mark.asyncio
    async def test_token_headers_attached_when_configured(self, monkeypatch: Any) -> None:
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "cf_access_client_id", "client-id-1")
        monkeypatch.setattr(settings, "cf_access_client_secret", "secret-1")
        _, client = await _run(_good_response())
        _, kwargs = client.stream.call_args
        assert kwargs["headers"]["CF-Access-Client-Id"] == "client-id-1"
        assert kwargs["headers"]["CF-Access-Client-Secret"] == "secret-1"

    @pytest.mark.asyncio
    async def test_no_token_headers_when_unconfigured(self, monkeypatch: Any) -> None:
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "cf_access_client_id", None)
        monkeypatch.setattr(settings, "cf_access_client_secret", None)
        _, client = await _run(_good_response())
        _, kwargs = client.stream.call_args
        assert "CF-Access-Client-Id" not in kwargs.get("headers", {})
