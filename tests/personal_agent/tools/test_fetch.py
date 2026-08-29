"""Unit tests for the native fetch_url tool (ADR-0028 Phase 3, FRE-1297).

Tests use mocked httpx responses via ``create_guarded_http_client`` — no network
access required. Egress-guard behaviour (blocked domains, refusal before connection)
is covered separately by ``tests/test_security/test_egress_seams.py``.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from personal_agent.security import DomainGuard, NovelDestinationTracker
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.fetch import (
    _SKIP_TAGS,
    _resolves_to_private_or_internal,
    fetch_url_executor,
    fetch_url_tool,
)

_CTX = TraceContext.new_trace()
_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "fetch"


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


# ── Void elements (FRE-1307) ─────────────────────────────────────────────
#
# `meta` and `link` are HTML void elements: html.parser never calls handle_endtag
# for a bare `<meta charset=utf-8>` or `<link rel=x>`, so the old skip_depth
# increment-on-starttag/decrement-on-endtag scheme never returned to 0 once either
# tag appeared — blanking the rest of the document. Every real HTML page carries
# at least one of them, so fetch_url never extracted a real page's text.


@pytest.mark.asyncio
async def test_real_archived_page_extracts_substantial_text() -> None:
    """AC-1: a real, previously-fetched page — not a hand-authored fragment — must
    extract a substantial amount of text. The hand-authored fixture in
    ``test_basic_html_extraction`` omits the one feature every real document has
    (meta/link tags), which is exactly the gap that let this ship.
    """
    html_body = (_FIXTURES_DIR / "lacuisinedemichel_jarret_de_veau.html").read_text(
        encoding="utf-8"
    )
    resp = _mock_html_response(html_body)
    with patch(
        "personal_agent.tools.fetch.create_guarded_http_client", return_value=_mock_client(resp)
    ):
        result = await fetch_url_executor(url="https://example.com", max_chars=50_000, ctx=_CTX)

    assert result["char_count"] > 2_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "html_body",
    [
        "<html><head><link rel=x></head><body><p>HELLO</p></body></html>",
        "<html><head><meta charset=utf-8></head><body><p>HELLO</p></body></html>",
        "<html><body><p>HELLO</p></body></html>",
    ],
    ids=["link", "meta", "neither"],
)
async def test_void_element_repro_is_pinned(html_body: str) -> None:
    """AC-2: the ticket's three-line repro, pinned as a test."""
    resp = _mock_html_response(html_body)
    with patch(
        "personal_agent.tools.fetch.create_guarded_http_client", return_value=_mock_client(resp)
    ):
        result = await fetch_url_executor(url="https://example.com", ctx=_CTX)

    assert "HELLO" in result["text"]


@pytest.mark.asyncio
async def test_self_closed_void_tag_does_not_prematurely_end_an_enclosing_skip_region() -> None:
    """A self-closed void tag (``<link />``) makes html.parser's default
    ``handle_startendtag`` call ``handle_starttag`` then ``handle_endtag`` for it.
    ``handle_endtag`` must ignore void tags too, or that synthesized end callback
    prematurely decrements the *enclosing* ``<head>``'s skip depth and leaks its
    remaining content (same shape as the original bug, codex-rescue plan review).
    """
    html_body = "<html><head><link /><title>SECRET</title></head><body>BODY</body></html>"
    resp = _mock_html_response(html_body)
    with patch(
        "personal_agent.tools.fetch.create_guarded_http_client", return_value=_mock_client(resp)
    ):
        result = await fetch_url_executor(url="https://example.com", ctx=_CTX)

    assert "SECRET" not in result["text"]
    assert "BODY" in result["text"]


@pytest.mark.asyncio
async def test_self_closed_void_tag_inside_boilerplate_stays_excluded() -> None:
    """AC-5 hardening: a self-closed void tag inside a boilerplate region (nav here)
    must not prematurely end that region's exclusion either.
    """
    html_body = "<html><body><nav><link />SECRET</nav><main>BODY</main></body></html>"
    resp = _mock_html_response(html_body)
    with patch(
        "personal_agent.tools.fetch.create_guarded_http_client", return_value=_mock_client(resp)
    ):
        result = await fetch_url_executor(url="https://example.com", ctx=_CTX)

    assert "SECRET" not in result["text"]
    assert "BODY" in result["text"]


@pytest.mark.asyncio
async def test_future_void_tag_added_to_skip_tags_does_not_blank_document() -> None:
    """AC-3: adding a new void element to _SKIP_TAGS must not reintroduce the bug.

    Simulates a future maintainer adding ``img`` (also a void element) to
    ``_SKIP_TAGS``. If the fix were only "delete meta/link", this would still
    blank the document; the fix must be structural (a void-tags concept).
    """
    html_body = '<html><body><img src="a.png"><p>AFTER</p></body></html>'
    resp = _mock_html_response(html_body)
    with (
        patch(
            "personal_agent.tools.fetch.create_guarded_http_client",
            return_value=_mock_client(resp),
        ),
        patch("personal_agent.tools.fetch._SKIP_TAGS", _SKIP_TAGS | {"img"}),
    ):
        result = await fetch_url_executor(url="https://example.com", ctx=_CTX)

    assert "AFTER" in result["text"]


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
async def test_empty_extraction_raises_instead_of_silent_success() -> None:
    """AC-4: a 200 response yielding zero extractable characters must surface as a
    failure the model can act on, not `success: true` / `char_count: 0`.
    """
    html_body = (
        "<html><head><meta charset=utf-8></head>"
        "<body><script>var x = 1;</script><style>body { color: red; }</style></body>"
        "</html>"
    )
    resp = _mock_html_response(html_body)
    with patch(
        "personal_agent.tools.fetch.create_guarded_http_client", return_value=_mock_client(resp)
    ):
        with pytest.raises(ToolExecutionError, match="no readable text"):
            await fetch_url_executor(url="https://example.com", ctx=_CTX)


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


# ── FRE-1330: named-bucket block (AC-3) + long-tail regression (AC-4) ──────
#
# The model emitted two fetch_url calls to a real Alibaba OSS bucket during a live
# turn; not present anywhere in this codebase's config or code. Blocked via
# DomainGuard's bundled list. These tests attempt the actual fetch (through
# fetch_url_executor, transport patched unreachable/mocked) rather than reading
# config, per the ticket's AC-3 failure condition.

_TICKET_BUCKET_URL = (
    "https://routify-file-proxy-sg.oss-ap-southeast-1.aliyuncs.com/proxy_temp_file/"
    "production/2026-08-30/trace_x/requestId_y/hash?Expires=1818630439"
)

# The 22-call fetch_url history from the ticket — every one of these must remain
# fetchable; the new bundled blocklist entry must not widen to a parent domain.
_LONG_TAIL_HOSTS = [
    "aventureculinaire.fr",
    "lacuisinedemichel.net",
    "pmc.ncbi.nlm.nih.gov",
    "en.wikipedia.org",
    "fr.wikipedia.org",
    "docs.searxng.org",
    "climate-api.open-meteo.com",
    "marine-api.open-meteo.com",
    "exa.ai",
    "docs.exa.ai",
    "github.com",
    "raw.githubusercontent.com",
    "partir.com",
    "snorkeling-report.com",
]


@pytest.mark.asyncio
async def test_fre1330_named_bucket_refused_before_connection() -> None:
    """AC-3: the exact bucket from the ticket is refused by the domain guard — proved
    by attempting the fetch (transport patched to fail the test if reached), not by
    reading config.
    """
    with patch.object(
        httpx.AsyncHTTPTransport,
        "handle_async_request",
        AsyncMock(side_effect=AssertionError("transport reached — guard did not refuse")),
    ):
        with pytest.raises(ToolExecutionError, match="domain guard"):
            await fetch_url_executor(url=_TICKET_BUCKET_URL, ctx=_CTX)


@pytest.mark.parametrize("hostname", _LONG_TAIL_HOSTS)
def test_fre1330_long_tail_host_still_allowed_by_guard(hostname: str) -> None:
    """AC-4: every host from the 22-call history remains allowed by DomainGuard."""
    g = DomainGuard()
    result = g.check_url(f"https://{hostname}/some/path")
    assert result.allowed is True


@pytest.mark.asyncio
async def test_fre1330_long_tail_host_end_to_end_not_blocked() -> None:
    """Representative long-tail host reaches the transport (neither the domain guard
    nor the private-target check refuses it) — pairs with the AC-3 blocked case above.
    """
    loop = asyncio.get_running_loop()

    async def _fake_handle_async_request(self: object, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html><body><p>Hello</p></body></html>",
            request=request,
        )

    with (
        patch.object(loop, "getaddrinfo", AsyncMock(return_value=_addrinfo("93.184.216.34"))),
        patch.object(httpx.AsyncHTTPTransport, "handle_async_request", _fake_handle_async_request),
    ):
        result = await fetch_url_executor(url="https://docs.exa.ai/quickstart", ctx=_CTX)

    assert "Hello" in result["text"]


# ── FRE-1330: novel egress destination signal (AC-1 / AC-2 mechanism) ──────


@pytest.mark.asyncio
async def test_fre1330_fresh_domain_logs_novel_destination_event(tmp_path: Path) -> None:
    """AC-1 mechanism: fetch_url logs a distinct event on a domain's first sighting."""
    resp = _mock_html_response("<p>hi</p>")
    tracker = NovelDestinationTracker(cache_path=tmp_path / "novelty.json")
    with (
        patch(
            "personal_agent.tools.fetch.create_guarded_http_client", return_value=_mock_client(resp)
        ),
        patch("personal_agent.tools.fetch.get_novelty_tracker", return_value=tracker),
        patch("personal_agent.tools.fetch.log") as mock_log,
    ):
        await fetch_url_executor(url="https://brand-new-domain.example/page", ctx=_CTX)

    logged_events = [call.args[0] for call in mock_log.info.call_args_list]
    assert "fetch_url_novel_destination" in logged_events


@pytest.mark.asyncio
async def test_fre1330_repeat_domain_does_not_log_novel_destination_event(tmp_path: Path) -> None:
    """AC-2 (seeded negative): a domain already fetched inside the window must not
    re-alert — a fetch_url_executor-level positive-only test would miss this failure
    mode entirely.
    """
    resp = _mock_html_response("<p>hi</p>")
    tracker = NovelDestinationTracker(cache_path=tmp_path / "novelty.json")
    await tracker.check_and_record("https://seen-before.example/first")
    with (
        patch(
            "personal_agent.tools.fetch.create_guarded_http_client", return_value=_mock_client(resp)
        ),
        patch("personal_agent.tools.fetch.get_novelty_tracker", return_value=tracker),
        patch("personal_agent.tools.fetch.log") as mock_log,
    ):
        await fetch_url_executor(url="https://seen-before.example/second", ctx=_CTX)

    logged_events = [call.args[0] for call in mock_log.info.call_args_list]
    assert "fetch_url_novel_destination" not in logged_events


@pytest.mark.asyncio
async def test_fre1330_novelty_tracker_failure_is_fail_open() -> None:
    """An observe-only signal must never break a fetch — a tracker raising (e.g. a
    disk I/O error) must not prevent fetch_url_executor from completing successfully.
    """
    resp = _mock_html_response("<p>hi</p>")
    broken_tracker = MagicMock()
    broken_tracker.check_and_record = AsyncMock(side_effect=RuntimeError("disk full"))
    with (
        patch(
            "personal_agent.tools.fetch.create_guarded_http_client", return_value=_mock_client(resp)
        ),
        patch("personal_agent.tools.fetch.get_novelty_tracker", return_value=broken_tracker),
    ):
        result = await fetch_url_executor(url="https://example.com/page", ctx=_CTX)

    assert "hi" in result["text"]
