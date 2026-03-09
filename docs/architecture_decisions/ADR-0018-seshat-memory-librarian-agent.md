# ADR-0018: Seshat — Memory Librarian Agent

**Status**: Accepted
**Date**: 2026-02-22 (Revised: 2026-03-09)
**Deciders**: System Architect
**Related**: ADR-0017 (Three-Tier Multi-Agent Orchestration), ADR-0016 (Service Architecture)

---

## 1. Context

### The Memory Stewardship Problem

The Personal Agent currently stores knowledge in Neo4j (84 nodes, 89 relationships) via the Second Brain background consolidation process. Entity extraction runs through qwen3-8b, and the brainstem scheduler triggers consolidation cycles. This works for basic knowledge accumulation, but has fundamental limitations:

| Problem | Impact |
|---------|--------|
| **No curation intelligence** | Everything extracted is stored with equal weight; noise accumulates alongside signal |
| **No contradiction detection** | Conflicting facts coexist without resolution |
| **No lifecycle management** | Knowledge grows monotonically; nothing is archived, demoted, or forgotten |
| **No confidence scoring** | Extracted facts have no provenance quality signal |
| **No cross-referencing** | Duplicate entities and relationships accumulate |
| **Passive retrieval only** | Memory is searched when asked; it never proactively surfaces relevant context |
| **Single memory type** | Neo4j stores "facts" — no distinction between episodic, procedural, semantic, derived, profile, or working memory |

### The Vision: A Librarian, Not a Database

Drawing from mythic archetypes of knowledge stewardship:

- **Seshat** (Egyptian goddess of writing, record-keeping, and libraries) — the primary namesake, patron of scribes and archivists
- **Thoth** (Egyptian god of knowledge and writing) — scribe of the gods, inventor of writing
- **Hermes Trismegistus** — syncretic sage and keeper of the Hermetica, a mythical library spanning all branches of knowledge
- **Mnemosyne** (Greek Titaness of memory) — the underlying "RAM" of all knowledge

A database stores data. A librarian **understands, organizes, curates, and serves** knowledge. The Second Brain should evolve from a background extraction job into an autonomous agent that actively manages the system's accumulated knowledge.

### Research Grounding

The SOTA memory taxonomy (2025-2026 surveys) argues for richer categories than "short-term vs long-term":
- **Forms**: token-level, parametric, latent
- **Functions**: factual, experiential, working
- **Dynamics**: formation, evolution, retrieval

Our research document identifies 6 practical memory types. Seshat is the agent that manages this taxonomy.

---

## 2. Decision

### Introduce a dedicated "Seshat" agent responsible for all memory stewardship: curation, consolidation, retrieval, lifecycle management, and context assembly.

### 2.1 Agent Definition

```python
SESHAT_SPEC = AgentSpec(
    name="seshat",
    description="Memory librarian — curates, consolidates, and serves knowledge",
    system_prompt=SESHAT_SYSTEM_PROMPT,
    tier=AgentTier.WORKER,
    model_role=ModelRole.REASONING,  # model TBD — empirically selected per ADR-0017
    allowed_tools=[
        "memory_store", "memory_search", "memory_consolidate",
        "memory_promote", "memory_demote", "memory_forget",
        "memory_provenance", "knowledge_graph_query",
        "knowledge_graph_mutate"
    ],
    max_iterations=20,  # curation cycles can be multi-step
    max_rework_attempts=2,
    autonomous=True,     # can run without user request (brainstem-scheduled)
)
```

Seshat is a **Tier 2 (Worker)** agent per ADR-0017, but with a unique characteristic: it operates both on-demand (orchestrator requests context assembly) and autonomously (brainstem-scheduled curation cycles). The specific SLM powering Seshat is an empirical choice — it needs strong entity extraction and reasoning about knowledge quality, so a capable reasoning-oriented SLM is preferred.

### 2.2 Responsibilities

#### On-Demand (Triggered by Other Agents)

| Operation | Description |
|-----------|-------------|
| `assemble_context(task, agent)` | Build relevant context bundle for a requesting agent's task |
| `search_memories(query, filters)` | Semantic + keyword + graph search across all memory types |
| `retrieve_provenance(fact_id)` | Trace a fact back to its source evidence |
| `check_consistency(claim)` | Verify a new claim against existing knowledge |

#### Autonomous (Scheduled by Brainstem)

| Operation | Frequency | Description |
|-----------|-----------|-------------|
| `consolidation_cycle()` | Every N interactions or hourly | Run entity extraction, merge duplicates, update relationships |
| `quality_audit()` | Daily | Scan for contradictions, orphaned nodes, low-confidence facts |
| `lifecycle_review()` | Daily | Promote episodic → semantic where warranted; archive stale knowledge |
| `derived_knowledge_generation()` | Weekly | Synthesize summaries, extract patterns, build "playbooks" from repeated workflows |
| `statistics_report()` | On demand | Report on knowledge graph health (coverage, freshness, confidence distribution) |

### 2.3 Abstract Memory Interface

Seshat operates through an abstract memory interface that decouples agents from storage implementation:

```python
class MemoryInterface(Protocol):
    """Abstract interface for memory operations. Enables A/B testing of backends."""

    async def store_event(self, event: MemoryEvent) -> str: ...
    async def store_fact(self, fact: MemoryFact, confidence: float) -> str: ...
    async def search_episodic(self, query: str, filters: SearchFilters) -> list[Episode]: ...
    async def search_semantic(self, query: str, top_k: int) -> list[KnowledgeChunk]: ...
    async def retrieve_working_context(self, task_id: str) -> WorkingContext: ...
    async def consolidate(self, source_ids: list[str]) -> ConsolidationResult: ...
    async def promote(self, memory_id: str, target_type: MemoryType) -> None: ...
    async def demote(self, memory_id: str, reason: str) -> None: ...
    async def forget(self, memory_id: str, reason: str) -> None: ...
    async def get_provenance(self, memory_id: str) -> ProvenanceChain: ...
```

### 2.4 Memory Type Implementation

| Type | Storage Backend | Seshat's Role |
|------|----------------|---------------|
| **Working** | In-process state (existing `SessionManager`) | Assembles task-relevant context on demand |
| **Episodic** | Neo4j + event metadata | Indexes interactions, extracts lessons learned |
| **Semantic** | Neo4j + document store (future) | Curates stable facts, manages versioning |
| **Procedural** | Registry / structured store | Captures reusable tool plans and workflows |
| **Profile** | PostgreSQL (with governance) | Maintains user preferences with consent tracking |
| **Derived** | Neo4j + provenance links | Generates summaries, patterns, validated heuristics |

### 2.5 Dual Operating Mode

```
Brainstem Scheduler ──────┐
                          │ (autonomous cycles: consolidation, audit, lifecycle)
                          ▼
                    ┌─────────────┐
                    │   Seshat    │
                    │  (Memory    │
                    │  Librarian) │
                    └──────┬──────┘
                           │
      ┌────────────────────┼────────────────────┐
      │                    │                    │
      ▼                    ▼                    ▼
┌───────────┐    ┌──────────────┐    ┌──────────────┐
│  Memory   │    │  Knowledge   │    │  Observability│
│  Service  │    │  Graph       │    │  (metrics,    │
│  (Neo4j)  │    │  (Neo4j)     │    │   traces)     │
└───────────┘    └──────────────┘    └──────────────┘

Orchestrator / Workers ── request context ── Seshat ── assembled context ── back to requester
```

---

## 3. Alternatives Considered

### Alternative A: Keep Second Brain as Background Job (Status Quo)

Continue with the current scheduled entity extraction without agent intelligence.

- **Pros**: Simple, working, no new abstractions
- **Cons**: No curation, no lifecycle, no contradiction detection, no proactive context assembly
- **Rejected because**: Memory quality will degrade as the graph grows; retrieval noise will increase without stewardship

### Alternative B: Embed Memory Logic in Every Agent

Each worker agent manages its own memory interactions directly.

- **Pros**: No central memory bottleneck, agents have task-specific memory logic
- **Cons**: Duplicated memory code, inconsistent storage patterns, no cross-agent knowledge synthesis, harder to A/B test memory strategies
- **Rejected because**: Violates separation of concerns; a unified memory access layer through a dedicated agent is cleaner and enables cross-worker knowledge synthesis

### Alternative C: External Memory Service (Separate Process)

Deploy memory as a standalone microservice with REST/gRPC API.

- **Pros**: True isolation, independent scaling, could be shared across projects
- **Cons**: Network overhead, operational complexity, premature for single-developer project
- **Rejected because**: Extract when we have competing backends or multi-project needs; in-process abstract interface gives the same decoupling benefits without operational cost

---

## 4. Consequences

### Positive

- **Knowledge quality**: Active curation prevents noise accumulation
- **Context relevance**: Workers and the orchestrator receive tailored context, not raw search results — reinforcing context isolation (ADR-0017 §2.6)
- **Memory A/B testing**: Abstract interface enables swapping backends without touching agents
- **Lifecycle management**: Knowledge doesn't just grow — it matures, archives, and forgets
- **Research value**: Memory stewardship is an underexplored area; this creates a novel research artifact
- **Architectural clarity**: Clear ownership of memory operations (single responsible agent)

### Negative

- **Single point of failure**: If Seshat is down/slow, context assembly degrades (mitigate: fallback to direct Neo4j queries)
- **Complexity**: Autonomous curation cycles add background processing load
- **Tuning**: Curation heuristics (what to promote, demote, forget) need calibration

### Risks

- Over-curation: Seshat forgets or demotes valuable knowledge (mitigate: soft-delete with recovery window, provenance trails)
- Under-curation: Seshat is too conservative and noise still accumulates (mitigate: configurable aggressiveness thresholds)

---

## 5. Acceptance Criteria

- [ ] `MemoryInterface` protocol defined and implemented for Neo4j backend
- [ ] Seshat agent registered in multi-agent system (ADR-0017)
- [ ] On-demand context assembly works (other agents can request context)
- [ ] At least one autonomous cycle operational (consolidation or quality audit)
- [ ] Memory provenance tracked for all stored facts
- [ ] Fallback to direct queries when Seshat is unavailable
- [ ] Memory statistics/health endpoint available
- [ ] Integration tests for context assembly and consolidation cycles

---

## 6. Implementation Notes

### Phase Placement

This is **Phase 2.5**, following Phase 2.4 (Multi-Agent Orchestration). Seshat requires the agent base class and orchestrator delegation from ADR-0017.

### Minimum Viable Seshat

Start with:
1. Abstract memory interface (Protocol class)
2. On-demand context assembly (most immediate value)
3. One autonomous cycle (consolidation, already partially implemented in `second_brain/`)

Defer to later iterations:
- Derived knowledge generation
- Full lifecycle management (promote/demote/forget)
- Multi-backend A/B testing

### Estimated Effort

- Abstract memory interface: 2-3 days
- Seshat agent definition + on-demand mode: 3-4 days
- Autonomous consolidation cycle migration from second_brain: 2-3 days
- Testing + observability: 2-3 days
- **Total**: ~10-13 days (2 weeks)

### Migration Path

The existing `second_brain/` module becomes an implementation detail *inside* Seshat. The entity extraction, background consolidation, and scheduling logic are preserved but wrapped in the agent's reasoning loop, allowing Seshat to make intelligent decisions about *when* and *what* to consolidate rather than running on a fixed schedule.

### Revision History

- **2026-02-22**: Original proposal
- **2026-03-09**: Aligned with revised ADR-0017 (three-tier architecture). Updated `AgentSpec` to include `tier` and `max_rework_attempts` fields. Clarified Seshat as a Tier 2 worker with autonomous scheduling. Changed hardcoded model reference to empirical selection. Updated terminology from "specialist agents" to "workers" for consistency.
