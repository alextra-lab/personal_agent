# ADR-0050: Remote Agent Harness Integration

**Status**: Accepted
**Date**: 2026-04-13
**Deciders**: Project owner
**Depends on**: ADR-0033 (Multi-Provider Model Taxonomy — delegation types), ADR-0043 (Three-Layer Separation), ADR-0045 (Infrastructure — Cloud Knowledge Layer)
**Related**: ADR-0028 (CLI-First Tool Migration), ADR-0011 (MCP Gateway Integration), ADR-0044 (Provider Abstraction & Dual-Harness), ADR-0049 (Application Modularity)

---

## Context

### Seshat delegates coding tasks — but integration is shallow

ADR-0033 established that coding is delegation, not a local model role. The `CODING` enum member was removed in favor of delegation targets — external agents with their own models, tools, and execution environments. Slice 2 defined `DelegationPackage` and `DelegationOutcome` types for structured delegation.

But the current delegation flow is one-directional and fire-and-forget:

```
Seshat  ──── DelegationPackage ────►  Claude Code
        ◄─── DelegationOutcome ────  (CLI subprocess)
```

What's missing:

1. **Knowledge access for external agents.** When Claude Code receives a delegation package for "refactor the memory module," it can't query Seshat's knowledge graph for entity relationships, past decisions, or architectural context. It works blind, relying only on the context stuffed into the delegation package. This is like giving a contractor a job description but no access to the building plans.

2. **Bidirectional interaction.** Currently, Seshat composes a delegation, waits for a result, and processes the outcome. There's no way for the external agent to ask Seshat questions mid-task ("I see two memory backends — which one is primary?"), request additional context, or report intermediate progress.

3. **Multiple external agents.** The delegation infrastructure assumes Claude Code. But Codex (OpenAI), Cursor, Windsurf, and other agent environments are emerging. Each has different capabilities, interfaces, and auth models. The integration point needs to be generic, not Claude-Code-specific.

4. **No reverse direction.** Claude Code sessions currently can't initiate queries to Seshat. A developer using Claude Code who asks "what does Seshat's knowledge graph say about entity X?" has no way to reach Seshat's memory without leaving Claude Code and using the CLI.

### The integration challenge

External agent harnesses are not homogeneous:

| Agent | Interface | Model | Execution env | State |
|-------|-----------|-------|---------------|-------|
| Claude Code | CLI subprocess, stdin/stdout | Claude (Anthropic) | Local filesystem, tools | Stateless per invocation |
| Codex | REST API | GPT-4o (OpenAI) | Cloud sandbox | Stateless |
| Cursor | IDE extension API | Various | IDE + local filesystem | IDE session |
| Windsurf | IDE extension API | Various | IDE + local filesystem | IDE session |

A generic integration layer can't assume CLI access, specific API shapes, or shared filesystem. It needs a **protocol-level** integration point that any agent can use.

### Two integration paths, not one

ADR-0028 established a three-tier tool integration model. The same tiers apply to how external agents integrate with Seshat:

**Tier 2 — SKILL.md + Seshat API**: For skill-capable harnesses (Claude Code, Codex, Cursor with OpenClaw support), a SKILL.md file teaches the agent how to query Seshat's Knowledge Layer API via CLI commands (`curl`, `httpx`) or the existing `agent memory search` CLI. Zero schema overhead in the external agent's context — it reads the skill doc from disk only when it needs Seshat's knowledge.

**Tier 3 — MCP server**: For agents that support MCP natively, Seshat exposes an MCP server with structured tool definitions. This adds schema overhead in the external agent's context (the ADR-0028 tradeoff), but provides richer integration: structured tool discovery, typed responses, and MCP resource URIs.

Both paths hit the same backend — the Seshat API Gateway (ADR-0045). Both are subject to the same authentication, scoping, and audit logging. The difference is how the external agent discovers and invokes Seshat's capabilities.

**Why both**: We don't yet know which path produces better delegation outcomes. SKILL.md is leaner (zero schema tokens) but less structured. MCP is heavier but provides typed tool contracts. Building both against the same API lets us compare them empirically — which path leads to better knowledge retrieval during delegated tasks, fewer failed tool calls, and lower total token cost.

---

## Decision

### D1: SKILL.md integration path (Tier 2)

For skill-capable agent harnesses, provide SKILL.md files that teach the agent to interact with Seshat's Knowledge Layer API:

```
docs/skills/
  seshat-knowledge.md     # Search/read/write knowledge graph entities
  seshat-sessions.md      # Read conversation history and session context
  seshat-observations.md  # Query execution traces and performance data
  seshat-delegate.md      # Delegate tasks back to Seshat
```

Each skill doc follows the OpenClaw/SKILL.md format (ADR-0028 Tier 2) and maps to the same API endpoints as the MCP server tools:

| Skill | CLI invocation | Equivalent MCP tool |
|-------|---------------|-------------------|
| `seshat-knowledge.md` | `curl $SESHAT_API/knowledge/search?q=...` | `seshat_search_knowledge` |
| `seshat-knowledge.md` | `curl $SESHAT_API/knowledge/entities/{id}` | `seshat_get_entity` |
| `seshat-knowledge.md` | `curl -X POST $SESHAT_API/knowledge/entities` | `seshat_store_fact` |
| `seshat-sessions.md` | `curl $SESHAT_API/sessions/{id}/messages` | `seshat_get_session_context` |
| `seshat-observations.md` | `curl $SESHAT_API/observations/query` | `seshat_query_observations` |
| `seshat-delegate.md` | `curl -X POST $SESHAT_API/delegate` | `seshat_delegate` |

**Advantages over MCP**: Zero schema tokens in the external agent's context window. The agent reads the skill doc once, then invokes CLI commands. Portable across any harness that supports SKILL.md or similar documentation patterns.

**Authentication**: The skill doc includes instructions for passing the API token as a bearer header. The token is stored in the agent's environment (e.g., Claude Code's `.claude/settings.json` or shell env var), not in the skill doc itself.

### D2: Seshat as an MCP server for external agents (Tier 3)

Expose Seshat's Knowledge Layer as an **MCP server** that external agent harnesses can connect to:

```
┌─────────────────────────────────────────────────────────┐
│  External Agent (Claude Code / Codex / Cursor)          │
│                                                          │
│  MCP Client ──────────────────────────────┐              │
└───────────────────────────────────────────┼──────────────┘
                                            │ MCP Protocol
                                            │ (stdio or SSE)
┌───────────────────────────────────────────▼──────────────┐
│  Seshat MCP Server                                       │
│                                                          │
│  Tools:                                                  │
│    seshat_search_knowledge    — query entities/relations  │
│    seshat_get_entity          — retrieve entity details   │
│    seshat_store_fact          — write new knowledge       │
│    seshat_get_session_context — read conversation history │
│    seshat_query_observations  — read performance data     │
│    seshat_delegate            — delegate sub-task back    │
│                                                          │
│  Resources:                                              │
│    seshat://knowledge/entities/{id}                       │
│    seshat://sessions/{id}/messages                        │
│    seshat://observations/recent                           │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Knowledge Layer API (ADR-0045)                  │   │
│  │  Neo4j · PostgreSQL · Elasticsearch              │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

**MCP server tools**:

| Tool | Purpose | Access level |
|------|---------|-------------|
| `seshat_search_knowledge` | Semantic search over entities and relationships. Returns ranked results with freshness and confidence metadata (ADR-0042, ADR-0047). | Read |
| `seshat_get_entity` | Retrieve a specific entity with its relationships, access history, and confidence scores. | Read |
| `seshat_store_fact` | Write a new entity or relationship to the knowledge graph. Source tagged as `"external_agent"`. | Write |
| `seshat_get_session_context` | Read conversation history from a specific session. Useful for delegation context. | Read |
| `seshat_query_observations` | Query execution traces, cost data, and performance metrics. | Read |
| `seshat_delegate` | Delegate a sub-task back to Seshat (reverse delegation). The external agent asks Seshat to do something. | Execute |

**MCP resources** (read-only data exposed via MCP resource URIs):

| Resource URI | Description |
|-------------|-------------|
| `seshat://knowledge/entities/{id}` | Entity detail with relationships |
| `seshat://knowledge/search?q={query}` | Knowledge search results |
| `seshat://sessions/{id}/messages` | Session conversation history |
| `seshat://observations/recent` | Recent execution observations |

### D3: Scoped access with audit logging

Not all external agents should have the same access. Access is controlled by **API tokens** with explicit scope:

```yaml
# config/mcp_server_access.yaml
tokens:
  - name: claude-code-local
    scopes:
      - knowledge:read
      - knowledge:write
      - sessions:read
      - observations:read
    rate_limit: 100/hour
    audit: true

  - name: codex-cloud
    scopes:
      - knowledge:read      # Read-only: Codex runs in a cloud sandbox
      - sessions:read
    rate_limit: 50/hour
    audit: true

  - name: cursor-dev
    scopes:
      - knowledge:read
      - knowledge:write
      - sessions:read
      - delegate            # Can delegate back to Seshat
    rate_limit: 100/hour
    audit: true
```

**Audit logging**: Every MCP server access is logged to Elasticsearch with:
- Timestamp
- Client identity (token name)
- Tool/resource accessed
- Arguments/query
- Result summary (not full payload)
- Trace ID (correlatable with Seshat's internal traces)

This creates a complete audit trail of what external agents read from and wrote to Seshat's knowledge.

### D4: Delegation from Seshat to external agents

Outbound delegation (Seshat → external agent) uses the existing `DelegationPackage`/`DelegationOutcome` types from ADR-0033/Slice 2, extended with MCP context:

```python
@dataclass(frozen=True)
class DelegationPackage:
    """Extended with MCP server connection info."""
    task_description: str
    context: DelegationContext
    constraints: DelegationConstraints
    # New: MCP server connection for knowledge access
    mcp_server: MCPServerConnection | None  # If set, the external agent can connect back
```

When Seshat delegates to Claude Code with an `mcp_server` field populated, the delegation wrapper configures Claude Code's MCP settings to include Seshat as a tool server. Claude Code can then query Seshat's knowledge mid-task.

**Flow**:

```
1. Seshat identifies a coding task needing delegation
2. Seshat composes DelegationPackage with:
   - Task description and constraints
   - Relevant context from Knowledge Layer
   - MCP server connection details (if bidirectional access needed)
3. Delegation executor launches the external agent:
   - Claude Code: CLI subprocess with --mcp-server flag
   - Codex: API call with tool configuration
   - Cursor: Extension API (future)
4. External agent executes task:
   - Reads delegation package
   - Optionally queries Seshat MCP server for additional context
   - Produces result
5. Seshat receives DelegationOutcome
6. Result stored in Knowledge Layer
7. Audit trail logged
```

### D5: Delegation from external agents to Seshat (reverse delegation)

The `seshat_delegate` MCP tool enables **reverse delegation**: an external agent asks Seshat to perform a task.

Use case: A developer is in Claude Code, working on a feature. They want to check what Seshat's knowledge graph says about a specific architectural pattern, then ask Seshat to create a Linear issue for follow-up work.

```
Claude Code user: "Ask Seshat to create a Linear issue for refactoring the memory query path"

Claude Code → seshat_delegate(
    task="Create a Linear issue: Refactor memory query path for freshness-weighted results",
    type="linear_issue",
    details={...}
)

Seshat MCP Server → processes delegation → creates Linear issue → returns result
```

Reverse delegation is scoped: only agents with the `delegate` scope in their access token can trigger it. Delegated tasks go through Seshat's normal governance pipeline (including approval gates if configured).

### D6: Agent-specific adapters

Each external agent needs a thin adapter that handles its specific integration mechanics:

```
src/personal_agent/
  delegation/
    protocols.py          # DelegationExecutorProtocol
    adapters/
      claude_code.py      # CLI subprocess, stdin/stdout, MCP config injection
      codex.py            # REST API, sandbox execution
      generic_mcp.py      # Any MCP-capable agent (fallback)
```

The adapter implements `DelegationExecutorProtocol`:

```python
class DelegationExecutorProtocol(Protocol):
    async def delegate(
        self,
        package: DelegationPackage,
        timeout: float,
        trace_ctx: TraceContext,
    ) -> DelegationOutcome: ...

    def available(self) -> bool:
        """Check if the external agent is reachable."""
        ...
```

This follows the modularity principle (ADR-0049): new agent integrations are new adapter implementations, not modifications to the orchestrator.

---

## Consequences

### Positive

- **External agents get Seshat's full knowledge**: Claude Code can query the knowledge graph, read past conversations, and understand architectural context — dramatically improving delegation quality.
- **Bidirectional interaction**: External agents aren't limited to the context stuffed into the delegation package. They can ask for more when they need it.
- **Two integration paths enable empirical comparison**: SKILL.md (Tier 2) and MCP server (Tier 3) both hit the same API. Comparing delegation quality, token cost, and tool-call success rates between them produces real data on which pattern works better — rather than choosing on theory alone.
- **SKILL.md path is immediately useful**: Writing skill docs is fast (no code, just documentation). Claude Code can start querying Seshat's knowledge graph as soon as the API exists and the skill doc is written.
- **MCP path covers non-skill agents**: Any MCP-capable agent works without needing SKILL.md support. Protocol-standard integration.
- **Audit trail**: Complete visibility into what external agents accessed and modified via either path. Essential for debugging delegation outcomes and understanding knowledge provenance.
- **Reverse delegation enables new workflows**: Developers using Claude Code or Cursor can leverage Seshat's capabilities (knowledge search, Linear integration, memory storage) from within their preferred tool.

### Negative

- **Two paths to maintain**: Both SKILL.md docs and the MCP server must be kept in sync with the underlying API. When the API changes, both integration surfaces need updates. Mitigation: both are thin wrappers over the same API — SKILL.md is documentation, MCP server is a thin adapter.
- **Security surface area**: Exposing the Knowledge Layer (via either path) creates an attack surface. Compromised tokens could leak knowledge or inject false facts. Mitigation: scoped tokens, rate limiting, audit logging, and the ability to revoke tokens instantly.
- **Complexity of bidirectional delegation**: Seshat delegates to Claude Code, which delegates back to Seshat — potential infinite loops. Mitigation: delegation depth limit (default: 2). Reverse delegations cannot trigger outbound delegations.
- **Agent-specific adapters need maintenance**: Each external agent's CLI/API interface can change. Claude Code flags, Codex API versions, Cursor extension APIs — these are external dependencies. Mitigation: adapters are thin and isolated. Breaking changes affect one adapter, not the system.

### Neutral

- **Existing MCP infrastructure reused**: Seshat already has an MCP gateway (`mcp/gateway.py`) and client (`mcp/client.py`). The MCP server is a new direction (Seshat as server, not just client) but uses the same protocol and SDK.
- **FRE-22 (Plugin/extension architecture) is related but broader**: This ADR focuses on agent-to-agent integration. FRE-22 encompasses a broader plugin model that may include non-agent extensions. They're complementary, not competing.
- **Delegation adapter for Claude Code already partially exists**: The delegation executor from Slice 2 launched Claude Code as a subprocess. This ADR formalizes and extends that pattern with MCP context injection, SKILL.md docs, and scoped access.
- **Convergence is expected**: Over time, usage data will show whether SKILL.md or MCP (or both) is the better integration path. The architecture supports deprecating one without affecting the other — they share the API layer, not each other.
