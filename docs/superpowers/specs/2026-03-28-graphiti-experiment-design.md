# Graphiti Experiment Design — EVAL-02

> **EXPERIMENT CLOSED (Apr 2026)**
> Infrastructure (ephemeral Neo4j + harness) archived to
> `scripts/archive/graphiti_experiment/`. Decision recorded in
> `docs/research/GRAPHITI_EXPERIMENT_REPORT.md`: keep Seshat + add embeddings.
> Design rationale below is preserved for architectural archaeology.

**Date:** 2026-03-28
**Linear Issue:** FRE-147
**Status:** Complete — see GRAPHITI_EXPERIMENT_REPORT.md for outcomes
**Spec ref:** COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 5.5, COGNITIVE_AGENT_ARCHITECTURE_v0.1.md Section 5

---

## Purpose

Compare Graphiti (by Zep) against the current hand-built Neo4j Seshat backend across the three architectural memory types (working, episodic, semantic) and the consolidation lifecycle. Determine whether to adopt Graphiti, keep current Neo4j, or pursue a hybrid approach — and whether the current memory type taxonomy needs to evolve.

## Hypothesis

Graphiti may provide a better storage backend for Seshat because it handles entity deduplication (three-tier: vector + BM25 + LLM), relationship extraction, temporal queries (bi-temporal model), and semantic search (embeddings) natively — capabilities the current hand-built Neo4j schema lacks or implements manually.

---

## Infrastructure

### Experiment Neo4j Container

Separate Neo4j instance to isolate Graphiti data from existing Seshat data.

```yaml
# Added to docker-compose.yml (or standalone docker run)
neo4j-experiment:
  image: neo4j:5.26-community
  ports:
    - "7688:7687"    # Bolt
    - "7475:7474"    # Browser (visualize Graphiti's graph)
  environment:
    NEO4J_AUTH: neo4j/graphiti_experiment
  volumes:
    - neo4j_experiment_data:/data
```

### Dependencies (experiment-only)

```
graphiti-core           # Graphiti framework
openai                  # LLM + embeddings for Graphiti
```

Not added to production `pyproject.toml` — installed in experiment virtualenv or as optional extras.

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | Graphiti LLM (gpt-4o-mini) + embeddings (text-embedding-3-small) |
| `ANTHROPIC_API_KEY` | Graphiti A/B test with Haiku 4.5 (already set) |
| `GRAPHITI_TELEMETRY_ENABLED` | Set to `false` — disable Graphiti's built-in telemetry |

The script must set `os.environ['GRAPHITI_TELEMETRY_ENABLED'] = 'false'` before importing graphiti-core.

### LLM Configurations Under Test

Graphiti uses a two-tier model architecture:
- **medium**: Entity extraction, edge extraction, node dedup reasoning
- **small**: Attribute extraction, summaries, edge dedup, contradiction detection

| Config | Medium Model | Small Model | Embedder | Purpose |
|--------|-------------|-------------|----------|---------|
| `graphiti-openai` | gpt-4.1-mini ($0.40/$1.60) | gpt-4.1-nano ($0.10/$0.40) | text-embedding-3-small | Graphiti defaults, native structured output |
| `graphiti-anthropic` | claude-haiku-4-5-latest ($1.00/$5.00) | claude-haiku-4-5-latest | text-embedding-3-small (OpenAI) | A/B quality + cost comparison |
| `seshat-matched` | gpt-4.1-mini | N/A | None | Seshat extraction re-run with same model as Graphiti (fair comparison) |

**Important context on existing data:**

The current Neo4j knowledge graph was built using **claude-sonnet** for entity extraction (configured in `config/models.yaml` as `entity_extraction_role: claude_sonnet`). This is a higher-quality (and more expensive) model than what we'll use for the Graphiti experiment.

To ensure a fair framework-vs-framework comparison, the quality scenarios (1-5) will **re-extract fresh** into both backends using the same model (gpt-4.1-mini), rather than comparing against existing Sonnet-extracted data. The existing production Neo4j data is not touched.

The A/B Anthropic test uses Haiku (not Sonnet) to explore cheaper alternatives. If Haiku quality is comparable to gpt-4.1-mini, this also informs whether to downgrade Seshat's production extraction from Sonnet.

---

## Test Data

### Real Data (quality scenarios)

Source: Evaluation telemetry from `telemetry/evaluation/run-*/` directories.

Parse existing conversation traces to extract user messages, assistant responses, timestamps, and entity references. Target: **50 episodes** covering diverse topics and intent types.

### Synthetic Data (scaling scenario)

Generator function producing realistic multi-topic conversations with:
- Known entities (with deliberate name variations for dedup testing)
- Known temporal references (absolute dates for verification)
- Known relationships between entities
- Varying complexity (simple Q&A through multi-turn analysis)

Target: **500+ episodes** with ground truth for precision/recall measurement.

---

## Scenarios

### Scenario 1: Episodic Memory — Store + Retrieve

**Architecture mapping:** Episodic memory (v0.1 Section 5.3) — rapid encoding of experiences with temporal context, similarity-based retrieval.

**What we test:**
- Store 50 conversation episodes in both backends with identical data
- Query by entity name, entity type, and **free-text similarity**
- Compare Seshat's keyword/graph-traversal recall vs Graphiti's hybrid search (semantic embeddings + BM25 + graph traversal)

**Key question:** Does embedding-based search (Graphiti) find relevant past experiences that keyword-based search (Seshat) misses?

**Metrics:**
- Retrieval latency (p50, p95)
- Precision (relevant results / total results) — scored against ground truth labels in synthetic data; for real data, binary relevant/not-relevant judgment per result
- Recall (found / expected) — against known ground truth entities and episodes
- Qualitative: Side-by-side comparison of top-5 results for the same query

### Scenario 2: Semantic Memory — Consolidation Quality

**Architecture mapping:** Semantic memory (v0.1 Section 5.4) — consolidated patterns, facts, skills, tool strategies. Plus consolidation pipeline (v0.1 Section 5.5).

**What we test:**
- After ingesting 50 episodes, examine what each backend "knows"
- Seshat: Run current entity extraction (Anthropic) + promotion pipeline. Examine promoted entities.
- Graphiti: Examine auto-extracted entities, edges, and communities after `add_episode`.
- Compare: Which produces richer, more accurate "semantic knowledge"?

**Key questions:**
- Does Graphiti's auto-extraction replace the need for a separate consolidation scheduler?
- Does Graphiti's community structure serve the role of "consolidated patterns"?
- How do extracted facts/relationships compare in quality?
- Does Graphiti's contradiction detection add value over Seshat's append-only model?

**Metrics:**
- Entity count and quality (meaningful entities vs noise)
- Relationship count and accuracy
- Fact accuracy (does the extracted knowledge match what was actually discussed?)
- Coverage: What percentage of important information from conversations is captured?

### Scenario 3: Temporal Queries

**Architecture mapping:** Multi-timescale learning (v0.1 Section 6.1) — immediate, short-term, long-term.

**What we test:**
- Store episodes spanning 30 days with known temporal references
- Queries: "What did I discuss about X last week?", "What was true about Y on March 15?", "How has my understanding of Z evolved?"
- Compare: Seshat's `recency_days` filter + timestamp ordering vs Graphiti's bi-temporal `valid_at`/`invalid_at` model

**Key questions:**
- Can Graphiti answer "what was true at time T?" (point-in-time queries) that Seshat cannot?
- Does Graphiti's contradiction detection correctly invalidate superseded facts?
- How does temporal ordering quality compare?

**Metrics:**
- Result relevance per temporal query (manually scored)
- Temporal ordering correctness
- Contradiction detection accuracy (for queries about changed facts)

### Scenario 4: Entity Deduplication

**Architecture mapping:** Cross-cutting concern affecting all memory types.

**What we test:**
- Store mentions of same entities with name variations:
  - Case: "Neo4j" / "neo4j" / "Neo4J"
  - Abbreviation: "Claude Code" / "claude-code" / "CC"
  - Synonym: "ML" / "machine learning"
  - Misspelling: "Elasticsearch" / "ElasticSearch" / "elastic search"
- Compare: Seshat's name-based MERGE vs Graphiti's three-tier resolution

**Key question:** How many false entities does each system create from the same underlying concepts?

**Metrics:**
- Raw mentions vs unique entities created (dedup ratio)
- False positives (incorrectly merged distinct entities)
- False negatives (failed to merge same entity)
- LLM cost per dedup operation

### Scenario 5: Consolidation Lifecycle

**Architecture mapping:** Full working → episodic → semantic flow (v0.1 Sections 5.2-5.5).

**What we test:**
- Simulate the full memory lifecycle:
  1. Working memory: Active conversation context (in-memory, not graph-stored — baseline)
  2. Episodic encoding: Store conversation as episode
  3. Consolidation trigger: After N episodes, run consolidation
  4. Semantic integration: Promote/extract durable knowledge
  5. Retrieval: Query semantic layer for consolidated facts
- Compare how naturally each backend supports this lifecycle

**Key questions:**
- Seshat requires explicit `promote()` calls — does Graphiti's auto-extraction on `add_episode` make the separate consolidation step unnecessary?
- Is Graphiti's "immediate extraction" better or worse than Seshat's "batch consolidation"?
- Do the current 6 memory types (WORKING, EPISODIC, SEMANTIC, PROCEDURAL, PROFILE, DERIVED) map to Graphiti's model? Which are valuable, which should evolve?

**Metrics:**
- Steps required to complete full lifecycle
- Code complexity comparison
- Knowledge quality at each stage
- Qualitative: Which model better matches the cognitive architecture vision?

### Scenario 6: Scaling

**Architecture mapping:** System viability under load.

**What we test:**
- Bulk populate both backends with synthetic data
- Measure at 100, 250, 500 episode marks
- Track ingestion throughput and query latency degradation

**Metrics:**
- Ingestion time per episode (including LLM calls)
- Query latency at each scale point (p50, p95)
- LLM API cost per episode (tokens used, estimated USD)
- Memory/resource usage

---

## Script Design

### File

`scripts/graphiti_experiment.py` — single rerunnable script.

### CLI Interface

```
python scripts/graphiti_experiment.py \
  --llm openai                              # or "anthropic" or "both"
  --scenarios 1,2,3,4,5,6                   # which to run (default: all)
  --episodes 50                             # quality test episode count
  --scale-episodes 500                      # scaling test episode count
  --output telemetry/evaluation/graphiti/   # results output dir
  --neo4j-uri bolt://localhost:7687         # existing Seshat
  --graphiti-neo4j-uri bolt://localhost:7688 # experiment container
```

### Output

Each run produces:

1. **`telemetry/evaluation/graphiti/YYYY-MM-DD-HH-MM-<llm>.json`** — full metrics per scenario
2. **Console summary** — comparison table
3. **`telemetry/evaluation/graphiti/report-fragment.md`** — markdown ready to paste into `docs/research/GRAPHITI_EXPERIMENT_REPORT.md`

### Result Schema

```json
{
  "run_id": "2026-03-28-14-30-openai",
  "config": {
    "llm": "openai",
    "llm_model": "gpt-4o-mini",
    "embedder": "text-embedding-3-small",
    "episodes": 50,
    "scale_episodes": 500
  },
  "scenarios": {
    "episodic_retrieval": {
      "seshat": { "latency_p50_ms": 12, "precision": 0.85, "recall": 0.90 },
      "graphiti": { "latency_p50_ms": 18, "precision": 0.92, "recall": 0.95 }
    },
    "semantic_consolidation": { "...": "quality scores" },
    "temporal_queries": { "...": "relevance + ordering scores" },
    "entity_dedup": {
      "seshat": { "raw_mentions": 120, "unique_entities": 95, "dedup_ratio": 0.79 },
      "graphiti": { "raw_mentions": 120, "unique_entities": 42, "dedup_ratio": 0.35 }
    },
    "consolidation_lifecycle": { "...": "lifecycle metrics" },
    "scaling": { "...": "latency at scale points" }
  },
  "cost": {
    "seshat_anthropic_tokens": 15000,
    "graphiti_openai_tokens": 22000,
    "graphiti_embedding_tokens": 8000,
    "estimated_cost_usd": { "seshat": 0.04, "graphiti_openai": 0.03, "graphiti_anthropic": 0.02 }
  }
}
```

---

## Recommendation Framework

### Scoring

Each dimension scored 1-5 for both backends:

| Dimension | Weight | Why |
|-----------|--------|-----|
| Episodic retrieval quality | 20% | Direct user experience — "do you remember X?" |
| Semantic consolidation quality | 20% | Core value of memory system — durable knowledge |
| Temporal query capability | 15% | Required for Slice 3 proactive memory |
| Entity deduplication | 15% | Core weakness of current approach |
| Consolidation lifecycle fit | 15% | How naturally it fits the cognitive architecture |
| Performance + cost at scale | 15% | Practical viability |

### Decision Outcomes

| Outcome | When |
|---------|------|
| **Adopt Graphiti** | Clear wins across most dimensions, integration effort justified |
| **Keep Seshat + add embeddings** | Graphiti's advantage is primarily from vector search, not the framework itself |
| **Hybrid** | Graphiti excels at some memory types, Seshat at others |
| **Keep Seshat as-is** | Graphiti doesn't justify the dependency and complexity |
| **Evolve memory types** | Experiment reveals current 6-type taxonomy doesn't match how memory actually works |

### Key Insight to Capture

If Graphiti wins, attribute the improvement: is it the **framework** (extraction, dedup, lifecycle management) or the **embeddings/vector search** that our implementation lacks? This directly informs whether we adopt Graphiti or just add embeddings to Seshat.

---

### Secondary Outcome: Entity Extraction Model Downgrade

Independent of the Graphiti decision, this experiment produces quality data on entity extraction at three model tiers:

| Model | Current Role | Cost (input/output per MTok) |
|-------|-------------|------------------------------|
| claude-sonnet | Production extraction (`models.yaml: entity_extraction_role`) | ~$3.00/$15.00 |
| gpt-4.1-mini | Experiment medium tier | $0.40/$1.60 |
| claude-haiku-4-5 | Experiment A/B | $1.00/$5.00 |

If gpt-4.1-mini or Haiku produce comparable extraction quality to Sonnet, update `config/models.yaml` to downgrade `entity_extraction_role` (and potentially `captains_log_role` and `insights_role`). This is an immediate cost win regardless of the Graphiti decision.

**Comparison method:** For the same 50 episodes, compare entity counts, entity quality (meaningful vs noise), relationship accuracy, and dedup behavior across all three models. Report as a side-by-side table in the experiment report.

---

## Post-Experiment

1. Fill in `docs/research/GRAPHITI_EXPERIMENT_REPORT.md` with quantitative results and qualitative observations
2. Write recommendation with clear rationale
3. Results feed into Seshat backend ADR (FRE-152) and Slice 3 priorities (FRE-153)
4. If Graphiti adopted: separate evaluation of Kuzu as alternative graph backend
5. If extraction model downgrade justified: update `config/models.yaml` roles

---

## References

- [Graphiti GitHub](https://github.com/getzep/graphiti)
- [Graphiti paper (arXiv)](https://arxiv.org/html/2501.13956v1)
- Cognitive Architecture v0.1: `docs/architecture/COGNITIVE_AGENT_ARCHITECTURE_v0.1.md` (Section 5)
- Cognitive Architecture Redesign v2: `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` (Section 5.5)
- Current MemoryProtocol: `src/personal_agent/memory/protocol.py`
- Current MemoryService: `src/personal_agent/memory/service.py`
- Entity extraction: `src/personal_agent/memory/entity_extraction.py`
- Promotion pipeline: `src/personal_agent/memory/promote.py`
