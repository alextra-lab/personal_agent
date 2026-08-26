"""Native URL fetch tool — ADR-0028 Phase 3, finished for ADR-0138 D2 (FRE-1297).

A typed fetch tool taking ``url`` as its only argument: the fetched page is an
admissible citation source, the model-chosen URL argument is not (D2's independence
rule). ``bash``/curl reads the same page but is a fully-excluded arbitrary-code tool
under that rule, so it never yields a citable source — this tool is what closes that
gap. Uses ``httpx`` for the fetch and stdlib ``html.parser`` for text extraction, no
new dependency required.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Any

import httpx

from personal_agent.security import EgressBlockedError, create_guarded_http_client
from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

# Tags whose inner content should be skipped entirely.
_SKIP_TAGS = frozenset(["script", "style", "noscript", "head", "meta", "link", "svg", "iframe"])
_BLOCK_TAGS = frozenset(["br", "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"])
_DEFAULT_MAX_CHARS = 10_000
_MAX_CHARS_CAP = 50_000
_TIMEOUT = 20.0


async def _resolves_to_private_or_internal(hostname: str) -> bool:
    """Whether ``hostname`` resolves to a loopback/private/link-local/reserved address.

    ``fetch_url`` is the one tool whose target host is fully model-chosen — every other
    ``create_guarded_http_client`` consumer (web_search, context7, the SLM health probe, …)
    hits a fixed, operator-configured base URL, some of them deliberately loopback/internal
    (e.g. the local model server). ``DomainGuard``'s blocklist mode only checks hostnames
    against a malicious-domain feed, so it does not stop a model-chosen URL from reaching an
    internal service. Rather than narrow ``DomainGuard`` itself (shared by seams that
    legitimately target internal addresses), this tool rejects such targets on its own.

    Resolves via DNS rather than only testing whether the host *string* parses as an IP
    literal — this deployment's internal services (``elasticsearch``, ``postgres``,
    ``neo4j``, ``redis``, …) are plain Docker service-name hostnames on a flat network
    (FRE-362), not IP literals, and an IP-literal-only check let them through (PR #957
    bounce). An unresolvable hostname is treated as not-internal here — the connection
    attempt itself then fails with a clear "cannot connect", rather than a DNS failure
    being misclassified as a security block.

    Residual gap, stated rather than claimed closed: the resolved address is used only to
    decide reachability here, not pinned for the request itself, so a DNS answer that
    changes between this check and the actual connect (rebinding) is not closed by this
    alone.
    """
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False

    for info in infos:
        raw_addr = info[4][0]
        try:
            addr = ipaddress.ip_address(raw_addr)
        except ValueError:
            continue
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_unspecified
        ):
            return True
    return False


async def _reject_private_targets(request: httpx.Request) -> None:
    """Request hook: refuse a target that resolves to a private/internal address.

    Installed alongside ``create_guarded_http_client``'s own ``DomainGuard`` hook, so it
    fires on the initial request and on every redirect (same hook mechanism ADR-0132 D2
    relies on for the domain-blocklist check).
    """
    hostname = request.url.host
    if await _resolves_to_private_or_internal(hostname):
        raise ToolExecutionError(
            f"URL host '{hostname}' resolves to a private or internal address and cannot "
            "be fetched."
        )


class _TextExtractor(HTMLParser):
    """Minimal HTML to plain text extractor using stdlib ``html.parser``."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")
        else:
            # Inline tags (span, a, td, …) carry no separator of their own; without
            # one, adjacent elements with no literal whitespace between them — a
            # common shape in templated markup, e.g. <td>A</td><td>B</td> — extract
            # as "AB" instead of "A B", corrupting the word boundary this tool's
            # citation-content use depends on. get_text() collapses the resulting
            # whitespace runs.
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag not in _BLOCK_TAGS:
            self._parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        """Return extracted text with collapsed whitespace."""
        raw = html.unescape("".join(self._parts))
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        lines = [line.strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line).strip()


def _extract_text(html_body: str) -> str:
    """Extract readable text from an HTML document."""
    extractor = _TextExtractor()
    extractor.feed(html_body)
    return extractor.get_text()


fetch_url_tool = ToolDefinition(
    name="fetch_url",
    description=(
        "Fetch the readable text content of a specific webpage URL. Strips HTML tags, "
        "scripts, and styles; returns clean plain text. Use when you already have a URL "
        "and need to read its full content — the fetched page is a citable source under "
        "the grounding contract, unlike a bash/curl fetch of the same page. For finding "
        "information without a known URL, use web_search instead. Returns up to max_chars "
        "characters of extracted text (default 10,000)."
    ),
    category="network",
    parameters=[
        ToolParameter(
            name="url",
            type="string",
            description="Full URL to fetch (must start with http:// or https://).",
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="max_chars",
            type="number",
            description="Maximum characters of extracted text to return (default 10,000, max 50,000).",
            required=False,
            default=None,
            json_schema=None,
        ),
    ],
    # risk_level is "medium" (the `network` category's own default), not web_search's
    # "low" override — deliberately not inherited. web_search's target is a single
    # fixed, operator-configured proxy (SearXNG); fetch_url's target host is fully
    # model-chosen and unbounded, so it does not earn the same override even with the
    # private-target guard above in place (PR #957 bounce). requires_approval stays
    # False: the concrete exploit that motivated raising this (unauthenticated internal
    # services reachable by Docker service name) is closed by the guard, not by adding
    # approval friction — gating every call behind human approval would reintroduce the
    # friction ADR-0138 built fetch_url to remove, for a residual risk (arbitrary
    # public-host content entering context) already accepted, unapproved, for web_search.
    risk_level="medium",
    allowed_modes=["NORMAL", "DEGRADED"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=30,
    rate_limit_per_hour=100,
)


async def fetch_url_executor(
    url: str = "",
    max_chars: int | None = None,
    *,
    ctx: TraceContext,
) -> dict[str, Any]:
    """Fetch and extract readable text from a URL.

    Args:
        url: Target URL (http or https).
        max_chars: Maximum characters to return (default 10,000, max 50,000).
        ctx: Trace context for logging.

    Returns:
        Dict with ``url``, ``text`` (extracted content), ``char_count``,
        and ``truncated`` (bool) keys.

    Raises:
        ToolExecutionError: On invalid URL, a blocked domain, connection failure,
            timeout, or non-2xx HTTP response.
    """
    url = (url or "").strip()
    if not url:
        raise ToolExecutionError("'url' parameter is required and cannot be empty.")
    if not url.startswith(("http://", "https://")):
        raise ToolExecutionError(f"Invalid URL '{url}'. Must start with http:// or https://.")

    cap = max(1, min(int(max_chars or _DEFAULT_MAX_CHARS), _MAX_CHARS_CAP))
    trace_id = ctx.trace_id

    log.info("fetch_url_started", trace_id=trace_id, url=url)

    try:
        async with create_guarded_http_client(
            timeout=_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "personal-agent/0.1 (research bot)"},
            event_hooks={"request": [_reject_private_targets]},
        ) as client:
            resp = await client.get(url)
            if resp.is_error:
                msg = f"HTTP {resp.status_code} fetching {url}"
                log.error(
                    "fetch_url_http_error", trace_id=trace_id, status=resp.status_code, url=url
                )
                raise ToolExecutionError(msg)

            content_type = resp.headers.get("content-type", "")
            text = (
                _extract_text(resp.text)
                if "html" in content_type or not content_type
                else resp.text
            )

    except ToolExecutionError:
        raise
    except EgressBlockedError as exc:
        log.warning(
            "fetch_url_blocked",
            trace_id=trace_id,
            url=url,
            reason=exc.reason,
            matched_entry=exc.matched_entry,
        )
        detail = f" (matched: {exc.matched_entry})" if exc.matched_entry else ""
        raise ToolExecutionError(
            f"URL blocked by domain guard: {url} [{exc.reason}{detail}]"
        ) from exc
    except httpx.ConnectError as exc:
        msg = f"Cannot connect to {url}."
        log.error("fetch_url_connect_failed", trace_id=trace_id, url=url, error=msg)
        raise ToolExecutionError(msg) from exc
    except httpx.TimeoutException as exc:
        msg = f"Request to {url} timed out after {_TIMEOUT}s."
        log.error("fetch_url_timeout", trace_id=trace_id, url=url)
        raise ToolExecutionError(msg) from exc
    except Exception as exc:
        log.error("fetch_url_failed", trace_id=trace_id, url=url, error=str(exc), exc_info=True)
        raise ToolExecutionError(str(exc)) from exc

    truncated = len(text) > cap
    output_text = text[:cap]

    log.info(
        "fetch_url_completed",
        trace_id=trace_id,
        url=url,
        char_count=len(output_text),
        truncated=truncated,
    )

    return {
        "url": url,
        "text": output_text,
        "char_count": len(output_text),
        "truncated": truncated,
    }
