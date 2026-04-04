# Tool Integration Guide — Three-Tier Model

> **ADR:** `docs/architecture_decisions/ADR-0028-external-tool-cli-migration.md`
> **Status:** Adopted (as of 2026-04-04, post CLI-First Migration FRE-170–173)

This guide documents the three-tier tool integration model for the Personal Agent. All new tools **must** follow this decision framework. MCP is no longer the default.

---

## Decision Framework

Before adding a new tool, work through this decision tree:

```
Can the capability be implemented as a pure Python function
using httpx, subprocess, or stdlib in ≤ 150 lines?
  YES → Tier 1: Native in-process Python tool
  NO ↓

Does a battle-tested CLI (curl, gh, docker, git, jq) already wrap this API?
  YES → Tier 2: CLI tool with SKILL.md documentation
  NO ↓

Does the integration require a stateful protocol, streaming,
browser automation, or bidirectional communication?
  YES → Tier 3: MCP (requires explicit justification)
  NO  → Re-evaluate: most REST APIs are Tier 1 or Tier 2
```

**The default is Tier 1.** MCP requires justification.

---

## Tier 1: Native In-Process Python Tools

### When to use
- Direct access to local infrastructure (Elasticsearch, Neo4j, PostgreSQL)
- REST API wrappers where Python can own the HTTP lifecycle
- Operations that benefit from type safety and structured error handling
- Tools called frequently (zero subprocess overhead)

### Pattern

Every Tier 1 tool consists of:

1. **`ToolDefinition`** — declares the contract (name, parameters, governance metadata)
2. **Executor function** — `async def <name>_executor(...) -> dict[str, Any]`
3. **Registration** — added to `register_mvp_tools()` in `src/personal_agent/tools/__init__.py`
4. **Governance entry** — in `config/governance/tools.yaml`
5. **Unit tests** — in `tests/test_tools/test_<name>.py` with mocked httpx

### File layout
```
src/personal_agent/tools/
├── __init__.py          ← add import + registration here
├── web.py               ← example: SearXNG web search
├── elasticsearch.py     ← example: ES|QL + index ops
├── perplexity.py        ← example: Perplexity AI queries
├── fetch.py             ← example: URL fetch + HTML→text
└── context7.py          ← example: library documentation
```

### Minimal template
```python
# src/personal_agent/tools/myservice.py
from __future__ import annotations
from typing import Any
import httpx
from personal_agent.config import settings
from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

my_tool = ToolDefinition(
    name="my_tool",
    description="What this tool does and when to use it.",
    category="network",  # or: read_only, system_write, memory
    parameters=[
        ToolParameter(name="query", type="string", description="...", required=True,
                      default=None, json_schema=None),
    ],
    risk_level="low",          # low | medium | high
    allowed_modes=["NORMAL", "DEGRADED"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=30,
    rate_limit_per_hour=100,
)

async def my_tool_executor(
    query: str = "",
    ctx: TraceContext | None = None,
) -> dict[str, Any]:
    """Execute the tool.

    Args:
        query: ...
        ctx: Optional trace context.

    Returns:
        Plain dict — ToolExecutionLayer wraps in ToolResult.

    Raises:
        ToolExecutionError: On failure.
    """
    query = (query or "").strip()
    if not query:
        raise ToolExecutionError("'query' is required.")
    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"
    log.info("my_tool_started", trace_id=trace_id, query=query[:80])
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(settings.my_service_url, params={"q": query})
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError as exc:
        raise ToolExecutionError(f"Cannot connect to my-service.") from exc
    except httpx.TimeoutException as exc:
        raise ToolExecutionError("my-service request timed out.") from exc
    except Exception as exc:
        raise ToolExecutionError(str(exc)) from exc
    log.info("my_tool_completed", trace_id=trace_id)
    return {"result": data}
```

Then in `src/personal_agent/tools/__init__.py`:
```python
from personal_agent.tools.myservice import my_tool, my_tool_executor
# inside register_mvp_tools():
registry.register(my_tool, my_tool_executor)
```

And in `config/governance/tools.yaml`:
```yaml
my_tool:
  category: "network"
  allowed_in_modes: ["NORMAL", "DEGRADED"]
  risk_level: "low"
  requires_approval: false
  timeout_seconds: 30
  rate_limit_per_hour: 100
```

### Existing Tier 1 tools

| Tool | File | Replaces | ADR |
|------|------|----------|-----|
| `web_search` | `web.py` | `mcp_search` (DDG) | ADR-0034 |
| `query_elasticsearch` | `elasticsearch.py` | `mcp_esql`, `mcp_list_indices`, `mcp_get_mappings`, `mcp_get_shards` | ADR-0028 Ph1 |
| `perplexity_query` | `perplexity.py` | `mcp_perplexity_ask/reason/research` | ADR-0028 Ph2 |
| `fetch_url` | `fetch.py` | `mcp_fetch_content` | ADR-0028 Ph3 |
| `get_library_docs` | `context7.py` | `mcp_get-library-docs`, `mcp_resolve-library-id` | ADR-0028 Ph3 |
| `search_memory` | `memory_search.py` | — | ADR-0026 |
| `read_file` | `filesystem.py` | — | — |
| `list_directory` | `filesystem.py` | — | — |
| `system_metrics_snapshot` | `system_health.py` | — | — |
| `self_telemetry_query` | `self_telemetry.py` | — | — |

---

## Tier 2: CLI Tools with SKILL.md Documentation

### When to use
- A mature CLI already exists and is the canonical interface (`gh`, `docker`, `git`, `curl`)
- The tool is called infrequently and subprocess overhead is acceptable
- The CLI handles auth, pagination, or complex state better than a raw HTTP client

### Pattern

Tier 2 tools are documented in SKILL.md files. The agent reads the SKILL.md and invokes the CLI directly using its shell execution capability.

**No Python wrapper code is needed.** The SKILL.md is the tool.

### SKILL.md file location
```
docs/skills/
└── <toolname>.md      ← SKILL.md file for the CLI tool
```

### SKILL.md template
See `docs/skills/SKILL_TEMPLATE.md`.

### Adding a Tier 2 tool
1. Create `docs/skills/<toolname>.md` from the template
2. Reference it in `docs/skills/INDEX.md`
3. No code changes required

---

## Tier 3: MCP Tools (Reserved)

### When to use
MCP is **reserved** for integrations that genuinely cannot be Tier 1 or Tier 2:

- **Browser automation** — requires stateful browser protocol (Playwright/CDP)
- **Bidirectional streaming** — MCP's streaming model is needed
- **Complex multi-server orchestration** — MCP Gateway's routing is essential

### Constraints
- Every new MCP tool requires an ADR section justifying why Tier 1/2 is insufficient
- MCP tools must be added to `config/governance/tools.yaml` as `allowed_in_modes: []` initially
- Enable modes only after verifying the tool works with `mcp_gateway_enabled: true`
- The MCP Gateway code in `src/personal_agent/mcp/` is preserved but disabled by default

### Currently preserved MCP tools (disabled, future use)
- `mcp_browser_*` — browser automation (22 tools), reserved for future Playwright integration

---

## Why This Model

### Token cost comparison (measured 2026-04-04)

| Integration type | Schema tokens injected per request | Context overhead |
|------------------|------------------------------------|-----------------|
| 13 MCP tools enabled | ~4,000–6,500 tokens | High |
| 10 native tools (post-migration) | ~1,200–1,800 tokens | Low |
| Savings per request | ~2,800–4,700 tokens | ~60–70% reduction |

MCP tools inject their full JSON schema into every request. Native tools use the same schema mechanism but expose only what the LLM needs. Fewer tools = smaller context = faster, cheaper inference.

### Other benefits
- **Zero subprocess overhead** — Tier 1 tools run in-process
- **No Docker dependency** — MCP Gateway requires Docker daemon
- **Type-safe** — Pydantic tool definitions catch errors at registration time
- **Governance integration** — `ToolExecutionLayer` applies mode/rate limits uniformly
- **Testable** — httpx can be mocked; MCP required a live Docker container for integration tests

---

## Checklist for New Tools

Before submitting a new tool:

- [ ] Justified the tier (Tier 1 vs 2 vs 3) in ADR or PR description
- [ ] For Tier 3: documented why Tier 1/2 is insufficient
- [ ] `ToolDefinition` has correct `risk_level`, `allowed_modes`, `timeout_seconds`
- [ ] Executor raises `ToolExecutionError` (never bare exceptions) 
- [ ] Executor uses `from personal_agent.config import settings` (never `os.getenv`)
- [ ] Executor logs `started` / `completed` events with `trace_id`
- [ ] Registered in `register_mvp_tools()` in `tools/__init__.py`
- [ ] Governance entry added to `config/governance/tools.yaml`
- [ ] Unit tests written with mocked external calls
- [ ] `uv run mypy src/`, `uv run ruff check src/`, `uv run pytest` all pass
