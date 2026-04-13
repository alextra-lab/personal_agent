# ADR-0049: Application Modularity

**Status**: Accepted
**Date**: 2026-04-13
**Deciders**: Project owner
**Related**: ADR-0043 (Three-Layer Separation), ADR-0044 (Provider Abstraction & Dual-Harness), ADR-0028 (CLI-First Tool Migration), ADR-0046 (Agent-to-UI Protocol Stack)
**Enables**: Self-hosting by others with custom module choices

---

## Context

### The system already has module boundaries — this ADR makes them a principle

The Personal Agent codebase has clear directory-level modules (`memory/`, `orchestrator/`, `llm_client/`, `telemetry/`, etc.) with reasonably clean separation. But modularity is a _consequence_ of organic growth, not a _design principle_. The difference matters:

1. **No explicit interface contracts between modules.** `MemoryProtocol` (Slice 1) is the one exception — it defines an abstract interface that both the real Neo4j implementation and test mocks satisfy. Other module boundaries are implicit: the orchestrator calls `llm_client.factory.get_llm_client()`, the gateway calls `memory_service.query()`, the service calls everything. There's no "what would I need to implement to swap out this module?" answer for most components.

2. **Coupling through imports.** The `service/app.py` file imports from nearly every module. The `orchestrator/executor.py` directly instantiates `LLMClient`, `ToolExecutor`, `MemoryService`. This makes it impossible to run the orchestrator with a different memory backend without modifying the orchestrator itself.

3. **Configuration is centralized but not modular.** `config/settings.py` is a monolithic Pydantic model. Adding a new module means adding fields to the root settings object. There's no way for a module to declare its own configuration schema independently.

4. **Self-hosting requires understanding the whole system.** If someone wants to run Seshat with a different graph database, a different search engine, or a different LLM provider, they'd need to read the codebase to find all the touchpoints. There's no "swap this module, implement this interface, you're done" guide.

### Why modularity matters for this project

This is a personal agent designed for eventual self-hosting by others. Different users will have different constraints:

- **Different LLM providers**: Some users will have local GPU hardware; others will use only cloud APIs. Some will prefer Anthropic; others Google or OpenAI.
- **Different storage backends**: Not everyone will want Neo4j for the knowledge graph. Some might prefer a simpler solution or a different graph database.
- **Different search engines**: Elasticsearch is powerful but heavyweight. A self-hoster might prefer Typesense, Meilisearch, or even SQLite FTS for a lighter footprint.
- **Different protocol preferences**: AG-UI (ADR-0046) is the current choice, but protocols evolve. The UI protocol should be swappable without rewiring the execution layer.

Modularity is also an internal development benefit: clear boundaries make testing easier, reduce the blast radius of changes, and enable parallel development across layers.

---

## Decision

### D1: Every major component is a replaceable module behind a Protocol

Each module in the three-layer architecture (ADR-0043) defines a **Protocol** (Python structural typing) that describes its interface. Implementations are pluggable.

**Knowledge Layer modules**:

| Module | Protocol | Default implementation | Alternatives |
|--------|----------|----------------------|-------------|
| Knowledge Graph | `KnowledgeGraphProtocol` | Neo4j (`memory/neo4j_store.py`) | FalkorDB, Memgraph, in-memory graph |
| Session Store | `SessionStoreProtocol` | PostgreSQL (`service/session.py`) | SQLite, DuckDB |
| Search Index | `SearchIndexProtocol` | Elasticsearch (`telemetry/es_client.py`) | Typesense, Meilisearch, SQLite FTS |
| Event Bus | `EventBusProtocol` | Redis Streams (`events/bus.py`) | In-process (for single-node), NATS |

**Execution Layer modules**:

| Module | Protocol | Default implementation | Alternatives |
|--------|----------|----------------------|-------------|
| LLM Client | `LLMClient` (exists) | `LocalLLMClient` + `LiteLLMClient` | Direct API clients, ollama |
| Tool Executor | `ToolExecutorProtocol` | Native tools + CLI (ADR-0028) | MCP-only, custom |
| Context Assembler | `ContextAssemblerProtocol` | Gateway context stage | Custom retrieval pipelines |

**Observation Layer modules**:

| Module | Protocol | Default implementation | Alternatives |
|--------|----------|----------------------|-------------|
| Trace Sink | `TraceSinkProtocol` | Elasticsearch via structlog | OpenTelemetry, Jaeger, stdout |
| Metrics Collector | `MetricsCollectorProtocol` | Captain's Log + ES | Prometheus, InfluxDB |

**Protocol Layer modules**:

| Module | Protocol | Default implementation | Alternatives |
|--------|----------|----------------------|-------------|
| UI Transport | `UITransportProtocol` | AG-UI SSE (ADR-0046) | WebSocket, custom SSE, Vercel AI SDK |
| Rich Visualization | `VisualizationProtocol` | MCP Apps (ADR-0046) | Custom rendering, A2UI |

### D2: Module boundaries aligned with the three-layer architecture

The directory structure reflects layer ownership:

```
src/personal_agent/
  knowledge/            # Knowledge Layer
    protocols.py        # KnowledgeGraphProtocol, SessionStoreProtocol, SearchIndexProtocol
    neo4j/              # KnowledgeGraphProtocol implementation
    postgres/           # SessionStoreProtocol implementation
    elasticsearch/      # SearchIndexProtocol implementation

  execution/            # Execution Layer
    protocols.py        # ToolExecutorProtocol, ContextAssemblerProtocol
    gateway/            # Pre-LLM gateway pipeline
    orchestrator/       # Agent orchestration
    llm_client/         # LLM dispatch (LLMClient protocol already exists)
    tools/              # Tool implementations

  observation/          # Observation Layer
    protocols.py        # TraceSinkProtocol, MetricsCollectorProtocol
    telemetry/          # Structured logging, ES indexing
    captains_log/       # Self-improvement capture
    insights/           # Pattern analysis

  transport/            # Protocol/Transport Layer
    protocols.py        # UITransportProtocol, VisualizationProtocol
    agui/               # AG-UI implementation
    mcp_apps/           # MCP Apps visualization

  infrastructure/       # Cross-cutting
    events/             # EventBusProtocol + Redis implementation
    config/             # Configuration
    security/           # Auth, rate limiting
```

**Important caveat**: This is the _target_ structure, not an immediate reorganization mandate. The current flat module layout works. Migration to this structure should happen incrementally as modules are touched, not as a big-bang refactor. The protocols can be defined first, with current code satisfying them in-place.

### D3: Protocol-based dependency injection

Modules receive their dependencies as protocol instances, not concrete classes:

```python
# Good: Depends on protocol, not implementation
class Orchestrator:
    def __init__(
        self,
        llm_client: LLMClient,
        knowledge: KnowledgeGraphProtocol,
        tools: ToolExecutorProtocol,
        trace_sink: TraceSinkProtocol,
    ) -> None:
        self._llm = llm_client
        self._knowledge = knowledge
        self._tools = tools
        self._trace = trace_sink

# Bad: Depends on concrete implementation
class Orchestrator:
    def __init__(self) -> None:
        self._llm = get_llm_client("primary")  # Hard-coded factory
        self._knowledge = MemoryService(...)     # Hard-coded class
        self._tools = ToolExecutor(...)          # Hard-coded class
```

A **composition root** (in `service/app.py` or a dedicated `bootstrap.py`) wires concrete implementations together at startup:

```python
def bootstrap(profile: ExecutionProfile) -> Orchestrator:
    """Wire up concrete implementations based on profile config."""
    knowledge = Neo4jKnowledgeGraph(settings.neo4j)
    sessions = PostgresSessionStore(settings.postgres)
    search = ElasticsearchIndex(settings.elasticsearch)
    events = RedisEventBus(settings.redis)
    trace = ElasticsearchTraceSink(search)
    llm = get_llm_client(profile)
    tools = build_tool_executor(profile)

    return Orchestrator(
        llm_client=llm,
        knowledge=knowledge,
        tools=tools,
        trace_sink=trace,
    )
```

This pattern already partially exists (the `LLMClient` protocol in `factory.py`, `MemoryProtocol` in Slice 1). This ADR extends it to all major module boundaries.

### D4: Module-scoped configuration

Each module declares its own configuration schema as a nested Pydantic model:

```python
# In knowledge/neo4j/config.py
class Neo4jConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    uri: str = "bolt://localhost:7687"
    username: str = "neo4j"
    password: SecretStr
    database: str = "neo4j"

# In the root settings
class Settings(BaseModel):
    neo4j: Neo4jConfig
    postgres: PostgresConfig
    elasticsearch: ElasticsearchConfig
    # ...
```

This is already close to how `config/settings.py` works (it uses nested models for some components). The extension is to ensure every module owns its config schema, and the root settings object composes them rather than defining them.

### D5: Self-hosting module selection

For eventual self-hosters, a configuration file declares which module implementations to use:

```yaml
# config/modules.yaml
modules:
  knowledge_graph: neo4j          # or: falkordb, memgraph, memory
  session_store: postgres         # or: sqlite
  search_index: elasticsearch     # or: typesense, meilisearch
  event_bus: redis                # or: in_process
  trace_sink: elasticsearch       # or: opentelemetry, stdout
  ui_transport: agui              # or: websocket, vercel_ai
```

The bootstrap function reads this file and wires up the corresponding implementations. Missing implementations fail fast at startup with a clear error ("Module 'falkordb' for knowledge_graph is not installed").

This is a **future** feature — the immediate deliverable is the protocol definitions and protocol-based DI. The module selection file comes when there are actually multiple implementations to choose from.

---

## Consequences

### Positive

- **Swappable components**: Each major component can be replaced by implementing a protocol. Want to try Typesense instead of Elasticsearch? Implement `SearchIndexProtocol`, update the bootstrap, done.
- **Testability**: Every module can be tested with mock implementations of its dependencies. The `MemoryProtocol` pattern (already proven in Slice 1) scales to all modules.
- **Parallel development**: Clear protocols mean two developers (or two Claude Code sessions) can work on different modules without stepping on each other, as long as the protocol doesn't change.
- **Self-hosting flexibility**: Users can choose lighter-weight alternatives (SQLite instead of PostgreSQL, Typesense instead of Elasticsearch) for their self-hosted instance.
- **Reduced coupling**: Explicit dependency injection prevents modules from reaching into each other's internals. Changes to Neo4j query patterns don't affect the orchestrator.

### Negative

- **Protocol design is hard**: Defining the right protocol interface — not too broad (leaky abstraction), not too narrow (requires escape hatches) — takes careful thought. Premature protocol design is worse than no protocol.
- **Indirection cost**: Protocol-based DI adds a layer of indirection. Debugging "which implementation is actually running?" requires checking the bootstrap, not just the calling code.
- **Only one implementation exists for most modules**: For now, there's one `KnowledgeGraphProtocol` implementation (Neo4j), one `SearchIndexProtocol` (Elasticsearch), etc. Protocols without alternatives are speculative abstraction. Mitigation: define protocols only when there's a concrete need (testing, planned alternatives, or identified coupling pain), not prophylactically.
- **Migration effort**: Moving from direct imports to protocol-based DI across the codebase is a significant refactor. Should be done incrementally, not all at once.

### Neutral

- **`MemoryProtocol` is the model**: The existing `MemoryProtocol` from Slice 1 is exactly what this ADR prescribes for all modules. It's proven, tested, and understood. New protocols follow the same pattern.
- **Directory reorganization is optional**: The target directory structure (D2) is a guideline, not a mandate. Protocols can be defined and consumed without physically moving files. Reorganization follows naturally when modules are refactored.
