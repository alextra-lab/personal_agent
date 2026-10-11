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

1. **`ToolDefinition`** — declares the contract (name, parameters, governance metadata,
   `error_codes`, `max_result_tokens`)
2. **Executor function** — `async def <name>_executor(...) -> dict[str, Any]`; it returns the
   success payload and raises its typed error on a failure (the tool contract, below)
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

### The tool contract (ADR-0156 D6, FRE-1575)

The model reads every tool result in one shape:

- success: `{"ok": true, ...payload}` — the layer adds `ok`; the payload never sets `ok`,
  `success: false`, `status: "error"` or a top-level `error` (the layer refuses such a payload
  as a `contract_violation`);
- failure: `{"ok": false, "error": {"code", "message", "retryable", "next_action"}}`.

Rules:

- **Typed codes.** Each tool declares `MyCode = Literal[...]` and `class MyError(ToolError[MyCode])`,
  and raises only `MyError`. mypy refuses an undeclared code. The definition sets
  `error_codes=error_codes(MyError)`; the layer refuses any other code at run time. A helper
  shared by several tools raises a shared class whose `Literal` is nested in each caller's.
- **`retryable`** is true only when repeating the same call can succeed. **`next_action`**
  names the next call that can work, or says that none exists.
- **Messages** are bounded at 2,000 characters, never clipped shorter.
- **Size.** `max_result_tokens` (at most 25,000) bounds the result; a longer one is truncated
  with `truncation_next_action`.
- **Telemetry.** Every call logs `tool_call_completed` once, with `success` equal to `ok` and
  `error_code` on a failure.
- **Description (S5).** At least 3 sentences: what it does, when to use it, when not (name the
  better tool), its limits, and 1–3 example calls written as `my_tool(query="...")`.
- **Coverage.** Every line that raises a failure is executed by a test
  (`scripts/check_tool_failure_coverage.py`, run in CI). Build each failure in a statement of
  its own: the script refuses a builder in `x or ...`, in a branch of `a if c else b`, in a
  lambda or in a comprehension, because statement coverage cannot prove that it ran. The contract test is
  `tests/personal_agent/tools/test_tool_contract.py`.

### Minimal template
```python
# src/personal_agent/tools/myservice.py
from __future__ import annotations
from typing import Any, Literal
import httpx
from personal_agent.config import settings
from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.contract import ToolError, error_codes
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

MyToolCode = Literal["missing_query", "service_unavailable", "service_timeout"]


class MyToolError(ToolError[MyToolCode]):
    """A my_tool failure."""


my_tool = ToolDefinition(
    name="my_tool",
    description=(
        "What this tool does. When to use it. When not to use it, and which tool to use "
        'instead. Its limits. Example: my_tool(query="...").'
    ),
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
    error_codes=error_codes(MyToolError),
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
        MyToolError: On failure.
    """
    query = (query or "").strip()
    if not query:
        raise MyToolError(
            "missing_query", "'query' is required.",
            retryable=True, next_action="Call my_tool again with a query.",
        )
    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"
    log.info("my_tool_started", trace_id=trace_id, query=query[:80])
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(settings.my_service_url, params={"q": query})
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError as exc:
        raise MyToolError(
            "service_unavailable", "Cannot connect to my-service.",
            retryable=True, next_action="Call my_tool once more; if it fails, tell the user.",
        ) from exc
    except httpx.TimeoutException as exc:
        raise MyToolError(
            "service_timeout", "my-service request timed out.",
            retryable=True, next_action="Call my_tool once more with a narrower query.",
        ) from exc
    log.info("my_tool_completed", trace_id=trace_id)
    return {"result": data}
```

Then in `src/personal_agent/tools/__init__.py`:
```python
from personal_agent.tools.myservice import my_tool, my_tool_executor
# inside register_mvp_tools():
registry.register(my_tool, my_tool_executor)
```

And in `config/governance/tools.yaml` (availability per mode, limits):
```yaml
my_tool:
  category: "network"
  allowed_in_modes: ["NORMAL", "DEGRADED"]
  risk_level: "low"
  timeout_seconds: 30
  rate_limit_per_hour: 100
```

And a rule in `config/governance/permissions.yaml` (ADR-0156 D8). A tool with no rule asks the
user on every call, so decide it on purpose:
```yaml
allow:
  - rule: "my_tool"            # every call runs; or my_tool(param=value*) for a subset
ask:
  - rule: "my_tool(mode=write)"
    reason: "Writes to the service."
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

A Tier 2 tool is documented by a skill in the Agent Skills format (ADR-0156 D1). The system
prompt lists every skill's name and description. When a task matches a skill, the model loads
the skill with `read_skill(name)` and runs the CLI with its shell tool. Nothing in the user's
message selects a skill (ADR-0156 D2).

**No Python wrapper code is needed.** The skill is the tool's interface.

### Skill folder
```
docs/skills/<name>/
├── SKILL.md          ← frontmatter (name, description, metadata) + instructions
├── evals.yaml        ← at least 3 scenarios
├── scripts/          ← optional tested scripts; they ship in the gateway image
└── references/       ← optional detail, loaded with read_skill(name, path)
```

### Skill template
See `docs/skills/SKILL_TEMPLATE.md`.

### Adding a Tier 2 tool
1. Create `docs/skills/<name>/SKILL.md` and `evals.yaml` from the template.
2. Add `<name>` to the inventory in `tests/docs/test_skill_validator.py`.
3. Run `make test-file FILE=tests/docs/test_skill_validator.py`. The validator checks the
   format, and `tests/personal_agent/governance/test_skill_commands_permissions.py` checks
   that every command is allowed or a documented ask.

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
- No MCP server is enabled by default (ADR-0156 D7.5): `docker/mcp/run-gateway.sh` has an
  empty default server set. `context7` and `sequentialthinking` were removed because they
  copied `get_library_docs` and the `sequential-thinking` skill
- The MCP client maps a result with `isError: true` onto the tool contract
  (`MCPToolError`, code `mcp_tool_error`, ADR-0156 D6.6)

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
- [ ] Executor raises its own typed `ToolError` subclass with a declared code, and the
      definition sets `error_codes` (ADR-0156 D6); every raise line has a test
- [ ] Description has at least 3 sentences, when not to use it, and an example call (D6.5)
- [ ] Executor uses `from personal_agent.config import settings` (never `os.getenv`)
- [ ] Executor logs `started` / `completed` events with `trace_id`
- [ ] Registered in `register_mvp_tools()` in `tools/__init__.py`
- [ ] Governance entry added to `config/governance/tools.yaml`
- [ ] Unit tests written with mocked external calls
- [ ] `uv run mypy src/`, `uv run ruff check src/`, `uv run pytest` all pass
