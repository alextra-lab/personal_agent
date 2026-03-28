# Graphiti Experiment Report — EVAL-02

**Date:** 2026-03-28
**Linear Issue:** FRE-147
**Spec:** `docs/superpowers/specs/2026-03-28-graphiti-experiment-design.md`

---

## Executive Summary

Graphiti (by Zep) was compared against the current hand-built Seshat Neo4j backend across 6 scenarios covering episodic retrieval, semantic consolidation, temporal queries, entity deduplication, consolidation lifecycle, and scaling. Two LLM configurations were A/B tested: OpenAI (gpt-4.1-mini / gpt-4.1-nano) and Anthropic (claude-haiku-4-5).

**Key Finding:** Graphiti dramatically outperforms Seshat on **quality** (entity dedup, retrieval precision) but is **1000x slower on ingestion** due to per-episode LLM calls. The quality advantage comes primarily from **embedding-based search and three-tier entity deduplication**, not the framework itself.

**Recommendation:** **Keep Seshat + add embeddings.** Graphiti's quality wins are attributable to vector search and LLM-assisted dedup — capabilities we can add to Seshat without adopting the full framework. Graphiti's ingestion latency (~8-10s/episode) is prohibitive for real-time use.

---

## Infrastructure

- **Seshat Neo4j:** bolt://localhost:7687 (existing production instance)
- **Graphiti Neo4j:** bolt://localhost:7688 (isolated experiment container, Neo4j 5.26-community)
- **Embedder:** text-embedding-3-small (OpenAI, 1024 dims) — used by both Graphiti configs
- **Test Data:** 50 real episodes from telemetry + 500 synthetic episodes for scaling
- **Graphiti version:** graphiti-core 0.28.2

### LLM Configurations

| Config | Medium Model | Small Model | Pricing (input/output per MTok) |
|--------|-------------|-------------|----------------------------------|
| OpenAI | gpt-4.1-mini | gpt-4.1-nano | $0.40/$1.60 (mini), $0.10/$0.40 (nano) |
| Anthropic | claude-haiku-4-5 | claude-haiku-4-5 | $1.00/$5.00 |

**Note:** Token tracking via `graphiti.token_tracker` returned zeros for all runs. Cost estimates below are based on observed ingestion times and typical token-per-episode rates from Graphiti's documentation (~2-4K tokens/episode).

---

## Results by Scenario

### Scenario 1: Episodic Memory — Store + Retrieve

| Metric | Seshat | Graphiti (OpenAI) | Graphiti (Anthropic) |
|--------|--------|-------------------|---------------------|
| Ingest p50 (ms) | 1.3 | 9,549 | 8,224 |
| Ingest p95 (ms) | 10.5 | 21,533 | 14,025 |
| Query p50 (ms) | 2.0 | 357 | 326 |
| Query p95 (ms) | 23.2 | 394 | 357 |
| Avg Precision | 0.0* | 0.7 | 0.7 |
| Avg Recall | 0.0* | 0.7 | 0.7 |

**Graphiti per-entity retrieval (both configs identical):**

| Entity Query | Found? |
|-------------|--------|
| Neo4j | Yes |
| Claude Code | Yes |
| Python | Yes |
| Elasticsearch | Yes |
| Machine Learning | Yes |
| FastAPI | Yes |
| Docker | Yes |
| Graphiti | No |
| Memory Consolidation | No |
| Cognitive Architecture | No |

*Seshat's 0% precision/recall is a measurement artifact: Seshat's keyword-based `query_memory()` was queried by canonical entity name, but the real episodes loaded from telemetry don't tag entities by those exact canonical names. Seshat did return results (2-20 per query) — they just couldn't be matched to expected IDs. This highlights that **Seshat lacks fuzzy entity matching**, which is exactly what Graphiti's embedding search provides.

**Analysis:** Graphiti found 7/10 entities with perfect precision — the 3 misses ("Graphiti", "Memory Consolidation", "Cognitive Architecture") are more abstract/compound concepts that Graphiti's extraction didn't isolate as named entities. Both OpenAI and Anthropic produced identical retrieval quality, suggesting the embedder (shared) drives search quality, not the LLM.

### Scenario 2: Semantic Memory — Consolidation Quality

| Metric | Seshat | Graphiti (OpenAI) | Graphiti (Anthropic) |
|--------|--------|-------------------|---------------------|
| Entities found | 100 | 10 | 10 |
| Facts extracted | N/A | 10 | 10 |

**Seshat top entities** (from existing graph, pre-populated with Sonnet extraction): Python (32 mentions), PostgreSQL (29), Kubernetes (21), Neo4j (20), Paris (17), Redis (14), AWS (14), Docker Compose (13), GraphQL (11), Elasticsearch (11).

**Graphiti entities** (auto-extracted by search query): Generic concepts like "entities", "component", "dependencies", "Type hints", "REST", "sessions". Lower quality — Graphiti's wildcard search (`*`) returned generic nodes rather than meaningful domain entities.

**Analysis:** The entity count comparison is misleading. Seshat's 100 entities come from the full production graph (built with Sonnet). Graphiti's `search_("*")` only returns the top-10 search results, not all entities in the graph. Graphiti auto-extracts entities and facts during `add_episode()` — the extracted facts show good quality (e.g., "User uses Neo4j for their knowledge graph", "Neo4j uses a labeled property graph model"). The limitation is in our measurement approach, not Graphiti's extraction.

### Scenario 3: Temporal Queries

| Metric | Seshat | Graphiti (OpenAI) | Graphiti (Anthropic) |
|--------|--------|-------------------|---------------------|
| Query p50 (ms) | 3.6 | 8.0 | 8.5 |
| 7-day results | 50 conversations | 50 episodes + 10 edges | 50 episodes + 10 edges |
| 14-day results | 50 conversations | 50 episodes + 10 edges | 50 episodes + 10 edges |
| 30-day results | 50 conversations | 50 episodes + 10 edges | 50 episodes + 10 edges |

**Analysis:** Both backends retrieved all episodes regardless of time window — our test data spans < 30 days, so all windows return full results. Graphiti's bi-temporal model (`valid_at`/`invalid_at` on edges) is architecturally superior for point-in-time queries ("what was true on March 15?"), but our test didn't exercise this differentiation. Seshat only supports `recency_days` filtering. Graphiti also surfaced temporal edge search results alongside episodes.

### Scenario 4: Entity Deduplication

| Metric | Seshat | Graphiti (OpenAI) | Graphiti (Anthropic) |
|--------|--------|-------------------|---------------------|
| Raw mentions | 40 | 40 | 40 |
| Unique entities created | 500 | 10 | 10 |
| Expected canonical | 10 | 10 | 10 |
| Dedup ratio | 12.5x | 0.25x | 0.25x |
| False negatives | 490 | 0 | 0 |
| Ingest p50 (ms) | 1.4 | 6,766 | 5,786 |

**This is the most decisive scenario.** Graphiti achieved **perfect deduplication** — 40 variations of 10 entities (e.g., "Neo4j"/"neo4j"/"Neo4J", "ML"/"machine learning") correctly resolved to exactly 10 canonical entities with zero false positives or false negatives.

Seshat created 500 unique entities (including pre-existing entities from the production graph) because it relies on exact-name `MERGE` — any spelling/casing variation creates a new node.

**Both LLM configs produced identical dedup quality.** Anthropic was ~15% faster on ingestion.

### Scenario 5: Consolidation Lifecycle

| Metric | Seshat | Graphiti (OpenAI) | Graphiti (Anthropic) |
|--------|--------|-------------------|---------------------|
| Store p50 (ms) | 1.1 | N/A | N/A |
| Promote time (ms) | 32.6 | N/A | N/A |
| Ingest p50 (ms) | N/A | 18,695 | 9,055 |
| Entities created | 5 (promoted) | 10 | 10 |
| Facts created | N/A | 10 | 10 |

**Seshat lifecycle:** Store episode (1ms) → Build promotion candidates → Run promotion pipeline (33ms) → Entities gain `memory_type='semantic'`. Total: ~35ms for 10 episodes + promotion.

**Graphiti lifecycle:** `add_episode()` handles extraction + dedup + edge creation in one call (~9-19s/episode). No separate promotion step. Entities immediately searchable.

**Analysis:** Graphiti's single-call lifecycle is simpler (fewer code paths, no scheduler needed) but 500x slower per episode. Seshat's explicit promotion pipeline gives more control over when and what gets promoted. For a real-time agent, Seshat's approach is more practical — Graphiti's ~10s/episode ingestion would block conversation flow.

### Scenario 6: Scaling (500 episodes)

| Checkpoint | Seshat Ingest (ms) | Graphiti OpenAI Ingest (ms) | Graphiti Anthropic Ingest (ms) |
|------------|-------|---------|----------|
| 100 | 8.5 | 9,296 | 7,580 |
| 250 | 7.5 | 9,822 | 7,662 |
| 500 | 7.5 | 9,657 | 7,484 |

| Checkpoint | Seshat Query p50 | Graphiti OpenAI Query p50 | Graphiti Anthropic Query p50 |
|------------|---------|---------|----------|
| 100 | 5.6ms | 174ms | 184ms |
| 250 | 4.2ms | 187ms | 187ms |
| 500 | 4.2ms | 201ms | 192ms |

**Scaling characteristics:**
- **Seshat:** Flat ingestion (~7.5ms), flat query latency (~4ms). No degradation at 500 episodes.
- **Graphiti OpenAI:** ~9.6s/episode ingestion (consistent), query latency grows 174→201ms (15% increase over 5x data).
- **Graphiti Anthropic:** ~7.5s/episode ingestion (20% faster than OpenAI), query latency 184→192ms (minimal growth).
- **Total ingestion time for 500 episodes:** Seshat ~3.7s, Graphiti OpenAI ~80min, Graphiti Anthropic ~62min.

---

## A/B Comparison: OpenAI vs Anthropic

| Dimension | OpenAI (gpt-4.1-mini/nano) | Anthropic (claude-haiku-4-5) | Winner |
|-----------|---------------------------|------------------------------|--------|
| Retrieval precision | 0.7 | 0.7 | Tie |
| Retrieval recall | 0.7 | 0.7 | Tie |
| Dedup quality | Perfect (10/10) | Perfect (10/10) | Tie |
| Ingest speed (p50) | 9,549ms | 8,224ms | Anthropic (-14%) |
| Ingest speed @ scale | 9,657ms | 7,484ms | Anthropic (-22%) |
| Query latency | 357ms | 326ms | Anthropic (-9%) |
| Dedup ingest speed | 6,766ms | 5,786ms | Anthropic (-14%) |
| Cost per MTok (medium) | $0.40/$1.60 | $1.00/$5.00 | OpenAI (-60%) |

**Quality is identical** between the two LLM providers. The embedder (OpenAI text-embedding-3-small, shared by both) drives search quality. Anthropic Haiku is consistently 14-22% faster but 2.5x more expensive per token.

**Recommendation for Graphiti LLM:** If adopting Graphiti, use **OpenAI gpt-4.1-mini/nano** — same quality at significantly lower cost. The speed advantage of Haiku doesn't justify the price premium.

---

## Secondary Outcome: Entity Extraction Model Downgrade

The current production entity extraction uses **claude-sonnet** ($3/$15 per MTok). This experiment shows:

- **gpt-4.1-mini** ($0.40/$1.60): Produced quality entity extraction and perfect dedup within Graphiti's framework
- **claude-haiku-4-5** ($1.00/$5.00): Identical quality to gpt-4.1-mini in this context

Both cheaper models produced good results for Graphiti's structured extraction prompts. However, this doesn't directly test Seshat's extraction pipeline (which uses different prompts). **Recommended next step:** Run Seshat's `entity_extraction.py` with gpt-4.1-mini and Haiku against the same 50 episodes and compare entity quality against current Sonnet output. If comparable, update `config/models.yaml` to downgrade `entity_extraction_role`.

---

## Scoring Matrix

| Dimension | Weight | Seshat | Graphiti | Notes |
|-----------|--------|--------|----------|-------|
| Episodic retrieval quality | 20% | 2 | 4 | Graphiti's embedding search finds entities Seshat's keyword search misses |
| Semantic consolidation quality | 20% | 3 | 3 | Both produce reasonable results; Graphiti auto-extracts but measurement was limited |
| Temporal query capability | 15% | 2 | 4 | Graphiti's bi-temporal model is architecturally superior (though not fully exercised in test) |
| Entity deduplication | 15% | 1 | 5 | Graphiti: perfect. Seshat: no fuzzy dedup at all |
| Consolidation lifecycle fit | 15% | 3 | 4 | Graphiti's single-call model is simpler; Seshat's explicit pipeline gives more control |
| Performance + cost at scale | 15% | 5 | 2 | Seshat is 1000x faster; Graphiti requires LLM calls per episode |
| **Weighted Score** | | **2.65** | **3.65** | |

---

## Recommendation

### Decision: **Keep Seshat + Add Embeddings** (with elements of Hybrid)

Graphiti wins on quality (3.65 vs 2.65), but the analysis reveals the quality advantage is attributable to two specific capabilities, not the framework as a whole:

1. **Embedding-based search** — Graphiti's hybrid search (semantic + BM25 + graph) finds entities that Seshat's keyword-based queries miss. This is the #1 value-add.

2. **Three-tier entity deduplication** — Vector similarity + BM25 + LLM reasoning produces perfect dedup. Seshat's exact-name MERGE creates explosion of near-duplicate entities.

These capabilities can be added to Seshat without adopting Graphiti's framework:

### Recommended Seshat Enhancements

1. **Add embedding vectors to Entity and Turn nodes** — Store embeddings from text-embedding-3-small (or a local model like nomic-embed-text) alongside existing nodes. Enable Neo4j vector index for similarity search.

2. **Add fuzzy entity dedup** — Implement a two-tier dedup on entity creation: (a) vector similarity check against existing entities, (b) LLM-assisted merge decision for close matches. This directly addresses the 500→10 dedup gap.

3. **Add bi-temporal fields** — Add `valid_at`/`invalid_at` to relationship edges. This enables point-in-time queries for Slice 3 proactive memory.

4. **Keep explicit promotion pipeline** — Seshat's control over when entities get promoted (working→episodic→semantic) is valuable for a real-time agent where 10s/episode ingestion latency is unacceptable.

### Why Not Full Graphiti Adoption

- **Ingestion latency is prohibitive:** 8-10s per episode (LLM calls for extraction + dedup per episode) vs Seshat's 1-7ms. A conversational agent can't block for 10s after each turn.
- **Less control over memory lifecycle:** Graphiti's `add_episode()` is a black box — extraction, dedup, and edge creation happen atomically. Seshat's explicit pipeline allows selective promotion and custom extraction.
- **Dependency risk:** Graphiti is a young framework (v0.28) with an opinionated graph schema. Adopting it locks the memory system to their data model and API evolution.
- **Token cost opacity:** The `token_tracker` didn't report usage, making cost modeling difficult.

### What Graphiti Validates About Our Architecture

- The 3-store memory model (working→episodic→semantic) is sound — Graphiti uses essentially the same model but collapses the boundaries.
- **Embeddings are non-negotiable** for a quality memory system. This should be the #1 priority for Seshat enhancement.
- **Entity dedup needs dedicated effort** — the current name-based MERGE is the weakest link in Seshat.
- The current 6 memory types (WORKING, EPISODIC, SEMANTIC, PROCEDURAL, PROFILE, DERIVED) remain valid but could potentially be simplified. Graphiti's success with fewer explicit types suggests the taxonomy serves organizational rather than functional purposes.

---

## Raw Data

- OpenAI full results: `telemetry/evaluation/graphiti/2026-03-28-12-48-openai.json`
- Anthropic full results: `telemetry/evaluation/graphiti/2026-03-28-14-38-anthropic.json`
- OpenAI report: `telemetry/evaluation/graphiti/2026-03-28-12-48-openai-report.md`
- Anthropic report: `telemetry/evaluation/graphiti/2026-03-28-14-38-anthropic-report.md`

---

## Caveats & Limitations

1. **Seshat retrieval scoring artifact:** Seshat's 0% precision/recall in Scenario 1 is a measurement issue (entity name matching), not a real quality signal. A fairer test would use semantic similarity for result evaluation.

2. **Token tracking broken:** Graphiti's `token_tracker.get_total_usage()` returned zeros. Actual LLM costs are unquantified but estimated at $0.50-2.00 for the full OpenAI run (~550 episodes x ~3K tokens/episode x 2 calls/episode).

3. **Scenario 2 measurement:** `search_("*")` returns top-10 results, not all entities. Graphiti likely extracted many more entities than reported; a Cypher query against the graph would give the true count.

4. **Scenario 3 temporal:** All test data fell within 30 days, so temporal windowing wasn't meaningfully tested. Graphiti's bi-temporal advantage is architectural, not demonstrated.

5. **Seshat ran against production graph:** Seshat's entity counts include pre-existing data from Sonnet extraction, inflating its numbers in Scenario 2 and 4.

---

## Next Steps

1. **FRE-152 (Seshat Backend ADR):** Use these results to inform the ADR. Recommend embedding addition as highest-priority enhancement.
2. **Entity dedup spike:** Prototype two-tier dedup (vector + LLM) in Seshat against the same 10 entity clusters.
3. **Embedding spike:** Add vector index to Neo4j Entity nodes, test retrieval quality vs current keyword approach.
4. **Model downgrade test:** Run Seshat's `entity_extraction.py` with gpt-4.1-mini against 50 episodes, compare to Sonnet baseline.
5. **FRE-153 (Slice 3):** Bi-temporal fields on edges enable proactive memory queries.
