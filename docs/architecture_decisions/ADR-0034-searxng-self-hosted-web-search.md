# ADR-0034: SearXNG Self-Hosted Web Search Integration

**Status**: Accepted
**Date**: 2026-03-27
**Deciders**: Project owner
**Extends**: ADR-0011 (MCP Gateway Integration), ADR-0026 (search_memory native tool)

---

## Context

The agent's web search capability is currently served by two MCP-proxied tools:

| Tool | Backend | What it returns | Cost |
|------|---------|-----------------|------|
| `mcp_perplexity_ask` / `mcp_perplexity_research` | Perplexity Sonar API | Synthesized prose with citations | Per-query (paid) |
| `mcp_search` | DuckDuckGo | Raw links + snippets | Free |
| `mcp_fetch_content` | HTTP fetch | Parsed page text | Free |

Three architectural problems exist:

### Problem 1: No Private Search Path

Every query sent through `mcp_perplexity_ask` is logged by Perplexity's servers. `mcp_search` routes through DuckDuckGo's API. For a personal agent handling potentially sensitive queries (health, financial, personal research), there is no search path where the query stays within infrastructure the project owner controls.

### Problem 2: No Search-Time Engine Routing

DuckDuckGo returns general web results regardless of query domain. There is no way to say "search StackOverflow and GitHub for this technical question" or "search arXiv and Semantic Scholar for this research query." The orchestrator has no control over which upstream engines are consulted — it can only choose between Perplexity (synthesized, paid) and DuckDuckGo (raw, single-engine).

### Problem 3: Native Tool Gap for Web Search

The `tools/AGENTS.md` lists `web.py` (web search tools) but no such file exists. The planned native `web_search` tool was never implemented. All web search flows through the MCP gateway, which:
- Requires Docker MCP Gateway subprocess to be running
- Adds subprocess + container latency to every query
- Cannot be called when `mcp_gateway_enabled` is `False`
- Does not integrate with the governance layer as cleanly as native tools (no `ctx` threading, no direct telemetry emit)

### The Opportunity

SearXNG is a self-hosted, open-source metasearch engine that:
- Aggregates results from 70+ upstream engines (Google, Brave, DuckDuckGo, StackOverflow, arXiv, Wikipedia, etc.)
- Provides a JSON API (`/search?format=json`) returning structured results
- Proxies queries so upstream engines see the SearXNG server's IP, not the user's
- Runs as a single Docker container (~150 MB)
- Supports per-query engine selection via `engines` and `categories` parameters
- Has zero per-query cost
- Is actively maintained (600+ contributors, frequent releases)

---

## Decision

### D1: Add SearXNG as a Docker Compose Service

Add SearXNG to `docker-compose.yml` alongside Postgres, Elasticsearch, Neo4j, and Kibana. It runs as a containerized service on the internal Docker network, accessible to the agent at `http://searxng:8080`.

```
┌──────────────────────────────────────────────────────────────┐
│ docker-compose.yml                                            │
│                                                               │
│  postgres ─── elasticsearch ─── kibana ─── neo4j ─── searxng │
│                                                               │
└──────────────────────────────────────────────────────────────┘
         ▲                                            ▲
         │                                            │
    Agent (Python) ───── httpx GET ───────────────────┘
```

SearXNG has no persistent data (no named data volume required for MVP). Configuration is bind-mounted from `config/searxng/settings.yml`. Note: SearXNG generates a random `secret_key` on first start and may write `uwsgi.ini` into the config directory — both are gitignored.

### D2: Implement `web_search` as a Native In-Process Tool

Create `src/personal_agent/tools/web.py` — the file already listed in `tools/AGENTS.md` but never implemented. This is a native tool (not MCP), matching the pattern established by `search_memory` (ADR-0026) and `self_telemetry_query`.

**Why native, not MCP:**
1. SearXNG runs on the local Docker network — a simple `httpx.get()` is sufficient
2. Native tools get `ctx` (TraceContext) threading, direct telemetry, and governance integration without adapter overhead
3. The tool works independently of `mcp_gateway_enabled` — no MCP dependency
4. Matches the planned `web_search` tool from the Tool Execution Validation Spec

The tool calls SearXNG's JSON API and returns structured results:

```
Agent → web_search(query, categories, engines, ...) → httpx → SearXNG → upstream engines
                                                                 ↓
                                                        JSON response
                                                                 ↓
                                                        ToolResult(output={results, suggestions, ...})
```

### D3: Layered Search Strategy

SearXNG does not replace Perplexity — they serve different purposes:

| Use case | Tool | Rationale |
|----------|------|-----------|
| Quick factual lookups | `web_search` (SearXNG) | Free, private, structured results |
| Technical queries | `web_search` with `categories=it` | Engine routing to SO, GitHub, MDN |
| Research queries | `web_search` with `categories=science` | Engine routing to arXiv, Scholar |
| Weather lookups | `web_search` with `categories=weather` | Engine routing to wttr.in |
| Deep synthesis requiring multi-hop reasoning | `mcp_perplexity_research` | Perplexity's synthesis is superior for complex research |
| URL content retrieval | `mcp_fetch_content` | Unchanged — fetch a known URL |

The orchestrator prompt instructs the agent to prefer `web_search` for routine lookups and reserve Perplexity for deep research where synthesis quality justifies cost.

### D4: SearXNG Configuration

Engine selection and search behavior are configured in `config/searxng/settings.yml`, not in Python code. This keeps search engine policy separate from application logic.

Key configuration decisions:
- **Default engines**: Google, Brave, DuckDuckGo (general); StackOverflow, GitHub (IT); arXiv, Semantic Scholar (science); wttr.in (weather)
- **Safe search**: Disabled (agent is a private tool, not a public-facing product)
- **Result format**: JSON only (the HTML UI is accessible for debugging but not used by the agent)
- **Rate limiting**: Configured per-engine to avoid upstream blocks
- **Outgoing proxies**: Optional (configurable if the project owner wants Tor or a VPN exit)

### D5: Governance Integration

The `web_search` tool is registered in `config/governance/tools.yaml` as a `network` category tool:

```yaml
web_search:
  category: "network"
  allowed_in_modes: ["NORMAL", "ALERT", "DEGRADED"]
  risk_level: "low"
  requires_approval: false
  timeout_seconds: 15
```

Risk level is `low` because:
- It is read-only (no side effects on external systems)
- Queries are proxied through SearXNG (no direct IP exposure to upstream engines)
- Results are structured data (no code execution)

### D6: Rebalance Prompt and Governance Bias Away from Perplexity

The current system has six separate locations that actively direct the LLM to use Perplexity as the primary (or only) web search tool. Simply adding `web_search` is insufficient — the existing bias would cause the LLM to ignore it in favor of Perplexity. All six must be updated:

| # | Location | Current bias | Change |
|---|----------|-------------|--------|
| 1 | `_TOOL_RULES` (prompts.py) | "call mcp_perplexity_ask for quick lookups... instead of answering from your own knowledge" | Rewrite to prefer `web_search` for routine lookups; reserve Perplexity for synthesized answers |
| 2 | `TOOL_USE_PROMPT_INJECTED` example (prompts.py) | Only example is `mcp_perplexity_ask` | Replace with `web_search` example; add Perplexity as secondary example for deep research |
| 3 | `mcp_perplexity_ask` description_override (tools.yaml) | "any question requiring live web data... always call this tool" | Narrow to "synthesized answers with citations when web_search results need deeper analysis" |
| 4 | `mcp_search` description_override (tools.yaml) | "prefer mcp_perplexity_ask" for direct answers | Update to "prefer web_search for most queries; this tool is a fallback if SearXNG is unavailable" |
| 5 | `mcp_fetch_content` description_override (tools.yaml) | "Prefer mcp_perplexity_ask or mcp_search" | Update to "Prefer web_search or mcp_search" |
| 6 | `get_tool_awareness_prompt()` (prompts.py) | Perplexity listed first as "internet search" | Add `web_search` entry first as primary; demote Perplexity to "AI-synthesized research" |

**Principle**: The agent should reach for `web_search` by default. Perplexity remains available for deep research where synthesis quality justifies the cost — but the system should no longer *instruct* the LLM to use it for every web query.

### D7: Tool Awareness Prompt Update

Add SearXNG awareness to `get_tool_awareness_prompt()` in `orchestrator/prompts.py`:

```python
if any("web_search" == n for n in tool_names_lower):
    capabilities.append("private web search via SearXNG (multi-engine)")
```

Update the system prompt to instruct the agent:
- Use `web_search` for most web lookups (free, private)
- Pass `categories` parameter for domain-specific searches
- Use `mcp_perplexity_ask` only when synthesis or citations are specifically needed
- Use `mcp_fetch_content` to read full page content after finding URLs via `web_search`

---

## Alternatives Considered

### A1: Add Brave Search API as a Native Tool

**Rejected**: Brave Search API is paid ($5/1000 queries at the Pro tier). It provides a single engine's perspective, not aggregated results. SearXNG can include Brave as one of many engines at zero marginal cost.

### A2: Use Tavily (AI-Optimized Search API)

**Rejected**: Tavily is a paid service ($0.01/search) specifically designed for AI agents. While the structured output is excellent, it has the same privacy concerns as Perplexity — queries are sent to Tavily's servers. SearXNG achieves comparable structured output while keeping queries on controlled infrastructure.

### A3: Use SearXNG via MCP Gateway (Not Native)

**Rejected**: There is no official SearXNG MCP server in Docker's catalog. Building a custom MCP server wrapping SearXNG adds unnecessary layers (Python agent → MCP client → MCP server → httpx → SearXNG). A native tool with a direct `httpx.get()` is simpler, faster, and fully integrated with the governance layer. The MCP gateway remains available for tools where the MCP ecosystem provides value (Perplexity, Context7, Elasticsearch).

### A4: Replace Perplexity Entirely with SearXNG + Agent Synthesis

**Deferred**: In principle, the agent could call `web_search`, then `mcp_fetch_content` on the top results, and synthesize an answer itself. This would eliminate Perplexity's cost entirely. However, Perplexity's multi-hop research with source grounding is currently superior for deep research tasks. This can be revisited after evaluating the agent's synthesis quality with SearXNG results in the evaluation harness.

### A5: Use Google Custom Search API

**Rejected**: Requires a Google Cloud project, API key, and programmable search engine ID. Limited to 100 queries/day on free tier, $5/1000 queries after. A single engine rather than aggregated. SearXNG can include Google results without the API overhead.

---

## Consequences

### Positive

- **Private search**: Queries stay on project-owned infrastructure; upstream engines see SearXNG's IP
- **Zero marginal cost**: No per-query charges; reduces Perplexity API spend for routine lookups
- **Engine routing**: Agent can target specific engine categories per query domain (IT, science, news)
- **Native tool gap filled**: `src/personal_agent/tools/web.py` finally implemented
- **Structured JSON output**: Clean results for LLM consumption without prose parsing
- **Composable**: `web_search` → pick URLs → `mcp_fetch_content` → agent synthesizes
- **Infrastructure alignment**: Joins existing Docker Compose stack; same operational model as Postgres/ES/Neo4j

### Negative

- **Another Docker service**: Adds ~150 MB image and ~100 MB runtime memory to the Compose stack
- **No synthesis**: Unlike Perplexity, SearXNG returns raw results — the agent must do its own reasoning over links and snippets
- **Upstream rate limiting risk**: If SearXNG sends too many queries to Google/Bing, upstream may throttle. Mitigation: configure rate limits per engine in `settings.yml`; use multiple engines for redundancy
- **Maintenance**: SearXNG configuration (engine list, rate limits) needs periodic review as upstream APIs change

### Risks

- **Engine deprecation**: Upstream engines may change their APIs, breaking SearXNG scrapers. **Mitigation**: SearXNG community maintains engine adapters actively; configure multiple engines per category so one failure doesn't block search.
- **Quality variance**: Aggregated results may include low-quality sources. **Mitigation**: Configure only trusted engines; the agent can filter results by score.
- **Docker network isolation**: SearXNG needs outbound internet access from within Docker. **Mitigation**: Docker's default bridge network allows outbound; no special configuration needed.

---

## Acceptance Criteria

- [ ] SearXNG service defined in `docker-compose.yml`, starts with `docker compose up`
- [ ] `config/searxng/settings.yml` exists with configured engines (general, IT, science categories)
- [ ] `src/personal_agent/tools/web.py` exists with `web_search` tool definition and executor
- [ ] `web_search` registered in `register_mvp_tools()` in `tools/__init__.py`
- [ ] `web_search` appears in `registry.list_tools()` output
- [ ] `config/governance/tools.yaml` has `web_search` entry with `category: "network"`
- [ ] `orchestrator/prompts.py` updated with SearXNG-aware capability line and routing guidance
- [ ] `_TOOL_RULES` no longer hardcodes `mcp_perplexity_ask` as the default web search tool
- [ ] `TOOL_USE_PROMPT_INJECTED` example uses `web_search` as primary, Perplexity as secondary
- [ ] `mcp_perplexity_ask` description_override narrowed to synthesized answers (not "any live web data")
- [ ] `mcp_search` description_override updated to reference `web_search` as primary
- [ ] `mcp_fetch_content` description_override no longer directs to Perplexity
- [ ] `get_tool_awareness_prompt()` lists `web_search` before Perplexity in capabilities
- [ ] `web_search(query="python asyncio tutorial", categories="it")` returns structured JSON results
- [ ] `web_search` returns `ToolResult(success=False, error=...)` when SearXNG is unreachable
- [ ] Unit tests for `web_search_executor` with mocked SearXNG responses
- [ ] Integration test verifying SearXNG container responds to `/search?format=json`
- [ ] `mypy src/personal_agent/tools/web.py` clean
- [ ] `ruff check src/personal_agent/tools/web.py` clean

---

## Related

- **ADR-0011**: MCP Gateway Integration (tool expansion architecture — SearXNG complements, not replaces)
- **ADR-0026**: `search_memory` Native Tool (same native tool pattern reused here)
- **ADR-0005**: Governance Configuration (tool permissions model)
- **Spec**: `docs/specs/SEARXNG_WEB_SEARCH_TOOL_SPEC.md` (implementation details)
- **Tool Execution Validation Spec**: `docs/architecture/TOOL_EXECUTION_VALIDATION_SPEC_v0.1.md` (planned `web_search` tool)
