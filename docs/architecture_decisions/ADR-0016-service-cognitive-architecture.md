# ADR-0016: Service-Based Cognitive Architecture

**Status**: Accepted
**Date**: 2026-01-21
**Deciders**: System Architect
**Related**: ADR-0011 (MCP Gateway), ADR-0012 (Request-Scoped Metrics), ADR-0014 (Structured Metrics)

---

## 1. Context

### Current Architecture: CLI Script

The Personal Agent currently runs as a CLI script (`agent chat "query"`), executing the full stack per invocation:

```
User → CLI → Initialize Stack → Execute → Cleanup → Exit
```

This architecture was ideal for MVP development (simple, debuggable, fast iteration) but has reached its limits.

### Problems Identified

| Problem | Impact | Severity |
|---------|--------|----------|
| **MCP Gateway per-request subprocess** | 2-3s tool discovery overhead per request | High |
| **Request metrics discarded** | No historical trend analysis | Medium |
| **Sessions ephemeral** | Context lost between CLI calls | High |
| **Full initialization per request** | 1-2s startup cost | Medium |
| **No background processing** | Cannot run consolidation tasks | High |

### Root Cause Analysis

1. **MCP Gateway Lifecycle**: Gateway subprocess spawns/dies with each CLI invocation
   - 41 tools rediscovered every request
   - Async cleanup errors (MCP SDK stdio transport)
   - No connection pooling

2. **Metrics Loss**: `RequestMonitor` collects excellent data but discarded on process exit
   - Captain's Log gets one-time snapshot
   - No longitudinal analysis possible
   - Homeostasis control loops cannot learn

3. **Session State**: `SessionManager` is in-memory only
   - Multi-turn conversations work within single invocation
   - State lost between CLI calls
   - No conversation continuity across days/weeks

4. **Initialization Overhead**: Every request pays full startup cost
   - LLM client connection
   - Tool registry rebuild
   - Configuration loading
   - MCP gateway discovery

### Why Evolution Is Necessary

The MVP architecture was **correct for its phase**. Now we need:
- Persistent memory for world modeling
- Background consolidation for reflection
- Concurrent inference for second brain
- Session continuity for relationship building

**We've outgrown the architecture. Time to evolve.**

---

## 2. Decision

### 2.1 Architecture: Always-Running Service

Transform from CLI script to **always-running service** with thin CLI client:

```
┌─────────────────────── Service Process (Always Running) ───────────────────────┐
│                                                                                 │
│  ┌────────────────┐  ┌──────────────┐  ┌──────────────────┐                   │
│  │  Orchestrator  │  │  Brainstem   │  │  Second Brain    │                   │
│  │  (Fast path)   │  │  (Autonomic) │  │  (Consolidation) │                   │
│  │                │  │              │  │                  │                   │
│  │ - User requests│  │ - Monitor    │  │ - Memory build   │                   │
│  │ - LLM calls    │  │   resources  │  │ - Entity extract │                   │
│  │ - Tool exec    │  │ - Mode mgmt  │  │ - Graph update   │                   │
│  │ - Sessions     │  │ - Schedule   │  │ - Consolidation  │                   │
│  └───────┬────────┘  │   2nd brain  │  │ - Meta-insights  │                   │
│          │           └──────┬───────┘  └────────┬─────────┘                   │
│          │                  │                    │                             │
│  ┌───────▼──────────────────▼────────────────────▼────────────────────────┐   │
│  │              Shared Infrastructure                                      │   │
│  │  - MCP Gateway (singleton, persistent connection)                       │   │
│  │  - LLM Client Pool (mlx-openai-server backend)                         │   │
│  │  - Memory Service (Neo4j world model)                                   │   │
│  │  - Session Store (SQLite persistence)                                   │   │
│  │  - Captain's Log (capture + reflection separation)                      │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
         │                    │                      │                    │
         ▼                    ▼                      ▼                    ▼
  ┌──────────┐        ┌─────────────┐       ┌────────────┐      ┌─────────────┐
  │  Neo4j   │        │mlx-openai-  │       │ Claude API │      │  SQLite     │
  │  Server  │        │  server     │       │ (Cloud     │      │  (Sessions) │
  │  (Graph) │        │  (Local)    │       │  LLM)      │      │             │
  └──────────┘        └─────────────┘       └────────────┘      └─────────────┘
         ▲                    ▲                                        ▲
         │                    │                                        │
  ┌─────────────────────────────────────────────────────────────────────────┐
  │                         Client Layer                                    │
  │  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐                   │
  │  │ Thin CLI    │   │  Web UI     │   │   API       │                   │
  │  │  Client     │   │  (Future)   │   │  (Future)   │                   │
  │  │  (Primary)  │   │             │   │             │                   │
  │  └─────────────┘   └─────────────┘   └─────────────┘                   │
  └─────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Technology Stack

| Component | Technology | Rationale |
|-----------|------------|-----------|
| **API Framework** | FastAPI | Async-native, OpenAPI docs, lightweight |
| **LLM Backend** | mlx-openai-server | Apple Silicon optimized, OpenAI-compatible API, concurrent inference |
| **Cloud LLM** | Claude 4.5 Sonnet | Second brain deep reasoning (background only) |
| **Session/Metrics Store** | PostgreSQL | Robust, concurrent, JSONB support, time-series capable |
| **Log/Event Store** | Elasticsearch | Full-text search, Kibana visualization, aggregations |
| **Memory Graph** | Neo4j Community | Cypher queries, graph algorithms, visualization |
| **CLI Client** | httpx | Async HTTP client, minimal dependencies |
| **Container Orchestration** | Docker Compose | Unified infrastructure management |

### 2.2.1 Why Postgres over SQLite

| Aspect | SQLite | Postgres | Decision |
|--------|--------|----------|----------|
| Concurrent writes | Limited | Excellent | **Postgres** - second brain writes while user requests |
| Query power | Basic SQL | Advanced (JSONB, CTEs, window functions) | **Postgres** - analytics queries |
| Extensions | Few | TimescaleDB, pg_stat | **Postgres** - future time-series optimization |
| Docker deployment | Awkward | Native | **Postgres** - consistent with lab stack |
| Multi-client future | Painful | Ready | **Postgres** - no migration needed |

### 2.2.2 Why Elasticsearch for Logs

| Aspect | JSONL Files | Elasticsearch | Decision |
|--------|-------------|---------------|----------|
| Search | grep/jq | Full-text, aggregations | **ES** - find patterns in logs |
| Visualization | Manual scripts | Kibana dashboards | **ES** - immediate insights |
| Retention | Manual rotation | ILM policies | **ES** - automatic management |
| Scale | Single file | Distributed | **ES** - future-proof |
| Cost | Free | Docker resource | **ES** - already available |

### 2.3 LLM Backend: mlx-openai-server

**Decision**: Use mlx-openai-server for local inference instead of LM Studio or Ollama.

**Rationale**:
- Apple's MLX framework = best Apple Silicon performance
- OpenAI-compatible API = no code changes to LLM client
- Concurrent inference support (critical for second brain parallelism)
- Active development (v1.0.14+)

**Configuration**:
```yaml
# config/llm_backends.yaml
backends:
  primary:
    type: "openai_compatible"
    base_url: "http://localhost:8080/v1"
    model: "qwen2.5-7b-instruct"

  second_brain:
    type: "anthropic"
    model: "claude-sonnet-4-5-20250514"
```

**Benefits over Alternatives**:
- vs LM Studio: Supports concurrent requests (LM Studio is sequential)
- vs Ollama: Native Apple Silicon via MLX (Ollama uses llama.cpp)
- vs llama.cpp: More integrated, fewer moving parts

### 2.4 Component Responsibilities

#### Orchestrator (Primary Brain)
- Handle real-time user requests
- Load session context from SQLite
- Query memory graph for relevant context
- Execute task (LLM + tools)
- Update session state
- Return response to user

**State**: Request-scoped (ExecutionContext)
**Models**: Local (mlx-openai-server)

#### Brainstem (Autonomic Control)
- Monitor CPU, RAM, GPU continuously
- Detect threshold violations (NORMAL → ALERT → DEGRADED)
- Trigger mode transitions
- Schedule second brain runs (when idle + resources available)
- Manage model lifecycle
- Implement feedback loops for adaptive scheduling

**State**: Service-level (persistent monitoring state)
**Logic**: Rule-based + adaptive scheduling

#### Second Brain (Reflective Consolidation)
- Triggered by Brainstem (idle + resources available)
- Reads: Recent conversations, telemetry, Captain's Log captures
- Processes: Entity extraction, relationship mapping, pattern finding
- Updates: Memory graph (Neo4j)
- Creates: Meta-insights, consolidated reflections
- Uses: Claude 4.5 for deep reasoning

**State**: Consolidation state (last run timestamp, processing queue)
**Cadence**: Adaptive (feedback loops)

#### Memory Service (World Model)
- Neo4j database (separate daemon)
- Hierarchical abstraction layers (L0→L3)
- Query API (Cypher)
- Entity and relationship management
- Plausibility scoring for retrieval

**State**: Persistent graph database
**Access**: Via service API (all components can query)

### 2.5 Captain's Log Evolution

**Current**: LLM reflection runs synchronously after each request (adds latency).

**New**: Separate capture (fast) from reflection (slow):

```
During Request:
  User request → Orchestrator executes → Quick structured capture (no LLM)
      ↓
  Write to: telemetry/captains_log/captures/YYYY-MM-DD/trace-id.json

Later (Second Brain):
  Second brain wakes → Reads captures → Claude 4.5 deep reflection
      ↓
  Write to: telemetry/captains_log/reflections/YYYY-MM-DD/trace-id-reflection.json
```

**Capture Format** (Fast, no LLM):
```json
{
  "trace_id": "abc-123",
  "timestamp": "2026-01-21T15:30:00Z",
  "user_message": "Tell me about Loire Valley trees",
  "steps": [...],
  "tools_used": ["web_search"],
  "duration_ms": 3200,
  "metrics_summary": {...},
  "outcome": "completed"
}
```

**Reflection Format** (Slow, Claude 4.5):
```json
{
  "trace_id": "abc-123",
  "reflection_timestamp": "2026-01-21T16:00:00Z",
  "rationale": "Deep analysis of conversation...",
  "entities_extracted": ["Loire Valley", "oak species", "ecology"],
  "connections_found": ["Related to Bucks County trees discussion"],
  "proposed_changes": [...]
}
```

### 2.6 Service Lifecycle

**Startup** (`agent serve`):
1. Initialize FastAPI app
2. Connect to Neo4j (verify connectivity)
3. Initialize SQLite (create tables if needed)
4. Start mlx-openai-server connection
5. Initialize MCP gateway singleton (connect once)
6. Load tool registry
7. Start Brainstem monitoring tasks
8. Bind to port, start accepting requests
9. Log: "Service ready on http://localhost:8000"

**Shutdown** (`SIGTERM` or `agent stop`):
1. Stop accepting new requests
2. Wait for active requests to complete (30s timeout)
3. Stop Brainstem tasks
4. Disconnect MCP gateway
5. Close Neo4j connection
6. Close SQLite connection
7. Exit cleanly

**Health Check** (`GET /health`):
```json
{
  "status": "healthy",
  "components": {
    "neo4j": "connected",
    "llm": "ready",
    "mcp_gateway": "connected",
    "second_brain": "idle",
    "brainstem": "monitoring"
  },
  "uptime_seconds": 3600
}
```

---

## 3. Implementation Phases

### Phase 2.1: Service Foundation (Week 5 - 5 days)

**Goal**: Get service running with basic functionality

| Day | Task | Deliverable |
|-----|------|-------------|
| 1 | mlx-openai-server integration | Updated LLM client with backend abstraction |
| 2 | FastAPI service skeleton | `src/personal_agent/service/app.py` |
| 2-3 | MCP Gateway singleton | Persistent connection in service |
| 3 | Session persistence | SQLite storage layer |
| 4 | Thin CLI client | HTTP-based CLI commands |
| 5 | Integration testing | E2E tests passing |

**Acceptance Criteria**:
- ✅ Service starts and runs continuously
- ✅ CLI client communicates with service
- ✅ MCP gateway persistent (no subprocess churn)
- ✅ Sessions persist across service restarts
- ✅ All existing functionality works
- ✅ Zero regressions in test suite

### Phase 2.2: Memory & Second Brain (Weeks 6-7 - 10 days)

**Goal**: Build world memory and consolidation

| Day | Task | Deliverable |
|-----|------|-------------|
| 6-7 | Neo4j integration | Memory service with CRUD |
| 7-8 | Memory query API | Graph queries and retrieval |
| 8 | Orchestrator integration | Memory enrichment in requests |
| 9 | Captain's Log refactoring | Capture/reflection separation |
| 10-11 | Second brain component | Background consolidation |
| 11-12 | Entity extraction | Claude 4.5 extraction pipeline |
| 12-13 | Brainstem scheduling | Adaptive trigger logic |
| 13-14 | Claude API integration | Cost tracking and rate limiting |
| 14-15 | Integration testing | E2E consolidation tests |

**Acceptance Criteria**:
- ✅ Neo4j graph operational
- ✅ Memory queries work
- ✅ Conversations enriched with past context
- ✅ Second brain consolidates automatically
- ✅ Captain's Log dual-mode working
- ✅ Entity extraction pipeline operational
- ✅ No user-perceivable latency increase

### Phase 2.3: Homeostasis & Feedback (Week 8 - 5 days)

**Goal**: Adaptive, self-regulating system

| Day | Task | Deliverable |
|-----|------|-------------|
| 16 | Feedback loop design | Adaptive scheduling algorithm |
| 17 | Model lifecycle management | Dynamic model loading |
| 18 | Resource monitoring enhanced | Service-level continuous monitoring |
| 19 | Mode transitions | Control loops with actions |
| 20 | Integration & tuning | Performance optimization |

**Acceptance Criteria**:
- ✅ Feedback loops operational
- ✅ Scheduling adapts to usage patterns
- ✅ Model lifecycle managed efficiently
- ✅ System self-regulates under load
- ✅ Performance meets targets (<1% overhead)

---

## 4. Migration Strategy

### Feature Flag Approach

```python
# config/settings.py
class AppConfig(BaseSettings):
    use_service_mode: bool = Field(default=False)
```

**Rollout Plan**:
1. **Week 5**: Service mode exists but disabled (old CLI default)
2. **Week 6**: Service mode tested, flag flippable
3. **Week 7**: Service mode default, old CLI deprecated
4. **Week 8**: Old CLI removed (single code path)

### Backward Compatibility

**CLI Behavior**:
```python
# In CLI command
if settings.use_service_mode:
    # New: HTTP request to service
    response = await http_client.post("/chat", json={...})
else:
    # Old: Direct execution (current behavior)
    response = await orchestrator.handle_user_request(...)
```

### Rollback Strategy

**If service mode fails**:
1. Set `use_service_mode=false` in config
2. System reverts to old CLI behavior
3. Sessions lost (in-memory), but functionality intact
4. MCP gateway reverts to per-request subprocess
5. User experience degraded but not broken

**Data Safety**:
- Neo4j graph persists (can rebuild if needed)
- SQLite sessions backed up
- Captain's Log files preserved
- No data loss on rollback

---

## 5. Consequences

### Positive

✅ **Eliminated startup overhead**: No per-request initialization (2-3s saved)
✅ **Persistent MCP gateway**: Tool discovery once, reuse forever
✅ **Session continuity**: Conversations span days/weeks
✅ **Background processing**: Second brain consolidation
✅ **World memory**: Graph-based context enrichment
✅ **Better reflections**: Claude 4.5 deep synthesis
✅ **Multiple clients**: Web UI, API clients possible (future)
✅ **Self-regulation**: Adaptive homeostasis

### Negative

⚠️ **Deployment complexity**: Service must be running
⚠️ **State management**: Session, memory, monitoring state
⚠️ **Neo4j dependency**: Additional daemon to manage
⚠️ **Claude API costs**: Budget monitoring required
⚠️ **More code**: 500-800 lines of new infrastructure

### Mitigations

| Risk | Mitigation |
|------|------------|
| Service crashes | Health checks, automatic restart, graceful degradation |
| Neo4j complexity | Run as separate daemon, well-documented |
| Claude costs | Rate limiting, budget alerts, cost tracking |
| State corruption | SQLite ACID, Neo4j transactions, backups |
| Migration breaks workflows | Feature flag, extensive testing, gradual rollout |

---

## 6. Success Metrics

### Functional

| Metric | Target |
|--------|--------|
| Service uptime | >99% (research environment) |
| MCP discovery overhead | 0s (eliminated) |
| Session persistence | 100% across restarts |
| Memory retrieval accuracy | >80% relevance |

### Performance

| Metric | Target |
|--------|--------|
| Request latency | <2s P95 (no regression) |
| Memory query latency | <500ms |
| Consolidation duration | <5 min per run |
| Service idle overhead | <1% CPU |
| Claude API cost | <$5/week |

### Quality

| Metric | Target |
|--------|--------|
| Conversation quality | Subjective improvement |
| Context relevance | Agent references past conversations |
| Entity extraction quality | >80% precision |
| Reflection depth | Better than current |

---

## 7. Alternatives Considered

### 7.1 Keep CLI, Add Persistence (Rejected)

**Approach**: Persistent MCP gateway process, session persistence to JSON files.

**Rejected Because**:
- Still pays initialization cost per request
- No background processing capability
- Doesn't enable second brain architecture
- Misses opportunity for world memory

### 7.2 Hybrid Mode (Rejected)

**Approach**: Optional service mode, fallback to direct execution if service not running.

**Rejected Because**:
- Most complex option (two code paths)
- Maintenance burden of dual execution modes
- Feature flag approach achieves same goal with less complexity

### 7.3 Ollama Instead of mlx-openai-server (Rejected)

**Approach**: Use Ollama for local inference.

**Rejected Because**:
- mlx-openai-server uses native MLX (better Apple Silicon performance)
- Already have mlx-openai-server working in project
- Both are OpenAI-compatible, so migration is trivial if needed

### 7.4 Redis Instead of SQLite (Rejected)

**Approach**: Use Redis for session storage.

**Rejected Because**:
- Overkill for single-user workload
- Adds deployment complexity
- SQLite provides ACID transactions
- Redis better suited for multi-user/distributed scenarios

---

## 8. Open Questions (Deferred)

### For Future Investigation

1. **Multi-client support**: How to add authentication, session isolation?
   - Document: Architecture supports it, not implementing now

2. **Memory pruning**: When to compress/summarize old conversations?
   - Experiment: E-014 (consolidation frequency)

3. **Embedding integration**: Should memory retrieval use embeddings?
   - Experiment: E-015 (retrieval algorithms)

4. **Graph schema evolution**: How to migrate schema as we learn?
   - Document: Schema versioning strategy

---

## 9. References

### Internal Documents

- `../plans/sessions/SESSION-2026-01-19-service-architecture-planning.md` (1,400+ lines planning)
- `../plans/IMPLEMENTATION_ROADMAP.md` (Phase 2 section)
- `../architecture/HOMEOSTASIS_MODEL.md` (control loop philosophy)
- `../architecture/COGNITIVE_AGENT_ARCHITECTURE_v0.1.md` (cognitive architecture)

### Research & Inspiration

- Yann LeCun's World Models (hierarchical abstraction, predictive learning)
- MemGPT/Letta (hierarchical memory patterns)
- GraphRAG (Microsoft, knowledge graph + RAG)
- Claude Desktop (service architecture inspiration)

### Related ADRs

- **ADR-0011**: MCP Gateway Integration (tool expansion)
- **ADR-0012**: Request-Scoped Metrics Monitoring (metrics foundation)
- **ADR-0014**: Structured Metrics in Captain's Log (analytics-ready)

---

## 10. Experiments Planned

Extending `./experiments/EXPERIMENTS_ROADMAP.md`:

### E-013: Entity Extraction Methods
- Compare: spaCy NER vs Qwen3-4B vs Claude 4.5
- Measure: Entity count, quality, cost, latency

### E-014: Consolidation Frequency
- Compare: Fixed intervals vs adaptive scheduling
- Measure: Memory freshness, resource usage, retrieval accuracy

### E-015: Retrieval Algorithm Comparison
- Compare: Graph-first vs embedding-first vs hybrid
- Measure: Retrieval precision, recall, latency

### E-016: World Model Effectiveness
- A/B test: With memory vs without memory
- Measure: Conversation quality, context relevance

---

**Decision Log**:
- 2026-01-19: Architecture planning session (1,400+ lines)
- 2026-01-21: ADR-0016 created, mlx-openai-server confirmed as LLM backend
- 2026-01-21: Ready for Phase 2.1 implementation
