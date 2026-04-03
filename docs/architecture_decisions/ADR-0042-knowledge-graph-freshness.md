# ADR-0042: Knowledge Graph Freshness via Access Tracking

**Status**: Approved
**Date**: 2026-04-03
**Deciders**: Project owner
**Depends on**: ADR-0035 (Seshat backend — Neo4j + Enhanced Seshat), ADR-0041 (Event Bus via Redis Streams — Phase 4)
**Related**: ADR-0039 (Proactive Memory — relevance scoring), ADR-0024 (Session Graph Model)
**Enables**: Decay-based pruning, access-weighted relevance scoring, Slice 3 self-improvement insights

---

## Context

### The visibility gap

Every time the agent queries memory — entity lookup during context assembly, relationship traversal during consolidation, `memory_search` tool calls — those accesses are invisible to Neo4j. Entities and relationships carry `created_at` and (in some cases) `weight` on relationships, but no `last_accessed_at`, no `access_count`, no access-context metadata.

The knowledge graph treats a fact retrieved 50 times yesterday identically to one never touched since creation 3 months ago. This is a fundamental modeling gap: **usage is the strongest signal of ongoing relevance, and the graph doesn't capture it.**

The current relevance scoring in `MemoryService._calculate_relevance_scores()` uses:

| Signal | Weight range | Source |
|--------|-------------|--------|
| Recency (creation time) | 0.20–0.40 | `created_at` on Turn nodes |
| Entity match | 0.20–0.40 | Name overlap with query |
| Entity importance | 0.10–0.20 | `mentions` count on Entity nodes |
| Vector similarity | 0–0.25 | Embedding cosine distance |
| Reranker score | 0–0.35 | Cross-attention reranking |

None of these signals reflect **retrieval recency or frequency**. An entity mentioned once during creation but accessed daily for context assembly scores identically to one created yesterday and never retrieved. The `mentions` field tracks extraction-time mentions, not runtime access.

### Why access tracking must be async

If access is tracked synchronously in the query path, every read becomes a read+write. Memory queries happen on the hot path:

- **Context assembly** (`request_gateway/context.py`): `_query_memory_for_intent()` runs on every non-trivial request. Adding Neo4j writes here directly increases response latency.
- **Proactive memory** (ADR-0039): `suggest_relevant()` will query on every turn. Synchronous access tracking would double Neo4j load on the critical path.
- **`memory_search` tool**: User-facing tool call. Write latency here is directly visible in response time.
- **Consolidation**: `SecondBrainConsolidator` traverses relationships. Synchronous writes during batch traversal would create write amplification.

Current Neo4j query latency is flat at ~4ms (EVAL-02 data). Adding synchronous writes would push this to ~8–15ms per access, compounding across multiple entities per query. On a request that touches 5–10 entities during context assembly, that's 40–150ms of added latency — unacceptable on the hot path.

### Why this needs the event bus (ADR-0041)

ADR-0041 already anticipates this workstream. The event taxonomy includes `memory.accessed` on `stream:memory.accessed`, published by the memory service query path, consumed by a future freshness tracking consumer. Phase 4 of the event bus migration explicitly establishes this event as a no-op stub awaiting the consumer design.

With the event bus, the pattern becomes:

```text
Memory query executes → returns results immediately
  → publishes memory.accessed event (entity IDs, timestamps, query context)
     → background consumer batch-updates Neo4j:
        - last_accessed_at
        - access_count (increment)
        - access_context (what triggered it)
```

The hot path stays fast. The bookkeeping is async, durable (Redis Streams won't lose it), and batchable (update 20 nodes in one Cypher transaction instead of 20 individual writes).

### What this unlocks

1. **Access-weighted relevance scoring**: `_calculate_relevance_scores()` gains a recency/frequency signal — recently and frequently accessed entities rank higher. This directly improves proactive memory (ADR-0039) quality.

2. **Decay functions**: Entities not accessed in N days get progressively downweighted. Decay is a continuous signal, not a binary threshold — it modulates relevance scoring rather than triggering immediate deletion.

3. **Stale data detection and pruning**: A scheduled job (fits naturally in the brainstem lifecycle loop alongside consolidation and insights) flags or archives entities below a decay threshold. The project owner can review flagged entities before deletion.

4. **Insight generation for self-improvement**: "These 5 entities were accessed 100+ times this week" vs "these 50 entities haven't been touched in 60 days" — actionable signal for Slice 3 self-improvement (ADR-0030, ADR-0040). High-access entities reveal the agent's actual working knowledge; zero-access entities reveal stale or low-value content.

5. **Relationship freshness**: Access tracking applies to relationships, not just entities. A relationship like `(ProjectOwner)-[:WORKS_ON]->(ProjectX)` that hasn't been accessed in 90 days may no longer reflect reality. Relationship decay enables the agent to surface potentially stale connections for review rather than treating all edges as equally current.

---

## Decision

Implement **knowledge graph freshness tracking** by:

1. Adding `last_accessed_at`, `access_count`, and `access_context` properties to Entity and Relationship nodes in Neo4j.
2. Publishing `memory.accessed` events from all memory query paths via the event bus (ADR-0041).
3. Implementing a background consumer (`cg:freshness`) that batch-updates access metadata in Neo4j.
4. Adding a decay function that modulates relevance scoring based on access recency and frequency.
5. Adding a scheduled staleness detection job to the brainstem lifecycle loop.

---

## Key Design Decisions

### Decision 1: Schema additions on Entity and Relationship nodes

**Options considered:**

| Option | Description | Verdict |
|--------|-------------|---------|
| A. Properties on nodes | Add `last_accessed_at`, `access_count`, `last_access_context` directly to Entity and Relationship nodes | **Selected** — simple, queryable, no join overhead |
| B. Separate AccessLog nodes | Create `(:AccessLog)` nodes linked via `[:ACCESSED]` relationships | Rejected — high write amplification, complex queries, graph bloat |
| C. External time-series store | Track access in a separate system (Postgres, InfluxDB) | Rejected — adds infrastructure; the data must be co-located with entities for relevance scoring |

**Selected approach: A.** Properties on existing nodes.

**Schema additions:**

| Property | Node type | Type | Default | Purpose |
|----------|-----------|------|---------|---------|
| `last_accessed_at` | Entity, Relationship | `datetime \| null` | `null` | Most recent retrieval timestamp |
| `access_count` | Entity, Relationship | `int` | `0` | Cumulative retrieval count |
| `last_access_context` | Entity, Relationship | `string \| null` | `null` | What triggered the most recent access (`search`, `context_assembly`, `consolidation`, `suggest_relevant`, `tool_call`) |
| `first_accessed_at` | Entity | `datetime \| null` | `null` | First retrieval timestamp (distinguishes "created but never retrieved" from "retrieved at least once") |

**Why not a full access history?** A rolling access log would provide richer data (access patterns over time) but creates unbounded storage growth. The summary fields (`last_accessed_at`, `access_count`, `first_accessed_at`) capture the signals needed for decay and relevance scoring. If temporal access patterns become valuable (e.g., "this entity is accessed every Monday"), that analysis can run from the event bus stream directly (Redis Streams retain events for a configurable period) without persisting full history in Neo4j.

**Migration:** These are nullable, additive properties. No schema migration is needed — Neo4j is schema-optional. Existing nodes without these properties are treated as `access_count=0, last_accessed_at=null`, which correctly represents "never accessed since tracking began." A one-time backfill job can set `first_accessed_at = created_at` for existing entities as a reasonable approximation.

### Decision 2: Access event shape and publish points

The `memory.accessed` event carries identifiers and context metadata, consistent with ADR-0041's design principle that events carry identifiers, not large payloads.

**Event model:**

```python
class MemoryAccessedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_type: Literal["memory.accessed"] = "memory.accessed"
    timestamp: datetime
    trace_id: str
    entity_ids: list[str]
    relationship_ids: list[str]
    access_context: AccessContext
    query_type: str  # "recall", "recall_broad", "suggest_relevant", "memory_search", "consolidation_traversal"
    session_id: str | None = None


class AccessContext(str, Enum):
    SEARCH = "search"
    CONTEXT_ASSEMBLY = "context_assembly"
    CONSOLIDATION = "consolidation"
    SUGGEST_RELEVANT = "suggest_relevant"
    TOOL_CALL = "tool_call"
```

**Publish points — where events are emitted:**

| Publish point | Method | Access context | Expected volume |
|--------------|--------|---------------|-----------------|
| `MemoryService.query_memory()` | After query returns results | `SEARCH` | ~5–15/day |
| `MemoryService.query_memory_broad()` | After query returns results | `SEARCH` | ~2–5/day |
| `MemoryServiceAdapter.recall()` | After protocol call returns | `CONTEXT_ASSEMBLY` | ~20–50/day (every non-trivial request) |
| `MemoryServiceAdapter.recall_broad()` | After protocol call returns | `CONTEXT_ASSEMBLY` | ~5–10/day |
| `MemoryServiceAdapter.suggest_relevant()` (ADR-0039) | After suggestion returns | `SUGGEST_RELEVANT` | ~30–60/day (every turn, when enabled) |
| `SecondBrainConsolidator` relationship traversal | After consolidation reads | `CONSOLIDATION` | ~5–20/day (batched per consolidation run) |
| `memory_search` MCP tool | After tool returns results | `TOOL_CALL` | ~2–10/day |

**Total estimated event volume:** ~70–170 events/day, each referencing 1–20 entity/relationship IDs. This is well within Redis Streams capacity and produces manageable Neo4j write volume in the background consumer.

**Publish overhead:** Serializing and publishing to Redis Streams is sub-millisecond (ADR-0041 Phase 4 validation target: <1ms overhead). The event is fire-and-publish — the query path does not wait for consumer acknowledgment.

### Decision 3: Background consumer design — batched updates

The freshness consumer (`cg:freshness`) reads from `stream:memory.accessed` and batch-updates Neo4j.

**Consumer behavior:**

1. Read events from stream via `XREADGROUP` (consumer group `cg:freshness`).
2. Accumulate events over a configurable batch window (default: 5 seconds or 50 events, whichever comes first).
3. Deduplicate entity/relationship IDs within the batch (if the same entity was accessed 5 times in the window, increment `access_count` by 5 and set `last_accessed_at` to the latest timestamp).
4. Execute a single Cypher transaction:

```cypher
UNWIND $updates AS update
MATCH (e:Entity {id: update.entity_id})
SET e.last_accessed_at = update.last_accessed_at,
    e.access_count = COALESCE(e.access_count, 0) + update.access_increment,
    e.last_access_context = update.access_context,
    e.first_accessed_at = COALESCE(e.first_accessed_at, update.last_accessed_at)
```

(Equivalent query for relationships.)

1. Acknowledge processed events via `XACK`.

**Why batching matters:** At 70–170 events/day, individual writes would be fine. But each event may reference 10–20 entities, and consolidation traversals may touch 50+ relationships. Batching avoids write amplification: 50 events × 10 entities = 500 node updates, consolidated into 1–3 Cypher transactions instead of 500.

**Failure handling:** Consistent with ADR-0041's consumer runner — unacknowledged events remain in the pending entry list (PEL). After `max_retries` (default 3), failed events route to `stream:dead_letter`. Access tracking is best-effort — a missed update means slightly stale metadata, not data loss or incorrect behavior.

**Idempotency:** `access_count` is an increment, not an absolute set, so replaying an event double-counts. This is acceptable: access counts are approximate signals for scoring, not accounting ledgers. If exact counts become important, add an event ID dedup set (Redis `SET` with TTL) — but this is premature optimization at current volumes.

### Decision 4: Decay function design

**Options considered:**

| Option | Description | Verdict |
|--------|-------------|---------|
| A. Exponential decay on access recency | Score = e^(-λ × days_since_last_access) | **Selected** — well-understood, tunable, biologically inspired |
| B. Linear decay | Score = max(0, 1 - days/max_days) | Rejected — cliff edge at max_days, no natural tail |
| C. Step function | Fresh if <N days, stale otherwise | Rejected — too coarse, no gradient for scoring |
| D. Frequency-weighted decay | Combine access count with recency | Selected as enhancement to A |

**Selected approach: A + D.** Exponential decay on recency, modulated by access frequency.

**Decay formula:**

```text
freshness(entity) = base_decay(days_since_last_access) × frequency_boost(access_count)

base_decay(days) = e^(-λ × days)
    where λ = ln(2) / half_life_days    (configurable, default half_life_days = 30)

frequency_boost(count) = min(1.0 + α × ln(1 + count), max_boost)
    where α = 0.1, max_boost = 1.5
```

**Interpretation:**

- An entity accessed yesterday with 50 prior accesses: `freshness ≈ 0.98 × 1.49 ≈ 1.46` (capped at 1.0 for scoring weight)
- An entity last accessed 30 days ago with 5 accesses: `freshness ≈ 0.50 × 1.18 ≈ 0.59`
- An entity last accessed 90 days ago with 1 access: `freshness ≈ 0.125 × 1.07 ≈ 0.13`
- An entity never accessed (`access_count=0`): `freshness = 0.0` (no access data — falls back to creation-time recency only)

**Half-life tunability:** The 30-day default means an entity loses half its freshness score every 30 days without access. This is configurable via `settings.freshness_half_life_days`. Shorter half-lives (7–14 days) suit rapidly changing domains; longer (60–90 days) suit stable reference knowledge.

**Integration with relevance scoring:** The freshness score becomes a new factor in `_calculate_relevance_scores()`. The weight allocation shifts to accommodate it:

| Signal | Current (full pipeline) | With freshness |
|--------|------------------------|----------------|
| Recency (creation) | 0.20 | 0.15 |
| Entity match | 0.20 | 0.20 |
| Entity importance | 0.10 | 0.05 |
| Vector similarity | 0.15 | 0.15 |
| Reranker score | 0.35 | 0.30 |
| **Freshness (access)** | — | **0.15** |

**Why reduce creation recency and importance?** Freshness subsumes part of the creation recency signal (recently created entities are often recently accessed) and refines the importance signal (a better proxy than static `mentions` count). Reranker weight decreases slightly because freshness provides a complementary relevance signal.

**Graceful degradation:** When freshness data is unavailable (cold start, entities created before tracking), the freshness factor is excluded and weights redistribute to the existing signals. This means the system works identically to today until access data accumulates.

### Decision 5: Staleness detection and lifecycle actions

A scheduled brainstem job (`freshness_review`) runs periodically (default: weekly, alongside the existing lifecycle loop) and identifies stale entities and relationships.

**Staleness tiers:**

| Tier | Condition | Action |
|------|-----------|--------|
| **Warm** | Accessed within `half_life_days` | No action — entity is actively relevant |
| **Cooling** | Last accessed between `half_life_days` and `2 × half_life_days` (default 30–60 days) | Downweighted in scoring (automatic via decay function). No structural action. |
| **Cold** | Last accessed between `2 × half_life_days` and `cold_threshold_days` (default 60–180 days) | Flagged in telemetry as `entity.stale`. Logged for weekly insights. Available for manual review. |
| **Dormant** | Last accessed > `cold_threshold_days` OR never accessed and created > `cold_threshold_days` ago | Candidate for archival. A Captain's Log proposal is generated: "N dormant entities identified — review for archival or re-validation." |

**Why not auto-delete?** Autonomous deletion of knowledge is a high-risk action. A fact about the project owner's allergy or legal obligation could be dormant but critical. The staleness job identifies candidates; the project owner decides (via Linear feedback per ADR-0040, or direct review). Auto-archival (soft-delete with recovery) can be considered after the feedback loop proves reliable.

**Relationship staleness:** Relationships follow the same tier model. A relationship like `(Owner)-[:WORKS_ON]->(OldProject)` last accessed 180 days ago is flagged as dormant. The staleness review can surface "N relationships not accessed in 90+ days — some may no longer be accurate" as an insight for the self-improvement pipeline.

**Integration with insights engine:** Staleness metrics feed into the weekly insights analysis (ADR-0030):

- Total entities by freshness tier (warm/cooling/cold/dormant)
- Week-over-week tier migration (how many entities moved from warm to cooling?)
- Top-accessed entities (usage hotspots)
- Never-accessed entities created more than 30 days ago (extraction noise?)
- Dormant relationships by type (which relationship types go stale fastest?)

---

## Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│                        Hot Path (unchanged)                      │
│                                                                  │
│  ┌──────────────┐    ┌──────────────┐    ┌───────────────────┐  │
│  │ recall()     │    │ recall_broad()│    │ suggest_relevant()│  │
│  │ query_memory │    │ query_memory_ │    │ (ADR-0039)       │  │
│  │              │    │ broad()       │    │                   │  │
│  └──────┬───────┘    └──────┬────────┘    └────────┬──────────┘  │
│         │                   │                      │             │
│         └───────────┬───────┴──────────────────────┘             │
│                     │                                            │
│              Return results immediately                          │
│                     │                                            │
│              ┌──────▼──────────────────────────┐                │
│              │  Publish memory.accessed event   │                │
│              │  (entity IDs, context, timestamp) │                │
│              └──────┬──────────────────────────┘                │
└─────────────────────┼───────────────────────────────────────────┘
                      │
                      │ Redis Streams (async, durable)
                      │ stream:memory.accessed
                      │
┌─────────────────────┼───────────────────────────────────────────┐
│                     │     Background Path                        │
│              ┌──────▼──────────────────────────┐                │
│              │  cg:freshness consumer           │                │
│              │  - Batch window: 5s / 50 events  │                │
│              │  - Deduplicate entity IDs         │                │
│              │  - Single Cypher transaction      │                │
│              └──────┬──────────────────────────┘                │
│                     │                                            │
│              ┌──────▼──────────────────────────┐                │
│              │  Neo4j batch update              │                │
│              │  SET last_accessed_at,           │                │
│              │      access_count += N,          │                │
│              │      last_access_context,        │                │
│              │      first_accessed_at           │                │
│              └─────────────────────────────────┘                │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                   Brainstem Lifecycle Loop                        │
│                                                                  │
│  ┌──────────────────────────────────┐                           │
│  │  freshness_review (weekly)       │                           │
│  │  - Classify entities by tier     │                           │
│  │  - Emit telemetry                │                           │
│  │  - Flag dormant for review       │                           │
│  │  - Generate insight proposals    │                           │
│  └──────────────────────────────────┘                           │
│                                                                  │
│  ┌──────────────────────────────────┐                           │
│  │  Relevance scoring integration   │                           │
│  │  _calculate_relevance_scores()   │                           │
│  │  + freshness factor (0–0.15)     │                           │
│  └──────────────────────────────────┘                           │
└─────────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Owns | Does NOT own |
|-----------|------|-------------|
| Memory service (query path) | Publishing `memory.accessed` events after query completion | Writing access metadata to Neo4j |
| Event bus (`stream:memory.accessed`) | Durable delivery of access events | Event interpretation or storage |
| Freshness consumer (`cg:freshness`) | Batching and writing access metadata to Neo4j | Deciding what access means (scoring, decay) |
| Relevance scoring (`_calculate_relevance_scores()`) | Computing freshness factor from stored metadata | Collecting or persisting access data |
| Brainstem freshness review job | Classifying staleness tiers, generating insights | Deleting or archiving entities autonomously |

### Code Location

New modules:

| Module | Purpose | Estimated size |
|--------|---------|---------------|
| `src/personal_agent/events/consumers/freshness_consumer.py` | Batch consumer for `memory.accessed` events | ~80–120 lines |
| `src/personal_agent/memory/freshness.py` | Decay function, freshness scoring, staleness classification | ~100–150 lines |
| `src/personal_agent/brainstem/jobs/freshness_review.py` | Scheduled staleness detection job | ~80–100 lines |

Existing module changes:

| File | Change |
|------|--------|
| `memory/service.py` | Add event publish calls after `query_memory()`, `query_memory_broad()` |
| `memory/protocol_adapter.py` | Add event publish calls after `recall()`, `recall_broad()`, `suggest_relevant()` |
| `memory/service.py` (`_calculate_relevance_scores()`) | Add freshness factor with graceful degradation |
| `events/models.py` | Add `MemoryAccessedEvent`, `AccessContext` models |
| `brainstem/scheduler.py` | Register `freshness_review` job |
| `config/settings.py` | Add `FreshnessSettings` (half_life_days, cold_threshold_days, batch_window_seconds, etc.) |

### Configuration

Via `personal_agent.config.settings`, consistent with project conventions:

```python
class FreshnessSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    half_life_days: float = 30.0
    cold_threshold_days: int = 180
    frequency_boost_alpha: float = 0.1
    frequency_boost_max: float = 1.5
    consumer_batch_window_seconds: float = 5.0
    consumer_batch_max_events: int = 50
    review_schedule_cron: str = "0 8 * * 0"  # Weekly, Sunday 08:00 UTC
    relevance_weight: float = 0.15
```

When `freshness.enabled` is `False`, no events are published and `_calculate_relevance_scores()` uses the existing weight distribution. Rollback is a config change.

---

## Alternatives Considered

### A. Synchronous access tracking (write-on-read)

Update `last_accessed_at` and `access_count` directly in the memory query Cypher.

**Pros:** Simpler — no event bus dependency, no background consumer. Data is immediately consistent.

**Cons:** Adds write latency to every read. At ~5–10ms per write and 5–10 entities per query, this adds 25–100ms to the hot path. Violates the sub-10ms query latency constraint (ADR-0035). Creates write contention during consolidation batch traversals. Not batchable.

**Rejected:** The performance penalty on the hot path is unacceptable for a conversational agent.

### B. Periodic batch scan (no event bus)

A scheduled job scans query logs or telemetry to reconstruct access patterns and batch-update Neo4j.

**Pros:** No event bus dependency. Works with existing infrastructure.

**Cons:** Requires persisting query logs with entity-level granularity (currently not captured). Introduces latency between access and metadata update (hours instead of seconds). Parsing structured logs for entity IDs is fragile. No real-time freshness signal — the scoring function always lags.

**Rejected:** Higher implementation complexity for worse freshness. The event bus exists (ADR-0041); using it is the natural pattern.

### C. Separate freshness store (Redis sorted sets)

Track access metadata in Redis sorted sets (entity ID → last access timestamp, sorted by score = access count) and query Redis during relevance scoring.

**Pros:** Very fast reads. No Neo4j write overhead. Natural fit for sorted access data.

**Cons:** Freshness data is split between Redis and Neo4j — Cypher queries cannot use it for graph-aware filtering. Adds a Redis query to every relevance scoring call (trades Neo4j write latency for Redis read latency). Data must be synced to Neo4j for staleness detection jobs that traverse the graph.

**Rejected:** Co-locating access metadata with entities in Neo4j enables graph-native queries ("find all entities connected to X that haven't been accessed in 90 days") that would require complex joins across stores.

---

## Consequences

### Positive

- Memory queries gain an access-weighted relevance signal — frequently and recently used knowledge ranks higher, improving proactive memory quality
- Stale data becomes visible: the system can distinguish "actively relevant" from "accumulated noise" for the first time
- Relationship freshness enables temporal truthfulness — the agent can flag connections that may no longer reflect reality
- Insight generation gains concrete data ("50 entities dormant for 60+ days" is actionable for self-improvement)
- Natural fit with existing event bus infrastructure — no new dependencies beyond ADR-0041
- Background processing means zero impact on hot-path latency
- Graceful degradation: the system works identically to today until access data accumulates

### Negative

- Requires ADR-0041 (event bus) to be partially implemented — specifically Phase 4 (memory.accessed event publishing). This ADR cannot ship independently.
- Access counts are approximate (no idempotency guarantee on event replay). Acceptable for scoring signals but not for precise analytics.
- Additional Neo4j write load from the background consumer (~100–500 property updates/day). Negligible at current scale but should be monitored.
- Decay function parameters (half_life_days, frequency_boost_alpha) require tuning through observation — initial values are educated guesses.

### Risks

| Risk | Mitigation |
|------|-----------|
| Freshness consumer falls behind during burst access | Batch window absorbs bursts; Redis Streams backpressure is natural (PEL grows, consumer catches up). At 170 events/day, this is not a realistic concern. |
| Decay function parameters are wrong | All parameters are configurable. Start with conservative defaults (30-day half-life). Tune based on 4+ weeks of access data via insights analysis. |
| Stale entity flagging creates review fatigue | Dormant tier threshold (180 days) is deliberately long. Weekly review job generates a single summary, not per-entity alerts. Proposals go through Linear feedback loop (ADR-0040), not real-time notifications. |
| Access tracking on relationships creates excessive events | Relationship access events are only published during explicit relationship traversal (consolidation, graph walks), not implicitly when entities are returned. Volume is bounded by consolidation frequency. |
| Cold-start problem: no access data for existing 990 entities | Graceful degradation — freshness factor is excluded when data is unavailable. Backfill `first_accessed_at = created_at` as an approximation. Data accumulates naturally within 2–4 weeks of deployment. |

---

## Implementation Priority

This workstream depends on ADR-0041 Phase 4 (memory.accessed event publishing). It can be parallelized with ADR-0039 (proactive memory) — both enhance `_calculate_relevance_scores()` but on independent axes (proactive memory adds a retrieval path; freshness adds a scoring signal).

| Order | Work | Rationale |
|-------|------|-----------|
| 1 | Schema additions (nullable properties on Entity/Relationship nodes) | Zero-risk, additive — can ship immediately |
| 2 | Event model (`MemoryAccessedEvent`, `AccessContext`) | Types-first design |
| 3 | Publish points in memory service / protocol adapter | Depends on ADR-0041 Phase 4 event bus infra |
| 4 | Freshness consumer (`cg:freshness`) | Batch writer — the core async pipeline |
| 5 | Decay function + relevance scoring integration | Read path — uses data from step 4 |
| 6 | Brainstem freshness review job | Lifecycle integration — uses data from step 4 |
| 7 | Insights engine integration (staleness metrics) | Observability — uses data from step 6 |
| 8 | Backfill job for existing entities | One-time migration |

**Estimated effort:** M (2–3 sessions). Steps 1–2 are S. Steps 3–5 are M (core value). Steps 6–8 are S (additive).

---

## Acceptance Criteria

- [ ] Entity and Relationship nodes support `last_accessed_at`, `access_count`, `last_access_context`, `first_accessed_at` properties
- [ ] `MemoryAccessedEvent` published from all memory query paths (`recall`, `recall_broad`, `suggest_relevant`, `query_memory`, `query_memory_broad`, consolidation traversal, `memory_search` tool)
- [ ] Freshness consumer (`cg:freshness`) batch-updates Neo4j with < 5s latency from event publish to property update (under normal load)
- [ ] Publishing `memory.accessed` adds < 1ms to memory query latency (consistent with ADR-0041 Phase 4 validation target)
- [ ] `_calculate_relevance_scores()` includes freshness factor when access data is available
- [ ] `_calculate_relevance_scores()` gracefully degrades to existing weights when freshness data is absent
- [ ] Brainstem freshness review job classifies entities into warm/cooling/cold/dormant tiers
- [ ] Freshness review emits telemetry: entity counts by tier, tier migration week-over-week
- [ ] Dormant entities/relationships generate Captain's Log insight proposals
- [ ] All new code uses structured logging with `trace_id`
- [ ] Unit tests for decay function, freshness scoring, staleness classification
- [ ] Integration test: access entity → verify consumer updates `access_count` and `last_accessed_at` within batch window
- [ ] Feature flag (`freshness.enabled`) disables all freshness tracking with no behavioral change

---

## What This ADR Does NOT Cover

1. **Automatic deletion of stale entities** — Staleness detection flags candidates; deletion requires human review. Autonomous deletion may be considered in a follow-on ADR after the feedback loop (ADR-0040) proves reliable for this use case.
2. **Memory importance rebalancing** — The current `mentions`-based importance score could be replaced by access-based importance. That's a scoring model change best evaluated after access data accumulates (4+ weeks).
3. **Cross-session access correlation** — "Entity X is always accessed in the same session as Entity Y" is a valuable co-access signal for clustering and recommendation. Deferred to Slice 3 self-improvement work.
4. **Embedding refresh on stale entities** — Stale entities may have outdated embeddings if the embedding model or strategy changes. Embedding lifecycle management is a separate concern.

---

## References

- ADR-0035: Seshat Backend Decision (Neo4j + Enhanced Seshat)
- ADR-0039: Proactive Memory via `suggest_relevant()` (relevance scoring consumer)
- ADR-0041: Event Bus via Redis Streams (infrastructure dependency, `memory.accessed` event)
- ADR-0040: Linear as Async Feedback Channel (feedback loop for dormant entity review)
- ADR-0030: Captain's Log Dedup & Self-Improvement Pipeline (insights integration)
- ADR-0024: Session Graph Model (entity and relationship schema)
- Cognitive Architecture v0.1: `docs/archive/COGNITIVE_AGENT_ARCHITECTURE_v0.1.md` §5 (memory consolidation, retention policies)
- Cognitive Architecture Redesign v2: `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` §5 (memory lifecycle)
- Proactive Memory Design Spec: `docs/specs/PROACTIVE_MEMORY_DESIGN.md` (recency weighting, decay)
- Ebbinghaus forgetting curve (1885): Exponential decay of memory strength without rehearsal — biological basis for the decay function
