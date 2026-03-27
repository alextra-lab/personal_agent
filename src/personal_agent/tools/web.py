"""Native web search tool via self-hosted SearXNG (ADR-0034).

Provides structured web search results from aggregated engines
without sending queries to third-party AI services.
"""

from __future__ import annotations

from typing import Any

import httpx

from personal_agent.config import settings
from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)


web_search_tool = ToolDefinition(
    name="web_search",
    description=(
        "Search the web using a private self-hosted metasearch engine. "
        "Aggregates results from Google, Brave, DuckDuckGo, StackOverflow, "
        "arXiv, and other engines. Returns structured results with titles, "
        "URLs, and snippets. Use 'categories' to target specific domains: "
        "'general' (default), 'it' (StackOverflow, GitHub, MDN), "
        "'science' (arXiv, Semantic Scholar), 'news' (Google News, Bing News), "
        "'weather' (wttr.in for forecasts and conditions). "
        "Use 'engines' to query specific engines by name. "
        "Prefer this tool for routine web lookups. "
        "Use mcp_perplexity_ask only when synthesized answers with citations "
        "are specifically needed."
    ),
    category="network",
    parameters=[
        ToolParameter(
            name="query",
            type="string",
            description="Search query text.",
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="categories",
            type="string",
            description=(
                "Comma-separated SearXNG categories to search. "
                "Options: general, it, science, news, weather, files, images, music, videos. "
                "Default: 'general'. Use 'it' for programming questions, "
                "'science' for academic research, 'weather' for forecasts."
            ),
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="engines",
            type="string",
            description=(
                "Comma-separated engine names to query directly. "
                "Overrides categories if provided. "
                "Examples: 'google,stackoverflow', 'arxiv,semantic scholar'."
            ),
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="language",
            type="string",
            description="Search language (BCP-47 code, e.g. 'en', 'fr'). Default: 'en'.",
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="time_range",
            type="string",
            description=(
                "Filter results by time. "
                "Options: 'day', 'week', 'month', 'year'. "
                "Omit for no time filter."
            ),
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="max_results",
            type="number",
            description="Maximum results to return (1-50, default from config).",
            required=False,
            default=None,
            json_schema=None,
        ),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=15,
    rate_limit_per_hour=120,
)


async def web_search_executor(
    query: str = "",
    categories: str | None = None,
    engines: str | None = None,
    language: str = "en",
    time_range: str | None = None,
    max_results: int | None = None,
    ctx: TraceContext | None = None,
) -> dict[str, Any]:
    """Execute a web search via the local SearXNG instance.

    Follows the same executor contract as ``search_memory_executor``:
    keyword arguments matching the tool's parameter names, optional
    ``ctx`` for tracing, returns a plain dict (the ``ToolExecutionLayer``
    wraps it in a ``ToolResult``), and raises ``ToolExecutionError`` on
    failure.

    Args:
        query: Search query text.
        categories: Comma-separated SearXNG categories (default from config).
        engines: Comma-separated engine names (overrides categories).
        language: BCP-47 language code.
        time_range: Time filter ('day', 'week', 'month', 'year').
        max_results: Maximum results to return (1-50, default from config).
        ctx: Optional trace context for logging.

    Returns:
        Dict with ``results``, ``result_count``, ``suggestions``,
        ``infoboxes``, and query metadata.

    Raises:
        ToolExecutionError: When SearXNG is unreachable, times out,
            or returns an unparseable response.
    """
    query = (query or "").strip()
    if not query:
        raise ToolExecutionError("query parameter is required and cannot be empty.")

    categories = categories or settings.searxng_default_categories
    capped_max = min(max(int(max_results or settings.searxng_max_results), 1), 50)

    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"

    log.info(
        "web_search_started",
        trace_id=trace_id,
        query=query[:120],
        categories=categories,
        engines=engines,
        time_range=time_range,
    )

    params: dict[str, str] = {
        "q": query,
        "format": "json",
        "categories": categories,
        "language": language,
        "pageno": "1",
    }
    if engines:
        params["engines"] = engines
    if time_range:
        params["time_range"] = time_range

    try:
        async with httpx.AsyncClient(
            timeout=settings.searxng_timeout_seconds,
        ) as client:
            response = await client.get(
                f"{settings.searxng_base_url}/search",
                params=params,
            )
            response.raise_for_status()
            data = response.json()

    except httpx.ConnectError as exc:
        error_msg = (
            f"Cannot connect to SearXNG at {settings.searxng_base_url}. "
            "Is the searxng Docker service running?"
        )
        log.error("web_search_connect_failed", trace_id=trace_id, error=error_msg)
        raise ToolExecutionError(error_msg) from exc

    except httpx.TimeoutException as exc:
        error_msg = f"SearXNG request timed out after {settings.searxng_timeout_seconds}s."
        log.error("web_search_timeout", trace_id=trace_id, error=error_msg)
        raise ToolExecutionError(error_msg) from exc

    except Exception as exc:
        log.error(
            "web_search_failed",
            trace_id=trace_id,
            error=str(exc),
            exc_info=True,
        )
        raise ToolExecutionError(str(exc)) from exc

    results = [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("content", ""),
            "engine": item.get("engine", ""),
            "score": item.get("score"),
        }
        for item in (data.get("results") or [])[:capped_max]
    ]

    output: dict[str, Any] = {
        "results": results,
        "result_count": len(results),
        "suggestions": data.get("suggestions", []),
        "infoboxes": [
            {
                "title": ib.get("infobox", ""),
                "content": ib.get("content", "")[:500],
                "urls": [u.get("url") for u in ib.get("urls", [])[:3]],
            }
            for ib in (data.get("infoboxes") or [])[:2]
        ],
        "query": query,
        "categories_used": categories,
        "engines_used": engines,
    }

    log.info(
        "web_search_completed",
        trace_id=trace_id,
        result_count=len(results),
    )

    return output
