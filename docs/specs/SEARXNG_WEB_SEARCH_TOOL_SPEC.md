# SearXNG Web Search Tool Spec

**Date**: 2026-03-27
**Status**: Proposed
**Phase**: Infrastructure + Native Tool
**Related**: ADR-0034 (SearXNG self-hosted web search), ADR-0026 (search_memory pattern), ADR-0011 (MCP gateway)

---

## Problem

The agent has no private, free, structured web search capability. MCP-proxied tools
(`mcp_perplexity_ask`, `mcp_search`) depend on the MCP gateway subprocess and send
queries to third-party servers. The planned native `web_search` tool
(`src/personal_agent/tools/web.py`) was never implemented.

## Proposal

1. Add a SearXNG Docker container to `docker-compose.yml`
2. Create `docker/searxng/settings.yml` with engine configuration
3. Implement `web_search` as a native in-process tool in `src/personal_agent/tools/web.py`
4. Register in the tool registry, governance config, and orchestrator prompts

---

## 1. Infrastructure: Docker Compose Service

### `docker-compose.yml` — Add service

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

Notes:
- Port 8888 on host maps to 8080 inside container (avoids conflicts with other services)
- `docker/searxng/` is mounted read-write because SearXNG writes a generated secret key on first start
- No named volume needed — SearXNG has no persistent state worth preserving
- Health check uses SearXNG's built-in `/healthz` endpoint
- `restart: unless-stopped` keeps it running after host reboots

### `docker/searxng/settings.yml`

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
  secret_key: ""
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

### `.gitignore` update

SearXNG writes generated files to the config dir on startup. Add:

```
docker/searxng/uwsgi.ini
```

Note: If SearXNG rewrites `settings.yml` to inject a generated `secret_key`, the file
will appear modified in `git status`. To avoid this, set a stable secret in
`settings.yml` before first start (any string ≥ 32 characters). Alternatively, accept
the empty default and gitignore the generated change.

---

## 2. Application Configuration

### `src/personal_agent/config/settings.py` — Add settings

```python
# SearXNG (ADR-0034)
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

Environment variable mapping (per ADR-0007):
- `AGENT_SEARXNG_BASE_URL` → `searxng_base_url`
- `AGENT_SEARXNG_TIMEOUT_SECONDS` → `searxng_timeout_seconds`
- `AGENT_SEARXNG_DEFAULT_CATEGORIES` → `searxng_default_categories`
- `AGENT_SEARXNG_MAX_RESULTS` → `searxng_max_results`

---

## 3. Native Tool: `src/personal_agent/tools/web.py`

### Tool Definition

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
```

### Executor Function

```python
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

### Registration in `tools/__init__.py`

```python
from personal_agent.tools.web import web_search_executor, web_search_tool

def register_mvp_tools(registry: ToolRegistry) -> None:
    # ... existing registrations ...
    registry.register(web_search_tool, web_search_executor)  # ADR-0034
```

---

## 4. Governance Configuration

### `config/governance/tools.yaml` — Add entry

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

---

## 5. Prompt and Governance Rebalancing (Perplexity Bias Remediation)

The current system has **six locations** that actively direct the LLM to use Perplexity
as the primary search tool. Simply adding `web_search` is insufficient — the existing
bias would cause the LLM to ignore it. All six must be updated in this implementation.

### 5.1 `_TOOL_RULES` in `orchestrator/prompts.py` (line ~46)

**Current** (hardcoded Perplexity instruction):
```
- Whenever the user asks about current events, recent news, CVEs, product versions,
  or anything requiring live web data, call mcp_perplexity_ask for quick lookups or
  mcp_perplexity_research for deeper research instead of answering from your own knowledge.
```

**Replace with**:
```
- Whenever the user asks about current events, recent news, CVEs, product versions,
  or anything requiring live web data, call web_search for quick lookups (free, private,
  multi-engine). Pass categories='it' for technical queries, 'science' for research,
  'news' for current events, 'weather' for forecasts.
- After web_search returns URLs, use mcp_fetch_content to read full page content
  when snippets are insufficient.
- Use mcp_perplexity_ask only when you specifically need a synthesized answer with
  citations, or when web_search results are insufficient for a complex question.
- Do NOT answer from your own knowledge when live information is needed; always search first.
```

### 5.2 `TOOL_USE_PROMPT_INJECTED` example in `orchestrator/prompts.py` (line ~71)

**Current** (only example is Perplexity):
```
User: "What CVEs affect OpenSSH this month?"
[TOOL_REQUEST]{"name": "mcp_perplexity_ask", "arguments": {"messages": [{"role": "user", "content": "CVEs affecting OpenSSH this month"}]}}[END_TOOL_REQUEST]
```

**Replace with** (web_search as primary example, Perplexity as secondary):
```
User: "What's the latest version of FastAPI?"
[TOOL_REQUEST]{"name": "web_search", "arguments": {"query": "FastAPI latest version 2026", "categories": "it"}}[END_TOOL_REQUEST]

User: "Give me a comprehensive comparison of React vs Svelte with citations"
[TOOL_REQUEST]{"name": "mcp_perplexity_ask", "arguments": {"messages": [{"role": "user", "content": "comprehensive comparison React vs Svelte 2026 with benchmarks"}]}}[END_TOOL_REQUEST]
```

### 5.3 `mcp_perplexity_ask` description_override in `config/governance/tools.yaml`

**Current**:
```
"Search the internet using Perplexity AI and get a direct answer with citations.
Use for factual questions, current events, CVEs, or any question requiring live web data.
Prefer this over mcp_perplexity_research for most queries — it is faster.
Do NOT answer from your own knowledge when live information is needed;
always call this tool instead."
```

**Replace with**:
```
"Get a synthesized answer with citations from Perplexity AI. Use when you need
a pre-digested summary with source links — e.g. multi-factor comparisons,
nuanced technical explanations, or when web_search snippets are insufficient.
For routine factual lookups, prefer web_search (faster, free, private).
Prefer this over mcp_perplexity_research unless broad multi-source coverage
is specifically needed."
```

### 5.4 `mcp_search` (DuckDuckGo) description_override in `config/governance/tools.yaml`

**Current**:
```
"Search DuckDuckGo and return a list of result links and snippets.
Use when you want to browse raw search results or pick specific URLs to read
with mcp_fetch_content. For direct answers with citations, prefer mcp_perplexity_ask."
```

**Replace with**:
```
"Search DuckDuckGo and return a list of result links and snippets.
Fallback search tool if web_search (SearXNG) is unavailable.
For most searches, prefer web_search which aggregates multiple engines."
```

### 5.5 `mcp_fetch_content` description_override in `config/governance/tools.yaml`

**Current**:
```
"Fetch and parse the full text of a specific webpage URL. Use when you already
have a URL and want to read its content. Prefer mcp_perplexity_ask or mcp_search
when you need to find information without a known URL."
```

**Replace with**:
```
"Fetch and parse the full text of a specific webpage URL. Use when you already
have a URL and want to read its content. Prefer web_search or mcp_search
when you need to find information without a known URL."
```

### 5.6 `get_tool_awareness_prompt()` in `orchestrator/prompts.py`

**Current** (Perplexity first, no web_search):
```python
if any("perplexity" in n for n in tool_names_lower):
    capabilities.append("internet search via Perplexity")
if any("duckduckgo" in n for n in tool_names_lower):
    capabilities.append("web search via DuckDuckGo")
```

**Replace with** (web_search first, Perplexity narrowed):
```python
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

---

## 6. Files to Create/Modify

| File | Action | Description |
|------|--------|-------------|
| `docker-compose.yml` | **Modify** | Add `searxng` service |
| `docker/searxng/settings.yml` | **Create** | SearXNG engine and server configuration |
| `.gitignore` | **Modify** | Add `docker/searxng/uwsgi.ini` |
| `src/personal_agent/config/settings.py` | **Modify** | Add `searxng_*` settings fields |
| `src/personal_agent/tools/web.py` | **Create** | Tool definition + executor |
| `src/personal_agent/tools/__init__.py` | **Modify** | Import and register `web_search` |
| `config/governance/tools.yaml` | **Modify** | Add `web_search` entry; update `description_override` for `mcp_perplexity_ask`, `mcp_search`, `mcp_fetch_content` (Section 5.3–5.5) |
| `src/personal_agent/orchestrator/prompts.py` | **Modify** | Rewrite `_TOOL_RULES` (5.1), replace `TOOL_USE_PROMPT_INJECTED` example (5.2), update `get_tool_awareness_prompt()` (5.6) |
| `tests/test_tools/test_web_search.py` | **Create** | Unit tests |
| `tests/test_tools/test_web_search_integration.py` | **Create** | Integration test (requires running SearXNG) |

---

## 7. Testing Strategy

### Unit tests (`tests/test_tools/test_web_search.py`)

These mock `httpx` responses — no SearXNG container needed.
The executor returns `dict[str, Any]` on success and raises `ToolExecutionError` on failure
(the `ToolExecutionLayer` wraps both into `ToolResult`):

1. **Happy path**: Mock SearXNG JSON response → verify returned dict has `results`, `result_count`, `suggestions`
2. **Empty query**: Verify raises `ToolExecutionError` with descriptive message
3. **Categories parameter**: Verify `categories` is passed in query params to httpx
4. **Engines parameter**: Verify `engines` overrides categories in query params
5. **Time range**: Verify `time_range` is passed through
6. **Max results capping**: Request 100 results → verify capped at 50
7. **Connection error**: Mock `httpx.ConnectError` → verify raises `ToolExecutionError`
8. **Timeout**: Mock `httpx.TimeoutException` → verify raises `ToolExecutionError`
9. **Malformed JSON**: Mock non-JSON response → verify raises `ToolExecutionError`
10. **Empty results**: Mock response with zero results → verify returns dict with `result_count=0`
11. **Infobox handling**: Mock response with infoboxes → verify truncation and structure
12. **LOCKDOWN mode blocked**: Execute via `ToolExecutionLayer` with mode=LOCKDOWN → verify `ToolResult(success=False)` with permission denied

### Integration test (`tests/test_tools/test_web_search_integration.py`)

Requires running `docker compose up searxng`. Mark with `@pytest.mark.integration`:

1. **Smoke test**: `web_search(query="python programming")` → verify non-empty results
2. **Category routing**: `web_search(query="asyncio", categories="it")` → verify results include SO/GitHub-sourced entries
3. **Health check**: GET `http://localhost:8888/healthz` → verify 200

### Test classification

Per project testing standards:
- Unit tests: `tests/test_tools/test_web_search.py` — runs in CI, no Docker needed
- Integration tests: `tests/test_tools/test_web_search_integration.py` — requires `docker compose up searxng`, marked `@pytest.mark.integration`

---

## 8. Acceptance Criteria

- [ ] `docker compose up searxng` starts SearXNG and `/healthz` returns 200
- [ ] `curl 'http://localhost:8888/search?q=python&format=json'` returns structured JSON with results
- [ ] `web_search` tool appears in `registry.list_tools()` output
- [ ] `web_search` appears in `get_tool_awareness_prompt()` capability list
- [ ] `web_search_executor(query="python asyncio", categories="it")` returns dict with structured results containing titles, URLs, snippets, engines
- [ ] `web_search_executor(query="...")` raises `ToolExecutionError` with descriptive message when SearXNG is down
- [ ] `ToolExecutionLayer.execute_tool("web_search", ...)` wraps executor dict into `ToolResult(success=True)`
- [ ] Governance allows `web_search` in NORMAL, ALERT, DEGRADED modes
- [ ] Governance blocks `web_search` in LOCKDOWN and RECOVERY modes (not in `allowed_modes`; unit test asserts `ToolExecutionLayer` returns permission-denied `ToolResult`)
- [ ] Orchestrator prompt instructs agent to prefer `web_search` over Perplexity for routine lookups
- [ ] `_TOOL_RULES` references `web_search` as default; Perplexity reserved for synthesized answers (5.1)
- [ ] `TOOL_USE_PROMPT_INJECTED` example uses `web_search` as primary example (5.2)
- [ ] `mcp_perplexity_ask` description_override narrowed — no longer claims "any question requiring live web data" (5.3)
- [ ] `mcp_search` description_override references `web_search` as preferred alternative (5.4)
- [ ] `mcp_fetch_content` description_override no longer directs users to Perplexity (5.5)
- [ ] `get_tool_awareness_prompt()` lists `web_search` first; Perplexity described as "AI-synthesized research" (5.6)
- [ ] All unit tests pass (`pytest tests/test_tools/test_web_search.py -v`)
- [ ] `mypy src/personal_agent/tools/web.py` clean
- [ ] `ruff check src/personal_agent/tools/web.py` clean

---

## 9. Non-Goals

- **Replacing Perplexity**: SearXNG complements, not replaces. Deep synthesis stays with Perplexity.
- **Image/video search**: Only text results in MVP. Categories `images`/`videos` can be enabled later.
- **Search result caching**: No caching layer in MVP. SearXNG has no built-in result cache. Can add Redis or in-memory TTL cache in a follow-up if latency is a concern.
- **Custom SearXNG plugins**: Use stock engine configuration. Custom SearXNG plugins (e.g., for internal wikis) are a future extension.
- **Tor/VPN routing**: Optional in config but not configured by default. Document how to enable in `docker/searxng/settings.yml` comments.

---

## 10. Use Cases (How the Agent Calls This)

**Routine factual lookup** — user asks "What's the latest version of Python?":

```json
{"name": "web_search", "arguments": {"query": "latest Python version 2026"}}
```

**Technical question** — user asks "How do I use Pydantic model_validator?":

```json
{"name": "web_search", "arguments": {"query": "Pydantic v2 model_validator usage", "categories": "it"}}
```

**Research question** — user asks "What are recent papers on retrieval-augmented generation?":

```json
{"name": "web_search", "arguments": {"query": "retrieval augmented generation recent papers 2026", "categories": "science"}}
```

**Weather lookup** — user asks "What's the weather in Paris this weekend?":

```json
{"name": "web_search", "arguments": {"query": "Paris weather this weekend", "categories": "weather"}}
```

**Current events** — user asks "What happened with the OpenAI board?":

```json
{"name": "web_search", "arguments": {"query": "OpenAI board news", "categories": "news", "time_range": "week"}}
```

**Multi-step: search then fetch** — agent finds URLs, then reads full content:

```json
{"name": "web_search", "arguments": {"query": "SearXNG Docker setup guide"}}
```
→ Results include `https://docs.searxng.org/admin/installation-docker.html`
```json
{"name": "mcp_fetch_content", "arguments": {"url": "https://docs.searxng.org/admin/installation-docker.html"}}
```
