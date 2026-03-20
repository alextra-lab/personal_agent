# Graphiti Experiment Report

**Date:** [Fill after experiment]
**Status:** Template — awaiting execution
**Spec ref:** COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 5.5

---

## Hypothesis

Graphiti (by Zep) may provide a better storage backend for Seshat's
episodic and temporal memory than the current hand-built Neo4j schema,
because it handles entity deduplication, relationship extraction, and
temporal queries natively.

## Experiment Design

### What to Compare

| Dimension | Current Neo4j | Graphiti |
|-----------|--------------|----------|
| Entity deduplication | Manual (consolidator) | Built-in |
| Temporal queries | Manual Cypher | Native API |
| Relationship extraction | LLM-based (entity_extraction.py) | Built-in |
| Recall relevance | Multi-factor scoring (service.py) | Graphiti search |
| Setup complexity | High (custom schema + migrations) | Lower (managed) |

### Test Scenarios

1. **Entity storage + retrieval:** Store 50 conversation episodes,
   query by entity name. Compare recall quality and latency.
2. **Temporal queries:** "What did I discuss about X last week?"
   Compare result relevance and ordering.
3. **Entity deduplication:** Store mentions of the same entity with
   slight name variations. Compare dedup accuracy.
4. **Scaling:** Store 500+ episodes. Compare query latency at scale.

### Metrics

- Recall latency (p50, p95, p99)
- Precision (relevant results / total results)
- Entity dedup accuracy (unique entities / raw mentions)
- Setup time and operational complexity

## Execution Steps

- [ ] Install Graphiti: `pip install graphiti-core`
- [ ] Create a test script that populates both backends with the same data
- [ ] Run each test scenario against both backends
- [ ] Capture metrics to ES with `backend` tag for dashboard comparison
- [ ] Write findings below

## Findings

[To be filled after experiment execution]

### Recommendation

- [ ] Keep current Neo4j (Graphiti adds complexity without sufficient benefit)
- [ ] Migrate to Graphiti (clear improvement in quality/latency/maintenance)
- [ ] Hybrid (use Graphiti for temporal queries, keep Neo4j for graph traversal)

## Impact on Architecture

If Graphiti is adopted, the MemoryProtocol abstraction means only the
adapter changes — no consuming code needs modification. This is exactly
why the protocol-first approach was chosen in Slice 1.
