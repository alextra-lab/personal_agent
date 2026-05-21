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

The memory pipeline concatenates rather than integrates across every layer
measured, and the system is causing **four independent, measured harms**
right now:

1. **Token / context-budget waste.** ~370 tokens of memory-section
   overhead emitted on **76.9% of gateway turns** (1,343 / 1,747 in 30 days).
   That's ~497k tokens of memory content per month, paid regardless of
   whether the LLM uses it. Self-hosted SLM so the cost is latency +
   context-window pressure, not dollars — still a constant tax.
2. **Empty descriptions for the most-mentioned entities.** A current
   broad-recall replay shows top-15 entries like `Paris` (328 mentions),
   `London` (168), `RareLanguage` (166) emitted with no description.
   The system is intended to *help* on what it sees most; on those
   entities it contributes nothing.
3. **Cross-contaminated descriptions on load-bearing entities.** `Neo4j`
   currently described as the definition of Cypher; `Qwen3.5-35B-A3B`
   as schema governance; `Self-Telemetry Query` as Redis pub/sub.
   These reach the prompt verbatim. Either the LLM uses them (wrong
   content out) or ignores them (wasted tokens). Both are losses.
4. **Self-incoherence.** The renderer appends *"Use this list to directly
   answer questions about what the user has previously discussed. Do NOT
   say you have no memory."* — instructing the model to defer to a list
   that is empty or wrong on its top-mention items. The system is actively
   misleading itself.

Substrate-level findings (Probes 1-2): `service.py:605` overwrites the
description on every merge; 237 / 2,541 (9.3%) entity pairs carry redundant
relationship types; the quality monitor is blind to all of the above.

### What the report does NOT establish

It does **not** quantify how often a specific user-visible assistant
response was degraded by a wrong description. That's a refinement, not a
gate — the four harms above hold regardless. A small behavioral probe
(read ~10 recent traces where a malformed entity was in top-15) would
characterize *how* the LLM is responding to the bad input, but is not
needed to justify acting.

### Framing-history note

A first draft of this report overclaimed on substrate evidence alone
("critical / load-bearing / already happened" without measuring impact).
A second draft over-hedged by extending a behavior-probe caveat into the
TL;DR, undercutting the system-level harms that Probe 5 had already
established. This version is calibrated to the four harms actually
measured.

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

### Probe 5 — Impact path measurement (added after first draft)

**Method:** trace whether the malformed descriptions from Probes 1-2
actually reach the LLM prompt. Done by code-reading the retrieval render
(`executor.py:1725-1739`), counting memory-injection frequency in 30 days
of Elasticsearch logs, and replaying the broad-recall query against the
current Neo4j to print the literal memory-section text that would be
emitted into the next prompt.

**Result:**

- **76.9% of gateway turns** in the last 30 days inject memory context
  (1,343 / 1,747).
- Both broad-recall (4.6% of turns) and proactive-memory (72.1% of turns)
  paths emit entries as `type: entity` and flow through the same renderer.
- The render format includes the entity's `description` field verbatim and
  appends *"Use this list to directly answer questions about what the
  user has previously discussed. Do NOT say you have no memory."*
- In a fresh broad-recall replay (limit 20, sliced to top 15):
  - **2 lines misleading** (Neo4j → Cypher's definition; Elasticsearch →
    narrow indexer-only framing). 13.3%.
  - **3 lines empty** (Paris, London, RareLanguage have no description
    despite 166–328 mentions each). 20%.
  - **10 lines adequate** under the strict known-drift heuristic; manual
    re-read suggests at least 3 of those are also cross-contaminated.

**The exact memory section the next `MEMORY_RECALL` turn would emit**
(replayed against current Neo4j):

```
## Your Memory Graph — Known Entities
- [LOCATION] Paris:  (mentioned 328x)
- [Technology] Neo4j: Query language used to interact with Neo4j in the Seshat stack. (mentioned 287x)
- [Technology] Elasticsearch: Search/indexing backend that receives request trace indexing from the ES indexer consumer group. (mentioned 277x)
- [LOCATION] London:  (mentioned 168x)
- [LANGUAGE] RareLanguage:  (mentioned 166x)
- [Technology] Embeddings: Platform referenced as integrated with SearXNG through community ecosystem integrations. (mentioned 138x)
- [Technology] Self-Telemetry Query: Session cache and pub/sub component used to support fast session access and messaging. (mentioned 79x)
- [Technology] Uvicorn: ASGI server observed running at elevated CPU while handling diagnostic commands, but determined not to be the sustained cause of slowness. (mentioned 76x)
- [Topic] context_compressor.py: A Python file in src/personal_agent/orchestrator/ that defines at least one async function. (mentioned 63x)
- [Topic] compression_manager.py: A Python file in src/personal_agent/orchestrator/ that defines at least one async function. (mentioned 61x)
- [Technology] Qwen3.5-35B-A3B: Governance mechanism for managing and evolving schemas (mentioned for Avro/Protobuf) to avoid breaking consumers. (mentioned 61x)
- [Technology] run_sysdiag: Performs external probing (HTTP/TCP) to monitor service health endpoints. (mentioned 51x)
- [Concept] Single-node Elasticsearch: Failure routing mechanism where events that exceed max retries are stored for later inspection instead of repeated redelivery. (mentioned 49x)
- [Location] Crete: A travel region discussed, including visits related to the island's cities and ancient history. (mentioned 49x)
- [Concept] Event Bus: Initialization step that creates a Redis client and verifies connectivity (ping) before running subscriptions. (mentioned 45x)

Use this list to directly answer questions about what the user has previously discussed. Do NOT say you have no memory.
```

This is what the LLM is told about itself today. The interpretation is up
to a behavioral probe that has not been run.

**Verdict:** the substrate → prompt path is confirmed live. The prompt →
behavior step still requires a separate behavioral probe.

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

**Filed as FRE-374 (Urgent, Needs Approval).** The four harms above stand
on Probe 5 alone — token waste, empty descriptions on top-mention entities,
cross-contamination, and self-incoherence are all confirmed system-level
failures regardless of whether any specific user-visible answer was
degraded.

Proposed scope: description-provenance (replace destructive overwrite with
append-and-version) + relationship consolidation (dedupe semantic edges
before write) + render-time fallback (skip empty-description lines instead
of emitting blank entries into the prompt) + quality-monitor signals to
catch regressions.

Optional refinement: small behavioral probe (~10 traces) to characterize
how the LLM is responding to bad descriptions today. Not a gate.

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
