"""Unit tests for the native fetch_url tool (ADR-0028 Phase 3, FRE-1297).

Tests use mocked httpx responses via ``create_guarded_http_client`` — no network
access required. Egress-guard behaviour (blocked domains, refusal before connection)
is covered separately by ``tests/test_security/test_egress_seams.py``.
"""

from __future__ import annotations

import asyncio
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.fetch import (
    _resolves_to_private_or_internal,
    fetch_url_executor,
    fetch_url_tool,
)

_CTX = TraceContext.new_trace()


def _addrinfo(ip: str) -> list[tuple[object, ...]]:
    """Build a minimal ``getaddrinfo``-shaped result for one IP address."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip, 0))]


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
    # "medium" (the network category's own default), not web_search's "low" override —
    # fetch_url's target host is fully model-chosen and unbounded, unlike web_search's
    # fixed SearXNG proxy (PR #957 bounce).
    assert fetch_url_tool.risk_level == "medium"
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
async def test_navigation_and_footer_boilerplate_is_excluded() -> None:
    """ADR-0138 D3(c): nav/footer text must not count as the page's content.

    A site-wide nav listing every section name would otherwise satisfy containment for a
    claim the article never makes — the citation-theatre shape D3(c) exists to close, one
    layer down (FRE-1282).
    """
    html_body = """
    <html><body>
      <header><span>Mercury Weekly</span></header>
      <nav><a>Health</a><a>Mercury</a><a>Products</a></nav>
      <aside><p>Related: mercury in tuna</p></aside>
      <main><p>The catch was landed in Bilbao.</p></main>
      <footer><p>Contact us about mercury testing</p></footer>
    </body></html>
    """
    resp = _mock_html_response(html_body)
    with patch(
        "personal_agent.tools.fetch.create_guarded_http_client", return_value=_mock_client(resp)
    ):
        result = await fetch_url_executor(url="https://example.com", ctx=_CTX)

    assert "The catch was landed in Bilbao." in result["text"]
    assert "mercury" not in result["text"].lower()


@pytest.mark.asyncio
async def test_adjacent_inline_tags_get_a_word_boundary() -> None:
    """Regression: <td>A</td><td>B</td> must extract as 'A B', not 'AB'.

    Only block tags (br/p/div/h1-6/li/tr) inserted a separator; td/th/span/a and any
    other inline tag did not, so two adjacent inline-tagged cells with no literal
    whitespace between them ran together — corrupting the word boundary this tool's
    citation-content use depends on.
    """
    html_body = "<table><tr><td>A</td><td>B</td></tr></table>"
    resp = _mock_html_response(html_body)
    with patch(
        "personal_agent.tools.fetch.create_guarded_http_client", return_value=_mock_client(resp)
    ):
        result = await fetch_url_executor(url="https://example.com", ctx=_CTX)

    assert "A B" in result["text"]
    assert "AB" not in result["text"]


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


# ── SSRF guard: resolve-then-check, not IP-literal-only (PR #957 bounce) ────
#
# The first version of this guard tested only whether the host *string* parsed as an
# IP literal, which missed the case that matters on this deployment: Docker service
# names (elasticsearch, postgres, neo4j, redis, …) are plain hostnames on a flat
# network (FRE-362) that resolve to private addresses. `_resolves_to_private_or_internal`
# resolves via DNS and checks the resolved address instead.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hostname",
    [
        "localhost",
        "LOCALHOST",
        "127.0.0.1",
        "169.254.169.254",  # cloud metadata endpoint
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "::1",
        "0.0.0.0",
    ],
)
async def test_ip_literal_and_localhost_targets_are_flagged(hostname: str) -> None:
    """IP literals and 'localhost' resolve locally — no DNS mocking needed."""
    assert await _resolves_to_private_or_internal(hostname) is True


@pytest.mark.asyncio
async def test_docker_service_hostname_resolving_to_private_address_is_flagged() -> None:
    """The exact regression from the PR #957 bounce: 'elasticsearch' is a plain
    hostname, not an IP literal, but resolves to a private address on this
    deployment's flat Docker network — reachable, unauthenticated (FRE-361), and
    with no network segmentation to fall back on (FRE-362). An IP-literal-only check
    let this through; resolving first catches it.
    """
    loop = asyncio.get_running_loop()
    with patch.object(loop, "getaddrinfo", AsyncMock(return_value=_addrinfo("172.20.0.5"))):
        assert await _resolves_to_private_or_internal("elasticsearch") is True


@pytest.mark.asyncio
async def test_public_hostname_resolving_publicly_is_not_flagged() -> None:
    """Paired positive — an ordinary public hostname resolving to a public address
    must not be denied.
    """
    loop = asyncio.get_running_loop()
    with patch.object(loop, "getaddrinfo", AsyncMock(return_value=_addrinfo("93.184.216.34"))):
        assert await _resolves_to_private_or_internal("example.com") is False


@pytest.mark.asyncio
async def test_unresolvable_hostname_is_not_flagged() -> None:
    """A DNS failure is not a security block — the connection attempt itself then
    fails with a clear 'cannot connect', rather than misclassifying resolution
    failure as a guard hit.
    """
    loop = asyncio.get_running_loop()
    with patch.object(loop, "getaddrinfo", AsyncMock(side_effect=socket.gaierror("not known"))):
        assert await _resolves_to_private_or_internal("this-host-does-not-exist.invalid") is False


@pytest.mark.asyncio
async def test_private_ip_url_refused_before_connection() -> None:
    """The request hook fires before any connection is attempted — a real
    ``httpx.AsyncClient`` is used (not a mocked one), with the transport patched to
    fail the test if reached, so this proves refusal happens pre-connection rather
    than merely asserting the final exception type.
    """
    with patch.object(
        httpx.AsyncHTTPTransport,
        "handle_async_request",
        AsyncMock(side_effect=AssertionError("transport reached — SSRF guard did not refuse")),
    ):
        with pytest.raises(ToolExecutionError, match="private or internal address"):
            await fetch_url_executor(url="http://127.0.0.1:9200/_cat/indices", ctx=_CTX)


@pytest.mark.asyncio
async def test_metadata_endpoint_refused_before_connection() -> None:
    with patch.object(
        httpx.AsyncHTTPTransport,
        "handle_async_request",
        AsyncMock(side_effect=AssertionError("transport reached — SSRF guard did not refuse")),
    ):
        with pytest.raises(ToolExecutionError, match="private or internal address"):
            await fetch_url_executor(url="http://169.254.169.254/latest/meta-data/", ctx=_CTX)


@pytest.mark.asyncio
async def test_docker_service_name_refused_before_connection() -> None:
    """End-to-end proof for the bounce's own example: ``fetch_url_executor`` refuses
    a hostname (not an IP literal) that resolves to a private address, before any
    connection is attempted.
    """
    loop = asyncio.get_running_loop()
    with (
        patch.object(loop, "getaddrinfo", AsyncMock(return_value=_addrinfo("172.20.0.5"))),
        patch.object(
            httpx.AsyncHTTPTransport,
            "handle_async_request",
            AsyncMock(side_effect=AssertionError("transport reached — SSRF guard did not refuse")),
        ),
    ):
        with pytest.raises(ToolExecutionError, match="resolves to a private or internal address"):
            await fetch_url_executor(url="http://elasticsearch:9200/_cat/indices", ctx=_CTX)
