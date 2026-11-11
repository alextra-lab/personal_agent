"""Unit tests for the web_search native tool (FRE-1766).

``web_search`` calls Exa and Brave LLM Context directly. Provider HTTP runs on
``httpx.MockTransport`` with responses recorded from one live call to each provider
(``fixtures/web_search``), so no network and no key is needed. The SearXNG template tests
stay until FRE-1763 T2 removes SearXNG.
"""

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
import yaml
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


# ── SearXNG config regression tests ────────────────────────────────────────


def _searxng_config_path() -> Path:
    """Return the SearXNG settings path to use in tests.

    FRE-1310: the real ``docker/searxng/settings.yml`` is gitignored (mirrors
    the ``budget.yaml``/FRE-1209 pattern — it now carries the Exa api_key,
    and this repo is public). A developer machine and the VPS both have the
    real file; a fresh clone and CI have only ``settings.yml.example``.
    """
    searxng_dir = Path(__file__).resolve().parents[2] / "docker" / "searxng"
    real = searxng_dir / "settings.yml"
    return real if real.exists() else searxng_dir / "settings.yml.example"


def _load_searxng_config() -> dict:
    return yaml.safe_load(_searxng_config_path().read_text())


def _load_searxng_example_config() -> dict:
    """Load the committed template, never the real (possibly-activated) file.

    For assertions about the *shipped default* — e.g. Exa's placeholder key
    and ``disabled: true`` — rather than a durable invariant. Once an operator
    activates Exa (real key, ``disabled: false``, per the file's own header
    instructions), the real file legitimately stops matching those values;
    only ``settings.yml.example`` is guaranteed to keep shipping the default.
    """
    searxng_dir = Path(__file__).resolve().parents[2] / "docker" / "searxng"
    return yaml.safe_load((searxng_dir / "settings.yml.example").read_text())


def test_chefkoch_not_in_general_category() -> None:
    """Chefkoch (recipe engine) must not be tagged under the default 'general' category.

    FRE-796: chefkoch was misconfigured with categories: general, so every
    default-category web_search call included German recipe results —
    confirmed live via a query about French/American Revolutionary War debt
    that returned "Creamy tomato pasta" and "Cheeseburger" recipes. It stays
    reachable via engines=chefkoch or categories=recipes.
    """
    cfg = _load_searxng_config()
    chefkoch = next(e for e in cfg["engines"] if e["name"] == "chefkoch")
    assert chefkoch["categories"] != "general"
    assert chefkoch["categories"] == "recipes"


def test_searxng_settings_yml_not_tracked_in_git() -> None:
    """The real settings.yml must never be committed once it can carry the Exa api_key.

    FRE-1310. Mirrors ``config/governance/budget.yaml``'s FRE-1209 handling:
    ``git rm --cached`` + ``.gitignore`` moved it out of version control so a
    live credential can never land in this public repo.
    """
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["git", "ls-files", "docker/searxng/settings.yml"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""


def test_exa_serves_general_and_its_own_category() -> None:
    """Exa answers a default web search and stays reachable as categories=exa.

    FRE-1310 first scoped Exa to its own opt-in category, because it is a managed
    third-party vendor that sees plaintext queries. The owner reversed that on
    2026-09-10 (the production file carries the decision), and FRE-1584 makes it
    the template's state: web search runs on ``braveapi`` + ``exa``. A template that
    kept Exa out of ``general`` would give a fresh install a one-engine web search.
    """
    cfg = _load_searxng_config()
    exa = next(e for e in cfg["engines"] if e["name"] == "exa")
    assert exa["engine"] == "exaapi"
    assert _engine_categories(exa) == {"general", "exa"}


def test_exa_search_type_pinned_to_auto() -> None:
    """search_type must be pinned to 'auto', never 'deep-reasoning'.

    FRE-1310 / FRE-1303: deep-reasoning's output is model-generated, not a
    retrieval — under the authorship-independence rule that is not EXTERNAL,
    it is another model's assertion wearing a retrieval's identifier.
    """
    cfg = _load_searxng_config()
    exa = next(e for e in cfg["engines"] if e["name"] == "exa")
    assert exa["search_type"] == "auto"


def test_exa_content_mode_and_length() -> None:
    """content_mode returns query-relevant highlights, not whole pages.

    FRE-1310 originally chose ``text`` at 10000 chars to collapse search-then-fetch
    into one call. FRE-1331 reverses that on owner direction, because the cost side
    was never measured: per-turn input already reaches 83k on an ordinary research
    turn (FRE-1138), and ``text`` at 10 results is roughly 25k tokens of context per
    single search call.

    Measured live 2026-08-30 against the real Exa API: ``highlights`` returns about
    1,850 content characters per result against ``text``'s 10,000 — a ~5x reduction,
    ~4.6k tokens per call instead of ~25k — while still returning on-topic material
    (the probe query surfaced EUR-Lex, Consilium and TÜV Rheinland).

    This does not weaken ADR-0138 containment: highlights are verbatim excerpts of
    the source page, so a claim can still be checked against returned bytes. It
    narrows what is available to check against, not whether checking is possible.

    Reads settings.yml.example specifically, not the real-file-preferring fallback,
    because the owner's real file may have a different content_mode for operational
    reasons. Only the template guarantees the shipped default.
    """
    cfg = _load_searxng_example_config()
    exa = next(e for e in cfg["engines"] if e["name"] == "exa")
    assert exa["content_mode"] == "highlights"
    assert exa["content_max_characters"] == 2000


def test_exa_shipped_disabled_with_placeholder_key() -> None:
    """The template ships Exa disabled with a placeholder key — no live Exa key exists yet.

    FRE-1310: this PR delivers the capability, not a live secret. Enabling it
    is an ops step (pass show seshat/EXA_API_KEY on the real, untracked file).

    Reads settings.yml.example specifically, not the real-file-preferring
    fallback: once an operator activates Exa on the real file (real key,
    disabled: false, per the template's own header instructions), it should
    no longer match these placeholder values — that's the intended, correct
    outcome, not a regression. Only the template is guaranteed to keep
    shipping the pre-activation default.
    """
    cfg = _load_searxng_example_config()
    exa = next(e for e in cfg["engines"] if e["name"] == "exa")
    assert exa["disabled"] is True
    assert exa["api_key"] == "REPLACE_WITH_EXA_API_KEY"


# FRE-1584: the planned engine set. Pinned by name so any change to it is a visible edit
# here, not a silent inheritance from SearXNG's upstream defaults. Rule: an engine stays
# only if it calls a JSON or XML API (not an HTML page). The template header names the
# three kept exceptions whose module marks `use_official_api: False`: currency,
# chefkoch and mdn.
_PLANNED_ENGINES: dict[str, frozenset[str]] = {
    "general": frozenset({"braveapi", "exa", "currency"}),
    "it": frozenset(
        {
            "stackoverflow",
            "github",
            "hackernews",
            "mdn",
            "docker hub",
            "huggingface",
            "huggingface datasets",
            "huggingface spaces",
        }
    ),
    "science": frozenset(
        {
            "arxiv",
            "semantic scholar",
            "pubmed",
            "crossref",
            "openalex",
            "openairepublications",
        }
    ),
    "news": frozenset({"wikinews"}),
    "social_media": frozenset({"lemmy posts"}),
    "recipes": frozenset({"chefkoch"}),
    "weather": frozenset({"openmeteo", "wttr.in"}),
}

# Engines that fetch HTML pages or call a site-internal endpoint. None may be enabled.
_SCRAPING_ENGINES = frozenset(
    {
        "google",
        "google cse",
        "google news",
        "google scholar",
        "bing",
        "bing news",
        "brave",
        "brave.news",
        "duckduckgo",
        "duckduckgo news",
        "startpage",
        "startpage news",
        "qwant",
        "qwant news",
        "reuters",
        "lobste.rs",
        "pypi",
    }
)


def _engine_categories(entry: dict) -> set[str]:
    raw = entry.get("categories") or []
    return {raw} if isinstance(raw, str) else set(raw)


def test_searxng_template_names_exactly_the_planned_engine_set() -> None:
    """The template's running set is its own file, not an upstream default (FRE-1584 AC-1).

    ``use_default_settings`` keeps every upstream engine the file does not remove, so
    the file declared 28 of the 90 enabled engines and 62 ran unnamed. ``keep_only``
    makes the file the whole set. This asserts the set is exactly the planned one, and that every
    engine the file configures is also kept, so nothing is configured but dropped.
    """
    cfg = _load_searxng_example_config()
    keep_only = cfg["use_default_settings"]["engines"]["keep_only"]
    planned = frozenset().union(*_PLANNED_ENGINES.values())
    assert set(keep_only) == planned
    assert len(keep_only) == len(set(keep_only)), "duplicate name in keep_only"
    assert {e["name"] for e in cfg["engines"]} == planned


def test_searxng_template_enables_no_scraping_engine() -> None:
    """No scraper and no ``google cse`` appears anywhere in the template (FRE-1584 AC-1).

    Not even as ``disabled: true``: ``keep_only`` does not filter engines the file
    declares itself, so a disabled scraper left in ``engines:`` would still register
    and show in ``/config``. Those blocks are deleted, not switched off.
    """
    cfg = _load_searxng_example_config()
    declared = {e["name"] for e in cfg["engines"]}
    kept = set(cfg["use_default_settings"]["engines"]["keep_only"])
    assert (declared | kept) & _SCRAPING_ENGINES == set()


def test_searxng_template_assigns_each_planned_engine_its_category() -> None:
    """Each planned engine serves the category the tool description promises."""
    cfg = _load_searxng_example_config()
    by_name = {e["name"]: e for e in cfg["engines"]}
    for category, names in _PLANNED_ENGINES.items():
        for name in names:
            if name in {"braveapi", "currency"}:
                # Upstream's default entry already places these in `general`; the file
                # does not restate that, so there is nothing to assert here.
                continue
            assert category in _engine_categories(by_name[name]), f"{name} not in {category}"


def test_searxng_template_braveapi_is_active() -> None:
    """``braveapi`` is not disabled, and overrides upstream's ``inactive: true``.

    FRE-1331: upstream ships ``braveapi`` inactive and ``use_default_settings`` merges
    by name, so without ``inactive: false`` the engine is silently never loaded.
    """
    cfg = _load_searxng_example_config()
    brave = next(e for e in cfg["engines"] if e["name"] == "braveapi")
    assert brave["inactive"] is False
    assert brave["disabled"] is False


def test_searxng_template_drops_wikipedia() -> None:
    """Wikipedia is removed: its engine treats the whole query as a page title (FRE-1584 AC-3).

    The engine builds ``/page/summary/<quote(query)>``. ``quote`` keeps ``/`` literal, so
    a query containing a slash adds a path segment and Wikipedia answers 400. A ``|``
    answers 500. Neither is the one 400 detail the engine swallows, so each one raises,
    and any unresponsive engine marks the whole search ``degraded_retrieval``. Measured
    over 14 days: 28 of 482 searches (5.8%) were degraded by Wikipedia alone, which on
    its own breaks the 5% target. The cause is upstream code, so no setting fixes it.
    """
    cfg = _load_searxng_example_config()
    declared = {e["name"] for e in cfg["engines"]}
    kept = set(cfg["use_default_settings"]["engines"]["keep_only"])
    assert "wikipedia" not in declared | kept


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
    # Grounding contract: a results list of url-bearing objects, empty answers/infoboxes.
    assert result["answers"] == [] and result["infoboxes"] == []
    assert all(set(r) == {"title", "url", "content", "provider"} for r in result["results"])


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
        (400, "bad_query"),
        (404, "bad_query"),
        (422, "bad_query"),
    ],
)
def test_classify_status(status: int, kind: str) -> None:
    """Owner decision 5: refusal (401/402/403/429/5xx) vs bad query (other 4xx)."""
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


def test_shape_search_response_keeps_the_harness_shape() -> None:
    """FRE-1641: the harness still shapes recorded SearXNG responses with it (ADR-0158 D1.8)."""
    from personal_agent.tools.web import build_search_params, shape_search_response

    data = {
        "results": [
            {"title": f"T{i}", "url": f"https://e.com/{i}", "content": f"S{i}", "engine": "exa"}
            for i in range(4)
        ],
        "answers": [{"engine": "calc", "answer": "42"}],
        "unresponsive_engines": [["braveapi", "timeout"]],
        "suggestions": ["s"],
        "infoboxes": [],
    }
    out = shape_search_response(data, query="q", categories="general", engines=None, max_results=2)
    assert out["results"] == [
        {"title": "T0", "url": "https://e.com/0", "snippet": "S0", "engine": "exa", "score": None},
        {"title": "T1", "url": "https://e.com/1", "snippet": "S1", "engine": "exa", "score": None},
    ]
    assert out["result_count"] == 2
    assert out["answers"] == [{"engine": "calc", "answer": "42"}]
    assert out["engines_contributed"] == ["exa"]
    assert out["degraded_retrieval"] is True
    assert out["retrieval_warning"].startswith("1 engine(s) failed this search (braveapi)")
    assert build_search_params("q", "general", None, "en", "year") == {
        "q": "q",
        "format": "json",
        "categories": "general",
        "language": "en",
        "pageno": "1",
        "time_range": "year",
    }
