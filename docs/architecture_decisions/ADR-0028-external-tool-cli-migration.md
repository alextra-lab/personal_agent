# ADR-0028: External Tool Integration — MCP Gateway vs CLI Army

**Status**: Accepted — Implemented 2026-04-04
**Date**: 2026-04-02 (research completed; original draft 2026-03-07)
**Implemented**: 2026-04-04 (FRE-171/170/173/172/188; all 4 phases complete)
**Deciders**: Project owner

---

## Context

The agent integrates external tools via three mechanisms today:

1. **Native in-process Python tools** (6 tools) — `read_file`, `list_directory`, `system_metrics_snapshot`, `search_memory` (ADR-0026), `self_telemetry_query`, `web_search` (ADR-0034). Zero overhead, no subprocess.
2. **MCP Gateway** (41 discovered tools, 13 enabled) — Docker subprocess speaking the Model Context Protocol. Routes all external tool calls through `src/personal_agent/mcp/gateway.py`.
3. **Memory CLI** (ADR-0027) — Direct terminal access to Neo4j via Typer sub-commands. Human-facing, no service dependency.

### Problems with the MCP Gateway

The following concerns were identified during project operation and confirmed by research:

**1. Context window overhead**
MCP tool schema discovery injects the full JSON schema of every registered tool into the LLM context. Industry benchmarks show MCP schemas cost 200-1,400 tokens per tool. With 13 enabled tools, our estimated overhead is **3,000-6,500 tokens per request** — consumed before the agent reasons about a single word of user input.

CLI tools the LLM already knows (e.g., `curl`, `docker`, `gh`) require near-zero schema tokens. Published comparisons report **4-32x fewer tokens** with CLI vs MCP for equivalent operations, and the `mcp2cli` project reports 96-99% savings on compatible tools.

**2. Docker dependency**
The gateway requires a running Docker daemon even for trivial operations like listing Elasticsearch indices (a single `curl` call). This adds startup latency (~10-15s for gateway initialization) and a hard dependency on Docker availability.

**3. Redundancy**
Of the 13 enabled MCP tools, several duplicate existing native capabilities:
- `mcp_search` duplicates native `web_search` (ADR-0034, SearXNG)
- `mcp_docker` wraps the `docker` CLI — the CLI the agent already knows
- `mcp_fetch_content` is a basic HTTP fetch, replaceable by `curl`

**4. Naming pollution**
The `mcp_` prefix on all tools created entity extraction noise, requiring explicit exclusion in ADR-0024. CLI tools carry no such prefix.

**5. Failure opacity**
A crashed gateway silently disables all MCP tools. Graceful degradation exists, but the failure mode is opaque to the agent and user.

### The CLI Army Pattern

The "CLI Army" pattern — building or adopting small, focused CLI tools with `--json` output and documenting them for the agent — was popularised by Peter Steinberger's [OpenClaw](https://github.com/openclaw/openclaw) project (190k+ GitHub stars) and analysed in Phil Rentier's widely-cited article *"Why CLIs Beat MCP for AI Agents"* (Feb 2026).

The pattern's core thesis: **CLI binary + skill doc = autonomous capability.** The agent reads a short markdown description of a CLI tool and invokes it directly — no protocol server, no schema discovery, no Docker subprocess.

### Industry Standardization Status (April 2026)

The CLI-first pattern has crossed the standardization threshold:

| Standard | Status | Adoption | Relevance |
|----------|--------|----------|-----------|
| **AGENTS.md** | Linux Foundation stewardship | 60,000+ repos, 20+ tools (Codex, Jules, Cursor, Copilot, Devin, etc.) | Project-level agent instructions; tool-agnostic |
| **SKILL.md / AgentSkills** | OpenClaw spec; ClawHub registry | 13,700+ skills on ClawHub | Per-tool documentation format; portable across agents |
| **CLAUDE.md** | Anthropic convention | Claude Code ecosystem | Our current equivalent; structurally similar to SKILL.md |
| **Claude Code Skills** | Superpowers ecosystem | Our project uses this | Markdown + frontmatter; same philosophy as SKILL.md |

**Key finding on portability**: SKILL.md skills are portable at the *documentation layer*, not the *execution layer*. A skill is a markdown file that teaches an agent how to use a CLI binary. The binary itself must be installed separately. You can browse [ClawHub](https://clawhub.com) for tool ideas and documentation patterns, but you cannot `clawhub install X` into a non-OpenClaw agent and have it work as a plugin. The portability is in the *pattern*, not the *runtime*.

Our existing Claude Code skill system (superpowers) is structurally identical: markdown files with YAML frontmatter that teach the agent workflows. We can adopt SKILL.md conventions for documenting CLI tools without changing our runtime.

---

## Research: Current MCP Tool Inventory

### Enabled MCP Tools (13)

| Tool | Purpose | CLI Alternative | Migration Effort |
|------|---------|----------------|-----------------|
| `mcp_esql` | Elasticsearch ES\|QL queries | `curl -X POST localhost:9200/_query` | Very low (~15 lines) |
| `mcp_get_mappings` | ES index field mappings | `curl localhost:9200/{index}/_mapping` | Very low (~10 lines) |
| `mcp_get_shards` | ES shard information | `curl localhost:9200/_cat/shards` | Very low (~10 lines) |
| `mcp_list_indices` | List ES indices | `curl localhost:9200/_cat/indices` | Very low (~10 lines) |
| `mcp_docker` | Docker CLI | `docker` (already a CLI) | Zero |
| `mcp_fetch_content` | HTTP content fetch | `curl` + `html2text` | Very low |
| `mcp_search` | DuckDuckGo search | Redundant with native `web_search` (ADR-0034) | Zero — remove |
| `mcp_perplexity_ask` | Perplexity Sonar API | `curl` to Perplexity REST API | Low (~25 lines) |
| `mcp_perplexity_reason` | Perplexity reasoning | `curl` to Perplexity REST API | Low (~25 lines) |
| `mcp_perplexity_research` | Perplexity deep research | `curl` to Perplexity REST API | Low (~25 lines) |
| `mcp_get-library-docs` | Context7 library docs | `curl` to Context7 API | Low (~20 lines) |
| `mcp_resolve-library-id` | Context7 ID resolution | `curl` to Context7 API | Low (~15 lines) |
| `mcp_sequentialthinking` | Structured chain-of-thought | Not a real tool — prompt technique | N/A — remove |

### Disabled MCP Tools (28)

- **22 browser automation tools** (`mcp_browser_*`): All disabled (`allowed_in_modes: []`). Awaiting LLM integration evaluation. Browser automation is a legitimate MCP use case.
- **1 code-mode tool**: Disabled. JavaScript multi-tool combiner.
- **5 MCP management tools** (`mcp_mcp-*`): Disabled. Meta-tools for managing MCP servers.

### Native Tools (6, for reference)

| Tool | Pattern | Notes |
|------|---------|-------|
| `read_file` | Native in-process | Core |
| `list_directory` | Native in-process | Core |
| `system_metrics_snapshot` | Native in-process | Core |
| `search_memory` | Native in-process | ADR-0026. Needs Python service access |
| `self_telemetry_query` | Native in-process | Needs Python service access |
| `web_search` | Native in-process | ADR-0034. SearXNG. Already replaced `mcp_search` |

### Schema Token Cost Estimate

With 13 enabled MCP tools at ~300-500 tokens schema each:
- **Current cost**: ~4,000-6,500 tokens per request for MCP schema injection
- **After migration**: 0 tokens (native tools use the same lean ToolDefinition as existing tools; no MCP schema discovery overhead)
- **Savings**: ~4,000-6,500 tokens per request returned to reasoning budget

---

## Decision

**Option B: Hybrid — CLI-first with MCP reserved for complex integrations.**

Establish a three-tier tool integration model:

```
+-----------------------------------------------------------+
|  Tier 1: Native In-Process Python Tools                   |
|  -----------------------------------------                |
|  For tools requiring direct Python service access.        |
|  Zero overhead. No subprocess, no Docker.                 |
|                                                           |
|  Examples: search_memory, self_telemetry, web_search      |
|  Pattern: ADR-0026                                        |
+-----------------------------------------------------------+
|  Tier 2: CLI Tools (NEW DEFAULT)                          |
|  -----------------------------------------                |
|  For external integrations with existing CLIs or          |
|  simple API wrappers. Near-zero context cost.             |
|  Battle-tested. Composable via pipes.                     |
|                                                           |
|  Examples: curl (ES, Perplexity, Context7), docker, gh    |
|  Pattern: ADR-0027 + SKILL.md docs                        |
+-----------------------------------------------------------+
|  Tier 3: MCP Tools (RESERVED)                             |
|  -----------------------------------------                |
|  For complex integrations requiring protocol-level        |
|  features: browser automation, multi-step OAuth,          |
|  tools with no CLI equivalent.                            |
|                                                           |
|  Examples: browser_* (future), proprietary SaaS           |
|  Pattern: Existing MCP Gateway                            |
+-----------------------------------------------------------+
```

**Tool routing rule**: New tool integrations MUST justify why Tier 1 or Tier 2 is insufficient before using Tier 3 (MCP).

### What Changes

1. **Disable MCP gateway by default** (already the case: `mcp_gateway_enabled=False`)
2. **Convert the 4 Elasticsearch tools** to a native Python tool using `httpx` (in-process, Tier 1 — since ES is local infrastructure, same as Neo4j in `search_memory`)
3. **Convert the 3 Perplexity tools** to a native Python tool or CLI wrapper
4. **Remove `mcp_search`** — fully redundant with native `web_search` (ADR-0034)
5. **Remove `mcp_sequentialthinking`** — prompt technique, not a tool
6. **Remove `mcp_docker`** — wraps an existing CLI; agent can call `docker` directly if needed
7. **Keep `mcp_fetch_content`** as a native Python tool (simple `httpx` + `html2text`)
8. **Keep Context7 tools** (`get-library-docs`, `resolve-library-id`) as native Python or CLI
9. **Preserve MCP Gateway code** — do not delete. Reserve for browser automation (Tier 3) when evaluated
10. **Adopt SKILL.md convention** for documenting any CLI tools added in the future

### What Does NOT Change

- Native in-process tools (Tier 1) remain unchanged
- MCP infrastructure code (`src/personal_agent/mcp/`, 826 lines) preserved
- Governance framework (`config/governance/tools.yaml`) preserved and extended
- Graceful degradation behaviour preserved
- The `LinearClient` MCP usage for Captain's Log (ADR-0040) is unaffected — it uses `call_tool()` directly, not the schema-injecting discovery path

---

## Migration Plan

### Phase 1: Elasticsearch to Native Python Tool (Highest Value)

**Why first**: ES tools are the most frequently used MCP tools and the easiest to replace. ES is local infrastructure (localhost:9200), same pattern as Neo4j in ADR-0026.

Create `src/personal_agent/tools/elasticsearch.py` with a single `query_elasticsearch` native tool that supports:
- ES|QL queries (replaces `mcp_esql`)
- Index listing (replaces `mcp_list_indices`)
- Mapping inspection (replaces `mcp_get_mappings`)
- Shard info (replaces `mcp_get_shards`)

Implementation: `httpx` async calls to `settings.elasticsearch_url`. ~80-120 lines.

### Phase 2: Perplexity to Native Python Tool

Create `src/personal_agent/tools/perplexity.py` with `perplexity_query` tool supporting ask/reason/research modes via the Perplexity REST API. ~60-80 lines.

### Phase 3: Cleanup Redundant Tools

- Remove `mcp_search`, `mcp_docker`, `mcp_sequentialthinking` from governance config
- Convert `mcp_fetch_content` to native `fetch_url` tool (~40 lines with `httpx` + `readability-lxml`)
- Convert Context7 tools to native or CLI

### Phase 4: Documentation and SKILL.md Adoption

- Document the three-tier model in `docs/reference/TOOL_INTEGRATION_GUIDE.md`
- Create SKILL.md files for any CLI tools (currently none needed — all migrations target Tier 1 native)
- Update CLAUDE.md with the tool routing rule

---

## On Reusing OpenClaw / Third-Party Skills

**Can we use CLI-tool skills from OpenClaw or other ecosystems?**

Yes, with caveats:

| Aspect | Status |
|--------|--------|
| **Format portability** | High. SKILL.md is markdown + YAML frontmatter — structurally identical to our Claude Code skills. Any SKILL.md can be read as documentation. |
| **Registry availability** | 13,700+ skills on [ClawHub](https://clawhub.com). Browsable and searchable. |
| **Runtime portability** | Low. Skills are *documentation for CLI binaries*, not the binaries themselves. The binary must be installed and accessible on the host. |
| **Practical reuse path** | Browse ClawHub for tool ideas, install the underlying CLI binary (e.g., `brew install goplaces`), write a SKILL.md or add to CLAUDE.md, agent invokes it. |
| **Our equivalent** | Claude Code's superpowers skill system. Same philosophy, same format. No need to switch — but we can learn from ClawHub's documentation patterns. |

**Recommendation**: Do not adopt OpenClaw's runtime or registry. Instead, adopt the SKILL.md *documentation convention* for any Tier 2 CLI tools we create. This makes our tools discoverable by any AGENTS.md-compatible agent in the future, should we ever want cross-agent compatibility.

---

## Consequences

### Positive

- **Context window savings**: ~4,000-6,500 tokens per request returned to the reasoning budget
- **Reduced dependencies**: No Docker daemon required for core tool operations
- **Faster startup**: Eliminates 10-15s MCP gateway initialization for the common case
- **Simpler debugging**: `curl` calls are testable in 2 seconds from a terminal
- **Three-tier model**: Clear decision framework for future tool integrations
- **Industry alignment**: CLI-first is the consensus pattern for local agent tooling (2026)
- **Future portability**: SKILL.md convention is compatible with AGENTS.md (Linux Foundation standard, 60K+ repos)

### Negative

- **Migration effort**: ~3-4 focused sessions to convert ES, Perplexity, and remaining tools to native Python
- **Two patterns during transition**: MCP and native tools coexist until migration completes (mitigated by MCP being disabled by default)
- **Browser automation deferred**: MCP remains the right answer for browser tools, but they are currently disabled anyway
- **LinearClient decoupled** (resolved FRE-243): `LinearClient` now calls the Linear GraphQL API directly via httpx; `MCPGatewayAdapter` is no longer a dependency of the Captain's Log pipeline

### Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Native ES tool has bugs not present in MCP version | Medium | Integration tests against real ES; same queries, same assertions |
| Perplexity API changes break native tool | Low | API is stable; same risk existed with MCP wrapper |
| Future tool needs MCP features we have deprecated knowledge of | Low | MCP code preserved; 826 lines intact; can re-enable |
| SKILL.md format evolves away from our convention | Low | Format is simple markdown; easy to adapt |

---

## Acceptance Criteria

- [x] Three-tier tool model documented in ADR and CLAUDE.md
- [x] Phase 1: Native `query_elasticsearch` tool — `src/personal_agent/tools/elasticsearch.py`; 4 actions (esql, indices, mappings, shards); 12 unit tests
- [x] Phase 2: Native `perplexity_query` tool — `src/personal_agent/tools/perplexity.py`; ask/reason/research modes; API key via `settings.perplexity_api_key`
- [x] Phase 3: `mcp_search`, `mcp_docker`, `mcp_sequentialthinking` disabled in governance config; `fetch_url` + `get_library_docs` native tools added
- [x] Phase 4: Tool integration guide at `docs/reference/TOOL_INTEGRATION_GUIDE.md`; `docs/skills/SKILL_TEMPLATE.md` created; CLAUDE.md updated with tool routing rule
- [x] MCP gateway code preserved but disabled by default (no code deletion)
- [x] No regression in agent tool capabilities (same operations available via new tools)
- [x] `run_sysdiag` subprocess tool added (FRE-188): 17 allow-listed commands, no shell=True, 32 KB output cap, 14 unit + 3 integration tests

---

## References

- Phil Rentier: [*"Why CLIs Beat MCP for AI Agents"*](https://rentierdigital.xyz/blog/why-clis-beat-mcp-for-ai-agents-and-how-to-build-your-own-cli-army) — CLI Army pattern analysis (Feb 2026)
- [OpenClaw](https://github.com/openclaw/openclaw) — Peter Steinberger's CLI-first agent (190K+ stars)
- [ClawHub](https://clawhub.com) — OpenClaw skill registry (13,700+ skills)
- [AGENTS.md](https://agents.md/) — Linux Foundation open standard for agent instructions (60K+ repos)
- [StackOne: MCP vs CLI — When Each One Wins](https://www.stackone.com/blog/mcp-vs-cli-for-ai-agents/) — Balanced hybrid analysis with benchmarks
- [OpenClaw Skills Documentation](https://docs.openclaw.ai/tools/skills) — SKILL.md format reference
- `src/personal_agent/mcp/gateway.py` — Current MCP implementation (preserved)
- `docs/architecture_decisions/ADR-0026-search-memory-native-tool.md` — Tier 1 native tool pattern
- `docs/architecture_decisions/ADR-0027-memory-cli-interface.md` — CLI-first pattern for developer tools
- `docs/architecture_decisions/ADR-0034-*.md` — Native `web_search` (already replaced `mcp_search`)
- `config/governance/tools.yaml` — Tool governance configuration (41 MCP tools inventoried)
