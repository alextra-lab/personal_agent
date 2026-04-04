"""Native URL fetch tool — Phase 3 of CLI-First Tool Migration (ADR-0028).

Replaces mcp_fetch_content with a lightweight in-process tool using
httpx for HTTP and stdlib html.parser for text extraction.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Any

import httpx

from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

# Tags whose inner content should be skipped entirely
_SKIP_TAGS = frozenset(["script", "style", "noscript", "head", "meta", "link", "svg", "iframe"])
_DEFAULT_MAX_CHARS = 10_000
_DEFAULT_TIMEOUT = 20.0


class _TextExtractor(HTMLParser):
    """Minimal HTML → plain text extractor using stdlib html.parser."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag.lower() in ("br", "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        """Return extracted text with collapsed whitespace."""
        raw = "".join(self._parts)
        raw = html.unescape(raw)
        # Collapse runs of blank lines to a single blank line
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        # Strip leading/trailing whitespace per line
        lines = [line.strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line).strip()


def _extract_text(html_body: str) -> str:
    """Extract readable text from HTML."""
    extractor = _TextExtractor()
    extractor.feed(html_body)
    return extractor.get_text()


fetch_url_tool = ToolDefinition(
    name="fetch_url",
    description=(
        "Fetch the readable text content of a specific webpage URL. "
        "Strips HTML tags, scripts, and styles; returns clean plain text. "
        "Use when you already have a URL and need to read its full content. "
        "For finding information without a known URL, use web_search instead. "
        "Returns up to max_chars characters of extracted text (default 10,000)."
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
    risk_level="low",
    allowed_modes=["NORMAL", "DEGRADED"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=30,
    rate_limit_per_hour=100,
)


async def fetch_url_executor(
    url: str = "",
    max_chars: int | None = None,
    ctx: TraceContext | None = None,
) -> dict[str, Any]:
    """Fetch and extract readable text from a URL.

    Args:
        url: Target URL (http or https).
        max_chars: Maximum characters to return (default 10,000, max 50,000).
        ctx: Optional trace context for structured logging.

    Returns:
        Dict with ``url``, ``text`` (extracted content), ``char_count``,
        and ``truncated`` (bool) keys.

    Raises:
        ToolExecutionError: On invalid URL, connection failure, timeout,
            or non-2xx HTTP response.
    """
    url = (url or "").strip()
    if not url:
        raise ToolExecutionError("'url' parameter is required and cannot be empty.")
    if not url.startswith(("http://", "https://")):
        raise ToolExecutionError(f"Invalid URL '{url}'. Must start with http:// or https://.")

    cap = max(1, min(int(max_chars or _DEFAULT_MAX_CHARS), 50_000))
    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"

    log.info("fetch_url_started", trace_id=trace_id, url=url)

    try:
        async with httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "personal-agent/0.1 (research bot)"},
        ) as client:
            resp = await client.get(url)
            if resp.is_error:
                msg = f"HTTP {resp.status_code} fetching {url}"
                log.error(
                    "fetch_url_http_error", trace_id=trace_id, status=resp.status_code, url=url
                )
                raise ToolExecutionError(msg)

            content_type = resp.headers.get("content-type", "")
            if "html" in content_type or not content_type:
                text = _extract_text(resp.text)
            else:
                # Non-HTML (plain text, JSON, etc.) — return as-is
                text = resp.text

    except ToolExecutionError:
        raise
    except httpx.ConnectError as exc:
        msg = f"Cannot connect to {url}."
        log.error("fetch_url_connect_failed", trace_id=trace_id, url=url, error=msg)
        raise ToolExecutionError(msg) from exc
    except httpx.TimeoutException as exc:
        msg = f"Request to {url} timed out after {_DEFAULT_TIMEOUT}s."
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
