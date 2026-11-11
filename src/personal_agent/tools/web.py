"""Native web search tool: Exa and Brave LLM Context, called directly (FRE-1766).

``web_search`` sends each query to both providers in parallel and merges the results here.
It used to go through the self-hosted SearXNG instance (ADR-0034), which stripped line
breaks and markdown tables from the text. SearXNG stays deployed until FRE-1763 T2 removes
it; :func:`build_search_params` and :func:`shape_search_response` remain for the FRE-1641
harness, which replays recorded SearXNG searches.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, NoReturn
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import SecretStr

from personal_agent.config import settings
from personal_agent.llm_client.cost_tracker import record_vendor_cost
from personal_agent.security import create_guarded_http_client
from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools import web_search_alerts
from personal_agent.tools.contract import ToolError, error_codes
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

WebSearchCode = Literal["missing_query", "search_unavailable", "bad_query"]


class WebSearchError(ToolError[WebSearchCode]):
    """A ``web_search`` failure (ADR-0156 D6.1)."""


web_search_tool = ToolDefinition(
    name="web_search",
    description=(
        "Search the live web when you need a fact about the real world that you are not "
        "certain of from training data — a specific brand, product, shop, price, current "
        "name, or availability. Returns titles, URLs and page excerpts. Each search goes to "
        "two search APIs at once, Exa and Brave, and their results are merged; both are "
        "third-party vendors that see the query text, and each search is paid. "
        "Excerpts keep the page's line breaks and markdown tables. "
        "A question about the world goes here, not to search_memory (what this system already "
        "knows) or recall_personal_history (the user's own past conversations). Naming a "
        "brand, product or shop without a result behind it is a guess, even when the question "
        "names it. Do not use it for arithmetic or reasoning that needs no outside fact. "
        "To read a page whose URL you have, use fetch_url."
        "\n\nCategories: general (default), news, it, science. 'news' searches recent news "
        "(past week). 'it' searches developer sites (GitHub, Stack Overflow, MDN, Hugging "
        "Face, Docker Hub). 'science' searches research sites (arXiv, PubMed, Semantic "
        "Scholar, Crossref, OpenAlex). Any other value searches the general web."
        "\n\nSize: 'brief' (default) returns short excerpts. Use 'full' when brief excerpts "
        "miss the detail you need, such as a whole table; it returns longer excerpts."
        "\n\nIf one of the two providers fails, the result carries 'degraded_retrieval': "
        "true, names the failed provider in 'failed_providers', and adds a "
        "'retrieval_warning' — treat such a result set with more caution than a normal result."
        '\n\nExamples: web_search(query="best tinned tuna brand France supermarket"), '
        'web_search(query="asyncio TaskGroup cancel", categories="it"), '
        'web_search(query="average monthly rainfall Lyon table", size="full").'
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
                "Search category. Options: general (default), news (past week), "
                "it (developer sites), science (research sites). Any other value searches "
                "the general web."
            ),
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="size",
            type="string",
            description=(
                "How much text each result carries. 'brief' (default): short excerpts. "
                "'full': longer excerpts, for when brief ones miss the detail you need."
            ),
            required=False,
            default=None,
            json_schema={"type": "string", "enum": ["brief", "full"]},
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
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=15,
    error_codes=error_codes(WebSearchError),
    # FRE-1766: a recorded size='full' search (24 merged pages, markdown tables) measured
    # 19,245 result tokens, and Exa returned up to 5,757 characters for a 2,000-character
    # highlight limit. The contract ceiling leaves headroom above that; brief stays far below.
    max_result_tokens=25_000,
    truncation_next_action="Call web_search again with size='brief' or a narrower query.",
)

# ── SearXNG form, kept for the FRE-1641 harness until FRE-1763 T2 ─────────────


def build_search_params(
    query: str,
    categories: str,
    engines: str | None,
    language: str,
    time_range: str | None,
) -> dict[str, str]:
    """Build the SearXNG ``/search`` query parameters of one ``web_search`` call.

    Used only by the FRE-1641 harness, which replays logged SearXNG searches (ADR-0158 D1.8).
    ``web_search`` itself no longer calls SearXNG (FRE-1766); FRE-1763 T2 removes this.

    Args:
        query: Search query text, after the weather-prefix strip.
        categories: Comma-separated SearXNG categories.
        engines: Comma-separated engine names, or ``None``.
        language: BCP-47 language code.
        time_range: Time filter, or ``None``.

    Returns:
        The query parameters.
    """
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
    return params


def shape_search_response(
    data: dict[str, Any],
    *,
    query: str,
    categories: str,
    engines: str | None,
    max_results: int,
) -> dict[str, Any]:
    """Shape a raw SearXNG JSON response into the ``web_search`` result.

    Used only by the FRE-1641 harness, which shapes recorded SearXNG responses (ADR-0158 D1.8).
    ``web_search`` itself no longer calls SearXNG (FRE-1766); FRE-1763 T2 removes this.

    Args:
        data: The parsed SearXNG JSON response.
        query: The query sent.
        categories: The categories sent.
        engines: The engines sent, or ``None``.
        max_results: The number of results to keep (already clamped to 1-50).

    Returns:
        The ``web_search`` result dict.
    """
    # SearXNG's own `number_of_results` total has been observed to read 0 on a
    # response whose `results` array held 20 real entries — it is independently
    # untrustworthy and must never be used as the count. `result_count` below is
    # always derived from `len(results)`.
    results = [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("content", ""),
            "engine": item.get("engine", ""),
            "score": item.get("score"),
        }
        for item in (data.get("results") or [])[:max_results]
    ]

    # FRE-1339: SearXNG reports engines it suspended or that errored in the same
    # response (`[engine_name, reason]` pairs) and we were discarding it — a
    # collapsed engine pool then looked identical to a healthy one everywhere we
    # look (result_count, HTTP status), while the surviving low-quality engines'
    # junk filled the model's context. Surfacing it here, keyed on engine health
    # rather than result_count, keeps a legitimately empty search for an obscure
    # query (healthy engines, zero hits) from tripping this signal.
    unresponsive_engines = [
        {"engine": str(entry[0]), "reason": str(entry[1])}
        for entry in (data.get("unresponsive_engines") or [])
        if isinstance(entry, list | tuple) and len(entry) >= 2
    ]
    engines_contributed = sorted({r["engine"] for r in results if r["engine"]})
    degraded_retrieval = bool(unresponsive_engines)

    # Plugin answers (timezone, unit converter, calculator, openmeteo weather).
    # These are returned in the top-level "answers" array rather than "results".
    answers: list[dict[str, Any]] = []
    for item in data.get("answers") or []:
        engine = item.get("engine", "")
        if engine == "openmeteo" or item.get("service") == "Open-meteo":
            # Weather answer: surface the current conditions summary + location.
            current = item.get("current", {})
            location = current.get("location", {})
            answers.append(
                {
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
                }
            )
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
        "unresponsive_engines": unresponsive_engines,
        "engines_contributed": engines_contributed,
        "degraded_retrieval": degraded_retrieval,
    }
    if degraded_retrieval:
        failed_names = ", ".join(e["engine"] for e in unresponsive_engines)
        output["retrieval_warning"] = (
            f"{len(unresponsive_engines)} engine(s) failed this search ({failed_names}); "
            f"results came only from: {', '.join(engines_contributed) or 'no engine'}. "
            "Treat this result set with caution — it may not be representative of what "
            "a healthy search would return."
        )
    return output


# ── Direct providers (FRE-1766) ─────────────────────────────────────────────

EXA_SEARCH_URL = "https://api.exa.ai/search"
BRAVE_LLM_CONTEXT_URL = "https://api.search.brave.com/res/v1/llm/context"

#: Brave LLM Context ``count``: how many pages Brave may draw its snippets from.
_BRAVE_COUNT = 20

ProviderName = Literal["exa", "brave"]
SearchSize = Literal["brief", "full"]
SearchCategory = Literal["general", "news", "it", "science"]
FailureKind = Literal["refusal", "bad_query"]
"""How a provider failed (FRE-1766), for the alert of FRE-1763 T3.

``refusal``: the provider did not serve the request — 401, 402, 403, 429, 5xx, a timeout, a
connection failure, a missing key or an unreadable response. ``bad_query``: any other 4xx,
so the provider rejected this query.
"""

#: ``api_costs`` model id of each provider's endpoint.
_COST_MODELS: Mapping[ProviderName, str] = {
    "exa": "exa/search",
    "brave": "brave/llm-context",
}

#: Domain lists of the ``it`` and ``science`` categories (owner decision, FRE-1766).
_CATEGORY_DOMAINS: Mapping[SearchCategory, tuple[str, ...]] = {
    "it": (
        "github.com",
        "stackoverflow.com",
        "developer.mozilla.org",
        "huggingface.co",
        "hub.docker.com",
    ),
    "science": (
        "arxiv.org",
        "pubmed.ncbi.nlm.nih.gov",
        "semanticscholar.org",
        "crossref.org",
        "openalex.org",
    ),
}

#: ``news`` category: Brave's freshness filter (past week).
_NEWS_FRESHNESS = "pw"

#: ``time_range`` argument -> (Brave ``freshness``, window for Exa ``startPublishedDate``).
_TIME_RANGES: Mapping[str, tuple[str, timedelta]] = {
    "day": ("pd", timedelta(days=1)),
    "week": ("pw", timedelta(weeks=1)),
    "month": ("pm", timedelta(days=31)),
    "year": ("py", timedelta(days=365)),
}

#: HTTP statuses that mean the provider refused to serve, not that the query was bad.
_REFUSAL_STATUSES = frozenset({401, 402, 403, 429})


@dataclass(frozen=True)
class SearchHit:
    """One page a provider returned.

    Attributes:
        title: Page title.
        url: Page URL, as the provider returned it.
        content: The provider's excerpts of the page, joined by a blank line. Line breaks
            and markdown tables are kept.
        provider: The provider that returned the page.
    """

    title: str
    url: str
    content: str
    provider: ProviderName


@dataclass(frozen=True)
class ProviderFailure:
    """A provider that did not return results for one search.

    Attributes:
        provider: The provider.
        kind: ``refusal`` or ``bad_query`` (see :data:`FailureKind`).
        reason: Short cause, such as ``HTTP 429`` or ``timeout``. Never holds a key.
    """

    provider: ProviderName
    kind: FailureKind
    reason: str


@dataclass(frozen=True)
class ProviderRequest:
    """The request a search sends to each provider.

    Attributes:
        exa_body: JSON body of the Exa ``/search`` POST.
        brave_params: Query parameters of the Brave LLM Context GET.
    """

    exa_body: Mapping[str, object]
    brave_params: Mapping[str, str | int]


def resolve_category(categories: str | None) -> SearchCategory:
    """Map the ``categories`` argument to one supported category.

    The first comma-separated name that is ``news``, ``it`` or ``science`` wins. Any other
    value, or none, is ``general``.

    Args:
        categories: The ``categories`` argument, or ``None``.

    Returns:
        The category the search uses.
    """
    for name in (categories or "").split(","):
        match name.strip().lower():
            case "news":
                return "news"
            case "it":
                return "it"
            case "science":
                return "science"
            case _:
                continue
    return "general"


def build_provider_request(
    query: str,
    *,
    category: SearchCategory,
    size: SearchSize,
    time_range: str | None,
    now: datetime | None = None,
) -> ProviderRequest:
    """Build the Exa and Brave requests of one search.

    Args:
        query: Search query text.
        category: The resolved category.
        size: ``brief`` or ``full``; picks the configured result sizes.
        time_range: ``day``, ``week``, ``month`` or ``year``, or ``None``. Any other value
            is ignored.
        now: Current time, for the Exa date filter. Defaults to the clock.

    Returns:
        The request to send to each provider.
    """
    if size == "full":
        num_results = settings.web_search_full_exa_num_results
        max_characters = settings.web_search_full_exa_max_characters
        brave_tokens = settings.web_search_full_brave_max_tokens
    else:
        num_results = settings.web_search_brief_exa_num_results
        max_characters = settings.web_search_brief_exa_max_characters
        brave_tokens = settings.web_search_brief_brave_max_tokens

    exa_body: dict[str, object] = {
        "query": query,
        "type": "auto",
        "numResults": num_results,
        "contents": {"highlights": {"maxCharacters": max_characters}},
    }
    brave_query = query
    freshness: str | None = None

    domains = _CATEGORY_DOMAINS.get(category)
    if domains:
        exa_body["includeDomains"] = list(domains)
        brave_query = f"{query} ({' OR '.join(f'site:{d}' for d in domains)})"
    if category == "news":
        exa_body["category"] = "news"
        freshness = _NEWS_FRESHNESS

    window = _TIME_RANGES.get((time_range or "").strip().lower())
    if window is not None:
        freshness = window[0]
        start = (now or datetime.now(UTC)) - window[1]
        exa_body["startPublishedDate"] = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    brave_params: dict[str, str | int] = {
        "q": brave_query,
        "count": _BRAVE_COUNT,
        "maximum_number_of_tokens": brave_tokens,
        "safesearch": "off",
    }
    if freshness is not None:
        brave_params["freshness"] = freshness
    return ProviderRequest(exa_body=exa_body, brave_params=brave_params)


def normalize_url(url: str) -> str:
    """Return the duplicate-detection key of a URL.

    Scheme and host are lowercased; the fragment and a trailing slash of the path are
    removed. The path and query keep their case.

    Args:
        url: A result URL.

    Returns:
        The key two results share when they name the same page.
    """
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _text(value: object) -> str:
    """Return ``value`` when it is a string, else an empty string."""
    return value if isinstance(value, str) else ""


def _joined(value: object) -> str:
    """Join a list of excerpt strings with a blank line; skip anything that is not a string."""
    if not isinstance(value, list):
        return ""
    return "\n\n".join(item for item in value if isinstance(item, str) and item)


def parse_exa_response(data: object) -> list[SearchHit]:
    """Read the hits of an Exa ``/search`` response.

    Args:
        data: The parsed JSON body.

    Returns:
        One hit per result that has a URL, in Exa's order.

    Raises:
        ValueError: The body is not an object with a ``results`` list.
    """
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise ValueError("Exa response has no results list")
    hits: list[SearchHit] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        url = _text(item.get("url"))
        if url:
            hits.append(
                SearchHit(
                    title=_text(item.get("title")),
                    url=url,
                    content=_joined(item.get("highlights")),
                    provider="exa",
                )
            )
    return hits


def parse_brave_response(data: object) -> list[SearchHit]:
    """Read the hits of a Brave LLM Context response (``grounding.generic``).

    Args:
        data: The parsed JSON body.

    Returns:
        One hit per page that has a URL, in Brave's order.

    Raises:
        ValueError: The body is not an object with a ``grounding`` object.
    """
    grounding = data.get("grounding") if isinstance(data, dict) else None
    if not isinstance(grounding, dict):
        raise ValueError("Brave response has no grounding object")
    generic = grounding.get("generic")
    hits: list[SearchHit] = []
    for item in generic if isinstance(generic, list) else []:
        if not isinstance(item, dict):
            continue
        url = _text(item.get("url"))
        if url:
            hits.append(
                SearchHit(
                    title=_text(item.get("title")),
                    url=url,
                    content=_joined(item.get("snippets")),
                    provider="brave",
                )
            )
    return hits


def merge_hits(*ranked: Sequence[SearchHit]) -> list[SearchHit]:
    """Interleave each provider's ranked hits and drop duplicate URLs.

    Rank 1 of each provider comes first, then rank 2, and so on, so neither provider's
    results all sit behind the other's. A hit whose :func:`normalize_url` key was already
    seen is dropped.

    Args:
        *ranked: Each provider's hits, in that provider's order.

    Returns:
        The merged hits.
    """
    merged: list[SearchHit] = []
    seen: set[str] = set()
    for rank in range(max((len(hits) for hits in ranked), default=0)):
        for hits in ranked:
            if rank >= len(hits):
                continue
            key = normalize_url(hits[rank].url)
            if key not in seen:
                seen.add(key)
                merged.append(hits[rank])
    return merged


def classify_status(status_code: int) -> FailureKind:
    """Classify an HTTP error status of a provider.

    Args:
        status_code: The response status.

    Returns:
        ``bad_query`` for a 4xx other than 401, 402, 403 and 429; ``refusal`` otherwise.
    """
    if 400 <= status_code < 500 and status_code not in _REFUSAL_STATUSES:
        return "bad_query"
    return "refusal"


def _secret(value: SecretStr | None) -> str:
    """Return a key's value, or an empty string when it is not set."""
    return value.get_secret_value().strip() if value is not None else ""


async def _call_provider(
    provider: ProviderName,
    client: httpx.AsyncClient,
    request: ProviderRequest,
    trace_id: str,
) -> list[SearchHit] | ProviderFailure:
    """Call one provider and read its hits; never raises except on cancellation.

    Args:
        provider: The provider to call.
        client: The guarded HTTP client shared by the search.
        request: The search's provider requests.
        trace_id: Trace id for logging.

    Returns:
        The provider's hits, or the failure.
    """
    key = _secret(settings.exa_api_key if provider == "exa" else settings.brave_api_key)
    if not key:
        log.warning(
            "web_search_failed",
            trace_id=trace_id,
            provider=provider,
            failure_kind="refusal",
            error="no API key configured",
        )
        return ProviderFailure(provider, "refusal", "no API key configured")
    try:
        # A total deadline per provider, below the tool's own timeout, so one slow provider
        # cannot make the tool time out and lose the other provider's results. The client's
        # timeout bounds each phase (connect, each read), not the whole call.
        async with asyncio.timeout(settings.web_search_timeout_seconds):
            if provider == "exa":
                response = await client.post(
                    EXA_SEARCH_URL, headers={"x-api-key": key}, json=dict(request.exa_body)
                )
            else:
                response = await client.get(
                    BRAVE_LLM_CONTEXT_URL,
                    headers={"x-subscription-token": key, "Accept": "application/json"},
                    params=dict(request.brave_params),
                )
        response.raise_for_status()
        data: object = response.json()
        return parse_exa_response(data) if provider == "exa" else parse_brave_response(data)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        kind = classify_status(status)
        log.warning(
            "web_search_failed",
            trace_id=trace_id,
            provider=provider,
            failure_kind=kind,
            status_code=status,
        )
        return ProviderFailure(provider, kind, f"HTTP {status}")
    except httpx.TimeoutException, TimeoutError:
        log.warning(
            "web_search_timeout",
            trace_id=trace_id,
            provider=provider,
            failure_kind="refusal",
            timeout_seconds=settings.web_search_timeout_seconds,
        )
        return ProviderFailure(provider, "refusal", "timeout")
    except httpx.ConnectError:
        log.warning(
            "web_search_connect_failed",
            trace_id=trace_id,
            provider=provider,
            failure_kind="refusal",
        )
        return ProviderFailure(provider, "refusal", "connection failed")
    except Exception as exc:  # noqa: BLE001 — any other failure is this provider's refusal
        log.warning(
            "web_search_failed",
            trace_id=trace_id,
            provider=provider,
            failure_kind="refusal",
            error_type=type(exc).__name__,
        )
        return ProviderFailure(provider, "refusal", type(exc).__name__)


def _cost_per_call_usd(provider: ProviderName) -> float:
    """Return the configured cost of one call to ``provider``."""
    if provider == "exa":
        return settings.exa_search_cost_per_call_usd
    return settings.brave_search_cost_per_call_usd


async def _record_search_cost(provider: ProviderName, ctx: TraceContext) -> None:
    """Write one ``api_costs`` row for a provider call that returned results (FRE-1656).

    Args:
        provider: The provider that served the call.
        ctx: Trace context that carries the trace and session ids.
    """
    await record_vendor_cost(
        provider=provider,
        model=_COST_MODELS[provider],
        tokens=0,
        cost_usd=_cost_per_call_usd(provider),
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        purpose="web_search",
    )


def _raise_both_failed(failures: Sequence[ProviderFailure], trace_id: str) -> NoReturn:
    """Raise the error of a search that no provider served.

    Args:
        failures: Each provider's failure.
        trace_id: Trace id for logging.

    Raises:
        WebSearchError: ``bad_query`` when every provider rejected the query, else
            ``search_unavailable``.
    """
    described = "; ".join(f"{f.provider}: {f.kind} ({f.reason})" for f in failures)
    log.error(
        "web_search_failed",
        trace_id=trace_id,
        failed_providers=[f"{f.provider}: {f.kind}: {f.reason}" for f in failures],
    )
    if all(f.kind == "bad_query" for f in failures):
        raise WebSearchError(
            "bad_query",
            f"Both search providers rejected the query. {described}.",
            retryable=False,
            next_action="Call web_search with a shorter or simpler query.",
        )
    raise WebSearchError(
        "search_unavailable",
        f"No search provider returned results. {described}.",
        retryable=True,
        next_action="Call web_search once more; if it fails again, tell the user.",
    )


async def web_search_executor(
    query: str = "",
    categories: str | None = None,
    size: str | None = None,
    time_range: str | None = None,
    *,
    ctx: TraceContext,
) -> dict[str, object]:
    """Search Exa and Brave LLM Context in parallel and merge their results (FRE-1766).

    Follows the executor contract of ``search_memory_executor``: keyword arguments that
    match the tool's parameter names, ``ctx`` for tracing, a plain dict on success (the
    ``ToolExecutionLayer`` wraps it in a ``ToolResult``), and ``WebSearchError`` on failure.

    Args:
        query: Search query text.
        categories: ``general``, ``news``, ``it`` or ``science``; anything else is general.
        size: ``brief`` (default) or ``full``; anything else is brief.
        time_range: ``day``, ``week``, ``month`` or ``year``, or ``None``.
        ctx: Trace context for logging and cost rows.

    Returns:
        Dict with ``results`` (each ``title``, ``url``, ``content``, ``provider``),
        ``result_count``, ``answers`` and ``infoboxes`` (always empty, kept for the
        grounding contract), ``query``, ``category``, ``size``, ``providers_contributed``,
        ``failed_providers`` (each ``provider``, ``kind``, ``reason``) and
        ``degraded_retrieval``. A ``retrieval_warning`` is added when a provider failed.

    Raises:
        WebSearchError: When the query is empty, or neither provider returned results.
    """
    query = (query or "").strip()
    if not query:
        raise WebSearchError(
            "missing_query",
            "query parameter is required and cannot be empty.",
            retryable=True,
            next_action="Call web_search again with a query.",
        )

    category = resolve_category(categories)
    resolved_size: SearchSize = "full" if (size or "").strip().lower() == "full" else "brief"
    trace_id = ctx.trace_id

    log.info(
        "web_search_started",
        trace_id=trace_id,
        query=query[:120],
        categories=category,
        size=resolved_size,
        time_range=time_range,
    )

    request = build_provider_request(
        query, category=category, size=resolved_size, time_range=time_range
    )
    providers: tuple[ProviderName, ProviderName] = ("exa", "brave")
    async with create_guarded_http_client(
        timeout=settings.web_search_timeout_seconds,
    ) as client:
        outcomes = await asyncio.gather(
            *(_call_provider(p, client, request, trace_id) for p in providers)
        )

    ranked: list[list[SearchHit]] = []
    failures: list[ProviderFailure] = []
    contributed: list[ProviderName] = []
    for provider, outcome in zip(providers, outcomes, strict=True):
        if isinstance(outcome, ProviderFailure):
            failures.append(outcome)
        else:
            ranked.append(outcome)
            contributed.append(provider)
            await _record_search_cost(provider, ctx)

    if not contributed:
        # FRE-1768: one owner alert per outage when both providers refused.
        web_search_alerts.note_search_failed(failures, trace_id=trace_id)
        _raise_both_failed(failures, trace_id)
    # FRE-1768: a served search ends an outage and may take the month to the budget.
    web_search_alerts.note_search_served(
        served_cost_usd=sum(_cost_per_call_usd(p) for p in contributed), trace_id=trace_id
    )

    hits = merge_hits(*ranked)
    output: dict[str, object] = {
        "answers": [],
        "results": [
            {"title": h.title, "url": h.url, "content": h.content, "provider": h.provider}
            for h in hits
        ],
        "result_count": len(hits),
        "infoboxes": [],
        "query": query,
        "category": category,
        "size": resolved_size,
        "providers_contributed": sorted(contributed),
        "failed_providers": [
            {"provider": f.provider, "kind": f.kind, "reason": f.reason} for f in failures
        ],
        "degraded_retrieval": bool(failures),
    }
    if failures:
        failed = ", ".join(f"{f.provider} ({f.reason})" for f in failures)
        output["retrieval_warning"] = (
            f"A search provider failed this search: {failed}. Results came only from "
            f"{', '.join(sorted(contributed))}. Treat this result set with caution — it may "
            "miss what a healthy search would return."
        )

    log.info(
        "web_search_completed",
        trace_id=trace_id,
        result_count=len(hits),
        result_urls=[h.url for h in hits],
        # Flattened to "provider: kind: reason" strings. The name is kept from the SearXNG
        # path so existing telemetry queries keep reading it.
        unresponsive_engine_reasons=[f"{f.provider}: {f.kind}: {f.reason}" for f in failures],
        degraded_retrieval=bool(failures),
    )
    return output
