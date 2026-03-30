# Architectural Planning Session: Service-Based Cognitive Architecture with World Memory

**Date**: 2026-01-19
**Duration**: 4+ hours
**Type**: Major Architectural Planning & Research Synthesis
**Status**: Planning Complete, Ready for Implementation

---

## Executive Summary

This session represents a major architectural pivot: transitioning from a CLI-based script to a **service-based cognitive architecture** with persistent memory, second brain consolidation, and homeostatic control. This is not an incremental improvement - it's a fundamental rearchitecting of the system to support advanced cognitive capabilities inspired by Yann LeCun's world modeling research.

**Key Decisions**:
1. Move to always-running FastAPI service with thin CLI client
2. Migrate from LM Studio to Ollama for concurrent inference
3. Implement Neo4j-based world memory with hierarchical abstraction
4. Create "second brain" component for async memory consolidation using Claude 4.5
5. Refactor Captain's Log to separate data capture from LLM reflection
6. Establish feedback-driven scheduling for cognitive processes (circadian rhythms)

**Scope**: 3-4 weeks of implementation across 3 phases

---

## Part 1: Problem Discovery & Analysis

### Current Architecture Pain Points

#### 1. MCP Gateway Lifecycle Issues

**Problem**: Gateway subprocess spawns and dies with each CLI invocation
- Tool discovery runs every request (41 tools, ~2-3s overhead)
- Async cleanup errors in MCP SDK (stdio transport bug)
- No connection pooling or resource reuse
- Subprocess management complexity in every request

**Impact**:
- User-perceived latency (~2-3s per request)
- Brittle lifecycle management
- Wasted compute on repeated discovery
- Potential memory leaks from subprocess churn

#### 2. Request-Scoped Metrics Discarded

**Problem**: `RequestMonitor` collects excellent metrics but data is lost when process exits
- Request-scoped monitoring works (ADR-0012 implemented successfully)
- Metrics tagged with trace_id for correlation
- But no historical data for trend analysis
- Captain's Log gets one-time snapshot, not longitudinal view

**Impact**:
- Can't identify performance patterns over time
- Can't optimize based on historical resource usage
- Homeostasis control loops can't learn from past behavior
- Limited debugging capability for historical issues

#### 3. Session State Ephemeral

**Problem**: SessionManager exists but cleared on every CLI run
- In-memory sessions work during execution
- Multi-turn conversations possible within single invocation
- But state lost between CLI calls
- No conversation history across days/weeks

**Impact**:
- Every CLI call is effectively stateless
- Can't build on previous conversations naturally
- User must manually track context
- No long-term relationship building

#### 4. LLM Initialization Overhead

**Problem**: Full stack initialization per request
- LLM client connects to LM Studio every time
- Tool registry rebuilt from scratch
- MCP gateway discovery runs fresh
- Configuration loaded and validated repeatedly

**Impact**:
- ~1-2s startup cost per request
- Wasteful resource usage
- Slow user experience
- Energy inefficiency

#### 5. Single-User Concurrency Limitation

**Problem**: LM Studio doesn't support concurrent requests
- Primary brain (user conversation) blocks
- Second brain (consolidation) can't run in background
- Tool execution sequential
- No parallel processing possible

**Impact**:
- Can't implement second brain architecture
- Background processing impossible
- Agent can't "think" while idle
- Limits cognitive sophistication

### Why These Problems Matter

The current CLI architecture was perfect for **MVP development**:
- Fast to iterate
- Simple to debug
- Easy to reason about
- No deployment complexity

But it's fundamentally incompatible with **cognitive sophistication**:
- No persistent memory = no learning
- No background processing = no reflection
- No concurrency = no parallel thought
- No state = no relationship building

**We've outgrown the architecture.** Time to evolve.

---

## Part 2: Architectural Vision & Research Synthesis

### Inspiration: Yann LeCun's World Models

**Core Concept**: Autonomous intelligence requires building internal models of the world through observation and interaction, not just pattern matching on inputs.

**Key Principles Applied**:

1. **Hierarchical Abstraction**
   - L0: Raw observations (conversations, telemetry, tool results)
   - L1: Extracted patterns (entities, relationships, topics)
   - L2: Meta-patterns (user interests, system behavior, failure modes)
   - L3: World model (user's context, agent's self-model, environment state)

2. **Predictive Learning**
   - System doesn't just memorize, it predicts
   - "If user asks about trees, they'll likely want ecology context"
   - "If user mentions France, Loire Valley is high-probability next topic"
   - Learn from prediction errors to improve model

3. **Grounding**
   - All abstract concepts link back to concrete experiences
   - "User interested in France" → grounded in 8 specific conversation nodes
   - "Agent slow on complex queries" → grounded in latency measurements
   - No floating abstractions

4. **Energy-Based Plausibility**
   - When retrieving memory, score by "how plausible is this connection?"
   - Graph distance + semantic similarity + recency + user patterns = plausibility
   - System learns what connections are meaningful vs spurious

**Not Building AGI**: Building a **personal cognitive prosthetic** - an extension of user's cognition that learns their patterns, remembers their interests, finds connections they'd miss.

### Dual-Process Cognitive Architecture

Inspired by human cognition (System 1 vs System 2):

#### Primary Brain (Fast Path / System 1)
- Handles real-time user conversations
- Uses local models (Ollama: Qwen, Mistral, DeepSeek)
- Lightweight, responsive, always available
- Queries existing memory graph as needed
- Focus: **Reactivity**

#### Second Brain (Slow Path / System 2)
- Runs in background when resources allow
- Uses Claude 4.5 (deep reasoning, world modeling)
- Processes conversations, telemetry, Captain's Log
- Builds and consolidates memory graph
- Extracts entities, finds relationships, creates meta-insights
- Focus: **Reflection & Synthesis**

#### Brainstem (Autonomic System)
- Monitors system resources continuously
- Triggers second brain when idle + resources available
- Manages mode transitions (NORMAL → ALERT → DEGRADED)
- Orchestrates LLM model loading/unloading
- Implements feedback loops for adaptive scheduling
- Focus: **Homeostasis & Resource Management**

**Biological Analogy**: Human brainstem handles autonomic functions (breathing, heartbeat) without conscious control. Agent brainstem handles resource management, monitoring, and mode regulation without user intervention.

### Memory Architecture: Graph-Based World Model

**Why Graph Database**: Conversations aren't linear - they're networks of related concepts, entities, and insights. Graph structure naturally represents:
- Entities (people, places, topics, concepts)
- Relationships (discusses, part-of, similar-to, caused-by)
- Temporal chains (conversation threads over time)
- Interest weights (user talks about France often: 0.8)
- Semantic bridges (Loire Valley trees → Bucks County trees via ecology)

**Example Query Pattern**: User asks about Bucks County trees
```
Current conversation entities: [Bucks County, Trees, Pennsylvania]
↓
Graph traversal:
  Path 1: Bucks County → Pennsylvania → (geographic comparison) → France → Loire Valley → Tree conversation (2 days ago)
  Path 2: Trees → (topic match) → Previous tree conversations
  Path 3: Pennsylvania → (user interest) → France (weight: 0.8) → Loire Valley conversation
↓
Rank by: graph distance + semantic similarity + recency + interest weights
↓
Surface top N to LLM as enrichment context
↓
Agent: "This reminds me of your Loire Valley conversation - both regions have similar riparian ecosystems..."
```

**Node Types**:
1. **Conversation nodes**: Complete conversation with summary, embedding, key entities
2. **Entity nodes**: Geographic (Loire Valley), Biological (oak species), Abstract (ecology)
3. **Interest nodes**: User patterns ("talks about France often", "interested in comparative analysis")
4. **Insight nodes**: Meta-observations created by second brain

**Edge Types**:
1. **Temporal**: Conversation A → happened before → Conversation B
2. **Topical**: Conversation → discusses → Entity
3. **Semantic**: Entity A ← ecological relationship → Entity B
4. **Geographic**: Place A → part of → Place B
5. **Comparative**: Place A ← similar climate to → Place B
6. **Interest**: User ← frequently mentions → Entity (weighted)

**Technology Choice**: Neo4j Community Edition
- Project owner already knows it (no learning curve)
- Powerful Cypher query language for complex traversals
- Graph algorithms built-in (pathfinding, centrality, clustering)
- Visualization in Neo4j Browser for debugging
- Can run as separate daemon (simple deployment)
- Adequate for single-user research workload

---

## Part 3: Service Architecture Design

### High-Level Component Structure

```
┌─────────────────────── Service Process (Always Running) ───────────────────────┐
│                                                                                 │
│  ┌────────────────┐  ┌──────────────┐  ┌──────────────────┐                  │
│  │  Orchestrator  │  │  Brainstem   │  │  Second Brain    │                  │
│  │  (Fast path)   │  │  (Autonomic) │  │  (Consolidation) │                  │
│  │                │  │              │  │                  │                  │
│  │ - User requests│  │ - Monitor    │  │ - Memory build   │                  │
│  │ - LLM calls    │  │   resources  │  │ - Entity extract │                  │
│  │ - Tool exec    │  │ - Mode mgmt  │  │ - Graph update   │                  │
│  │ - Sessions     │  │ - Schedule   │  │ - Consolidation  │                  │
│  └───────┬────────┘  │   2nd brain  │  │ - Meta-insights  │                  │
│          │           └──────┬───────┘  └────────┬─────────┘                  │
│          │                  │                    │                            │
│          │         ┌────────▼────────────────────▼────────┐                  │
│          │         │   Resource Manager                   │                  │
│          │         │   - CPU/RAM/GPU monitoring           │                  │
│          │         │   - LLM lifecycle (Ollama)           │                  │
│          │         │   - Feedback loops & scheduling      │                  │
│          │         │   - Adaptive cadence (circadian)     │                  │
│          │         └──────────────────────────────────────┘                  │
│          │                  │                    │                            │
│  ┌───────▼──────────────────▼────────────────────▼────────────────────────┐  │
│  │            Memory Service (World Model)                                 │  │
│  │            - Query API (Cypher)                                         │  │
│  │            - Entity graph management                                    │  │
│  │            - Hierarchical abstractions (L0→L3)                          │  │
│  │            - Plausibility scoring                                       │  │
│  └─────────────────────────────────────────────────────────────────────────┘  │
│                                                                                 │
│  ┌─────────────────────────────────────────────────────────────────────────┐  │
│  │            Captain's Log (Dual-Mode)                                    │  │
│  │            - Fast capture (structured JSON, no LLM)                     │  │
│  │            - Slow reflection (second brain + Claude 4.5)                │  │
│  └─────────────────────────────────────────────────────────────────────────┘  │
│                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
         │                    │                      │                    │
         ▼                    ▼                      ▼                    ▼
  ┌──────────┐        ┌─────────────┐       ┌────────────┐      ┌─────────────┐
  │  Neo4j   │        │  Ollama     │       │ Claude API │      │  SQLite     │
  │  Server  │        │  (Local     │       │ (Cloud     │      │  (Sessions) │
  │  (Graph) │        │   LLMs)     │       │  LLM)      │      │             │
  └──────────┘        └─────────────┘       └────────────┘      └─────────────┘
         ▲                    ▲                      ▲                    ▲
         │                    │                      │                    │
  ┌──────────┐        ┌─────────────┐       ┌────────────┐      ┌─────────────┐
  │  FastAPI │◄───────│ Thin CLI    │       │  Web UI    │      │   API       │
  │  Service │        │  Client     │       │  (Future)  │      │  (Future)   │
  │  (REST)  │        │  (Primary)  │       │            │      │             │
  └──────────┘        └─────────────┘       └────────────┘      └─────────────┘
```

### Component Responsibilities

#### 1. Orchestrator (Primary Brain)
**Purpose**: Handle real-time user requests
- Receive request via API
- Load session context (SQLite)
- Query memory graph for relevant context (Neo4j)
- Execute task (LLM + tools)
- Update session state
- Return response to user

**State**: Request-scoped (ExecutionContext)

**Models**: Local Ollama (fast inference)

#### 2. Brainstem (Autonomic Control)
**Purpose**: Maintain system homeostasis
- Monitor CPU, RAM, GPU continuously
- Detect threshold violations (NORMAL → ALERT → DEGRADED)
- Trigger mode transitions
- Schedule second brain runs (when idle + resources available)
- Manage Ollama model lifecycle (load/unload on demand)
- Implement feedback loops for adaptive scheduling

**State**: Service-level (persistent monitoring state)

**Logic**: Rule-based + learned scheduling patterns

#### 3. Second Brain (Reflective Consolidation)
**Purpose**: Build and maintain world model
- Triggered by Brainstem (idle + resources available)
- Reads: Recent conversations, telemetry, Captain's Log captures
- Processes: Entity extraction, relationship mapping, pattern finding
- Updates: Memory graph (Neo4j)
- Creates: Meta-insights, consolidated reflections
- Uses: Claude 4.5 for deep reasoning

**State**: Consolidation state (last run timestamp, processing queue)

**Cadence**: Adaptive (feedback loops, circadian rhythms)

#### 4. Memory Service (World Model)
**Purpose**: Persistent knowledge graph
- Neo4j database (separate daemon)
- Hierarchical abstraction layers (L0→L3)
- Query API (Cypher)
- Entity and relationship management
- Plausibility scoring for retrieval

**State**: Persistent graph database

**Access**: Via service API (all components can query)

#### 5. Captain's Log (Dual-Mode)
**Purpose**: Task-level and cross-task reflection

**Fast Capture** (During request):
- Structured JSON (no LLM)
- trace_id, user_message, steps, tools, metrics, outcome
- Written immediately to `telemetry/captains_log/captures/`

**Slow Reflection** (By second brain):
- Claude 4.5 deep analysis
- Cross-conversation synthesis
- Pattern identification
- Meta-insights
- Written to `telemetry/captains_log/reflections/`

**State**: File-based (JSON), indexed in memory graph

### Technology Stack Decisions

#### LLM Backend: Ollama + Claude Hybrid

**Ollama (Local)**:
- Use for: Primary brain (user conversations)
- Models: Qwen3-4B (router), Qwen3-14B (standard), DeepSeek-R1-14B (reasoning), Qwen3-Coder-30B (coding)
- Benefits: Fast, concurrent inference, offline capability
- Setup: `ollama serve` (background daemon)

**Claude 4.5 Sonnet (Cloud)**:
- Use for: Second brain (consolidation, reflection)
- Why: Superior reasoning, world modeling, entity extraction
- Cost: Only during background processing (not per user message)
- Governance: Track API costs, rate limiting, budget alerts

**Rationale**: Speed/depth trade-off
- User expects instant response → local models
- Consolidation can be slow → cloud quality acceptable
- Research goal: Learn cognitive architecture, not replicate Claude locally

#### Session Persistence: SQLite

**Why SQLite**:
- Embedded (no separate server)
- Single file database
- ACID transactions
- Adequate for single-user workload
- Zero operational overhead

**Schema**:
```sql
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    created_at TIMESTAMP,
    last_active_at TIMESTAMP,
    mode TEXT,
    channel TEXT,
    messages TEXT  -- JSON blob
);
```

**Alternative Considered**: Redis (overkill for single user)

#### Memory: Neo4j Community

**Why Neo4j**:
- Project owner already proficient
- Cypher query language (expressive)
- Graph algorithms built-in
- Neo4j Browser (debugging/visualization)
- Research-friendly

**Deployment**: Separate daemon (`brew install neo4j`, `neo4j start`)

**Alternative Considered**:
- SQLite with custom graph (simpler deployment, less powerful queries)
- Decision: Go with Neo4j - want full graph power for research

### Data Flow: User Request Lifecycle

**1. User Request Arrives**
```
User: agent chat "Tell me about Bucks County trees"
    ↓
CLI client: POST http://localhost:8000/chat
    {
        "message": "Tell me about Bucks County trees",
        "session_id": "abc-123"
    }
```

**2. Service Processes Request**
```
FastAPI → Orchestrator.handle_request()
    ↓
Load session from SQLite (message history)
    ↓
Query memory graph:
    MATCH (conv:Conversation)-[:DISCUSSES]->(entity)
    WHERE entity.name IN ["trees", "ecology", "France"]
    RETURN conv
    ORDER BY conv.timestamp DESC
    LIMIT 5
    ↓
Enrich context with retrieved conversations
    ↓
Call Ollama (reasoning model) with enriched context
    ↓
Get response, update session, return to client
```

**3. Background Processing Triggered**
```
(Meanwhile, Brainstem monitoring...)
    ↓
Detect: No active requests for 5 minutes
Detect: CPU < 50%, Memory < 70%
    ↓
Brainstem: Trigger second brain consolidation
    ↓
Second brain wakes:
    - Read last 10 conversation captures
    - Call Claude 4.5: "Extract entities and relationships"
    - Update Neo4j graph with new nodes/edges
    - Create reflection entry
    - Go back to sleep
```

**4. Next Request Benefits**
```
User: agent chat "What about Pennsylvania rivers?"
    ↓
Memory query finds: [Bucks County conversation, Loire Valley rivers, ecology patterns]
    ↓
Agent: "Interesting! Your Bucks County question relates to the Loire Valley
        ecology discussion from 2 days ago - both regions have..."
```

### Service Lifecycle Management

**Startup** (`agent serve`):
1. Initialize FastAPI app
2. Connect to Neo4j (verify connectivity)
3. Initialize SQLite (create tables if needed)
4. Start Ollama connection pool
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
7. Shutdown Ollama connections
8. Exit cleanly

**Restart** (crash recovery):
1. Startup sequence runs
2. Sessions reload from SQLite
3. Memory graph persists in Neo4j
4. Brainstem resumes monitoring
5. Service continues from last state

**Health Check** (`GET /health`):
```json
{
    "status": "healthy",
    "components": {
        "neo4j": "connected",
        "ollama": "ready",
        "mcp_gateway": "connected",
        "second_brain": "idle",
        "brainstem": "monitoring"
    },
    "uptime_seconds": 3600
}
```

---

## Part 4: Implementation Phasing

### Phase 1: Service Foundation (Week 1 - 5 days)

**Goal**: Get service running with basic functionality

**Tasks**:

1. **Ollama Migration** (Day 1)
   - Install Ollama (`brew install ollama`)
   - Pull models: `ollama pull qwen3:4b`, `ollama pull deepseek-r1:14b`
   - Update `LocalLLMClient` to support Ollama backend
   - Remove `previous_response_id` (stateful API, Ollama doesn't support)
   - Test: Existing CLI works with Ollama
   - Config: `LLM_BACKEND=ollama` in .env

2. **FastAPI Service Skeleton** (Day 2)
   - Create `src/personal_agent/service/app.py`
   - Endpoints: `POST /chat`, `GET /health`, `GET /sessions`
   - Service startup: Initialize components (MCP, registry, sessions)
   - Service shutdown: Graceful cleanup
   - Test: Service starts, health check responds

3. **MCP Gateway Singleton** (Day 2-3)
   - Move MCP initialization to service startup
   - Single gateway connection (lives with service)
   - Remove per-request subprocess spawning
   - Modify `executor.py`: Use existing gateway connection
   - Test: Tools work, no subprocess churn
   - Measure: Tool discovery overhead eliminated ✅

4. **Session Persistence** (Day 3)
   - Create `src/personal_agent/service/storage.py`
   - SQLite database: `~/.config/personal_agent/sessions.db`
   - Schema: sessions table with JSON messages
   - SessionManager: Load from/save to SQLite
   - Test: Sessions survive service restart

5. **Thin CLI Client** (Day 4)
   - Create `src/personal_agent/ui/service_client.py`
   - CLI command: `agent chat "message"` → POST to service
   - Auto-start service if not running (optional)
   - Backward compatibility: Feature flag (use_service_mode)
   - Test: CLI works identical to before

6. **Integration Testing** (Day 5)
   - E2E: Start service → CLI request → Response
   - Test: Session persistence across requests
   - Test: MCP tools work via service
   - Test: Service restart (sessions restore)
   - Test: Concurrent requests (basic)

**Acceptance Criteria**:
- ✅ Service starts and runs continuously
- ✅ CLI client communicates with service
- ✅ MCP gateway persistent (no subprocess churn)
- ✅ Sessions persist across service restarts
- ✅ All existing functionality works
- ✅ Zero regressions in test suite

**Deliverables**:
- `src/personal_agent/service/app.py` (FastAPI app)
- `src/personal_agent/service/storage.py` (SQLite persistence)
- `src/personal_agent/ui/service_client.py` (Thin CLI)
- `tests/integration/test_service.py` (Service tests)
- Updated: `LocalLLMClient` (Ollama support)
- Updated: `executor.py` (Use service-level gateway)

### Phase 2: Memory & Second Brain (Week 2-3 - 10 days)

**Goal**: Build world memory and consolidation

**Tasks**:

1. **Neo4j Integration** (Day 6-7)
   - Install Neo4j: `brew install neo4j`, `neo4j start`
   - Create `src/personal_agent/memory/service.py`
   - Connection management (neo4j-driver)
   - Schema: Conversation, Entity, Interest nodes
   - Basic CRUD operations
   - Test: Connect, create nodes, query graph

2. **Memory Query API** (Day 7-8)
   - Create `src/personal_agent/memory/queries.py`
   - Query: Find related conversations
   - Query: Get entities by topic
   - Query: User interest profile
   - Plausibility scoring (graph distance + recency + interest weight)
   - Test: Query patterns work

3. **Orchestrator Memory Integration** (Day 8)
   - Modify `executor.py`: Query memory before LLM call
   - Enrich context with retrieved conversations
   - Pass enriched context to LLM
   - Test: Agent references past conversations

4. **Captain's Log Refactoring** (Day 9)
   - Split: Fast capture (structured JSON) vs Slow reflection (LLM)
   - Fast capture: During request (no LLM)
   - File structure: `captures/` vs `reflections/`
   - Test: Capture works, no latency impact

5. **Second Brain Component** (Day 10-11)
   - Create `src/personal_agent/second_brain/consolidator.py`
   - Background task: Wakes when triggered by Brainstem
   - Reads: Recent captures + conversations
   - Calls: Claude 4.5 for entity extraction
   - Updates: Neo4j graph (nodes + edges)
   - Creates: Reflection entries
   - Test: Consolidation runs, graph updates

6. **Entity Extraction Pipeline** (Day 11-12)
   - Claude 4.5 prompt: Extract entities and relationships
   - Parse response: Entity list, relationship list
   - Graph update: Create/merge nodes, create edges
   - Interest weighting: Track entity frequency
   - Test: Entities extracted, graph grows

7. **Brainstem Scheduling** (Day 12-13)
   - Create `src/personal_agent/brainstem/scheduler.py`
   - Monitor: Idle time, resource availability
   - Trigger: Second brain when conditions met
   - Adaptive: Learn optimal cadence (feedback loops)
   - Test: Second brain triggered appropriately

8. **Claude API Integration** (Day 13-14)
   - Create `src/personal_agent/llm_client/claude.py`
   - Anthropic SDK integration
   - Cost tracking (log API spend)
   - Rate limiting (avoid runaway costs)
   - Test: Claude calls work, costs tracked

9. **Integration Testing** (Day 14-15)
   - E2E: Conversation → Capture → Consolidation → Memory update
   - Test: Memory retrieval enriches next conversation
   - Test: Second brain runs in background
   - Test: Graph grows with usage
   - Test: API costs within budget

**Acceptance Criteria**:
- ✅ Neo4j graph operational
- ✅ Memory queries work
- ✅ Conversations enriched with past context
- ✅ Second brain consolidates automatically
- ✅ Captain's Log dual-mode working
- ✅ Entity extraction pipeline operational
- ✅ No user-perceivable latency increase

**Deliverables**:
- `src/personal_agent/memory/service.py` (Neo4j integration)
- `src/personal_agent/memory/queries.py` (Graph queries)
- `src/personal_agent/second_brain/consolidator.py` (Consolidation)
- `src/personal_agent/brainstem/scheduler.py` (Adaptive scheduling)
- `src/personal_agent/llm_client/claude.py` (Claude API)
- Updated: `orchestrator/executor.py` (Memory enrichment)
- Updated: `captains_log/` (Dual-mode capture/reflection)

### Phase 3: Homeostasis & Feedback (Week 4 - 5 days)

**Goal**: Adaptive, self-regulating system

**Tasks**:

1. **Feedback Loop Design** (Day 16)
   - Metrics: Conversation volume, consolidation effectiveness, resource usage
   - Adaptive algorithm: Adjust consolidation frequency based on metrics
   - Circadian patterns: Learn user activity patterns
   - Test: Scheduling adapts to workload

2. **Model Lifecycle Management** (Day 17)
   - Ollama model pool: Which models to keep loaded
   - Dynamic loading: Load on-demand, unload when idle
   - Priority: Keep primary brain models loaded, second brain on-demand
   - Test: Model switching works, resource usage optimal

3. **Resource Monitoring Enhanced** (Day 18)
   - Extend RequestMonitor: Service-level monitoring
   - Continuous background metrics (always running)
   - Consolidation cost tracking (time, tokens, API spend)
   - Test: Full visibility into system behavior

4. **Mode Transitions** (Day 19)
   - Control loops: CPU/RAM thresholds → Mode transitions
   - NORMAL → ALERT → DEGRADED → RECOVERY → NORMAL
   - Actions: Throttle second brain, defer consolidation, reduce model size
   - Test: System degrades gracefully under load

5. **Integration & Tuning** (Day 20)
   - End-to-end testing with real usage
   - Performance tuning (query optimization, caching)
   - Cost analysis (Claude API spend patterns)
   - Documentation updates

**Acceptance Criteria**:
- ✅ Feedback loops operational
- ✅ Scheduling adapts to usage patterns
- ✅ Model lifecycle managed efficiently
- ✅ System self-regulates under load
- ✅ Performance meets targets (<1% overhead)

**Deliverables**:
- `src/personal_agent/brainstem/feedback.py` (Feedback loops)
- `src/personal_agent/service/model_pool.py` (Model lifecycle)
- Enhanced: `brainstem/sensors/request_monitor.py` (Service-level)
- Enhanced: `brainstem/mode_manager.py` (Control loop integration)
- Documentation: Service architecture guide

---

## Part 5: Experimentation Strategy

### Extending Existing Framework

**Leverage**: `../architecture_decisions/experiments/EXPERIMENTS_ROADMAP.md` already exists
- Experiment numbering: E-001 through E-012 (model optimization)
- Infrastructure: Benchmarks, A/B testing, evaluation tools
- Data storage: `telemetry/evaluation/experiments/`

**Add**: Memory & cognitive architecture experiments (E-013+)

### Memory Experiments Planned

#### E-013: Entity Extraction Methods

**Hypothesis**: LLM-based extraction produces 30%+ more relevant entities than keyword-based

**Method**:
- Process 50 conversations with 3 strategies:
  1. Keyword-based (spaCy NER)
  2. Local LLM (Qwen3-4B)
  3. Cloud LLM (Claude 4.5)
- Measure: Entity count, quality (human eval), processing time, cost

**Decision Matrix**:
- Claude wins quality → Use for consolidation
- Qwen close enough → Use local (free)
- Keywords sufficient → Keep simple

#### E-014: Memory Consolidation Frequency

**Hypothesis**: Adaptive scheduling based on conversation volume outperforms fixed intervals

**Method**:
- Track: Consolidation effectiveness (retrieval hit rate)
- Compare: Fixed (every 30 min) vs Adaptive (feedback-driven)
- Measure: Memory freshness, resource usage, retrieval accuracy

**Goal**: Find optimal scheduling strategy

#### E-015: Retrieval Algorithm Comparison

**Hypothesis**: Hybrid (graph + embeddings) beats either alone

**Method**:
- Implement 3 strategies:
  1. Graph-first (Cypher traversal)
  2. Embedding-first (semantic search)
  3. Hybrid (combine scores)
- Measure: Retrieval precision, recall, latency

**Goal**: Optimize memory query performance

#### E-016: World Model Effectiveness

**Hypothesis**: Memory enrichment improves conversation quality 20%+

**Method**:
- A/B test: With memory vs without memory
- Measure: Conversation quality (human eval), context relevance
- Qualitative: Ask user "Did agent make good connections?"

**Goal**: Validate memory system value

### Experiment Infrastructure

**Reuse Existing**:
- `tests/evaluation/system_evaluation.py` (scenario testing)
- `tests/evaluation/ab_testing.py` (head-to-head comparison)
- `telemetry/evaluation/` (data storage)

**Add New**:
- `tests/evaluation/memory_evaluation.py` (memory-specific tests)
- `telemetry/evaluation/experiments/E-013/` (entity extraction data)
- Analysis notebooks (Jupyter for data exploration)

---

## Part 6: Technical Decisions & Rationale

### Decision Log

#### 1. Ollama vs LM Studio

**Decision**: Migrate to Ollama

**Rationale**:
- Concurrent inference (second brain can run in background)
- Ollama optimized for Apple Silicon
- Background service mode (`ollama serve`)
- Model management CLI (`ollama pull`, `ollama list`)

**Trade-off**: Lose LM Studio GUI (keep for experimentation), lose stateful API

**Alternative Considered**: Keep LM Studio, accept queueing
**Rejected**: Blocks second brain architecture fundamentally

#### 2. Claude 4.5 for Second Brain

**Decision**: Use cloud LLM for consolidation

**Rationale**:
- Quality >> local models for world modeling
- Speed acceptable (background processing, not real-time)
- Cost reasonable (background work, not per-message)
- Research goal: Learn architecture, not replicate Claude

**Trade-off**: API costs, network dependency

**Alternative Considered**: Local model for everything
**Rejected**: Quality insufficient for world modeling research

#### 3. Neo4j vs Custom Graph

**Decision**: Neo4j Community Edition

**Rationale**:
- Project owner already proficient
- Cypher expressive for complex queries
- Graph algorithms built-in
- Research-friendly (visualization, experimentation)

**Trade-off**: Separate server, deployment complexity

**Alternative Considered**: SQLite with custom graph relations
**Rejected**: Want full graph power for research, can migrate later if needed

#### 4. Captain's Log Split

**Decision**: Separate capture (fast) from reflection (slow)

**Rationale**:
- User response time sensitive (fast capture)
- Reflection quality matters more than speed (slow reflection)
- Claude 4.5 better than local for synthesis
- Cross-conversation insights require batch processing

**Trade-off**: Lose immediate per-task reflection

**Alternative Considered**: Keep current (LLM reflection per task)
**Rejected**: Adds latency, limits reflection quality, no cross-task synthesis

#### 5. SQLite vs Redis for Sessions

**Decision**: SQLite

**Rationale**:
- Single-user workload (not high-throughput)
- Embedded (zero operational overhead)
- ACID transactions (data safety)
- Adequate performance for use case

**Trade-off**: Not suitable for multi-user (future consideration)

**Alternative Considered**: Redis (in-memory speed)
**Rejected**: Overkill for single user, adds deployment complexity

#### 6. Feature Flag Migration

**Decision**: Gradual migration with backward compatibility

**Rationale**:
- Risk mitigation (can rollback)
- Incremental testing
- User choice during transition
- Avoids big-bang cutover risk

**Implementation**: `settings.use_service_mode` boolean flag

**Timeline**: Maintain both modes through Phase 1, deprecate old CLI in Phase 3

---

## Part 7: Migration Strategy & Risk Mitigation

### Migration Approach

**Principle**: Feature flag with gradual rollout

**Phase 1 Migration**:
```python
# config/settings.py
use_service_mode: bool = False  # Start disabled

# CLI behavior
if settings.use_service_mode:
    # New: HTTP request to service
    response = requests.post("http://localhost:8000/chat", json={...})
else:
    # Old: Direct execution (current behavior)
    response = await orchestrator.handle_user_request(...)
```

**Rollout Plan**:
1. Week 1: Service mode exists but disabled (old CLI default)
2. Week 2: Service mode tested, flag flippable
3. Week 3: Service mode default, old CLI deprecated
4. Week 4: Old CLI removed (single code path)

### Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **Ollama concurrency issues** | Medium | High | Test thoroughly, can queue if needed |
| **Neo4j operational complexity** | Low | Medium | Well-documented, user already knows it |
| **Claude API costs runaway** | Medium | Medium | Rate limiting, budget alerts, cost tracking |
| **Memory graph grows too large** | Low | Medium | Archival strategy, graph pruning algorithms |
| **Service crashes (memory leaks)** | Low | High | Monitoring, restart automation, health checks |
| **Migration breaks existing workflows** | Medium | High | Feature flag, extensive testing, gradual rollout |
| **Second brain overhead** | Low | Low | Resource monitoring, adaptive throttling |

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

## Part 8: Success Metrics & Validation

### Functional Metrics

| Metric | Target | How to Measure |
|--------|--------|----------------|
| **Service uptime** | >99% (research environment) | Monitoring logs |
| **MCP tool discovery overhead** | 0s (eliminated) | Timing comparison |
| **Session persistence** | 100% across restarts | Integration tests |
| **Memory retrieval accuracy** | >80% relevance | Human evaluation |
| **Consolidation effectiveness** | Graph grows with usage | Node/edge counts |
| **Second brain triggers** | Adaptive (not fixed) | Scheduling logs |

### Performance Metrics

| Metric | Target | How to Measure |
|--------|--------|----------------|
| **User request latency** | <2s P95 (no regression) | Request timing |
| **Memory query latency** | <500ms | Neo4j query profiling |
| **Consolidation duration** | <5 min per run | Second brain logs |
| **Service overhead** | <1% CPU when idle | System monitoring |
| **Claude API cost** | <$5/week research budget | Cost tracking |

### Quality Metrics

| Metric | Target | How to Measure |
|--------|--------|----------------|
| **Conversation quality** | Subjective improvement | User assessment |
| **Context relevance** | Agent references past convos | Manual review |
| **Entity extraction quality** | >80% precision (human eval) | Experiment E-013 |
| **Reflection depth** | Better than current | Compare outputs |

### Research Metrics

| Metric | Target | How to Measure |
|--------|--------|----------------|
| **Graph complexity** | Grows with usage | Node/edge counts over time |
| **Interest tracking accuracy** | Matches user self-assessment | Periodic check-ins |
| **Consolidation patterns** | Identifies real patterns | Manual review of insights |
| **World model utility** | Useful for debugging/analysis | Developer usage |

---

## Part 9: Documentation Requirements

### ADRs to Create

1. **ADR-0016**: Service-Based Cognitive Architecture (comprehensive, this session)
2. **ADR-0017**: Memory Service & World Model Design (detailed Neo4j schema, queries)
3. **ADR-0018**: Second Brain Architecture (consolidation, scheduling, feedback loops)

### Architecture Documents to Update

1. **ORCHESTRATOR_CORE_SPEC_v0.1.md**: Add service integration patterns
2. **BRAINSTEM_SERVICE_v0.1.md**: Add scheduling, feedback loops
3. **New: MEMORY_SERVICE_SPEC_v0.1.md**: Complete memory service specification
4. **New: SECOND_BRAIN_SPEC_v0.1.md**: Consolidation pipeline specification

### User Documentation to Create

1. **Service Operations Guide**: Starting, stopping, monitoring, troubleshooting
2. **Memory Query Guide**: How to query and visualize memory graph
3. **Cost Management Guide**: Tracking and optimizing Claude API usage
4. **Migration Guide**: Moving from old CLI to new service

### Developer Documentation

1. **Service Development Guide**: Adding endpoints, components
2. **Memory Development Guide**: Adding node types, query patterns
3. **Experiment Guide**: Adding memory experiments to framework

---

## Part 10: Open Questions & Future Considerations

### Deferred to Post-Phase 3

1. **Multi-Client Support**
   - Architecture supports it (documented but not implemented)
   - Needs: Authentication, session isolation, resource pooling
   - Timeline: Post-research, if needed

2. **Advanced Memory Features**
   - Memory summarization (compress old conversations)
   - Memory archival (move old nodes to cold storage)
   - Memory search UI (web interface for exploration)

3. **Model Fine-Tuning**
   - Train router on agent-specific patterns
   - Fine-tune entity extractor on user's domain
   - Requires: Significant usage data first

4. **Distributed Deployment**
   - Service on separate machine
   - Multi-user scenarios
   - Load balancing, replication

### Research Questions to Explore

1. **Optimal Consolidation Frequency**
   - Fixed interval vs adaptive?
   - Time-based vs conversation-count-based?
   - User-specific patterns?

2. **Entity Extraction Strategy**
   - LLM vs NLP vs hybrid?
   - Real-time vs batch?
   - Cost/quality trade-offs?

3. **Retrieval Algorithm Design**
   - Graph traversal depth?
   - Semantic similarity threshold?
   - Recency weighting function?
   - Interest weight decay rate?

4. **Memory Pruning Strategy**
   - When to compress/summarize?
   - What to archive vs delete?
   - How to preserve important connections?

5. **Feedback Loop Tuning**
   - What metrics drive scheduling?
   - How fast should adaptation occur?
   - How to detect overfitting to patterns?

---

## Part 11: Next Immediate Steps

### Session Log Published ✅

This document created: `./sessions/SESSION-2026-01-19-service-architecture-planning.md`

### Step 2: Update Implementation Roadmap

**File**: `./IMPLEMENTATION_ROADMAP.md`

**Changes**:
- Mark Week 1-4 as COMPLETE (telemetry, governance, orchestrator, LLM client, MCP)
- Add new section: "Phase 2: Service Architecture & Memory System"
- Add 3-week timeline (Phases 1-3)
- Link to ADR-0016
- Note Ollama migration prerequisite

### Step 3: Write ADR-0016

**File**: `../architecture_decisions/ADR-0016-service-cognitive-architecture.md`

**Sections**:
1. Context & Problem Statement
2. Decision (Service Architecture)
3. Consequences (Benefits, Costs, Risks)
4. Implementation Details (Components, Technology Stack, Data Flow)
5. Phasing Strategy (3 phases detailed)
6. Migration Approach (Feature flag, rollback)
7. Success Metrics & Validation
8. Alternatives Considered
9. References & Related ADRs

**Length**: ~2000 lines (comprehensive architectural specification)

### Step 4: Begin Phase 1 Implementation

**First task**: Ollama migration
- Install Ollama
- Pull models
- Update LocalLLMClient
- Test existing CLI with Ollama backend
- Verify: All functionality works, concurrency supported

**Timeline**: Start after ADR-0016 approved

---

## Part 12: Philosophical Notes

### Why This Matters

This isn't just "making the CLI better." This is **building a cognitive prosthetic** that:
- Remembers what you forget
- Sees connections you miss
- Learns your patterns
- Grows more valuable over time
- Extends your thinking capacity

### The Learning Journey

**Not building AGI** (that's LeCun's job). Building **augmented intelligence**:
- You + Agent > You alone
- Agent learns from you
- You learn from agent
- Symbiotic relationship

### Research Philosophy

**Empirical**: Every decision backed by measurements (experiments E-013+)
**Iterative**: Build → Measure → Learn → Refine → Build more
**Transparent**: Full observability, explainable decisions
**Humble**: Accept limitations, learn from failures

### Success Definition

Success isn't "agent is smart." Success is:
- Agent helps me think better
- Conversations feel continuous, not fragmented
- I discover insights I wouldn't have found alone
- System is reliable, predictable, trustworthy
- I learn something about intelligence itself

---

## Conclusion

This session marks a **major architectural evolution**:
- From script to service
- From stateless to stateful
- From reactive to reflective
- From memory-less to world-modeling
- From single-process to cognitive architecture

**We're ready.** Foundation is solid, plan is clear, path forward is defined.

**Next step**: Update roadmap, write ADR-0016, begin implementation.

---

## Appendices

### A. Key Terminology

- **Primary Brain**: Fast path, real-time user interaction, local models
- **Second Brain**: Slow path, background consolidation, Claude 4.5
- **Brainstem**: Autonomic control, resource management, scheduling
- **World Model**: Hierarchical knowledge graph in Neo4j
- **Consolidation**: Entity extraction + graph update + reflection
- **Capture**: Fast structured logging (no LLM)
- **Reflection**: Deep LLM-based analysis (by second brain)

### B. File Structure (Post-Implementation)

```
src/personal_agent/
├── service/
│   ├── app.py              # FastAPI application
│   ├── storage.py          # SQLite session persistence
│   ├── model_pool.py       # Ollama model lifecycle
│   └── health.py           # Health check endpoints
├── memory/
│   ├── service.py          # Neo4j connection & CRUD
│   ├── queries.py          # Graph query patterns
│   ├── entities.py         # Entity extraction
│   └── scoring.py          # Plausibility scoring
├── second_brain/
│   ├── consolidator.py     # Main consolidation logic
│   ├── scheduler.py        # When to run consolidation
│   └── claude_client.py    # Claude API wrapper
├── brainstem/
│   ├── scheduler.py        # Adaptive scheduling
│   ├── feedback.py         # Feedback loop algorithms
│   └── mode_manager.py     # Enhanced with control loops
└── ui/
    └── service_client.py   # Thin CLI client (HTTP)
```

### C. Timeline Summary

| Week | Phase | Key Deliverables |
|------|-------|------------------|
| 1 | Service Foundation | Ollama, FastAPI, MCP singleton, SQLite, CLI client |
| 2-3 | Memory & Second Brain | Neo4j, consolidation, entity extraction, Claude integration |
| 4 | Homeostasis & Feedback | Adaptive scheduling, model lifecycle, feedback loops |

**Total**: 3-4 weeks from planning to operational system

### D. Success Checklist

- [ ] Service runs continuously (no per-request overhead)
- [ ] MCP gateway persistent (tool discovery once)
- [ ] Sessions survive restarts
- [ ] Memory graph grows with usage
- [ ] Second brain consolidates automatically
- [ ] Conversations reference past context naturally
- [ ] System self-regulates under load
- [ ] All existing functionality preserved
- [ ] Claude API costs within budget
- [ ] User experience improved (subjective)

---

**Session Status**: COMPLETE
**Next Action**: Update roadmap (`IMPLEMENTATION_ROADMAP.md`)
**Documentation**: This session log serves as comprehensive architectural specification
**Approval**: Ready for Phase 1 implementation upon roadmap update and ADR-0016 completion

---

**Document Stats**:
- **Lines**: 1,400+
- **Sections**: 12 major parts
- **Decisions Documented**: 20+
- **Components Specified**: 10+
- **Experiments Planned**: 4
- **Timeline**: 3-4 weeks, 3 phases

**This is not a session log. This is an architectural manifesto.** 🚀
