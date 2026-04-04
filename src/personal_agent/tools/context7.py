"""Native Context7 library documentation tool — Phase 3 of CLI-First Tool Migration (ADR-0028).

Replaces two MCP tools (mcp_resolve-library-id, mcp_get-library-docs) with a
single in-process tool that resolves a library name and fetches LLM-ready docs
from the Context7 REST API in one call.
"""

from __future__ import annotations

from typing import Any

import httpx

from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

_CONTEXT7_BASE = "https://context7.com/api/v1"
_DEFAULT_TOKENS = 5000
_MAX_TOKENS = 20000
_TIMEOUT = 30.0


get_library_docs_tool = ToolDefinition(
    name="get_library_docs",
    description=(
        "Fetch up-to-date official documentation for a library or package from Context7. "
        "Automatically resolves the library name to a Context7 ID, then retrieves "
        "LLM-optimized documentation. Use when writing code that requires accurate, "
        "current API references (e.g. fastapi, pydantic, react, pytorch). "
        "Returns documentation text truncated to 'tokens' (default 5,000)."
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
)


async def get_library_docs_executor(
    library: str = "",
    topic: str | None = None,
    tokens: int | None = None,
    ctx: TraceContext | None = None,
) -> dict[str, Any]:
    """Resolve a library name and fetch its documentation from Context7.

    Performs two sequential REST calls:
    1. ``GET /api/v1/search?query=<library>`` — resolves to a Context7 library ID.
    2. ``GET /api/v1/<id>?tokens=<N>&topic=<topic>`` — fetches LLM-ready docs.

    Args:
        library: Library or package name.
        topic: Optional documentation topic filter.
        tokens: Max tokens to retrieve (default 5,000, max 20,000).
        ctx: Optional trace context.

    Returns:
        Dict with ``library_id``, ``library_name``, ``docs``, and ``tokens_used`` keys.

    Raises:
        ToolExecutionError: When library name is empty, no match is found,
            or the API call fails.
    """
    library = (library or "").strip()
    if not library:
        raise ToolExecutionError("'library' parameter is required and cannot be empty.")

    token_cap = max(100, min(int(tokens or _DEFAULT_TOKENS), _MAX_TOKENS))
    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"

    log.info("get_library_docs_started", trace_id=trace_id, library=library, topic=topic)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            # Step 1: resolve library name → Context7 ID
            library_id, resolved_name = await _resolve_library(client, library, trace_id)

            # Step 2: fetch documentation
            docs = await _fetch_docs(client, library_id, topic, token_cap, trace_id)

    except ToolExecutionError:
        raise
    except httpx.ConnectError as exc:
        msg = f"Cannot connect to Context7 API at {_CONTEXT7_BASE}."
        log.error("get_library_docs_connect_failed", trace_id=trace_id, error=msg)
        raise ToolExecutionError(msg) from exc
    except httpx.TimeoutException as exc:
        msg = f"Context7 API request timed out after {_TIMEOUT}s."
        log.error("get_library_docs_timeout", trace_id=trace_id)
        raise ToolExecutionError(msg) from exc
    except Exception as exc:
        log.error(
            "get_library_docs_failed",
            trace_id=trace_id,
            library=library,
            error=str(exc),
            exc_info=True,
        )
        raise ToolExecutionError(str(exc)) from exc

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


async def _resolve_library(
    client: httpx.AsyncClient,
    library: str,
    trace_id: str,
) -> tuple[str, str]:
    """Search Context7 for the library and return (library_id, display_name)."""
    resp = await client.get(
        f"{_CONTEXT7_BASE}/search",
        params={"query": library},
    )
    if resp.is_error:
        raise ToolExecutionError(
            f"Context7 search returned HTTP {resp.status_code} for library '{library}'."
        )

    data = resp.json()
    results: list[dict[str, Any]] = data.get("results") or []
    if not results:
        raise ToolExecutionError(
            f"No Context7 documentation found for library '{library}'. "
            "Try a different spelling or check https://context7.com."
        )

    # Pick the best match: highest stars / first result
    best = results[0]
    library_id: str = best.get("id") or best.get("library_id") or ""
    if not library_id:
        raise ToolExecutionError(
            f"Context7 returned a result for '{library}' but it had no library ID."
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
    """Fetch LLM-ready documentation for the resolved library ID."""
    params: dict[str, Any] = {"tokens": tokens}
    if topic:
        params["topic"] = topic

    # Context7 serves docs at /api/v1/{library_id}
    resp = await client.get(f"{_CONTEXT7_BASE}/{library_id.lstrip('/')}", params=params)
    if resp.is_error:
        raise ToolExecutionError(
            f"Context7 docs fetch returned HTTP {resp.status_code} for library ID '{library_id}'."
        )

    # Context7 returns plain text or markdown
    return resp.text
