"""Native Context7 library documentation tool — Phase 3 of CLI-First Tool Migration (ADR-0028).

Replaces two MCP tools (mcp_resolve-library-id, mcp_get-library-docs) with a
single in-process tool that resolves a library name and fetches LLM-ready docs
from the Context7 REST API in one call.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx

from personal_agent.security import EgressBlockedError, create_guarded_http_client
from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.contract import ToolError, error_codes
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

GetLibraryDocsCode = Literal[
    "invalid_argument",
    "egress_blocked",
    "upstream_unavailable",
    "upstream_timeout",
    "upstream_failed",
    "upstream_rejected",
    "not_found",
    "bad_reply",
]
"""``get_library_docs`` failure codes (ADR-0156 D6.1)."""


class GetLibraryDocsError(ToolError[GetLibraryDocsCode]):
    """A ``get_library_docs`` failure."""


_RETRY_NEXT = "Call get_library_docs once more; if it fails again, answer without it."

_CONTEXT7_BASE = "https://context7.com/api/v1"
_DEFAULT_TOKENS = 5000
_MAX_TOKENS = 20000
_TIMEOUT = 30.0


get_library_docs_tool = ToolDefinition(
    name="get_library_docs",
    description=(
        "Fetch current documentation for a software library from Context7: it resolves the "
        "library name to the best-matching Context7 entry, then returns that entry's docs text. "
        "Use it when writing or explaining code that needs an accurate, current API reference. "
        "Do not use it for general web questions (use web_search) or for this project's own "
        "code (use read or bash grep). "
        "Only the first search match is used, and the docs are cut to 'tokens' "
        f"(default {_DEFAULT_TOKENS}, max {_MAX_TOKENS}). "
        'Examples: get_library_docs(library="fastapi", topic="dependencies"), '
        'get_library_docs(library="pydantic", topic="validators", tokens=8000).'
    ),
    category="network",
    parameters=[
        ToolParameter(
            name="library",
            type="string",
            description=(
                "Library or package name to look up "
                "(e.g. 'fastapi', 'pydantic', 'react', 'numpy', 'pytorch')."
            ),
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="topic",
            type="string",
            description="Optional topic to focus the documentation on (e.g. 'routing', 'validators').",
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="tokens",
            type="number",
            description=f"Max documentation tokens to retrieve (default {_DEFAULT_TOKENS}, max {_MAX_TOKENS}).",
            required=False,
            default=None,
            json_schema=None,
        ),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=40,
    rate_limit_per_hour=60,
    error_codes=error_codes(GetLibraryDocsError),
    max_result_tokens=25_000,
    truncation_next_action="Call get_library_docs again with a lower tokens value or a topic.",
)


async def get_library_docs_executor(
    library: str = "",
    topic: str | None = None,
    tokens: int | None = None,
    *,
    ctx: TraceContext,
) -> dict[str, Any]:
    """Resolve a library name and fetch its documentation from Context7.

    Performs two sequential REST calls:
    1. ``GET /api/v1/search?query=<library>`` — resolves to a Context7 library ID.
    2. ``GET /api/v1/<id>?tokens=<N>&topic=<topic>`` — fetches LLM-ready docs.

    Args:
        library: Library or package name.
        topic: Optional documentation topic filter.
        tokens: Max tokens to retrieve (default 5,000, max 20,000).
        ctx: Trace context.

    Returns:
        Dict with ``library_id``, ``library_name``, ``docs``, and ``tokens_used`` keys.

    Raises:
        GetLibraryDocsError: When the library name is empty, no match is found, the egress
            guard refuses the host, or the API call fails or returns an unusable reply.
    """
    library = (library or "").strip()
    if not library:
        raise GetLibraryDocsError(
            "invalid_argument",
            "'library' parameter is required and cannot be empty.",
            retryable=False,
            next_action='Call get_library_docs with a library name, e.g. library="fastapi".',
        )

    token_cap = max(100, min(int(tokens or _DEFAULT_TOKENS), _MAX_TOKENS))
    trace_id = ctx.trace_id

    log.info("get_library_docs_started", trace_id=trace_id, library=library, topic=topic)

    try:
        async with create_guarded_http_client(timeout=_TIMEOUT, follow_redirects=True) as client:
            # Step 1: resolve library name → Context7 ID
            library_id, resolved_name = await _resolve_library(client, library, trace_id)

            # Step 2: fetch documentation
            docs = await _fetch_docs(client, library_id, topic, token_cap, trace_id)

    except EgressBlockedError as exc:
        log.error("get_library_docs_egress_blocked", trace_id=trace_id, reason=exc.reason)
        raise GetLibraryDocsError(
            "egress_blocked",
            f"Context7 API call refused by egress policy: {exc.reason}",
            retryable=False,
            next_action="Context7 cannot be reached from here. Use web_search instead.",
        ) from exc
    except httpx.ConnectError as exc:
        msg = f"Cannot connect to Context7 API at {_CONTEXT7_BASE}."
        log.error("get_library_docs_connect_failed", trace_id=trace_id, error=msg)
        raise GetLibraryDocsError(
            "upstream_unavailable", msg, retryable=True, next_action=_RETRY_NEXT
        ) from exc
    except httpx.TimeoutException as exc:
        msg = f"Context7 API request timed out after {_TIMEOUT}s."
        log.error("get_library_docs_timeout", trace_id=trace_id)
        raise GetLibraryDocsError(
            "upstream_timeout", msg, retryable=True, next_action=_RETRY_NEXT
        ) from exc
    except httpx.HTTPError as exc:
        log.error(
            "get_library_docs_failed",
            trace_id=trace_id,
            library=library,
            error=str(exc),
            exc_info=True,
        )
        raise GetLibraryDocsError(
            "upstream_failed",
            f"Context7 API request failed: {exc}",
            retryable=True,
            next_action=_RETRY_NEXT,
        ) from exc

    log.info(
        "get_library_docs_completed",
        trace_id=trace_id,
        library_id=library_id,
        doc_length=len(docs),
    )

    return {
        "library_id": library_id,
        "library_name": resolved_name,
        "docs": docs,
        "tokens_used": token_cap,
    }


def _transient(status_code: int) -> bool:
    """Report whether an HTTP status can clear on a repeat of the same call.

    Args:
        status_code: The HTTP status Context7 returned.

    Returns:
        ``True`` for a rate limit (429) or a server error (5xx).
    """
    return status_code == 429 or status_code >= 500


def _rejected(what: str, status_code: int) -> GetLibraryDocsError:
    """Build the error for an HTTP error status from Context7.

    Args:
        what: The request and its subject, for the message.
        status_code: The HTTP status Context7 returned.

    Returns:
        An ``upstream_rejected`` error, retryable only for a 429 or a 5xx.
    """
    transient = _transient(status_code)
    return GetLibraryDocsError(
        "upstream_rejected",
        f"Context7 {what} returned HTTP {status_code}.",
        retryable=transient,
        next_action=(
            _RETRY_NEXT
            if transient
            else "Do not repeat this call. Try another library name, or use web_search."
        ),
    )


async def _resolve_library(
    client: httpx.AsyncClient,
    library: str,
    trace_id: str,
) -> tuple[str, str]:
    """Search Context7 for the library and return its ID and display name.

    Args:
        client: The guarded HTTP client.
        library: Library or package name.
        trace_id: Trace id of the call.

    Returns:
        ``(library_id, display_name)`` of the first search match.

    Raises:
        GetLibraryDocsError: On an HTTP error status, a reply that is not a JSON object,
            no match, or a match without an ID.
    """
    resp = await client.get(
        f"{_CONTEXT7_BASE}/search",
        params={"query": library},
    )
    if resp.is_error:
        raise _rejected(f"search for library '{library}'", resp.status_code)

    try:
        data = resp.json()
    except ValueError as exc:
        raise GetLibraryDocsError(
            "bad_reply",
            "Context7 search returned a reply that is not JSON.",
            retryable=True,
            next_action=_RETRY_NEXT,
        ) from exc
    if not isinstance(data, dict):
        raise GetLibraryDocsError(
            "bad_reply",
            "Context7 search returned a reply that is not a JSON object.",
            retryable=True,
            next_action=_RETRY_NEXT,
        )
    results: list[dict[str, Any]] = data.get("results") or []
    if not results:
        raise GetLibraryDocsError(
            "not_found",
            f"No Context7 documentation found for library '{library}'.",
            retryable=False,
            next_action=(
                "Call get_library_docs with another spelling or the package's registry name, "
                "or use web_search."
            ),
        )

    # Pick the best match: highest stars / first result
    best = results[0]
    library_id: str = best.get("id") or best.get("library_id") or ""
    if not library_id:
        raise GetLibraryDocsError(
            "bad_reply",
            f"Context7 returned a result for '{library}' but it had no library ID.",
            retryable=False,
            next_action="Call get_library_docs with a more specific library name, or use web_search.",
        )

    display_name: str = best.get("title") or best.get("name") or library
    return library_id, display_name


async def _fetch_docs(
    client: httpx.AsyncClient,
    library_id: str,
    topic: str | None,
    tokens: int,
    trace_id: str,
) -> str:
    """Fetch LLM-ready documentation for the resolved library ID.

    Args:
        client: The guarded HTTP client.
        library_id: The Context7 library ID.
        topic: Optional topic filter.
        tokens: Context7's token cap for the docs.
        trace_id: Trace id of the call.

    Returns:
        The documentation text.

    Raises:
        GetLibraryDocsError: On an HTTP error status.
    """
    params: dict[str, Any] = {"tokens": tokens}
    if topic:
        params["topic"] = topic

    # Context7 serves docs at /api/v1/{library_id}
    resp = await client.get(f"{_CONTEXT7_BASE}/{library_id.lstrip('/')}", params=params)
    if resp.is_error:
        raise _rejected(f"docs fetch for library ID '{library_id}'", resp.status_code)

    # Context7 returns plain text or markdown
    return resp.text
