"""Unit tests for the native fetch_url tool (ADR-0028 Phase 3, FRE-1297).

Tests use mocked httpx responses via ``create_guarded_http_client`` — no network
access required. Egress-guard behaviour (blocked domains, refusal before connection)
is covered separately by ``tests/test_security/test_egress_seams.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.fetch import fetch_url_executor, fetch_url_tool

_CTX = TraceContext.new_trace()


def _mock_html_response(
    body: str,
    content_type: str = "text/html; charset=utf-8",
    status_code: int = 200,
) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.is_error = status_code >= 400
    resp.text = body
    resp.headers = {"content-type": content_type}
    return resp


def _mock_client(response: MagicMock) -> AsyncMock:
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


# ── Tool definition ──────────────────────────────────────────────────────


def test_tool_definition() -> None:
    """Tool has correct metadata and is Tier-1 (no MCP dependency)."""
    assert fetch_url_tool.name == "fetch_url"
    assert fetch_url_tool.category == "network"
    assert fetch_url_tool.risk_level == "low"
    assert "NORMAL" in fetch_url_tool.allowed_modes
    param_names = {p.name for p in fetch_url_tool.parameters}
    assert {"url", "max_chars"} <= param_names


# ── Validation ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_url_raises() -> None:
    with pytest.raises(ToolExecutionError, match="'url' parameter is required"):
        await fetch_url_executor(url="", ctx=_CTX)


@pytest.mark.asyncio
async def test_invalid_url_scheme_raises() -> None:
    with pytest.raises(ToolExecutionError, match="Invalid URL"):
        await fetch_url_executor(url="ftp://example.com", ctx=_CTX)


@pytest.mark.asyncio
async def test_http_error_raises() -> None:
    resp = _mock_html_response("", status_code=404)
    with patch(
        "personal_agent.tools.fetch.create_guarded_http_client", return_value=_mock_client(resp)
    ):
        with pytest.raises(ToolExecutionError, match="HTTP 404"):
            await fetch_url_executor(url="https://example.com/missing", ctx=_CTX)


# ── HTML extraction ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_basic_html_extraction() -> None:
    """Script/style tags stripped; body text preserved."""
    html_body = """
    <html>
    <head><title>Test</title><style>body { color: red; }</style></head>
    <body>
      <script>alert('xss')</script>
      <h1>Hello World</h1>
      <p>This is a test paragraph.</p>
    </body>
    </html>
    """
    resp = _mock_html_response(html_body)
    with patch(
        "personal_agent.tools.fetch.create_guarded_http_client", return_value=_mock_client(resp)
    ):
        result = await fetch_url_executor(url="https://example.com", ctx=_CTX)

    assert "Hello World" in result["text"]
    assert "This is a test paragraph" in result["text"]
    assert "alert(" not in result["text"]
    assert "color: red" not in result["text"]
    assert result["url"] == "https://example.com"
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_truncation() -> None:
    """Long content is truncated to max_chars."""
    html_body = f"<p>{'x' * 20000}</p>"
    resp = _mock_html_response(html_body)
    with patch(
        "personal_agent.tools.fetch.create_guarded_http_client", return_value=_mock_client(resp)
    ):
        result = await fetch_url_executor(url="https://example.com", max_chars=100, ctx=_CTX)

    assert result["char_count"] == 100
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_plain_text_response_returned_as_is() -> None:
    """Non-HTML content-type returns body without HTML parsing."""
    resp = _mock_html_response("raw text content", content_type="text/plain")
    with patch(
        "personal_agent.tools.fetch.create_guarded_http_client", return_value=_mock_client(resp)
    ):
        result = await fetch_url_executor(url="https://example.com/data.txt", ctx=_CTX)

    assert result["text"] == "raw text content"


# ── Error paths ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_error_raises() -> None:
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    with patch("personal_agent.tools.fetch.create_guarded_http_client", return_value=client):
        with pytest.raises(ToolExecutionError, match="Cannot connect"):
            await fetch_url_executor(url="https://example.com", ctx=_CTX)


@pytest.mark.asyncio
async def test_timeout_raises() -> None:
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    with patch("personal_agent.tools.fetch.create_guarded_http_client", return_value=client):
        with pytest.raises(ToolExecutionError, match="timed out"):
            await fetch_url_executor(url="https://example.com", ctx=_CTX)
