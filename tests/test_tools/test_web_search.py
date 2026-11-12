"""Unit tests for the web_search native tool (FRE-1766).

``web_search`` calls Exa and Brave LLM Context directly. Provider HTTP runs on
``httpx.MockTransport`` with responses recorded from one live call to each provider
(``fixtures/web_search``), so no network and no key is needed.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.web import (
    BRAVE_LLM_CONTEXT_URL,
    EXA_SEARCH_URL,
    SearchHit,
    WebSearchError,
    build_provider_request,
    classify_status,
    merge_hits,
    normalize_url,
    resolve_category,
    web_search_executor,
    web_search_tool,
)

_CTX = TraceContext.new_trace()


# ── Direct providers (FRE-1766) ────────────────────────────────────────────

_FIXTURES = Path(__file__).parent / "fixtures" / "web_search"
_EXA_KEY = "test-exa-key-not-real"
_BRAVE_KEY = "test-brave-key-not-real"


def _fixture(name: str) -> dict:
    """Load a recorded provider response (one live call each, 2026-11-11, trimmed)."""
    return json.loads((_FIXTURES / name).read_text())


Reply = httpx.Response | Exception


class _Providers:
    """A fake Exa and Brave behind ``httpx.MockTransport``; records each request it gets."""

    def __init__(self, exa: Reply | None = None, brave: Reply | None = None) -> None:
        self.exa = exa if exa is not None else httpx.Response(200, json=_fixture("exa_search.json"))
        self.brave = (
            brave
            if brave is not None
            else httpx.Response(200, json=_fixture("brave_llm_context.json"))
        )
        self.requests: dict[str, httpx.Request] = {}

    def _handle(self, request: httpx.Request) -> httpx.Response:
        name = "exa" if request.url.host == "api.exa.ai" else "brave"
        self.requests[name] = request
        reply = self.exa if name == "exa" else self.brave
        if isinstance(reply, Exception):
            raise reply
        return reply

    def client(self, **kwargs: object) -> httpx.AsyncClient:
        """Stand in for ``create_guarded_http_client``."""
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    @property
    def exa_body(self) -> dict:
        return json.loads(self.requests["exa"].content)

    @property
    def brave_params(self) -> dict[str, str]:
        return dict(self.requests["brave"].url.params)


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set both provider keys to fake values."""
    monkeypatch.setattr("personal_agent.tools.web.settings.exa_api_key", SecretStr(_EXA_KEY))
    monkeypatch.setattr("personal_agent.tools.web.settings.brave_api_key", SecretStr(_BRAVE_KEY))


async def _search(providers: _Providers, **kwargs: object) -> dict:
    with (
        patch("personal_agent.tools.web.create_guarded_http_client", providers.client),
        patch("personal_agent.tools.web.record_vendor_cost", AsyncMock()),
    ):
        return await web_search_executor(ctx=_CTX, **{"query": "paris temperature", **kwargs})


def _error(status: int, url: str) -> httpx.Response:
    return httpx.Response(status, json={"error": "x"}, request=httpx.Request("GET", url))


# ── Tool definition ─────────────────────────────────────────────────────────


def test_web_search_tool_definition() -> None:
    """Tool has correct metadata for governance and LLM awareness."""
    assert web_search_tool.name == "web_search"
    assert web_search_tool.category == "network"
    assert web_search_tool.risk_level == "low"
    assert set(web_search_tool.allowed_modes) == {"NORMAL", "ALERT", "DEGRADED"}
    assert [p.name for p in web_search_tool.parameters] == [
        "query",
        "categories",
        "size",
        "time_range",
    ]
    assert [p.name for p in web_search_tool.parameters if p.required] == ["query"]
    size = next(p for p in web_search_tool.parameters if p.name == "size")
    assert size.json_schema == {"type": "string", "enum": ["brief", "full"]}


def test_web_search_description_states_when_to_reach_for_it() -> None:
    """Lead with when to use it, and say who sees the query."""
    description = web_search_tool.description
    assert description.startswith("Search the live web when")
    assert "Prefer perplexity_query" not in description
    assert "Exa" in description and "Brave" in description
    assert "third-party" in description
    assert "SearXNG" not in description
    # FRE-1766 owner decision 4: plugin answers are gone, so the text must not offer them.
    assert "Timezone" not in description and "Calculator" not in description
    assert "Categories: general (default), news, it, science." in description


# ── AC-1: one merged list from saved responses ──────────────────────────────


@pytest.mark.asyncio
async def test_saved_responses_merge_into_one_list_without_duplicate_urls(keys: None) -> None:
    """AC-1: both recorded responses become one list; the 3 shared URLs appear once."""
    exa_urls = [r["url"] for r in _fixture("exa_search.json")["results"]]
    brave_urls = [g["url"] for g in _fixture("brave_llm_context.json")["grounding"]["generic"]]
    shared = {normalize_url(u) for u in exa_urls} & {normalize_url(u) for u in brave_urls}
    assert len(shared) == 3  # the recorded responses really overlap

    result = await _search(_Providers())

    urls = [r["url"] for r in result["results"]]
    assert len({normalize_url(u) for u in urls}) == len(urls)
    assert result["result_count"] == len(exa_urls) + len(brave_urls) - 3 == 6
    assert {r["provider"] for r in result["results"]} == {"exa", "brave"}
    assert result["providers_contributed"] == ["brave", "exa"]
    assert result["degraded_retrieval"] is False
    assert result["failed_providers"] == []
    assert "retrieval_warning" not in result
    # Grounding contract: a results list of url-bearing objects.
    base = {"title", "url", "content", "provider"}
    assert all(set(r) - {"published_date"} == base for r in result["results"])


@pytest.mark.asyncio
async def test_saved_responses_keep_line_breaks_and_markdown_tables(keys: None) -> None:
    """AC-1: the excerpts keep their newlines and the markdown table rows (SearXNG lost both)."""
    result = await _search(_Providers())

    by_provider = {
        p: [r for r in result["results"] if r["provider"] == p] for p in ("exa", "brave")
    }
    assert any("\n" in r["content"] for r in by_provider["exa"])
    table = next(r["content"] for r in result["results"] if "| --- |" in r["content"])
    assert "\n| January | 1.7 | 7.6 | 4.6 |\n" in table


def test_normalize_url_folds_case_trailing_slash_and_fragment() -> None:
    """Owner decision 1: scheme/host lowercase, no trailing slash, no fragment."""
    assert (
        normalize_url("HTTPS://Example.COM/Path/#top")
        == normalize_url("https://example.com/Path")
        == "https://example.com/Path"
    )
    # Path case and the query string still tell pages apart.
    assert normalize_url("https://example.com/path") != normalize_url("https://example.com/Path")
    assert normalize_url("https://example.com/a?x=1") != normalize_url("https://example.com/a")


def test_merge_interleaves_by_rank_and_keeps_the_first_duplicate() -> None:
    """Rank 1 of each provider first; a later duplicate URL is dropped."""
    exa = [
        SearchHit("E1", "https://a.example/1", "e1", "exa"),
        SearchHit("E2", "https://b.example/2/", "e2", "exa"),
    ]
    brave = [
        SearchHit("B1", "https://C.example/3#x", "b1", "brave"),
        SearchHit("B2", "https://B.example/2", "b2", "brave"),
        SearchHit("B3", "https://d.example/4", "b3", "brave"),
    ]
    assert [h.title for h in merge_hits(exa, brave)] == ["E1", "B1", "E2", "B3"]


# ── AC-2: size sends the configured sizes ───────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("size", "num", "chars", "tokens"),
    [(None, 10, 800, 2048), ("brief", 10, 800, 2048), ("full", 10, 2000, 8192)],
)
async def test_size_sends_the_configured_sizes(
    keys: None, size: str | None, num: int, chars: int, tokens: int
) -> None:
    """AC-2: brief (the default) and full send the owner's sizes to both providers."""
    providers = _Providers()
    kwargs = {} if size is None else {"size": size}

    result = await _search(providers, **kwargs)

    assert providers.exa_body["numResults"] == num
    assert providers.exa_body["contents"] == {"highlights": {"maxCharacters": chars}}
    assert providers.brave_params["maximum_number_of_tokens"] == str(tokens)
    assert providers.brave_params["count"] == "20"
    assert result["size"] == (size or "brief")


@pytest.mark.asyncio
async def test_size_values_come_from_settings(keys: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-2: the sizes are settings, not constants."""
    monkeypatch.setattr("personal_agent.tools.web.settings.web_search_full_exa_num_results", 7)
    monkeypatch.setattr("personal_agent.tools.web.settings.web_search_full_exa_max_characters", 99)
    monkeypatch.setattr("personal_agent.tools.web.settings.web_search_full_brave_max_tokens", 1234)
    providers = _Providers()

    await _search(providers, size="full")

    assert providers.exa_body["numResults"] == 7
    assert providers.exa_body["contents"]["highlights"]["maxCharacters"] == 99
    assert providers.brave_params["maximum_number_of_tokens"] == "1234"


# ── AC-3: categories ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_news_category_sets_exa_news_and_brave_past_week(keys: None) -> None:
    """AC-3: news → Exa category news + Brave freshness pw; no domain filter."""
    providers = _Providers()

    result = await _search(providers, categories="news")

    assert providers.exa_body["category"] == "news"
    assert "includeDomains" not in providers.exa_body
    assert providers.brave_params["freshness"] == "pw"
    assert providers.brave_params["q"] == "paris temperature"
    assert result["category"] == "news"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "domains"),
    [
        (
            "it",
            [
                "github.com",
                "stackoverflow.com",
                "developer.mozilla.org",
                "huggingface.co",
                "hub.docker.com",
            ],
        ),
        (
            "science",
            [
                "arxiv.org",
                "pubmed.ncbi.nlm.nih.gov",
                "semanticscholar.org",
                "crossref.org",
                "openalex.org",
            ],
        ),
    ],
)
async def test_domain_categories_filter_both_providers(
    keys: None, category: str, domains: list[str]
) -> None:
    """AC-3: it/science → Exa includeDomains + Brave ``(site:a OR site:b ...)`` on the query."""
    providers = _Providers()

    await _search(providers, query="asyncio cancel", categories=category)

    assert providers.exa_body["includeDomains"] == domains
    assert "category" not in providers.exa_body
    sites = " OR ".join(f"site:{d}" for d in domains)
    assert providers.brave_params["q"] == f"asyncio cancel ({sites})"
    assert "freshness" not in providers.brave_params


@pytest.mark.asyncio
@pytest.mark.parametrize("category", [None, "general", "weather", "recipes", "exa", "social_media"])
async def test_other_categories_search_the_general_web(keys: None, category: str | None) -> None:
    """AC-3: any other category → general, with no filter on either provider."""
    providers = _Providers()
    kwargs = {} if category is None else {"categories": category}

    result = await _search(providers, **kwargs)

    assert set(providers.exa_body) == {"query", "type", "numResults", "contents"}
    assert providers.exa_body["type"] == "auto"
    assert set(providers.brave_params) == {"q", "count", "maximum_number_of_tokens", "safesearch"}
    assert providers.brave_params["safesearch"] == "off"
    assert result["category"] == "general"


def test_first_supported_name_in_a_list_wins() -> None:
    """A comma list (the old SearXNG form) resolves to its first supported category."""
    assert resolve_category("general,it") == "it"
    assert resolve_category(" News ") == "news"
    assert resolve_category("weather,science,news") == "science"


def test_time_range_filters_both_providers() -> None:
    """``time_range`` keeps working: Brave freshness and an Exa start date."""
    now = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
    request = build_provider_request(
        "q", category="news", size="brief", time_range="month", now=now
    )
    assert request.brave_params["freshness"] == "pm"  # overrides news' past week
    assert request.exa_body["startPublishedDate"] == "2026-10-11T12:00:00.000Z"
    ignored = build_provider_request("q", category="general", size="brief", time_range="decade")
    assert "freshness" not in ignored.brave_params
    assert "startPublishedDate" not in ignored.exa_body


# ── AC-4: one provider fails / both fail ────────────────────────────────────


@pytest.mark.asyncio
async def test_exa_refusal_returns_brave_results_and_names_exa(keys: None) -> None:
    """AC-4: Exa answers 429 → Brave's results, with Exa named as failed."""
    result = await _search(_Providers(exa=_error(429, EXA_SEARCH_URL)))

    assert result["result_count"] == 5
    assert {r["provider"] for r in result["results"]} == {"brave"}
    assert result["failed_providers"] == [
        {"provider": "exa", "kind": "refusal", "reason": "HTTP 429"}
    ]
    assert result["providers_contributed"] == ["brave"]
    assert result["degraded_retrieval"] is True
    assert "exa (HTTP 429)" in result["retrieval_warning"]


@pytest.mark.asyncio
async def test_brave_timeout_returns_exa_results_and_names_brave(keys: None) -> None:
    """AC-4: Brave times out → Exa's results, with Brave named as failed."""
    result = await _search(_Providers(brave=httpx.ReadTimeout("slow")))

    assert {r["provider"] for r in result["results"]} == {"exa"}
    assert result["failed_providers"] == [
        {"provider": "brave", "kind": "refusal", "reason": "timeout"}
    ]


@pytest.mark.asyncio
async def test_a_hung_provider_fails_alone_at_its_deadline(
    keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider that never answers fails at its own deadline; the other's results return."""
    monkeypatch.setattr("personal_agent.tools.web.settings.web_search_timeout_seconds", 0.05)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.exa.ai":
            await asyncio.Event().wait()  # hangs until the deadline cancels it
        return httpx.Response(200, json=_fixture("brave_llm_context.json"))

    def client(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with (
        patch("personal_agent.tools.web.create_guarded_http_client", client),
        patch("personal_agent.tools.web.record_vendor_cost", AsyncMock()),
    ):
        result = await asyncio.wait_for(web_search_executor(query="q", ctx=_CTX), timeout=5)

    assert result["providers_contributed"] == ["brave"]
    assert result["failed_providers"] == [
        {"provider": "exa", "kind": "refusal", "reason": "timeout"}
    ]


@pytest.mark.asyncio
async def test_a_missing_key_leaves_that_provider_out_without_a_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider with no key (or an empty one) is a refusal, and no request is sent."""
    monkeypatch.setattr("personal_agent.tools.web.settings.exa_api_key", SecretStr(""))
    monkeypatch.setattr("personal_agent.tools.web.settings.brave_api_key", SecretStr(_BRAVE_KEY))
    providers = _Providers()

    result = await _search(providers)

    assert "exa" not in providers.requests
    assert result["failed_providers"] == [
        {"provider": "exa", "kind": "refusal", "reason": "no API key configured"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exa", "brave", "code"),
    [
        (_error(500, EXA_SEARCH_URL), httpx.ConnectError("refused"), "search_unavailable"),
        (_error(400, EXA_SEARCH_URL), _error(402, BRAVE_LLM_CONTEXT_URL), "search_unavailable"),
        (_error(400, EXA_SEARCH_URL), _error(422, BRAVE_LLM_CONTEXT_URL), "bad_query"),
    ],
)
async def test_both_failing_is_an_error_result(
    keys: None, exa: Reply, brave: Reply, code: str
) -> None:
    """AC-4: both providers fail → an error naming both; bad_query only when both rejected it."""
    with pytest.raises(WebSearchError) as exc_info:
        await _search(_Providers(exa=exa, brave=brave))

    assert exc_info.value.code == code
    assert "exa:" in str(exc_info.value) and "brave:" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "body", "reason"),
    [
        ("exa", httpx.Response(200, text="<html>"), "JSONDecodeError"),
        ("exa", httpx.Response(200, json={"error": "x"}), "ValueError"),
        ("brave", httpx.Response(200, json=["not", "an", "object"]), "ValueError"),
    ],
)
async def test_an_unreadable_body_is_a_refusal(
    keys: None, provider: str, body: httpx.Response, reason: str
) -> None:
    """A 200 whose body is not the provider's shape is that provider's failure, not a crash."""
    result = await _search(_Providers(**{provider: body}))

    assert result["failed_providers"] == [
        {"provider": provider, "kind": "refusal", "reason": reason}
    ]


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, "refusal"),
        (402, "refusal"),
        (403, "refusal"),
        (429, "refusal"),
        (500, "refusal"),
        (503, "refusal"),
        (404, "refusal"),
        (405, "refusal"),
        (400, "bad_query"),
        (410, "bad_query"),
        (422, "bad_query"),
    ],
)
def test_classify_status(status: int, kind: str) -> None:
    """ADR-0160 D1.4: refusal (401/402/403/404/405/429/5xx) vs bad query (other 4xx)."""
    assert classify_status(status) == kind


@pytest.mark.asyncio
async def test_failure_logs_and_results_never_hold_a_key(keys: None) -> None:
    """The keys go only in request headers: never in the result, the error or a log event."""
    providers = _Providers(exa=_error(401, EXA_SEARCH_URL))
    with patch("personal_agent.tools.web.log") as mock_log:
        result = await _search(providers)

    assert providers.requests["exa"].headers["x-api-key"] == _EXA_KEY
    assert providers.requests["brave"].headers["x-subscription-token"] == _BRAVE_KEY
    assert _BRAVE_KEY not in str(providers.requests["brave"].url)
    logged = repr(mock_log.mock_calls)
    for key in (_EXA_KEY, _BRAVE_KEY):
        assert key not in json.dumps(result)
        assert key not in logged


@pytest.mark.asyncio
async def test_cancelling_the_search_cancels_both_provider_calls(keys: None) -> None:
    """A tool timeout cancels the search; neither provider call is left running."""
    started: list[str] = []
    cancelled: list[str] = []
    both_started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.host
        started.append(name)
        if len(started) == 2:
            both_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(name)
            raise
        raise AssertionError("unreachable")

    def client(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with patch("personal_agent.tools.web.create_guarded_http_client", client):
        task = asyncio.create_task(web_search_executor(query="q", ctx=_CTX))
        await asyncio.wait_for(both_started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert sorted(cancelled) == ["api.exa.ai", "api.search.brave.com"]


# ── AC-5: one api_costs row per successful provider call ────────────────────


async def _costs(providers: _Providers) -> AsyncMock:
    record = AsyncMock()
    ctx = TraceContext(trace_id=uuid4().hex, session_id=str(uuid4()))
    with (
        patch("personal_agent.tools.web.create_guarded_http_client", providers.client),
        patch("personal_agent.tools.web.record_vendor_cost", record),
    ):
        try:
            await web_search_executor(query="flights", ctx=ctx)
        except WebSearchError:
            pass
    for call in record.await_args_list:
        assert call.kwargs["trace_id"] == ctx.trace_id
        assert call.kwargs["session_id"] == ctx.session_id
    return record


@pytest.mark.asyncio
async def test_each_successful_provider_call_writes_one_cost_row(keys: None) -> None:
    """AC-5: both providers served → one row each, named by provider, non-zero."""
    record = await _costs(_Providers())

    calls = {c.kwargs["provider"]: c.kwargs for c in record.await_args_list}
    assert record.await_count == 2
    assert calls["exa"]["model"] == "exa/search"
    assert calls["brave"]["model"] == "brave/llm-context"
    for call in calls.values():
        assert call["cost_usd"] > 0
        assert call["purpose"] == "web_search"


@pytest.mark.asyncio
async def test_a_failed_provider_call_writes_no_cost_row(keys: None) -> None:
    """AC-5: only the provider that returned results is billed."""
    record = await _costs(_Providers(brave=_error(503, BRAVE_LLM_CONTEXT_URL)))
    assert [c.kwargs["provider"] for c in record.await_args_list] == ["exa"]

    record = await _costs(
        _Providers(exa=_error(503, EXA_SEARCH_URL), brave=_error(503, BRAVE_LLM_CONTEXT_URL))
    )
    record.assert_not_awaited()


# ── Telemetry ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_completed_log_carries_result_urls_and_provider_health(keys: None) -> None:
    """``web_search_completed`` keeps ``result_urls`` (FRE-1339 provenance) and the failures."""
    with patch("personal_agent.tools.web.log") as mock_log:
        result = await _search(_Providers(exa=_error(429, EXA_SEARCH_URL)))

    completed = next(c for c in mock_log.info.call_args_list if c.args[0] == "web_search_completed")
    assert completed.kwargs["result_urls"] == [r["url"] for r in result["results"]]
    assert completed.kwargs["degraded_retrieval"] is True
    assert completed.kwargs["unresponsive_engine_reasons"] == ["exa: refusal: HTTP 429"]
    assert completed.kwargs["trace_id"] == _CTX.trace_id
    failed = next(c for c in mock_log.warning.call_args_list if c.args[0] == "web_search_failed")
    assert failed.kwargs["provider"] == "exa"
    assert failed.kwargs["failure_kind"] == "refusal"


# ── FRE-1770: ADR-0160 D9.8 telemetry, D9.3 cost, D4/D9.1/D9.6 shape, D9.7 text ──


def _exa_with_cost(total: object, results: list | None = None) -> httpx.Response:
    """The recorded Exa response plus a ``costDollars`` block (the fixture is trimmed)."""
    body = _fixture("exa_search.json")
    body["costDollars"] = {"total": total}
    if results is not None:
        body["results"] = results
    return httpx.Response(200, json=body)


def _logged(mock_log: object, event: str) -> dict:
    return next(c for c in mock_log.info.call_args_list if c.args[0] == event).kwargs  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_fre1770_ac1_started_and_completed_carry_size(keys: None) -> None:
    """AC-1: both events carry the resolved size."""
    with patch("personal_agent.tools.web.log") as mock_log:
        await _search(_Providers(), size="full")

    assert _logged(mock_log, "web_search_started")["size"] == "full"
    assert _logged(mock_log, "web_search_completed")["size"] == "full"


@pytest.mark.asyncio
async def test_fre1770_ac1_completed_carries_result_chars_and_tokens(keys: None) -> None:
    """AC-1: length and contract token count of the shaped result rendered as JSON."""
    from personal_agent.tools.contract import result_tokens

    with patch("personal_agent.tools.web.log") as mock_log:
        result = await _search(_Providers())

    rendered = json.dumps({"ok": True, **result}, default=str)
    completed = _logged(mock_log, "web_search_completed")
    assert completed["result_chars"] == len(rendered)
    assert completed["result_tokens"] == result_tokens(rendered)


@pytest.mark.asyncio
async def test_fre1770_ac1_provider_outcomes_when_both_serve(keys: None) -> None:
    """AC-1: one entry per provider; Exa carries cost_usd from costDollars.total."""
    with patch("personal_agent.tools.web.log") as mock_log:
        await _search(_Providers(exa=_exa_with_cost(0.0123)))

    outcomes = {
        o["provider"]: o for o in _logged(mock_log, "web_search_completed")["provider_outcomes"]
    }
    assert set(outcomes) == {"exa", "brave"}
    exa, brave = outcomes["exa"], outcomes["brave"]
    assert exa["http_status"] == 200 and brave["http_status"] == 200
    assert exa["outcome"] == "ok" and brave["outcome"] == "ok"
    assert exa["result_count"] == 4 and brave["result_count"] == 5
    assert isinstance(exa["latency_ms"], int) and exa["latency_ms"] >= 0
    assert isinstance(brave["latency_ms"], int) and brave["latency_ms"] >= 0
    assert exa["cost_usd"] == 0.0123
    assert "cost_usd" not in brave


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exa", "status", "outcome"),
    [
        (_error(429, EXA_SEARCH_URL), 429, "refused"),
        (_error(400, EXA_SEARCH_URL), 400, "bad_query"),
        (httpx.ReadTimeout("slow"), None, "refused"),
        (httpx.ConnectError("down"), None, "refused"),
    ],
)
async def test_fre1770_ac1_provider_outcomes_of_a_failed_provider(
    keys: None, exa: Reply, status: int | None, outcome: str
) -> None:
    """AC-1: a failed provider's outcome; http_status is absent without an HTTP response."""
    with patch("personal_agent.tools.web.log") as mock_log:
        await _search(_Providers(exa=exa))

    outcomes = {
        o["provider"]: o for o in _logged(mock_log, "web_search_completed")["provider_outcomes"]
    }
    entry = outcomes["exa"]
    assert entry["outcome"] == outcome
    assert entry["result_count"] == 0
    assert isinstance(entry["latency_ms"], int)
    if status is None:
        assert "http_status" not in entry
    else:
        assert entry["http_status"] == status
    assert entry["cost_usd"] is None  # no 2xx, so no api_costs row
    assert outcomes["brave"]["outcome"] == "ok"


def _rows(record: AsyncMock) -> dict[str, float]:
    return {c.kwargs["provider"]: c.kwargs["cost_usd"] for c in record.await_args_list}


@pytest.mark.asyncio
async def test_fre1770_ac2_exa_writes_cost_dollars_total(keys: None) -> None:
    """AC-2: the Exa row is costDollars.total from the response, not the setting."""
    record = await _costs(_Providers(exa=_exa_with_cost(0.0123)))
    assert record.await_count == 2
    assert _rows(record) == {"exa": 0.0123, "brave": 0.005}


@pytest.mark.asyncio
@pytest.mark.parametrize("cost_dollars", [None, {}, {"total": "x"}, {"total": True}])
async def test_fre1770_ac2_exa_without_a_readable_total_writes_the_setting(
    keys: None, monkeypatch: pytest.MonkeyPatch, cost_dollars: object
) -> None:
    """AC-2: a parsed body with no usable costDollars.total falls back to the setting."""
    monkeypatch.setattr("personal_agent.tools.web.settings.exa_search_cost_per_call_usd", 0.0071)
    body = _fixture("exa_search.json")
    if cost_dollars is not None:
        body["costDollars"] = cost_dollars
    record = await _costs(_Providers(exa=httpx.Response(200, json=body)))
    assert _rows(record)["exa"] == 0.0071


@pytest.mark.asyncio
async def test_fre1770_ac2_a_2xx_with_zero_results_still_writes_a_row(keys: None) -> None:
    """AC-2: the vendor bills an empty answer, so both rows are written."""
    empty_brave = httpx.Response(200, json={"grounding": {"generic": []}, "sources": {}})
    record = await _costs(_Providers(exa=_exa_with_cost(0.006, results=[]), brave=empty_brave))
    assert _rows(record) == {"exa": 0.006, "brave": 0.005}


@pytest.mark.asyncio
async def test_fre1770_ac2_an_unparseable_2xx_writes_the_setting(
    keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: a 2xx whose body does not parse is billed at the setting, even when both fail."""
    monkeypatch.setattr("personal_agent.tools.web.settings.exa_search_cost_per_call_usd", 0.0072)
    monkeypatch.setattr("personal_agent.tools.web.settings.brave_search_cost_per_call_usd", 0.0052)
    record = await _costs(
        _Providers(exa=httpx.Response(200, text="<html>"), brave=httpx.Response(200, text="{"))
    )
    assert _rows(record) == {"exa": 0.0072, "brave": 0.0052}


@pytest.mark.asyncio
async def test_fre1770_ac2_a_call_without_2xx_writes_no_row(keys: None) -> None:
    """AC-2: an error status, a timeout or a missing key writes no row."""
    record = await _costs(
        _Providers(exa=_error(402, EXA_SEARCH_URL), brave=httpx.ReadTimeout("slow"))
    )
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_fre1770_ac2_alert_gets_the_sum_of_the_rows(keys: None) -> None:
    """AC-2 / FRE-1768: the budget check adds what this search's rows add up to."""
    served = patch("personal_agent.tools.web.web_search_alerts.note_search_served")
    with served as note:
        await _costs(_Providers(exa=_exa_with_cost(0.0123)))
    assert note.call_args.kwargs["served_cost_usd"] == pytest.approx(0.0123 + 0.005)


@pytest.mark.asyncio
async def test_fre1770_ac2_logged_exa_cost_equals_its_row(keys: None) -> None:
    """AC-5's offline half: the logged Exa cost_usd is the value of its api_costs row."""
    record = AsyncMock()
    with (
        patch(
            "personal_agent.tools.web.create_guarded_http_client",
            _Providers(exa=_exa_with_cost(0.0099)).client,
        ),
        patch("personal_agent.tools.web.record_vendor_cost", record),
        patch("personal_agent.tools.web.log") as mock_log,
    ):
        await web_search_executor(query="q", ctx=_CTX)
    outcomes = {
        o["provider"]: o for o in _logged(mock_log, "web_search_completed")["provider_outcomes"]
    }
    assert outcomes["exa"]["cost_usd"] == _rows(record)["exa"]


@pytest.mark.asyncio
async def test_fre1770_ac3_result_has_no_answers_infoboxes_or_score(keys: None) -> None:
    """AC-3 (D4, D9.1): the plugin fields and score are gone."""
    result = await _search(_Providers())
    assert "answers" not in result and "infoboxes" not in result
    assert all("score" not in r for r in result["results"])


@pytest.mark.asyncio
async def test_fre1770_ac3_results_carry_published_date_when_given(keys: None) -> None:
    """AC-3 (D9.1): Exa publishedDate and Brave sources[url].age become published_date."""
    brave = _fixture("brave_llm_context.json")
    first, second = (g["url"] for g in brave["grounding"]["generic"][1:3])
    brave["sources"] = {
        first: {"title": "x", "age": ["Monday, March 2, 2026", "2026-03-02", "254 days ago"]},
        second: {"title": "y", "age": "2025-12-01"},
    }
    result = await _search(_Providers(brave=httpx.Response(200, json=brave)))

    by_url = {r["url"]: r for r in result["results"]}
    assert by_url["https://info-paris.fr/climat-de-paris/"]["published_date"] == (
        "2026-08-25T11:32:15.000Z"
    )
    assert by_url[second]["published_date"] == "2025-12-01"
    assert by_url[second]["provider"] == "brave"
    # ``first`` is also Exa's rank-1 URL, which has no date; the merge keeps the Exa entry.
    assert by_url[first]["provider"] == "exa"
    # No date from the provider, no key: the other entries carry none.
    dated = {u for u, r in by_url.items() if "published_date" in r}
    assert dated == {"https://info-paris.fr/climat-de-paris/", second}


def test_fre1770_ac3_brave_age_list_prefers_the_iso_date() -> None:
    """AC-3: Brave's age list gives its ISO date; a list without one gives its first text."""
    from personal_agent.tools.web import parse_brave_response

    data = {
        "grounding": {
            "generic": [
                {"url": "https://a.example/", "title": "A", "snippets": ["s"]},
                {"url": "https://b.example/", "title": "B", "snippets": ["s"]},
            ]
        },
        "sources": {
            "https://a.example/": {"age": ["Monday, March 2, 2026", "2026-03-02", "254 days ago"]},
            "https://b.example/": {"age": ["3 days ago"]},
        },
    }
    a, b = parse_brave_response(data)
    assert a.published_date == "2026-03-02"
    assert b.published_date == "3 days ago"


def test_fre1770_ac3_web_search_has_no_call_level_source_keys() -> None:
    """AC-3 (D9.6): with answers and infoboxes gone, the call-level tuple is empty."""
    from personal_agent.grounding.source_registry import ENTRY_SOURCE_TOOLS

    assert ENTRY_SOURCE_TOOLS["web_search"] == ()


def test_fre1770_ac4_description_points_weather_to_the_weather_tool() -> None:
    """AC-4 (D9.7): weather questions go to ``weather``; no plugin examples or engine list."""
    description = web_search_tool.description
    assert "weather tool" in description
    assert "engines" not in description.lower()
    for plugin in ("Timezone", "time zone", "Calculator", "currency", "Currency"):
        assert plugin not in description
    assert not any(p.name == "engines" for p in web_search_tool.parameters)


# ── Executor errors and governance ──────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "   "])
async def test_web_search_empty_query_raises(query: str) -> None:
    """An empty or whitespace query raises with a descriptive message."""
    with pytest.raises(ToolExecutionError, match="query parameter is required"):
        await web_search_executor(query=query, ctx=_CTX)


@pytest.mark.asyncio
async def test_web_search_blocked_in_lockdown_mode() -> None:
    """ToolExecutionLayer returns permission-denied ToolResult in LOCKDOWN mode."""
    from personal_agent.brainstem.mode_manager import ModeManager
    from personal_agent.config.governance_loader import load_governance_config
    from personal_agent.governance.models import Mode
    from personal_agent.tools.executor import ToolExecutionLayer
    from personal_agent.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(web_search_tool, web_search_executor)

    gov = load_governance_config()
    mode_mgr = ModeManager(governance_config=gov)

    with patch.object(mode_mgr, "get_current_mode", return_value=Mode.LOCKDOWN):
        layer = ToolExecutionLayer(registry=reg, governance_config=gov, mode_manager=mode_mgr)
        trace_ctx = TraceContext.new_trace()
        result = await layer.execute_tool("web_search", {"query": "test"}, trace_ctx)

    assert result.success is False
    assert result.error is not None
    assert "LOCKDOWN" in result.error or "not allowed" in result.error.lower()
