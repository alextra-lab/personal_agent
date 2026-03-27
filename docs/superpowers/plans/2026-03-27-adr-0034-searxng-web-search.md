# SearXNG Web Search (ADR-0034) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a private, self-hosted web search tool (`web_search`) backed by a SearXNG Docker container, replacing Perplexity as the default search path for routine queries.

**Architecture:** SearXNG runs as a Docker Compose service on port 8888 (host) / 8080 (container). A native in-process tool (`tools/web.py`) calls it via `httpx`, following the same executor contract as `search_memory`: keyword-arg signature, `dict` return, `ToolExecutionError` on failure. Six locations in prompts and governance config that bias toward Perplexity are all updated.

**Tech Stack:** Python 3.12+, httpx (already in pyproject.toml), SearXNG Docker image (`searxng/searxng:latest`), pytest-asyncio, unittest.mock.

**Spec:** `docs/specs/SEARXNG_WEB_SEARCH_TOOL_SPEC.md`
**ADR:** `docs/architecture_decisions/ADR-0034-searxng-self-hosted-web-search.md`

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `docker-compose.yml` | Modify | Add `searxng` service |
| `docker/searxng/settings.yml` | **Create** | SearXNG engine + server config |
| `.gitignore` | Modify | Ignore SearXNG-generated files |
| `src/personal_agent/config/settings.py` | Modify | Add 4 `searxng_*` settings fields |
| `tests/test_tools/test_web_search.py` | **Create** | 12 unit tests (written first, TDD) |
| `src/personal_agent/tools/web.py` | **Create** | Tool definition + executor |
| `src/personal_agent/tools/__init__.py` | Modify | Import + register `web_search` |
| `config/governance/tools.yaml` | Modify | Add `web_search`; update 3 description_overrides |
| `src/personal_agent/orchestrator/prompts.py` | Modify | 3 prompt changes (lines 39-46, 70-71, 144-147) |
| `tests/test_tools/test_web_search_integration.py` | **Create** | 3 integration tests (requires Docker) |

---

## Task 1: Infrastructure — Docker Compose + SearXNG Config

**Files:**
- Create: `docker/searxng/settings.yml`
- Modify: `docker-compose.yml`
- Modify: `.gitignore`

- [ ] **Step 1.1: Create the SearXNG config directory and settings file**

```bash
mkdir -p /Users/Alex/Dev/personal_agent/docker/searxng
```

Create `docker/searxng/settings.yml` with this exact content:

```yaml
# SearXNG configuration for personal agent (ADR-0034)
# Docs: https://docs.searxng.org/admin/settings/

use_default_settings: true

general:
  instance_name: "PersonalAgent Search"
  debug: false
  enable_metrics: true

search:
  safe_search: 0
  default_lang: "en"
  formats:
    - html
    - json

server:
  bind_address: "0.0.0.0"
  port: 8080
  secret_key: "personal-agent-searxng-secret-change-me-32chars"
  limiter: false
  public_instance: false

ui:
  default_theme: simple
  results_on_new_tab: false

engines:
  # ── General web search ──────────────────────────────────
  - name: google
    engine: google
    shortcut: g
    disabled: false

  - name: brave
    engine: brave
    shortcut: br
    disabled: false

  - name: duckduckgo
    engine: duckduckgo
    shortcut: ddg
    disabled: false

  - name: wikipedia
    engine: wikipedia
    shortcut: wp
    disabled: false

  # ── IT / Development ────────────────────────────────────
  - name: stackoverflow
    engine: stackoverflow
    shortcut: so
    disabled: false
    categories: it

  - name: github
    engine: github
    shortcut: gh
    disabled: false
    categories: it

  - name: mdn
    engine: mdn
    shortcut: mdn
    disabled: false
    categories: it

  - name: pypi
    engine: pypi
    shortcut: pypi
    disabled: false
    categories: it

  # ── Science / Research ──────────────────────────────────
  - name: arxiv
    engine: arxiv
    shortcut: ax
    disabled: false
    categories: science

  - name: semantic scholar
    engine: semantic_scholar
    shortcut: ss
    disabled: false
    categories: science

  # ── News ────────────────────────────────────────────────
  - name: google news
    engine: google_news
    shortcut: gn
    disabled: false
    categories: news

  - name: bing news
    engine: bing_news
    shortcut: bn
    disabled: false
    categories: news

  # ── Weather ─────────────────────────────────────────────
  - name: wttr.in
    engine: wttr
    shortcut: wttr
    disabled: false
    categories: weather

outgoing:
  request_timeout: 8
  max_request_timeout: 15
  useragent_suffix: ""
  # Uncomment to route through a proxy:
  # proxies:
  #   all://:
  #     - socks5h://tor:9050
```

- [ ] **Step 1.2: Add the `searxng` service to `docker-compose.yml`**

In `docker-compose.yml`, add this block **before** the `volumes:` section (after the `neo4j` service block):

```yaml
  # SearXNG metasearch engine for private web search (ADR-0034)
  searxng:
    image: searxng/searxng:latest
    environment:
      - SEARXNG_BASE_URL=http://localhost:8888
    volumes:
      - ./docker/searxng:/etc/searxng:rw
    ports:
      - "8888:8080"
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "wget -q --spider http://localhost:8080/healthz || exit 1"]
      interval: 15s
      timeout: 5s
      retries: 3
```

- [ ] **Step 1.3: Update `.gitignore` to exclude SearXNG-generated files**

Add these lines at the end of `.gitignore`:

```
# SearXNG generated files (ADR-0034)
docker/searxng/uwsgi.ini
```

- [ ] **Step 1.4: Verify docker-compose config is valid**

```bash
cd /Users/Alex/Dev/personal_agent && docker compose config --quiet
```

Expected: exits 0 with no errors. If errors appear, check YAML indentation in docker-compose.yml.

- [ ] **Step 1.5: Commit**

```bash
cd /Users/Alex/Dev/personal_agent
git add docker/searxng/settings.yml docker-compose.yml .gitignore
git commit -m "feat(infra): add SearXNG Docker service and config (ADR-0034)"
```

---

## Task 2: App Config — Add `searxng_*` Settings

**Files:**
- Modify: `src/personal_agent/config/settings.py`

- [ ] **Step 2.1: Add searxng settings to `AppConfig`**

In `src/personal_agent/config/settings.py`, find the `# MCP Gateway` block (around line 179). Insert the following block **after** the MCP Gateway section (after `mcp_gateway_enabled_servers` field, before `# Request Gateway`):

```python
    # SearXNG web search (ADR-0034)
    searxng_base_url: str = Field(
        default="http://localhost:8888",
        description="SearXNG instance base URL",
    )
    searxng_timeout_seconds: int = Field(
        default=12,
        ge=1,
        description="Timeout for SearXNG search requests",
    )
    searxng_default_categories: str = Field(
        default="general",
        description="Default SearXNG categories (comma-separated)",
    )
    searxng_max_results: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum results to return per search",
    )
```

The env vars (auto-mapped by `env_prefix="AGENT_"`):
- `AGENT_SEARXNG_BASE_URL` → `searxng_base_url`
- `AGENT_SEARXNG_TIMEOUT_SECONDS` → `searxng_timeout_seconds`
- `AGENT_SEARXNG_DEFAULT_CATEGORIES` → `searxng_default_categories`
- `AGENT_SEARXNG_MAX_RESULTS` → `searxng_max_results`

- [ ] **Step 2.2: Verify settings load correctly**

```bash
cd /Users/Alex/Dev/personal_agent
uv run python -c "
from personal_agent.config import settings
print('base_url:', settings.searxng_base_url)
print('timeout:', settings.searxng_timeout_seconds)
print('categories:', settings.searxng_default_categories)
print('max_results:', settings.searxng_max_results)
"
```

Expected output:
```
base_url: http://localhost:8888
timeout: 12
categories: general
max_results: 10
```

- [ ] **Step 2.3: Commit**

```bash
git add src/personal_agent/config/settings.py
git commit -m "feat(config): add searxng_* settings to AppConfig (ADR-0034)"
```

---

## Task 3: Unit Tests — Write Failing Tests First (TDD)

**Files:**
- Create: `tests/test_tools/test_web_search.py`

- [ ] **Step 3.1: Create the unit test file**

Create `tests/test_tools/test_web_search.py` with this full content:

```python
"""Unit tests for web_search native tool (ADR-0034).

Tests use mocked httpx responses — no SearXNG container required.
The executor returns dict[str, Any] on success and raises ToolExecutionError on failure.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.web import web_search_executor, web_search_tool


def _mock_searxng_response(
    results: list[dict] | None = None,
    suggestions: list[str] | None = None,
    infoboxes: list[dict] | None = None,
) -> MagicMock:
    """Build a mock httpx response with SearXNG JSON structure."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "results": results or [],
        "suggestions": suggestions or [],
        "infoboxes": infoboxes or [],
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
        result = await web_search_executor(query="python docs")

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
    """Zero results still returns success=True dict with result_count=0."""
    resp = _mock_searxng_response(results=[])
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        result = await web_search_executor(query="xyzzy_nonexistent_query")

    assert isinstance(result, dict)
    assert result["result_count"] == 0
    assert result["results"] == []


@pytest.mark.asyncio
async def test_web_search_categories_passed_in_params() -> None:
    """categories parameter is forwarded to SearXNG query params."""
    resp = _mock_searxng_response()
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        await web_search_executor(query="asyncio", categories="it")

    call_kwargs = client.get.call_args
    assert call_kwargs is not None
    params = call_kwargs.kwargs.get("params") or call_kwargs.args[1] if len(call_kwargs.args) > 1 else call_kwargs.kwargs["params"]
    assert params["categories"] == "it"


@pytest.mark.asyncio
async def test_web_search_engines_passed_in_params() -> None:
    """engines parameter is forwarded to SearXNG query params."""
    resp = _mock_searxng_response()
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        await web_search_executor(query="test", engines="google,stackoverflow")

    call_kwargs = client.get.call_args
    params = call_kwargs.kwargs.get("params", {})
    assert params.get("engines") == "google,stackoverflow"


@pytest.mark.asyncio
async def test_web_search_time_range_passed() -> None:
    """time_range parameter is forwarded to SearXNG query params."""
    resp = _mock_searxng_response()
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        await web_search_executor(query="openai news", time_range="week")

    call_kwargs = client.get.call_args
    params = call_kwargs.kwargs.get("params", {})
    assert params.get("time_range") == "week"


@pytest.mark.asyncio
async def test_web_search_max_results_capped_at_50() -> None:
    """Requesting 100 results is silently capped at 50."""
    # Return 5 results regardless — the cap applies to slicing
    many_results = [
        {"title": f"r{i}", "url": f"https://example.com/{i}", "content": "", "engine": "g", "score": 0.5}
        for i in range(60)
    ]
    resp = _mock_searxng_response(results=many_results)
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        result = await web_search_executor(query="test", max_results=100)

    assert result["result_count"] <= 50


@pytest.mark.asyncio
async def test_web_search_infobox_handling() -> None:
    """Infoboxes are truncated to 2 entries with content capped at 500 chars."""
    long_content = "x" * 1000
    infoboxes = [
        {"infobox": "Python", "content": long_content, "urls": [{"url": "https://a.com"}, {"url": "https://b.com"}, {"url": "https://c.com"}, {"url": "https://d.com"}]},
        {"infobox": "Guido", "content": "Creator of Python", "urls": []},
        {"infobox": "Third", "content": "Should be dropped", "urls": []},
    ]
    resp = _mock_searxng_response(infoboxes=infoboxes)
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        result = await web_search_executor(query="python creator")

    assert len(result["infoboxes"]) == 2  # capped at 2
    assert len(result["infoboxes"][0]["content"]) <= 500  # content truncated
    assert len(result["infoboxes"][0]["urls"]) <= 3  # urls capped at 3


# ── Executor error tests ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_web_search_empty_query_raises() -> None:
    """Empty query string raises ToolExecutionError with descriptive message."""
    with pytest.raises(ToolExecutionError, match="query parameter is required"):
        await web_search_executor(query="")


@pytest.mark.asyncio
async def test_web_search_whitespace_query_raises() -> None:
    """Whitespace-only query raises ToolExecutionError."""
    with pytest.raises(ToolExecutionError, match="query parameter is required"):
        await web_search_executor(query="   ")


@pytest.mark.asyncio
async def test_web_search_connect_error_raises() -> None:
    """ConnectError raises ToolExecutionError with actionable message."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        with pytest.raises(ToolExecutionError, match="Cannot connect to SearXNG"):
            await web_search_executor(query="test query")


@pytest.mark.asyncio
async def test_web_search_timeout_raises() -> None:
    """TimeoutException raises ToolExecutionError mentioning timeout duration."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        with pytest.raises(ToolExecutionError, match="timed out"):
            await web_search_executor(query="test query")


@pytest.mark.asyncio
async def test_web_search_malformed_json_raises() -> None:
    """Non-JSON response body raises ToolExecutionError."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.side_effect = ValueError("not JSON")
    client = _mock_client(mock_resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        with pytest.raises(ToolExecutionError):
            await web_search_executor(query="test query")


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
```

- [ ] **Step 3.2: Run tests to verify they all FAIL (tool doesn't exist yet)**

```bash
cd /Users/Alex/Dev/personal_agent
uv run pytest tests/test_tools/test_web_search.py -v 2>&1 | head -30
```

Expected: `ImportError` or `ModuleNotFoundError` for `personal_agent.tools.web`. All tests fail — that's correct.

- [ ] **Step 3.3: Commit the failing tests**

```bash
git add tests/test_tools/test_web_search.py
git commit -m "test(tools): add failing unit tests for web_search executor (TDD, ADR-0034)"
```

---

## Task 4: Implement `src/personal_agent/tools/web.py`

**Files:**
- Create: `src/personal_agent/tools/web.py`

- [ ] **Step 4.1: Create the tool file**

Create `src/personal_agent/tools/web.py` with this exact content:

```python
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
        ),
        ToolParameter(
            name="language",
            type="string",
            description="Search language (BCP-47 code, e.g. 'en', 'fr'). Default: 'en'.",
            required=False,
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
        ),
        ToolParameter(
            name="max_results",
            type="number",
            description="Maximum results to return (1-50, default from config).",
            required=False,
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
        error_msg = (
            f"SearXNG request timed out after {settings.searxng_timeout_seconds}s."
        )
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
```

- [ ] **Step 4.2: Run unit tests to verify they pass**

```bash
cd /Users/Alex/Dev/personal_agent
uv run pytest tests/test_tools/test_web_search.py -v
```

Expected: all tests pass except `test_web_search_categories_passed_in_params` may need adjustment based on how `call_args` is accessed. If that test fails, check: `call_kwargs = client.get.call_args; params = call_kwargs.kwargs["params"]`.

All 12 tests should pass (or 11 pass + 1 skipped if SearXNG isn't running — that's the LOCKDOWN mode test which doesn't need Docker).

- [ ] **Step 4.3: Run mypy**

```bash
uv run mypy src/personal_agent/tools/web.py
```

Expected: `Success: no issues found in 1 source file`

- [ ] **Step 4.4: Run ruff**

```bash
uv run ruff check src/personal_agent/tools/web.py && uv run ruff format --check src/personal_agent/tools/web.py
```

Expected: no errors. If ruff format reports changes, run: `uv run ruff format src/personal_agent/tools/web.py`

- [ ] **Step 4.5: Commit**

```bash
git add src/personal_agent/tools/web.py
git commit -m "feat(tools): implement web_search native tool via SearXNG (ADR-0034)"
```

---

## Task 5: Register `web_search` in the Tool Registry

**Files:**
- Modify: `src/personal_agent/tools/__init__.py`

- [ ] **Step 5.1: Add import and registration**

In `src/personal_agent/tools/__init__.py`:

1. Add the import after the existing tool imports (after `from personal_agent.tools.system_health import ...`):

```python
from personal_agent.tools.web import (
    web_search_executor,
    web_search_tool,
)
```

2. In `register_mvp_tools()`, add the registration line after `registry.register(self_telemetry_query_tool, self_telemetry_query_executor)`:

```python
    registry.register(web_search_tool, web_search_executor)  # ADR-0034
```

3. Update the docstring of `register_mvp_tools` to mention `web_search`:

Replace:
```python
    """Register MVP tools with the registry.

    This function registers the initial set of tools:
    - read_file: Read file contents
    - list_directory: List directory contents
    - system_metrics_snapshot: Get system health metrics
    - search_memory: Query memory graph (ADR-0026)
```

With:
```python
    """Register MVP tools with the registry.

    This function registers the initial set of tools:
    - read_file: Read file contents
    - list_directory: List directory contents
    - system_metrics_snapshot: Get system health metrics
    - search_memory: Query memory graph (ADR-0026)
    - web_search: Private web search via SearXNG (ADR-0034)
```

- [ ] **Step 5.2: Verify `web_search` appears in the registry**

```bash
cd /Users/Alex/Dev/personal_agent
uv run python -c "
from personal_agent.tools import get_default_registry
r = get_default_registry()
names = r.list_tool_names()
print('tools:', names)
assert 'web_search' in names, 'web_search not registered!'
print('OK: web_search is registered')
"
```

Expected:
```
tools: ['read_file', 'list_directory', 'system_metrics_snapshot', 'search_memory', 'self_telemetry_query', 'web_search']
OK: web_search is registered
```

- [ ] **Step 5.3: Run full test suite to check nothing broke**

```bash
uv run pytest tests/test_tools/ -v --tb=short
```

Expected: all existing tests still pass, all new `test_web_search.py` tests pass.

- [ ] **Step 5.4: Commit**

```bash
git add src/personal_agent/tools/__init__.py
git commit -m "feat(tools): register web_search in tool registry (ADR-0034)"
```

---

## Task 6: Governance Config — Add `web_search`, Update Three Description Overrides

**Files:**
- Modify: `config/governance/tools.yaml`

- [ ] **Step 6.1: Add `web_search` entry to `tools:` section**

In `config/governance/tools.yaml`, find the `tools:` section. Add the following block **before** `read_file:` (i.e., as the first entry in `tools:`):

```yaml
  # Native web search via SearXNG (ADR-0034)
  web_search:
    category: "network"
    allowed_in_modes: ["NORMAL", "ALERT", "DEGRADED"]
    risk_level: "low"
    requires_approval: false
    timeout_seconds: 15
    rate_limit_per_hour: 120

```

- [ ] **Step 6.2: Update `mcp_perplexity_ask` description_override**

Find this line (around line 501):
```yaml
    description_override: "Search the internet using Perplexity AI and get a direct answer with citations. Use for factual questions, current events, CVEs, or any question requiring live web data. Prefer this over mcp_perplexity_research for most queries — it is faster. Do NOT answer from your own knowledge when live information is needed; always call this tool instead. Input: messages array with {role: 'user', content: 'your question'}."
```

Replace it with:
```yaml
    description_override: "Get a synthesized answer with citations from Perplexity AI. Use when you need a pre-digested summary with source links — e.g. multi-factor comparisons, nuanced technical explanations, or when web_search snippets are insufficient. For routine factual lookups, prefer web_search (faster, free, private). Prefer this over mcp_perplexity_research unless broad multi-source coverage is specifically needed. Input: messages array with {role: 'user', content: 'your question'}."
```

- [ ] **Step 6.3: Update `mcp_search` description_override**

Find this line (around line 553):
```yaml
    description_override: "Search DuckDuckGo and return a list of result links and snippets. Use when you want to browse raw search results or pick specific URLs to read with mcp_fetch_content. For direct answers with citations, prefer mcp_perplexity_ask."
```

Replace it with:
```yaml
    description_override: "Search DuckDuckGo and return a list of result links and snippets. Fallback search tool if web_search (SearXNG) is unavailable. For most searches, prefer web_search which aggregates multiple engines."
```

- [ ] **Step 6.4: Update `mcp_fetch_content` description_override**

Find this line (around line 379):
```yaml
    description_override: "Fetch and parse the full text of a specific webpage URL. Use when you already have a URL and want to read its content. Prefer mcp_perplexity_ask or mcp_search when you need to find information without a known URL."
```

Replace it with:
```yaml
    description_override: "Fetch and parse the full text of a specific webpage URL. Use when you already have a URL and want to read its content. Prefer web_search or mcp_search when you need to find information without a known URL."
```

- [ ] **Step 6.5: Verify governance config loads without errors**

```bash
cd /Users/Alex/Dev/personal_agent
uv run python -c "
from personal_agent.config.governance_loader import load_governance_config
gov = load_governance_config()
assert 'web_search' in gov.tools, 'web_search not in governance tools'
ws = gov.tools['web_search']
print('web_search policy:', ws)
print('OK: governance config valid')
"
```

Expected: prints the web_search policy and `OK: governance config valid`.

- [ ] **Step 6.6: Commit**

```bash
git add config/governance/tools.yaml
git commit -m "feat(governance): add web_search tool policy, demote Perplexity as default (ADR-0034)"
```

---

## Task 7: Prompt Rebalancing — Three Changes in `prompts.py`

**Files:**
- Modify: `src/personal_agent/orchestrator/prompts.py`

- [ ] **Step 7.1: Update `_TOOL_RULES` (lines 39-46)**

Find the current `_TOOL_RULES` block:
```python
_TOOL_RULES = """\
Rules:
- If no tool is needed to answer accurately, respond directly without calling any tool.
- Do not invent tools or parameters. If no tool fits, say so directly.
- Provide ALL required parameters (e.g., list_directory requires {"path": "..."}).
- For large directories, prefer calling list_directory with include_details=false and/or max_entries (unless the user explicitly asked for every entry).
- After tool results are returned, synthesize a final natural-language answer. Do NOT request the same tool again unless the path/args must change.
- Whenever the user asks about current events, recent news, CVEs, product versions, or anything requiring live web data, call mcp_perplexity_ask for quick lookups or mcp_perplexity_research for deeper research instead of answering from your own knowledge."""
```

Replace it with:
```python
_TOOL_RULES = """\
Rules:
- If no tool is needed to answer accurately, respond directly without calling any tool.
- Do not invent tools or parameters. If no tool fits, say so directly.
- Provide ALL required parameters (e.g., list_directory requires {"path": "..."}).
- For large directories, prefer calling list_directory with include_details=false and/or max_entries (unless the user explicitly asked for every entry).
- After tool results are returned, synthesize a final natural-language answer. Do NOT request the same tool again unless the path/args must change.
- Whenever the user asks about current events, recent news, CVEs, product versions, or anything requiring live web data, call web_search for quick lookups (free, private, multi-engine). Pass categories='it' for technical queries, 'science' for research, 'news' for current events, 'weather' for forecasts.
- After web_search returns URLs, use mcp_fetch_content to read full page content when snippets are insufficient.
- Use mcp_perplexity_ask only when you specifically need a synthesized answer with citations, or when web_search results are insufficient for a complex question.
- Do NOT answer from your own knowledge when live information is needed; always search first."""
```

- [ ] **Step 7.2: Update `TOOL_USE_PROMPT_INJECTED` example (lines 70-71)**

Find:
```python
Example:

User: "What CVEs affect OpenSSH this month?"
[TOOL_REQUEST]{"name": "mcp_perplexity_ask", "arguments": {"messages": [{"role": "user", "content": "CVEs affecting OpenSSH this month"}]}}[END_TOOL_REQUEST]
```

Replace with:
```python
Examples:

User: "What's the latest version of FastAPI?"
[TOOL_REQUEST]{"name": "web_search", "arguments": {"query": "FastAPI latest version 2026", "categories": "it"}}[END_TOOL_REQUEST]

User: "Give me a comprehensive comparison of React vs Svelte with citations"
[TOOL_REQUEST]{"name": "mcp_perplexity_ask", "arguments": {"messages": [{"role": "user", "content": "comprehensive comparison React vs Svelte 2026 with benchmarks"}]}}[END_TOOL_REQUEST]
```

- [ ] **Step 7.3: Update `get_tool_awareness_prompt()` capabilities block (lines 144-147)**

Find:
```python
        tool_names_lower = [t.name.lower() for t in tools]
        capabilities = []
        if any("perplexity" in n for n in tool_names_lower):
            capabilities.append("internet search via Perplexity")
        if any("duckduckgo" in n for n in tool_names_lower):
            capabilities.append("web search via DuckDuckGo")
```

Replace with:
```python
        tool_names_lower = [t.name.lower() for t in tools]
        capabilities = []
        if any("web_search" == n for n in tool_names_lower):
            capabilities.append(
                "private web search via SearXNG "
                "(multi-engine, categories: general/it/science/news/weather)"
            )
        if any("perplexity" in n for n in tool_names_lower):
            capabilities.append("AI-synthesized research via Perplexity (for deep questions with citations)")
        if any("duckduckgo" in n for n in tool_names_lower):
            capabilities.append("web search via DuckDuckGo (fallback)")
```

- [ ] **Step 7.4: Verify prompts module imports without errors**

```bash
cd /Users/Alex/Dev/personal_agent
uv run python -c "
from personal_agent.orchestrator.prompts import (
    _TOOL_RULES,
    TOOL_USE_PROMPT_INJECTED,
    get_tool_awareness_prompt,
)
assert 'web_search' in _TOOL_RULES, '_TOOL_RULES not updated'
assert 'web_search' in TOOL_USE_PROMPT_INJECTED, 'TOOL_USE_PROMPT_INJECTED not updated'
print('_TOOL_RULES snippet:', _TOOL_RULES[-200:])
print('OK: all prompt changes verified')
"
```

Expected: prints the last 200 chars of `_TOOL_RULES` (should mention `web_search`) and `OK: all prompt changes verified`.

- [ ] **Step 7.5: Run mypy on prompts module**

```bash
uv run mypy src/personal_agent/orchestrator/prompts.py
```

Expected: `Success: no issues found`

- [ ] **Step 7.6: Run full test suite**

```bash
uv run pytest tests/ -v --tb=short -x
```

Expected: all tests pass. If any prompt-related test fails, inspect the specific test.

- [ ] **Step 7.7: Commit**

```bash
git add src/personal_agent/orchestrator/prompts.py
git commit -m "feat(prompts): rebalance search tool bias — web_search default, Perplexity for synthesis (ADR-0034)"
```

---

## Task 8: Integration Tests

**Files:**
- Create: `tests/test_tools/test_web_search_integration.py`

> **Note:** These tests require a running SearXNG container. Run `docker compose up searxng -d` before executing. They are skipped in CI unless `SEARXNG_INTEGRATION=1` is set.

- [ ] **Step 8.1: Create the integration test file**

Create `tests/test_tools/test_web_search_integration.py`:

```python
"""Integration tests for web_search tool — require running SearXNG container.

Run with:
    docker compose up searxng -d
    SEARXNG_INTEGRATION=1 uv run pytest tests/test_tools/test_web_search_integration.py -v

Skip automatically unless SEARXNG_INTEGRATION=1 is set.
"""

import os

import httpx
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def require_searxng() -> None:
    """Skip all tests in this module unless SEARXNG_INTEGRATION=1."""
    if not os.environ.get("SEARXNG_INTEGRATION"):
        pytest.skip("SEARXNG_INTEGRATION not set — skipping integration tests")


SEARXNG_URL = "http://localhost:8888"


@pytest.mark.asyncio
async def test_web_search_executor_smoke() -> None:
    """Smoke test: executor returns non-empty results for a broad query."""
    from personal_agent.tools.web import web_search_executor

    result = await web_search_executor(query="python programming")

    assert isinstance(result, dict)
    assert result["result_count"] > 0, "Expected live results from SearXNG"
    assert result["results"][0]["url"].startswith("http")
    assert result["results"][0]["title"] != ""


@pytest.mark.asyncio
async def test_web_search_category_routing_it() -> None:
    """Category routing: 'it' category returns results from technical engines."""
    from personal_agent.tools.web import web_search_executor

    result = await web_search_executor(query="asyncio event loop", categories="it")

    assert isinstance(result, dict)
    assert result["result_count"] > 0, "Expected results for IT category query"
    # Results should come from technical engines (SO, GitHub, MDN, etc.)
    engines_used = {r["engine"] for r in result["results"] if r.get("engine")}
    assert len(engines_used) > 0, "Expected engine attribution in results"


def test_searxng_health_check() -> None:
    """SearXNG /healthz endpoint returns 200."""
    resp = httpx.get(f"{SEARXNG_URL}/healthz", timeout=5)
    assert resp.status_code == 200, f"SearXNG healthz returned {resp.status_code}"
```

- [ ] **Step 8.2: Verify integration tests skip correctly (no Docker needed)**

```bash
cd /Users/Alex/Dev/personal_agent
uv run pytest tests/test_tools/test_web_search_integration.py -v
```

Expected: all 3 tests show `SKIPPED` with reason `SEARXNG_INTEGRATION not set`.

- [ ] **Step 8.3: Commit**

```bash
git add tests/test_tools/test_web_search_integration.py
git commit -m "test(tools): add integration tests for web_search / SearXNG (ADR-0034)"
```

---

## Task 9: Final Quality Check

- [ ] **Step 9.1: Full mypy check**

```bash
cd /Users/Alex/Dev/personal_agent
uv run mypy src/personal_agent/tools/web.py src/personal_agent/config/settings.py src/personal_agent/orchestrator/prompts.py src/personal_agent/tools/__init__.py
```

Expected: `Success: no issues found in 4 source files`

- [ ] **Step 9.2: Full ruff check and format**

```bash
uv run ruff check src/personal_agent/tools/web.py src/personal_agent/config/settings.py src/personal_agent/orchestrator/prompts.py src/personal_agent/tools/__init__.py
uv run ruff format --check src/personal_agent/tools/web.py src/personal_agent/config/settings.py src/personal_agent/orchestrator/prompts.py src/personal_agent/tools/__init__.py
```

Expected: no errors. If format check fails, run without `--check` to apply fixes.

- [ ] **Step 9.3: Full unit test suite**

```bash
uv run pytest tests/ -v --tb=short --ignore=tests/test_tools/test_web_search_integration.py
```

Expected: all tests pass.

- [ ] **Step 9.4: Verify acceptance criteria checklist**

```bash
uv run python -c "
from personal_agent.tools import get_default_registry
from personal_agent.orchestrator.prompts import _TOOL_RULES, TOOL_USE_PROMPT_INJECTED, get_tool_awareness_prompt
from personal_agent.config import settings

r = get_default_registry()
assert 'web_search' in r.list_tool_names(), 'FAIL: web_search not registered'
assert 'web_search' in _TOOL_RULES, 'FAIL: _TOOL_RULES not updated'
assert 'web_search' in TOOL_USE_PROMPT_INJECTED, 'FAIL: TOOL_USE_PROMPT_INJECTED not updated'
assert 'mcp_perplexity_ask' not in _TOOL_RULES or 'only when' in _TOOL_RULES, 'FAIL: Perplexity still default in _TOOL_RULES'
assert settings.searxng_base_url == 'http://localhost:8888', 'FAIL: settings not loaded'

print('✓ web_search registered in tool registry')
print('✓ _TOOL_RULES references web_search as default')
print('✓ TOOL_USE_PROMPT_INJECTED uses web_search as primary example')
print('✓ Settings loaded correctly')
print()
print('All acceptance criteria passed.')
"
```

---

## Acceptance Criteria (from spec)

- [ ] `docker compose up searxng` starts SearXNG and `/healthz` returns 200
- [ ] `curl 'http://localhost:8888/search?q=python&format=json'` returns structured JSON
- [ ] `web_search` appears in `registry.list_tools()` output
- [ ] `web_search` appears in `get_tool_awareness_prompt()` capability list (first, before Perplexity)
- [ ] `web_search_executor(query="python asyncio", categories="it")` returns dict with titles, URLs, snippets
- [ ] `web_search_executor(query="")` raises `ToolExecutionError` with descriptive message
- [ ] `ToolExecutionLayer.execute_tool("web_search", ...)` returns `ToolResult(success=True)` wrapping the dict
- [ ] `ToolExecutionLayer` returns permission-denied `ToolResult` in LOCKDOWN mode
- [ ] `_TOOL_RULES` references `web_search` as default; Perplexity reserved for synthesized answers
- [ ] `TOOL_USE_PROMPT_INJECTED` example uses `web_search` as primary example
- [ ] `mcp_perplexity_ask` description_override narrowed — no longer "any question requiring live web data"
- [ ] `mcp_search` description_override references `web_search` as preferred alternative
- [ ] `mcp_fetch_content` description_override no longer directs to Perplexity
- [ ] `get_tool_awareness_prompt()` lists `web_search` first; Perplexity as "AI-synthesized research"
- [ ] All 12 unit tests pass (`pytest tests/test_tools/test_web_search.py -v`)
- [ ] `mypy src/personal_agent/tools/web.py` clean
- [ ] `ruff check src/personal_agent/tools/web.py` clean
