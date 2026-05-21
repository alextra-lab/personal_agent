# Investigation Plan: Does Our Memory Pipeline Integrate or Concatenate?

> **Note on filename**: This file was auto-named by the plan-mode session. Once a
> Linear issue exists, rename to
> `docs/superpowers/plans/2026-05-21-fre-XXX-memory-integration-probe.md`
> per project convention.

## Context

A discussion of consciousness theories (Global Workspace Theory, predictive
processing, Metzinger self-models, IIT) surfaced a structural question about
our own architecture: do our memory operations actually *integrate* facts
(cross-constrain them, resolve conflicts, support each other) or do they merely
*concatenate* (accumulate, deduplicate by name, top-K inject)?

A read-only audit of the current pipeline (see "Findings" below) returned a
clean verdict: **concatenates across every layer**. Promotion writes only
entity attributes, the Neo4j schema has no fact-level nodes or
SUPPORTS/CONTRADICTS edges, no code path detects or resolves contradictions,
retrieval is top-K injection with no post-retrieval reasoning, and the quality
monitor measures only surface metrics (counts, orphans, freshness).

This investigation does **not** propose to add an integration layer yet. It
proposes to **quantify the gap empirically** on live VPS data, so we know
whether the missing integration is a real problem (frequent contradictions,
redundant retrieval, fact drift) or a theoretical concern (rare in practice
because the LLM tolerates the noise). The output is a short report that feeds
a future ADR / Linear issue.

## Findings from initial audit

| Dimension | Verdict | Key file |
|---|---|---|
| Promotion (`promote_entity`) | Concatenates — entity name + confidence only | `src/personal_agent/memory/promote.py:38-62` |
| Neo4j schema | Entity↔Entity edges only; no fact nodes | `src/personal_agent/memory/service.py:880-945` |
| Conflict handling | Accumulation — zero detection; vector dedup on names only | `src/personal_agent/memory/dedup.py:49-154` |
| Retrieval (Stage 6) | Top-K list → direct message injection | `src/personal_agent/request_gateway/context.py:247-357` |
| Quality monitor | Surface metrics (counts, orphans, freshness) | `src/personal_agent/second_brain/quality_monitor.py:145-356` |

The seams where integration *could* be added (post-retrieval, consolidation,
promotion, quality monitor) are identified but out of scope for this probe.

## Goals

1. Measure how often the concatenation produces visible problems on the live
   `/opt/seshat` instance, using real data from accumulated sessions.
2. Produce a short report with numbers + examples that lets us triage whether
   to open an ADR for a cross-fact constraint layer.
3. Avoid building anything yet. This is a measurement task.

## Non-goals

- Designing the integration layer itself (that's the follow-up ADR).
- Modifying memory promotion, retrieval, or schema.
- Adding new node types or edges.

## Probes

All probes are **read-only** against the live VPS Neo4j and live Elasticsearch.
Each is a single Cypher / ES query plus a short Python script in
`scripts/research/memory_integration_probe/` (new directory).

### Probe 1 — Entity attribute drift (Neo4j)

Pick the top 20 entities by `DISCUSSES` edge count. For each, fetch the
`description` and `properties` that have been written across multiple turns.

**Question**: Do attributes on the *same* entity change incoherently across
turns, or do they converge? Specifically:
- Count entities whose `description` was overwritten with a contradictory
  string (manual review of ~20).
- Count entities where `properties` accumulated keys that conflict.

**Output**: `probe_1_entity_drift.md` — number of drift cases out of 20,
worst three examples quoted verbatim.

### Probe 2 — Redundant relationships (Neo4j)

For each pair `(source_entity, target_entity)` with ≥ 2 relationships,
list the relationship types.

**Question**: How often do we have multiple edges expressing the same semantic
relation between the same pair (e.g., `RELATED_TO` and `SIMILAR_TO` and
`USES` all between `seshat` and `personal_agent`)?

**Output**: `probe_2_redundant_edges.md` — count + top-10 worst pairs.

### Probe 3 — Retrieval payload audit (Elasticsearch)

Pull the last 20 gateway traces from Elasticsearch where Stage 6 context
assembly fired. For each, capture the list of entities/episodes injected into
the prompt.

**Question**:
- How many injected items are near-duplicates (same entity surfaced twice
  under different names)?
- How many pairs are mutually contradictory (manual review)?
- What fraction of the assembled context turned out to be redundant?

**Output**: `probe_3_retrieval_audit.md` — duplicate rate + contradiction rate
across 20 traces, with examples.

### Probe 4 — Quality monitor blind spots (synthetic check)

Construct a small synthetic contradiction (e.g., write two `Entity` nodes
with the same name but contradictory descriptions, in a *test* Neo4j) and
verify the quality monitor flags zero anomalies. This confirms the audit
finding empirically.

**Output**: One-paragraph confirmation that the monitor is blind to the
contradiction. Test data cleaned up afterward.

## Critical files (read-only, will not modify)

- `src/personal_agent/memory/promote.py:38-62`
- `src/personal_agent/memory/service.py:880-945`, `1933-1975`
- `src/personal_agent/memory/dedup.py:49-154`
- `src/personal_agent/request_gateway/context.py:247-357`
- `src/personal_agent/second_brain/quality_monitor.py:145-356`
- `src/personal_agent/second_brain/consolidator.py:460-521`

## Deliverables

1. `scripts/research/memory_integration_probe/probe_1_entity_drift.py`
2. `scripts/research/memory_integration_probe/probe_2_redundant_edges.py`
3. `scripts/research/memory_integration_probe/probe_3_retrieval_audit.py`
4. `scripts/research/memory_integration_probe/README.md`
5. `docs/research/2026-05-21-memory-integration-probe-report.md` — the
   consolidated report (one page), with:
   - Findings from each probe (numbers).
   - One recommendation: open ADR / open Linear issue / no action needed,
     justified by the numbers.

## Verification

The probes succeed if they produce **numbers** (not just code that runs).
Specifically:
- Probe 1: a count `X / 20 entities show attribute drift` with three
  named examples.
- Probe 2: a count `Y pairs have ≥ 2 edges of overlapping semantics`.
- Probe 3: a percentage `Z% of injected context items are redundant
  across 20 traces`.
- Probe 4: confirmed blind spot in quality monitor.

End-to-end run command:

```bash
cd /opt/seshat
uv run python scripts/research/memory_integration_probe/probe_1_entity_drift.py
uv run python scripts/research/memory_integration_probe/probe_2_redundant_edges.py
uv run python scripts/research/memory_integration_probe/probe_3_retrieval_audit.py
# Probe 4 is manual (test Neo4j scratch + quality monitor invocation)
```

Then compile findings into the report. If any number is "concerning" (TBD by
the report author at write-time — see thresholds below), recommend opening a
Linear issue in `Needs Approval` for a follow-up ADR on cross-fact
constraint checking.

**Suggested thresholds for "concerning":**
- Probe 1: ≥ 3/20 entities show drift.
- Probe 2: ≥ 5 pairs with redundant edges.
- Probe 3: ≥ 15% redundancy rate in retrieval.

## Out of scope (explicitly)

- Building a contradiction detector.
- Modifying the Neo4j schema (e.g., adding `:Fact` nodes).
- Adding a post-retrieval reasoning step.
- Any change to the request gateway.

Those are downstream decisions that depend on this probe's numbers.

## Open question to revisit before execution

The audit notes that the entity extraction model is qwen3-8b. If it produces
shallow descriptions to begin with, "drift" might be measurement noise rather
than a real integration failure. Probe 1 should distinguish these — flag
cases where the descriptions are *both* substantive but contradictory, vs.
cases where they're both vague boilerplate.
