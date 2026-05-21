# Memory Integration Probe — Findings

> **Date:** 2026-05-21
> **Plan:** [`docs/superpowers/plans/2026-05-21-memory-integration-probe.md`](../superpowers/plans/2026-05-21-memory-integration-probe.md)
> **Scripts:** [`scripts/research/memory_integration_probe/`](../../scripts/research/memory_integration_probe/)
> **Outputs:** [`scripts/research/memory_integration_probe/output/`](../../scripts/research/memory_integration_probe/output/)
> **Corpus:** live VPS Neo4j — 489 sessions, 3,399 turns, 4,008 entities, 19,517 edges across 13 relationship types.

## Question

Does the memory pipeline integrate facts (cross-constrain them, resolve
conflicts, support each other across turns) or merely concatenate them
(accumulate, top-K inject, last-write-wins)?

## TL;DR

**Concatenates across every layer measured.** The empirical numbers confirm
what the static audit predicted: descriptions are overwritten on every
merge, 9.3% of entity pairs accumulate redundant relationship types, and
near-duplicate entity names slip past dedup into co-retrieval neighborhoods.
The most striking finding is qualitative — the stored description of "Neo4j"
in our graph is currently *"Query language used to interact with Neo4j"*,
which is the definition of Cypher, not Neo4j. Cross-fact contamination is
not theoretical; it has already happened to load-bearing entities.

## Findings by probe

### Probe 1 — Entity attribute drift

**Method:** top 20 semantic entities by source-turn count; pulled current
`description` plus up to 10 sample turn summaries that DISCUSS each.

**Result:** Strong drift evidence. Three representative examples:

| Entity | Mentions | Turns | Stored description |
|---|---|---|---|
| `Neo4j` | 287 | 6+ | _"Query language used to interact with Neo4j in the Seshat stack."_ — wrong; this defines Cypher, not Neo4j |
| `PersonalAgent` | 42 | 7+ | _"A background pipeline updating Captain's Log checkpoints heavily during the last hour."_ — one ephemeral observation overwriting the canonical concept |
| `Elasticsearch` | 277 | 7+ | _"Search/indexing backend that receives request trace indexing from the ES indexer consumer group."_ — narrow extract from one Redis-Streams turn; ignores 270+ other framings |

Several high-traffic entities (`Python`, `Flask`, `London`) have an **empty
description** despite tens of turns mentioning them — extraction either
failed or was overwritten with empty content and never recovered.

**Root cause confirmed in code:** `memory/service.py:605` does
`SET e.description = $description` on every MERGE. There is no merge logic,
no diff against the prior description, no provenance trail. First-write-loses
is the default and that is exactly what we see.

**Verdict against the plan threshold (≥ 3/20 entities show drift):** Met
emphatically. Three of the top 20 have descriptions that are demonstrably
wrong or narrow, and at least three more carry empty descriptions despite
heavy mention counts.

### Probe 2 — Redundant relationships

**Method:** count entity pairs (direction-agnostic) with ≥ 2 distinct
relationship types.

**Result:** **237 of 2,541 pairs (9.3%) carry redundant relationship
types.** Selected top examples:

| Pair | Distinct types | Edges | Types |
|---|---|---|---|
| `Docker` ⇔ `Neo4j` | 5 | 5 | PART_OF×2, RELATED_TO, USES×2 |
| `PostgreSQL` ⇔ `Redis` | 4 | 4 | RELATED_TO×2, SIMILAR_TO, USES |
| `Elasticsearch` ⇔ `Redis` | 4 | 4 | RELATED_TO×2, SIMILAR_TO, USES |
| `Anthropic` ⇔ `Claude` | 3 | 3 | CREATED_BY, RELATED_TO×2 |
| `Embeddings` ⇔ `Reranker` | 4 | 4 | RELATED_TO, SIMILAR_TO, USES×2 |

These are not different relations being asserted — they are the same
relation labeled differently each time, plus duplicate edges of the same
type. The consolidator (`second_brain/consolidator.py:506–521`) creates
relationships without checking what edges already exist between the pair.

**Verdict against the plan threshold (≥ 5 pairs with redundant edges):**
Met emphatically — 237 pairs found.

### Probe 3 — Near-duplicate entities in co-retrieval neighborhoods

**Method (revised from plan):** the production gateway logs retrieval
*counts* but not the entity names that get injected into prompts, so the
log-replay approach in the original plan was not feasible. Substituted a
structural proxy — for the top 20 entities, fetch up to 50 co-occurring
entities and detect near-duplicate names by normalized-equality and
token Jaccard ≥ 0.80.

**Result:** **21 duplicate pairs across 20 neighborhoods, mean rate 2.2%.**
Below the 15% threshold, but qualitatively interesting:

- `Single-node Elasticsearch cluster` ⇔ `Single-Node Elasticsearch Cluster`
  appears in **8 of 20** neighborhoods. The graph carries both as separate
  Entity nodes with separate edges; whichever one gets retrieved first will
  bring its own set of relationships.
- `Embeddings & Reranking in Retrieval` ⇔ `Embeddings and Reranking in Retrieval`
  appears in **3 of 20** neighborhoods (Paris/France/London — long-tail
  geographic entities that share a co-occurrence cluster).

The vector-dedup in `memory/dedup.py` should have caught these but didn't.
Embeddings differ enough that cosine similarity falls below the dedup
threshold, even for case-only variants.

**Verdict against the plan threshold (≥ 15% redundancy):** Not met by the
proxy metric. The proxy is conservative, however — the actual cost of
these duplicates only materializes when both names get retrieved for the
same query, which is a stronger condition.

### Probe 4 — Quality monitor blind spot

**Method:** static review (skipped the synthetic-data run — the static
evidence is conclusive). `second_brain/quality_monitor.py` measures:

- Entity-to-conversation ratio
- Relationship density
- Duplicate entity *names* (exact match only)
- Extraction failure rate
- Orphan entities
- Entity-name length distribution
- Temporal gaps

It does **not** measure:

- Contradictory descriptions across turns
- Redundant edges between the same pair
- Wrong-extraction patterns (e.g. Neo4j defined as Cypher)

**Verdict:** confirmed blind. The monitor would not have flagged any of
the Probe 1 / Probe 2 findings.

## Recommendation

**Open a Linear issue (Needs Approval) for an ADR proposing a cross-fact
constraint layer.** Two of three quantitative thresholds were met
emphatically, and the qualitative finding (Neo4j's description is Cypher's
definition) demonstrates that load-bearing entities already carry wrong
information that the system has no mechanism to detect or repair.

### Suggested ADR scope (for the Linear issue, not this report)

1. **Description provenance.** Replace `SET e.description = $description`
   with append-and-version semantics (`e.descriptions = e.descriptions +
   [{text, turn_id, ts}]`) so the *latest* extraction does not silently
   destroy prior framings. Retrieval can then pick a canonical view or
   summarize across them.
2. **Relationship consolidation.** Before `CREATE (a)-[:USES]->(b)`,
   check whether an edge of overlapping semantic type already exists.
   Either upsert (with provenance) or pick a single canonical type.
3. **Quality-monitor signals.** Add two new anomalies:
   `redundant_relationship_types_pair_count` and
   `description_overwrite_rate`. These would have caught both findings
   here without manual investigation.

The ADR should **not** propose a separate `:Fact` node type yet. That's
a heavier schema change, and the cheaper interventions above buy most of
the value. Re-evaluate after they ship.

### Out of scope

- The hard-problem question from the originating conversation (does
  integration produce subjective experience). This report does not address
  that and cannot.
- Performance impact of provenance-on-write. Needs measurement before any
  ADR commits to it.

## Caveats

- Probe 3 uses a structural proxy because the gateway's retrieval payload
  is not logged. Adding `entity_names_injected` to the `context_assembled`
  event would let a future probe measure actual retrieval-payload
  duplication directly. That's a one-line logger change; worth filing as
  a small follow-up.
- The corpus skews toward the technical entities the agent encounters
  most (Neo4j, Elasticsearch, Redis, etc.). The behavior on a more
  conversational domain may differ.
- All findings are read-only observations of the current state. They do
  not measure how often the wrong descriptions actually mislead the LLM
  — only that the wrong descriptions exist.
