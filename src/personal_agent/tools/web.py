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
        "Search the web via a private self-hosted metasearch engine. "
        "Returns titles, URLs, snippets, infoboxes, and plugin answers. "
        "\n\nPlugin capabilities (returned in the 'answers' field — no engine needed):\n"
        "  - Timezone: query 'time Berlin' or 'clock Tokyo' → current local time\n"
        "  - Unit conversion: query '20 °C in °F' or '10 EUR in USD' (use symbols, not words) → converted value\n"
        "  - Calculator: query '2^10 * 3' → computed result\n"
        "\nWeather: use engines='openmeteo' or categories='weather' for current conditions + hourly forecast.\n"
        "\nCategories: general (default), it, science, news, weather, social_media, files, images, music, videos.\n"
        "Use 'it' for programming questions, 'science' for academic research, "
        "'news' for current events, 'social_media' for Reddit/Lemmy discussions.\n"
        "Prefer perplexity_query for synthesized answers with citations."
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
                "Options: general, it, science, news, weather, social_media, files, images, music, videos. "
                "Default: 'general'. Use 'it' for programming questions, "
                "'science' for academic research, 'weather' for forecasts, "
                "'social_media' for Reddit/Lemmy community discussions."
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
                "General: google, brave, duckduckgo, startpage, qwant. "
                "IT: stackoverflow, github, hackernews, lobste.rs, huggingface, docker hub. "
                "Science: arxiv, google scholar, pubmed, crossref, openalex, springer nature, astrophysics data system, semantic scholar. "
                "News: google news, bing news, reuters, wikinews, qwant news. "
                "Social: reddit, lemmy posts. "
                "Weather: openmeteo, wttr.in. "
                "Recipes: chefkoch."
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
        Dict with ``answers`` (plugin results: timezone, unit conversion,
        calculator, weather), ``results``, ``result_count``, ``suggestions``,
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

    # Plugin answers (timezone, unit converter, calculator, openmeteo weather).
    # These are returned in the top-level "answers" array rather than "results".
    answers: list[dict[str, Any]] = []
    for item in data.get("answers") or []:
        engine = item.get("engine", "")
        if engine == "openmeteo" or item.get("service") == "Open-meteo":
            # Weather answer: surface the current conditions summary + location.
            current = item.get("current", {})
            location = current.get("location", {})
            answers.append({
                "engine": engine,
                "type": "weather",
                "location": location.get("name", ""),
                "country": location.get("country_code", ""),
                "summary": current.get("summary", ""),
                "temperature": current.get("temperature", {}),
                "condition": current.get("condition", ""),
                "feels_like": current.get("feels_like", {}),
                "humidity": current.get("humidity", {}),
                "wind_speed": current.get("wind_speed", {}),
            })
        else:
            text = item.get("answer")
            if text:
                answers.append({"engine": engine, "answer": text})

    output: dict[str, Any] = {
        "answers": answers,
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
