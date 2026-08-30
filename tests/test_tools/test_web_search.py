"""Unit tests for web_search native tool (ADR-0034).

Tests use mocked httpx responses — no SearXNG container required.
The executor returns dict[str, Any] on success and raises ToolExecutionError on failure.
"""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import yaml

from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.web import web_search_executor, web_search_tool

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


def test_exa_not_in_general_category() -> None:
    """Exa must not be tagged under the default 'general' category.

    FRE-1310: SearXNG is self-hosted (queries never leave our infra); Exa is a
    managed third-party vendor that sees plaintext queries. Scoped to its own
    category so adoption is opt-in per query, not a silent default-search
    exposure for health/travel/personal queries. Reachable via engines=exaapi
    or categories=exa.
    """
    cfg = _load_searxng_config()
    exa = next(e for e in cfg["engines"] if e["name"] == "exa")
    assert exa["engine"] == "exaapi"
    assert exa["categories"] != "general"
    assert exa["categories"] == "exa"


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
    """
    cfg = _load_searxng_config()
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


def _mock_searxng_response(
    results: list[dict] | None = None,
    suggestions: list[str] | None = None,
    infoboxes: list[dict] | None = None,
    unresponsive_engines: list[list[str]] | None = None,
) -> MagicMock:
    """Build a mock httpx response with SearXNG JSON structure.

    ``unresponsive_engines`` mirrors the real shape: a list of ``[engine_name,
    reason]`` pairs (FRE-1339) — SearXNG uses both a bare reason
    ("access denied") and a "Suspended: ..." prefix once it has taken the
    engine out of rotation; callers may pass either.
    """
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "results": results or [],
        "suggestions": suggestions or [],
        "infoboxes": infoboxes or [],
        "unresponsive_engines": unresponsive_engines or [],
        # SearXNG's own total is independently untrustworthy (FRE-1339) and
        # deliberately never matches len(results) here, so a test that
        # accidentally started trusting it would fail loudly.
        "number_of_results": 0,
    }
    return mock_resp


def _mock_client(response: MagicMock) -> AsyncMock:
    """Build a mock AsyncClient that returns the given response on .get()."""
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


# ── Tool definition tests ──────────────────────────────────────────────────


def test_web_search_tool_definition() -> None:
    """Tool has correct metadata for governance and LLM awareness."""
    assert web_search_tool.name == "web_search"
    assert web_search_tool.category == "network"
    assert web_search_tool.risk_level == "low"
    assert "NORMAL" in web_search_tool.allowed_modes
    assert "ALERT" in web_search_tool.allowed_modes
    assert "DEGRADED" in web_search_tool.allowed_modes
    assert "LOCKDOWN" not in web_search_tool.allowed_modes
    assert "RECOVERY" not in web_search_tool.allowed_modes
    param_names = {p.name for p in web_search_tool.parameters}
    assert "query" in param_names
    assert "categories" in param_names
    assert "engines" in param_names
    assert "language" in param_names
    assert "time_range" in param_names
    assert "max_results" in param_names
    # query is the only required parameter
    required = [p for p in web_search_tool.parameters if p.required]
    assert len(required) == 1
    assert required[0].name == "query"


def test_web_search_description_states_when_to_reach_for_it() -> None:
    """Lead with when to use it, not read as a generic utility that redirects elsewhere."""
    assert web_search_tool.description.startswith("Search the live web when")
    assert "Prefer perplexity_query" not in web_search_tool.description
    # Plugin/category detail is preserved, only the framing changes.
    assert "Timezone" in web_search_tool.description
    assert "Categories:" in web_search_tool.description


def test_web_search_description_mentions_exa() -> None:
    """The model must be told exa exists and how to reach it — it's opt-in, not in general.

    FRE-1310: categories is a free-form string with no code-level validation
    (tools/web.py), so discoverability is entirely a documentation problem —
    an engine the model is never told about is dead capability (the same
    failure mode FRE-1290 found for web_search as a whole).
    """
    assert "exa" in web_search_tool.description
    categories_param = next(p for p in web_search_tool.parameters if p.name == "categories")
    assert "exa" in categories_param.description


# ── Executor happy-path tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_web_search_happy_path() -> None:
    """Successful search returns structured dict with results."""
    resp = _mock_searxng_response(
        results=[
            {
                "title": "Python 3.12 docs",
                "url": "https://docs.python.org/3.12/",
                "content": "Official Python docs",
                "engine": "google",
                "score": 0.9,
            }
        ],
        suggestions=["python tutorial"],
    )
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        result = await web_search_executor(query="python docs", ctx=_CTX)

    assert isinstance(result, dict)
    assert result["result_count"] == 1
    assert result["results"][0]["title"] == "Python 3.12 docs"
    assert result["results"][0]["url"] == "https://docs.python.org/3.12/"
    assert result["results"][0]["snippet"] == "Official Python docs"
    assert result["results"][0]["engine"] == "google"
    assert result["suggestions"] == ["python tutorial"]
    assert result["query"] == "python docs"


@pytest.mark.asyncio
async def test_web_search_empty_results() -> None:
    """Zero results still returns success dict with result_count=0."""
    resp = _mock_searxng_response(results=[])
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        result = await web_search_executor(query="xyzzy_nonexistent_query", ctx=_CTX)

    assert isinstance(result, dict)
    assert result["result_count"] == 0
    assert result["results"] == []


# ── Engine health signal (FRE-1339) ────────────────────────────────────────

# Real observed unresponsive_engines payload from a collapsed pool: a mix of
# the bare-reason and "Suspended: ..." forms SearXNG actually emits.
_COLLAPSED_POOL = [
    ["brave", "Suspended: too many requests"],
    ["duckduckgo", "CAPTCHA"],
    ["startpage", "Suspended: CAPTCHA"],
    ["qwant", "Suspended: access denied"],
    ["karmasearch", "access denied"],
    ["karmasearch videos", "access denied"],
    ["mojeek", "access denied"],
    ["yep", "access denied"],
    ["presearch", "access denied"],
]


@pytest.mark.asyncio
async def test_web_search_skips_malformed_unresponsive_engine_entries() -> None:
    """A malformed unresponsive_engines entry is skipped, not a crash.

    ``entry[0]``/``entry[1]`` on a bare string indexes characters, a dict raises
    ``KeyError``, and ``len()`` on a scalar raises ``TypeError`` — none of these
    should ever reach the caller as an unhandled exception from a field SearXNG
    itself controls the shape of.
    """
    resp = _mock_searxng_response(
        results=[],
        unresponsive_engines=[
            "brave",  # bare string, not a [name, reason] pair
            {"engine": "bing", "reason": "down"},  # dict, not indexable by position
            42,  # scalar — len() would raise
            ["short"],  # too short
            ["qwant", "access denied"],  # the one well-formed entry
        ],
    )
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        result = await web_search_executor(query="anything", ctx=_CTX)

    assert result["unresponsive_engines"] == [{"engine": "qwant", "reason": "access denied"}]
    assert result["degraded_retrieval"] is True


@pytest.mark.asyncio
async def test_web_search_surfaces_unresponsive_engines() -> None:
    """AC-1: a collapsed engine pool is visible on the tool result, not just result_count.

    Modeled on the ticket's own incident: the surviving low-quality engines
    still return a full page of "successful" junk results while most of the
    configured pool is down — result_count alone reads as healthy.
    """
    resp = _mock_searxng_response(
        results=[
            {
                "title": "Anmelden bei Hotmail",
                "url": "https://outlook.live.com/",
                "content": "",
                "engine": "bing",
                "score": 1.0,
            }
        ],
        unresponsive_engines=_COLLAPSED_POOL,
    )
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        result = await web_search_executor(query="EU product safety enforcement", ctx=_CTX)

    assert result["degraded_retrieval"] is True
    assert len(result["unresponsive_engines"]) == len(_COLLAPSED_POOL)
    assert {"engine": "brave", "reason": "Suspended: too many requests"} in result[
        "unresponsive_engines"
    ]
    assert {"engine": "karmasearch", "reason": "access denied"} in result["unresponsive_engines"]
    assert result["engines_contributed"] == ["bing"]


@pytest.mark.asyncio
async def test_web_search_degraded_result_warns_the_model() -> None:
    """AC-2: degraded state is on the tool result itself, not only in logs."""
    resp = _mock_searxng_response(
        results=[
            {"title": "junk", "url": "https://example.com/junk", "content": "", "engine": "bing"}
        ],
        unresponsive_engines=_COLLAPSED_POOL,
    )
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        result = await web_search_executor(query="anything", ctx=_CTX)

    assert "retrieval_warning" in result
    assert "brave" in result["retrieval_warning"]
    assert "bing" in result["retrieval_warning"]


@pytest.mark.asyncio
async def test_web_search_seeded_negative_obscure_query_not_degraded() -> None:
    """AC-4 (negative case): a legitimately empty search on healthy engines is NOT degraded.

    The signal must be keyed on engine health, not on result_count — otherwise
    a genuinely obscure query with zero hits would misfire the same alert a
    collapsed engine pool trips.
    """
    resp = _mock_searxng_response(results=[], unresponsive_engines=[])
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        result = await web_search_executor(query="xyzzy_genuinely_obscure_query", ctx=_CTX)

    assert result["result_count"] == 0
    assert result["degraded_retrieval"] is False
    assert result["unresponsive_engines"] == []
    assert "retrieval_warning" not in result


@pytest.mark.asyncio
async def test_web_search_seeded_positive_collapsed_pool_is_degraded() -> None:
    """AC-4 (positive case, paired with the negative above).

    The collapsed-pool fixture above DOES trip the signal, even with a full
    page of results.
    """
    resp = _mock_searxng_response(
        results=[
            {"title": f"r{i}", "url": f"https://example.com/{i}", "content": "", "engine": "bing"}
            for i in range(10)
        ],
        unresponsive_engines=_COLLAPSED_POOL,
    )
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        result = await web_search_executor(query="anything", ctx=_CTX)

    assert result["result_count"] == 10
    assert result["degraded_retrieval"] is True


@pytest.mark.asyncio
async def test_web_search_completed_log_carries_result_urls_and_engine_health() -> None:
    """AC-1/AC-3: the telemetry event records engine health, not just the tool result.

    Also carries the result URL list, so a collapsed pool is visible after the
    fact and ``fetch_url.url in prior_search.result_urls`` becomes checkable.
    """
    resp = _mock_searxng_response(
        results=[
            {
                "title": "Python 3.12 docs",
                "url": "https://docs.python.org/3.12/",
                "content": "",
                "engine": "google",
            }
        ],
        unresponsive_engines=_COLLAPSED_POOL,
    )
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        with patch("personal_agent.tools.web.log") as mock_log:
            await web_search_executor(query="python docs", ctx=_CTX)

    completed_call = next(
        c for c in mock_log.info.call_args_list if c.args[0] == "web_search_completed"
    )
    assert completed_call.kwargs["result_urls"] == ["https://docs.python.org/3.12/"]
    assert completed_call.kwargs["degraded_retrieval"] is True
    assert (
        "brave: Suspended: too many requests"
        in completed_call.kwargs["unresponsive_engine_reasons"]
    )


@pytest.mark.asyncio
async def test_web_search_categories_passed_in_params() -> None:
    """Categories parameter is forwarded to SearXNG query params."""
    resp = _mock_searxng_response()
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        await web_search_executor(query="asyncio", categories="it", ctx=_CTX)

    call_kwargs = client.get.call_args
    params = call_kwargs.kwargs["params"]
    assert params["categories"] == "it"


@pytest.mark.asyncio
async def test_web_search_engines_passed_in_params() -> None:
    """Engines parameter is forwarded to SearXNG query params."""
    resp = _mock_searxng_response()
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        await web_search_executor(query="test", engines="google,stackoverflow", ctx=_CTX)

    call_kwargs = client.get.call_args
    params = call_kwargs.kwargs["params"]
    assert params.get("engines") == "google,stackoverflow"


@pytest.mark.asyncio
async def test_web_search_weather_category_strips_weather_prefix() -> None:
    """When categories='weather', a leading 'weather ' token is stripped.

    The wttr.in and duckduckgo_weather engines treat the full query as a
    geo location and raise ValueError on 'weather <city>'. Stripping the
    prefix at the router lets those engines resolve the location.
    """
    resp = _mock_searxng_response()
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        await web_search_executor(query="weather Berlin", categories="weather", ctx=_CTX)

    params = client.get.call_args.kwargs["params"]
    assert params["q"] == "Berlin"


@pytest.mark.asyncio
async def test_web_search_weather_engines_strips_weather_prefix() -> None:
    """When engines targets a weather engine, leading 'weather ' is stripped."""
    resp = _mock_searxng_response()
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        await web_search_executor(query="weather Tokyo", engines="wttr.in,openmeteo", ctx=_CTX)

    params = client.get.call_args.kwargs["params"]
    assert params["q"] == "Tokyo"


@pytest.mark.asyncio
async def test_web_search_weather_prefix_case_insensitive() -> None:
    """'Weather ' / 'WEATHER ' (any case) is stripped for weather targets."""
    resp = _mock_searxng_response()
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        await web_search_executor(query="Weather Paris", categories="weather", ctx=_CTX)

    params = client.get.call_args.kwargs["params"]
    assert params["q"] == "Paris"


@pytest.mark.asyncio
async def test_web_search_general_category_keeps_weather_prefix() -> None:
    """General-category searches preserve the literal query (no strip).

    A user asking 'weather Berlin' against the general category wants
    weather-related pages, not a location lookup.
    """
    resp = _mock_searxng_response()
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        await web_search_executor(query="weather Berlin", categories="general", ctx=_CTX)

    params = client.get.call_args.kwargs["params"]
    assert params["q"] == "weather Berlin"


@pytest.mark.asyncio
async def test_web_search_non_leading_weather_token_not_stripped() -> None:
    """Only a leading 'weather ' is stripped; mid-query occurrences stay."""
    resp = _mock_searxng_response()
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        await web_search_executor(query="cold weather climate", categories="weather", ctx=_CTX)

    params = client.get.call_args.kwargs["params"]
    assert params["q"] == "cold weather climate"


@pytest.mark.asyncio
async def test_web_search_sets_x_forwarded_for_header() -> None:
    """Outbound httpx GET sends an X-Forwarded-For header.

    SearXNG's botdetection logs ERROR-level warnings on requests with
    neither X-Forwarded-For nor X-Real-IP. The agent calls SearXNG
    directly over the docker network (no Caddy proxy), so we set the
    header ourselves to keep telemetry signal clean.
    """
    resp = _mock_searxng_response()
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        await web_search_executor(query="anything", ctx=_CTX)

    headers = client.get.call_args.kwargs.get("headers") or {}
    assert "X-Forwarded-For" in headers
    assert headers["X-Forwarded-For"]


@pytest.mark.asyncio
async def test_web_search_time_range_passed() -> None:
    """time_range parameter is forwarded to SearXNG query params."""
    resp = _mock_searxng_response()
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        await web_search_executor(query="openai news", time_range="week", ctx=_CTX)

    call_kwargs = client.get.call_args
    params = call_kwargs.kwargs["params"]
    assert params.get("time_range") == "week"


@pytest.mark.asyncio
async def test_web_search_max_results_capped_at_50() -> None:
    """Requesting 100 results is silently capped at 50."""
    many_results = [
        {
            "title": f"r{i}",
            "url": f"https://example.com/{i}",
            "content": "",
            "engine": "g",
            "score": 0.5,
        }
        for i in range(60)
    ]
    resp = _mock_searxng_response(results=many_results)
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        result = await web_search_executor(query="test", max_results=100, ctx=_CTX)

    assert result["result_count"] <= 50


@pytest.mark.asyncio
async def test_web_search_infobox_handling() -> None:
    """Infoboxes are truncated to 2 entries with content capped at 500 chars."""
    long_content = "x" * 1000
    infoboxes = [
        {
            "infobox": "Python",
            "content": long_content,
            "urls": [
                {"url": "https://a.com"},
                {"url": "https://b.com"},
                {"url": "https://c.com"},
                {"url": "https://d.com"},
            ],
        },
        {"infobox": "Guido", "content": "Creator of Python", "urls": []},
        {"infobox": "Third", "content": "Should be dropped", "urls": []},
    ]
    resp = _mock_searxng_response(infoboxes=infoboxes)
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        result = await web_search_executor(query="python creator", ctx=_CTX)

    assert len(result["infoboxes"]) == 2  # capped at 2
    assert len(result["infoboxes"][0]["content"]) <= 500  # content truncated
    assert len(result["infoboxes"][0]["urls"]) <= 3  # urls capped at 3


# ── Executor error tests ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_web_search_empty_query_raises() -> None:
    """Empty query string raises ToolExecutionError with descriptive message."""
    with pytest.raises(ToolExecutionError, match="query parameter is required"):
        await web_search_executor(query="", ctx=_CTX)


@pytest.mark.asyncio
async def test_web_search_whitespace_query_raises() -> None:
    """Whitespace-only query raises ToolExecutionError."""
    with pytest.raises(ToolExecutionError, match="query parameter is required"):
        await web_search_executor(query="   ", ctx=_CTX)


@pytest.mark.asyncio
async def test_web_search_connect_error_raises() -> None:
    """ConnectError raises ToolExecutionError with actionable message."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        with pytest.raises(ToolExecutionError, match="Cannot connect to SearXNG"):
            await web_search_executor(query="test query", ctx=_CTX)


@pytest.mark.asyncio
async def test_web_search_timeout_raises() -> None:
    """TimeoutException raises ToolExecutionError mentioning timeout duration."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        with pytest.raises(ToolExecutionError, match="timed out"):
            await web_search_executor(query="test query", ctx=_CTX)


@pytest.mark.asyncio
async def test_web_search_malformed_json_raises() -> None:
    """Non-JSON response body raises ToolExecutionError."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.side_effect = ValueError("not JSON")
    client = _mock_client(mock_resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        with pytest.raises(ToolExecutionError):
            await web_search_executor(query="test query", ctx=_CTX)


# ── Governance / mode tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_web_search_blocked_in_lockdown_mode() -> None:
    """ToolExecutionLayer returns permission-denied ToolResult in LOCKDOWN mode."""
    from unittest.mock import patch as _patch

    from personal_agent.brainstem.mode_manager import ModeManager
    from personal_agent.config.governance_loader import load_governance_config
    from personal_agent.governance.models import Mode
    from personal_agent.telemetry import TraceContext
    from personal_agent.tools.executor import ToolExecutionLayer
    from personal_agent.tools.registry import ToolRegistry
    from personal_agent.tools.web import web_search_executor, web_search_tool

    reg = ToolRegistry()
    reg.register(web_search_tool, web_search_executor)

    gov = load_governance_config()
    mode_mgr = ModeManager(governance_config=gov)

    with _patch.object(mode_mgr, "get_current_mode", return_value=Mode.LOCKDOWN):
        layer = ToolExecutionLayer(registry=reg, governance_config=gov, mode_manager=mode_mgr)
        trace_ctx = TraceContext.new_trace()
        result = await layer.execute_tool("web_search", {"query": "test"}, trace_ctx)

    assert result.success is False
    assert result.error is not None
    assert "LOCKDOWN" in result.error or "not allowed" in result.error.lower()
