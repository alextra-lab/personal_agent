# ADR-0028: External Tool Integration — MCP Gateway vs CLI Army

**Status**: Draft — Research Required  
**Date**: 2026-03-07  
**Deciders**: TBD after research  

---

## Context

The agent currently integrates external tools (Elasticsearch, web search, GitHub, etc.) via Docker's MCP Gateway (`src/personal_agent/mcp/gateway.py`). This gateway speaks the Model Context Protocol (MCP) and proxies all tool calls through a running Docker container.

Concerns raised in project discussion:

1. **Context window consumption**: MCP tool schema discovery dumps the full schema of every registered server into the LLM's context window.
2. **Startup latency**: The gateway requires Docker and a subprocess handshake at agent startup (`initialize()`).
3. **Fragility**: A crashed gateway silently disables all MCP tools (graceful degradation exists, but the failure mode is opaque).
4. **Naming pollution**: The `mcp_` prefix on all tools was added as entity extraction noise and had to be explicitly excluded in ADR-0024.
5. **Dependency**: A running Docker daemon is required even for lightweight tool calls (e.g. a simple web search).

The "CLI Army" pattern — building small, focused CLI binaries with `--json` output and documenting them for the agent — was discussed as a potential alternative. This approach was publicly validated by Peter Steinberger's OpenClaw project and endorsed by OpenAI. Phil Rentier's article *"Why CLIs Beat MCP for AI Agents"* provides a detailed analysis.

**This ADR is a placeholder. A decision cannot be made without completing the research and analysis described below.**

---

## Research Required Before Deciding

This is the work to be done (see linked Linear issue):

### 1. Inventory current MCP tool usage
- Which MCP servers are currently enabled in `settings.mcp_gateway_enabled_servers`?
- Which MCP tools are actively used in production traces (last 30 days)?
- What is the actual schema size (tokens) injected per tool discovery cycle?
- How many MCP tool calls fail in a typical session, and what is the failure mode?

### 2. CLI candidates for high-value MCP tools
For each actively used MCP tool, assess:
- Does a standalone CLI already exist? (e.g. `gh` for GitHub, `supabase` for Supabase)
- Could a thin wrapper script be written in < 50 lines that provides `--json` output?
- What is the approximate token cost of the current MCP schema vs a one-page SKILL.md doc?

### 3. Benchmark: MCP vs CLI for the same task
- Measure end-to-end latency for an Elasticsearch query via MCP vs a direct `curl` / `es_cli` command
- Measure context window tokens consumed for schema discovery (MCP) vs SKILL.md instruction (CLI)
- Measure agent reliability: does the LLM call the tool correctly more often with a schema or with a doc?

### 4. Evaluate hybrid approach
Could the project run:
- MCP for tools where no CLI exists and the schema is small (< 500 tokens)
- CLI binaries for high-frequency, high-schema tools
- Native in-process Python tools for tools requiring direct Python service access (e.g. `search_memory`, ADR-0026)

### 5. Migration cost
- What would it cost to migrate the top 3 MCP tools to CLIs?
- What Cursor/agent documentation changes are needed (AGENTS.md, SKILL.md files)?
- Is there a risk of capability regression during a migration period?

---

## Pre-Decision Options (for reference after research)

**Option A: Full CLI migration**
Remove the MCP gateway entirely. Replace all MCP tools with CLI binaries documented in AGENTS.md and individual SKILL.md files. In-process Python tools (like `search_memory`) remain.

*Pros*: Zero Docker dependency, no schema bloat, full composability via pipes.  
*Cons*: Migration effort; some tools may not have CLI equivalents; loses MCP's standardised error handling.

**Option B: Hybrid (CLI for heavy tools, MCP for the rest)**
Keep the MCP gateway for low-frequency / low-schema tools. Build CLIs for the top 5 most-used tools. Disable MCP schema injection for tools documented as CLIs.

*Pros*: Incremental migration, low risk.  
*Cons*: Two integration patterns to maintain; developer cognitive overhead.

**Option C: Keep MCP, fix the pain points**
Address the known issues without removing MCP:
- Reduce schema injection by listing only enabled tools (already partially done via `mcp_gateway_enabled_servers`)
- Add a SKILL.md-style description for each MCP tool to reduce schema token cost
- Add health monitoring to surface gateway crashes clearly

*Pros*: Zero migration effort.  
*Cons*: Does not address fundamental context-window bloat or Docker dependency.

**Option D: Replace MCP gateway with a lightweight protocol**
Evaluate alternatives to Docker MCP: stdio-based local MCP servers, FastMCP, or a direct REST microservice architecture.

*Requires additional research.*

---

## Links and References

- Phil Rentier: *"Why CLIs Beat MCP for AI Agents"*, Feb 2026
- Peter Steinberger / OpenClaw: CLI-first agent tooling, ~10 custom CLIs
- `src/personal_agent/mcp/gateway.py` — current MCP implementation
- ADR-0026 — `search_memory` as first native in-process tool (establishes the pattern)
- ADR-0027 — Memory CLI (establishes CLI-first pattern for agent tools)
- `config/models.yaml` — `mcp_gateway_enabled_servers` configuration
- `docs/architecture_decisions/ADR-0024-session-graph-model.md` — `mcp_*` entity exclusion rationale

---

## Decision

**Deferred.** Complete the research tasks above, then update this ADR with a full context, decision, and consequence analysis.

---

## Notes

- Do not begin migration work until this ADR reaches `Accepted` status
- The `src/personal_agent/mcp/gateway.py` graceful-degradation behaviour should remain intact regardless of which option is chosen
- ADR-0026 (`search_memory`) and ADR-0027 (memory CLI) establish the patterns; use them as reference implementations when evaluating CLI migration
