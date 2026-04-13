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

### MCP is the natural integration protocol

MCP (Model Context Protocol) is already the standard for agent-tool integration. Seshat already runs MCP servers (ADR-0011) and consumes MCP tools. The key insight: **Seshat itself can be an MCP server** that external agents connect to.

Claude Code, Codex, and Cursor all support MCP servers natively. An MCP server that exposes Seshat's Knowledge Layer gives any MCP-capable agent read/write access to the knowledge graph, conversation history, and observation data — through a standard protocol they already understand.

---

## Decision

### D1: Seshat as an MCP server for external agents

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

### D2: Scoped access with audit logging

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

### D3: Delegation from Seshat to external agents

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

### D4: Delegation from external agents to Seshat (reverse delegation)

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

### D5: Agent-specific adapters

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
- **Any MCP-capable agent works**: The MCP server is protocol-standard. New agent harnesses that support MCP can connect to Seshat without custom integration work.
- **Audit trail**: Complete visibility into what external agents accessed and modified. Essential for debugging delegation outcomes and understanding knowledge provenance.
- **Reverse delegation enables new workflows**: Developers using Claude Code or Cursor can leverage Seshat's capabilities (knowledge search, Linear integration, memory storage) from within their preferred tool.

### Negative

- **Security surface area**: Exposing the Knowledge Layer via MCP server creates an attack surface. Compromised tokens could leak knowledge or inject false facts. Mitigation: scoped tokens, rate limiting, audit logging, and the ability to revoke tokens instantly.
- **MCP server maintenance**: The MCP server becomes a dependency for external agent workflows. Downtime affects delegation quality. Mitigation: the server is lightweight (thin wrapper over Knowledge Layer API), and delegations degrade gracefully (package context is still embedded even without MCP access).
- **Complexity of bidirectional delegation**: Seshat delegates to Claude Code, which delegates back to Seshat — potential infinite loops. Mitigation: delegation depth limit (default: 2). Reverse delegations cannot trigger outbound delegations.
- **Agent-specific adapters need maintenance**: Each external agent's CLI/API interface can change. Claude Code flags, Codex API versions, Cursor extension APIs — these are external dependencies. Mitigation: adapters are thin and isolated. Breaking changes affect one adapter, not the system.

### Neutral

- **Existing MCP infrastructure reused**: Seshat already has an MCP gateway (`mcp/gateway.py`) and client (`mcp/client.py`). The MCP server is a new direction (Seshat as server, not just client) but uses the same protocol and SDK.
- **FRE-22 (Plugin/extension architecture) is related but broader**: This ADR focuses on agent-to-agent integration via MCP. FRE-22 encompasses a broader plugin model that may include non-agent extensions. They're complementary, not competing.
- **Delegation adapter for Claude Code already partially exists**: The delegation executor from Slice 2 launched Claude Code as a subprocess. This ADR formalizes and extends that pattern with MCP context injection and scoped access.
