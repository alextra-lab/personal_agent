# ADR-0035: Seshat Backend Decision — Neo4j vs Graphiti

**Date:** 2026-03-28
**Status:** Accepted
**Deciders:** Alex (project lead)
**Linear Issue:** FRE-151 (EVAL-06)
**Depends on:** EVAL-02 (FRE-147, Graphiti experiment), EVAL-03 (FRE-148, promotion quality)
**Blocks:** EVAL-08 (Slice 3 priorities)

---

## Context

Seshat — the Personal Agent's memory subsystem — stores episodic conversations, semantic entities, and inter-entity relationships in a Neo4j knowledge graph. Before committing to Slice 3 (intelligence layer: proactive memory, programmatic delegation, self-improvement), we must decide whether the current hand-built Neo4j backend is the right foundation or whether adopting [Graphiti](https://github.com/getzep/graphiti) (by Zep) would deliver better memory quality.

This is the highest-stakes architectural decision before Slice 3. It determines whether the memory system needs a major backend migration or can build incrementally on the current foundation.

### Current Seshat Implementation

**Schema:** Three node types (`Turn`, `Session`, `Entity`) connected by four relationship types (`DISCUSSES`, `CONTAINS`, `NEXT`, plus 6 domain types like `USES`, `PART_OF`).

**Key capabilities:**
- 7-stage entity extraction pipeline via LLM (currently `gpt-4.1-nano`)
- Episodic→semantic promotion with stability scoring
- Multi-factor relevance scoring (recency 0–0.4, entity match 0–0.4, importance 0–0.2)
- Session graph linking with sequential `NEXT` chains
- `MemoryProtocol` abstraction layer (Slice 1) enabling backend substitution
- 990 semantic entities in production graph

**Known weaknesses (from ADR-0018, EVAL-02, EVAL-03):**
- No fuzzy entity matching — exact-name `MERGE` creates near-duplicate explosion
- No embedding-based search — keyword/name matching only
- Stability score prevents organic promotion (requires 50 mentions or 90 days)
- No contradiction detection or lifecycle management
- No proactive memory surfacing
- Cross-session recall unvalidated

### Experiment Data Available

**EVAL-02 (FRE-147):** Graphiti vs Seshat across 6 scenarios (episodic retrieval, semantic consolidation, temporal queries, entity dedup, consolidation lifecycle, scaling to 500 episodes). Two LLM providers A/B tested (OpenAI gpt-4.1-mini/nano, Anthropic claude-haiku-4-5). Full report: `docs/research/GRAPHITI_EXPERIMENT_REPORT.md`.

**EVAL-03 (FRE-148):** Promotion pipeline quality evaluation. 22 seeded entities across 5 scenarios. Full report: `docs/research/EVAL_03_MEMORY_PROMOTION_REPORT.md`.

---

## Decision

**Option C: Enhanced Seshat** — Keep the current Neo4j backend and add the two specific capabilities that drive Graphiti's quality advantage: embedding-based search and fuzzy entity deduplication.

---

## Options Evaluated

### Option A: Keep Current Neo4j Schema (No Changes)

Keep Seshat exactly as-is. Accept current quality limitations and focus Slice 3 effort elsewhere.

**Pros:**
- Zero migration effort
- Proven stability: flat 4ms query latency, 7ms ingestion at 500 episodes
- Full control over schema and promotion pipeline
- `MemoryProtocol` abstraction already in place

**Cons:**
- Entity deduplication is broken: 40 mentions of 10 entities → 500 nodes (EVAL-02 Scenario 4)
- No semantic search: keyword matching misses entities Graphiti finds (0% vs 70% precision in EVAL-02 Scenario 1, though Seshat's 0% was partly a measurement artifact)
- Promotion threshold prevents organic use (EVAL-03 Finding 1)
- Proactive memory (Slice 3 core feature) requires semantic retrieval capabilities that don't exist

**Verdict:** Insufficient for Slice 3. The entity dedup problem alone means the knowledge graph degrades with use rather than improving.

### Option B: Adopt Graphiti

Replace Seshat's Neo4j backend with Graphiti. Reimplement `MemoryProtocol` against Graphiti's API.

**Pros:**
- Perfect entity deduplication: 40 mentions → 10 canonical entities (3-tier: vector + BM25 + LLM)
- 70% precision/recall on entity retrieval via embedding search
- Bi-temporal model (`valid_at`/`invalid_at`) enables point-in-time queries
- Single `add_episode()` call replaces multi-step consolidation pipeline
- Auto-extracts facts with good quality ("User uses Neo4j for their knowledge graph")
- Active open-source project with growing community

**Cons:**
- **Ingestion latency is prohibitive:** 8–10s per episode (LLM calls for extraction + dedup per episode) vs Seshat's 1–7ms — a 1000x slowdown. A conversational agent cannot block for 10s after each turn.
- **Less lifecycle control:** `add_episode()` is a black box — extraction, dedup, and edge creation happen atomically. No selective promotion, no deferred consolidation, no working→episodic→semantic staging.
- **Dependency risk:** Young framework (v0.28.2), opinionated schema, API evolution unpredictable. Locks memory system to Graphiti's data model.
- **Token cost opacity:** `token_tracker` returned zeros in all runs. Cost modeling requires external estimation (~$0.50–2.00 per 500 episodes with gpt-4.1-mini).
- **Loses existing 990-entity graph:** Migration path unclear — Graphiti's schema differs significantly.
- **Integration effort:** High. Must reimplement `MemoryProtocol`, `SecondBrainConsolidator`, promotion pipeline, all query patterns, and telemetry hooks.

**Scored comparison (from EVAL-02):**

| Dimension | Weight | Seshat | Graphiti | Notes |
|-----------|--------|--------|----------|-------|
| Episodic retrieval quality | 20% | 2 | 4 | Embedding search finds what keyword misses |
| Semantic consolidation quality | 20% | 3 | 3 | Both produce reasonable results |
| Temporal query capability | 15% | 2 | 4 | Bi-temporal model architecturally superior |
| Entity deduplication | 15% | 1 | 5 | Graphiti perfect; Seshat has no fuzzy dedup |
| Consolidation lifecycle fit | 15% | 3 | 4 | Graphiti simpler; Seshat gives more control |
| Performance + cost at scale | 15% | 5 | 2 | Seshat 1000x faster ingestion |
| **Weighted Score** | | **2.65** | **3.65** | |

**Verdict:** Quality wins are real but attributable to two specific capabilities (embeddings, dedup) rather than the framework as a whole. The 1000x ingestion penalty and loss of lifecycle control make full adoption impractical for a real-time conversational agent.

### Option C: Enhanced Seshat (Selected)

Keep Seshat's Neo4j backend, `MemoryProtocol`, and explicit promotion pipeline. Add the two capabilities that account for Graphiti's quality advantage.

**Enhancement 1 — Embedding vectors on Entity and Turn nodes:**
- Store embeddings from `text-embedding-3-small` (or local `nomic-embed-text`) alongside existing nodes
- Enable Neo4j vector index for similarity search
- Replace keyword-only `query_memory()` with hybrid search (vector + keyword + graph traversal)
- Expected impact: closes the 0% → 70% retrieval precision gap from EVAL-02 Scenario 1

**Enhancement 2 — Fuzzy entity deduplication:**
- Two-tier dedup on entity creation: (a) vector similarity check against existing entities, (b) LLM-assisted merge for close matches
- Directly addresses the 500 → 10 dedup gap from EVAL-02 Scenario 4
- Can be async/batched to avoid ingestion latency hit

**Enhancement 3 — Bi-temporal fields on relationship edges:**
- Add `valid_at`/`invalid_at` to edges (inspired by Graphiti's temporal model)
- Enables point-in-time queries for Slice 3 proactive memory
- Low-effort schema addition — no migration, just new optional properties

**Enhancement 4 — Promotion threshold redesign:**
- Current formula requires 50 mentions or 90 days — prevents organic promotion (EVAL-03)
- Three options to evaluate in Slice 3: recency boost, relative top-N, lower min_mentions to 3–5
- No code change needed now — `min_mentions` parameter already exists

**Pros:**
- Preserves 990-entity production graph — no migration
- Keeps sub-10ms ingestion latency (critical for real-time agent)
- Keeps explicit promotion pipeline and lifecycle control
- `MemoryProtocol` unchanged — enhancements are internal to `MemoryService`
- Cherry-picks the two highest-value capabilities from Graphiti
- Incremental: each enhancement can be shipped, tested, and evaluated independently
- No new framework dependency

**Cons:**
- Must build embedding infrastructure and dedup logic ourselves
- Won't get Graphiti's future improvements (community, new features)
- Embedding storage increases Neo4j memory footprint (~4KB per 1024-dim vector)
- LLM-assisted dedup adds per-entity cost (mitigated by batching and caching)

**Integration effort:** Medium. All changes are additive to existing `MemoryService`:
- Vector index: Neo4j 5.x native vector index + embedding generation pipeline
- Fuzzy dedup: New method in `MemoryService.create_entity()` flow
- Bi-temporal: Schema extension on existing relationship creation
- No changes to `MemoryProtocol`, `MemoryServiceAdapter`, or `SecondBrainConsolidator` interfaces

**Verdict:** Best risk/reward ratio. Gets ~80% of Graphiti's quality advantage at ~20% of the integration cost, with no performance penalty and no framework lock-in.

---

## Impact on MemoryProtocol / MemoryService Interfaces

### No breaking changes to MemoryProtocol

The `MemoryProtocol` abstract interface remains unchanged. All enhancements are internal to `MemoryService`:

| Method | Change |
|--------|--------|
| `recall()` | Internal: query uses vector similarity in addition to keyword match |
| `recall_broad()` | Internal: entity ranking uses embedding similarity |
| `store_episode()` | Internal: generates embedding on store |
| `promote()` | No change |
| `is_connected()` | No change |

### MemoryService additions

| New capability | Method affected | Nature of change |
|----------------|-----------------|-------------------|
| Embedding generation | `create_conversation()`, `create_entity()` | Generate and store embedding vector on write |
| Vector search | `query_memory()`, `query_memory_broad()` | Hybrid search: vector + keyword + graph |
| Fuzzy dedup | `create_entity()` | Check vector similarity before MERGE |
| Bi-temporal edges | `create_relationship()` | Add optional `valid_at`/`invalid_at` properties |

### New dependencies

| Dependency | Purpose | Alternative |
|------------|---------|-------------|
| `text-embedding-3-small` (OpenAI) | Embedding generation | `nomic-embed-text` (local, via MLX) |
| Neo4j vector index | Similarity search | Already supported in Neo4j 5.x |

---

## Consequences for Slice 3 Design

### Enables

1. **Proactive memory surfacing** — Embedding-based recall can find relevant entities even when the user doesn't name them explicitly. This is the foundation for Slice 3's proactive memory feature.
2. **Entity-aware context assembly** — Fuzzy dedup means the knowledge graph accurately represents the user's mental model rather than accumulating noise.
3. **Temporal reasoning** — Bi-temporal edges enable "what was true at time T?" queries for self-improvement and pattern detection.
4. **Promotion threshold redesign** — With embeddings, promotion can factor in semantic similarity to existing semantic entities, not just mention count and time span.

### Constrains

1. **Embedding model dependency** — Slice 3 must decide: cloud embeddings (OpenAI, lower latency) vs local embeddings (nomic-embed-text via MLX, no API cost). Can be configured per-environment.
2. **Dedup pipeline ordering** — Entity dedup must run before relationship creation. This may require restructuring `SecondBrainConsolidator.consolidate_recent_captures()` to batch entity creation with dedup checks before relationship wiring.
3. **Neo4j memory footprint** — 990 entities × 4KB embedding ≈ 4MB. At 10K entities: 40MB. Manageable but should be monitored.
4. **Cross-session recall validation** — EVAL-03 identified this as a critical gap. Must be validated after embedding search is implemented, before Slice 3 proactive memory can be considered reliable.

### Does not affect

- Captain's Log / insights engine
- Delegation framework
- Pre-LLM Gateway pipeline
- Brainstem scheduling
- MCP tool infrastructure

---

## Implementation Priority for Slice 3

| Priority | Enhancement | Rationale |
|----------|------------|-----------|
| P0 | Embedding vectors + vector search | Prerequisite for all quality improvements; closes biggest gap |
| P1 | Fuzzy entity deduplication | Second biggest quality gap; prevents graph degradation |
| P2 | Geospatial context as retrieval dimension | Bio-inspired: spatial context is a core memory retrieval cue (see below) |
| P3 | Promotion threshold redesign | Enables organic promotion; required for proactive memory |
| P4 | Bi-temporal edge fields | Enables temporal queries; lower urgency until proactive memory ships |

### P2 Rationale: Geospatial as a Core Retrieval Dimension

Elevated from P4 based on neuroscience alignment with the project's biologically-inspired architecture.

**Why spatial context is foundational, not optional:**

The hippocampus — the brain's episodic memory center — is also its spatial navigation center. This is not coincidental. Place cells (O'Keefe, 1971; Nobel 2014) and grid cells (Moser & Moser, 2005; Nobel 2014) demonstrate that spatial representation is the *scaffolding* on which episodic memory was evolutionarily built. The spatial scaffolding hypothesis holds that the hippocampal spatial system was repurposed for episodic memory — space came first, memory was built on top of it.

Key biological parallels:
- **Context-dependent recall:** Memories are strongly bound to the location where they formed. Returning to a place triggers recall (environmental reinstatement effect).
- **Multi-dimensional indexing:** The brain retrieves memories via time, entity co-occurrence, *and* spatial context simultaneously — not as separate query dimensions.
- **Method of loci:** The oldest mnemonic technique (ancient Greece) works precisely because the brain naturally indexes memories spatially.

**For Seshat, this means:** Location is a retrieval cue on par with recency and entity matching — not just a queryable attribute for a future mobile client. Memories formed "in Paris" or "while working on the Lyon project" should be retrievable by spatial proximity, just as they are by entity name or time window.

**Implementation approach:**

Neo4j natively supports `Point` types and spatial indexes. Location entities are already extracted (`entity_type="Location"`) and the `LOCATED_IN` relationship type exists. Geospatial search is architecturally parallel to vector search — both are index-backed similarity queries on node properties.

| Component | Description | Dependency |
|-----------|-------------|------------|
| Schema fields | `coordinates: Point \| null` + `geocoded: bool` on Entity nodes | Add during P0 embedding work |
| Geocoding pipeline | Resolve Location entity names → lat/lon coordinates | Lightweight; free geocoding APIs available |
| Spatial index | Neo4j native spatial index on `coordinates` | Add alongside vector index in P0 |
| Proximity queries | `MemoryService` methods for "entities near X" | After spatial index exists |
| Retrieval integration | Spatial proximity as a factor in `_calculate_relevance_scores()` | After proximity queries work |

**Schema fields to add during P0** (nullable, zero cost):

| Field | Node | Type | Purpose |
|-------|------|------|---------|
| `coordinates` | Entity | `Point \| null` | Lat/lon for Location entities; null for others |
| `geocoded` | Entity | `bool` | Distinguishes "not yet geocoded" from "not a location" |

The geocoding pipeline and spatial queries follow in P2 proper. Adding the fields during P0 avoids a future schema migration.

---

## References

- EVAL-02 Graphiti Experiment Report: `docs/research/GRAPHITI_EXPERIMENT_REPORT.md`
- EVAL-03 Memory Promotion Report: `docs/research/EVAL_03_MEMORY_PROMOTION_REPORT.md`
- Evaluation Phase Guide: `docs/guides/EVALUATION_PHASE_GUIDE.md`
- Seshat Memory ADR: `docs/architecture_decisions/ADR-0018-seshat-memory-librarian-agent.md`
- Session Graph Model: `docs/architecture_decisions/ADR-0024-session-graph-model.md`
- Cognitive Architecture Redesign v2: `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` §5
- Graphiti (Zep): https://github.com/getzep/graphiti
