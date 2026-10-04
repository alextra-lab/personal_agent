# FRE-1545 — Proactive reranker relevance bound: calibration and latency

**Date:** 2026-10-04 · **Ticket:** FRE-1545 · **Design intent:** ADR-0148 D4 ·
**Harness:** `scripts/eval/fre1545_proactive_reranker_calibration/` ·
**Artifact:** `config/calibration/proactive_rerank_relevance_bound.json`

## Result

| Quantity | Value |
|---|---|
| Serving reranker | `rerank-2.5` (Voyage) |
| Probe set | `scripts/eval/fre435_memory_recall/semantic_probe.yaml` (54 queries, the FRE-1477 labelled set) |
| Labelled positives | 110 (57 entity candidates, 53 episode candidates) |
| Negatives (strongest non-match per query) | 54, median **0.337891**, max 0.578125 |
| **Bound** | **0.338891** |
| Positives admitted | **93.6%** (entity 93.0%, episode 94.3%) — D4 requires ≥ 90% |
| Negatives rejected | 50.0% (the median is rejected, as D4 requires) |
| Added time per proactive call, p50 / p95 | **277.6 ms / 740.1 ms** (max 772.0 ms), n = 54 |
| Documents per production-width call | median 39, max 42 |

The bound is configured as `proactive_memory_rerank_relevance_bound = 0.338891`.

## Checks

All three passed before the number was reported.

| Check | Result |
|---|---|
| P1 — proactive document builder + production `rerank()` vs the independent Voyage arm, entity documents | `pos_median` Δ 0.0078, `neg_median` Δ 0.0078, `neg_p95` Δ 0.0021 (gate 0.02). `neg_max` Δ 0.0859, reported, not gated (FRE-1479's reason) |
| P2R — instrument sanity | relevant 0.7852 > irrelevant 0.2021 |
| P3 — score movement between the wide set (top_k 100) and the production-width set (top_k 20), per shared candidate, all queries | max 0.0039 |

## The bound equals broad recall's. That is a measurement, not a copy.

The ticket says not to copy 0.338891 from broad recall. The harness did not. It measured the
proactive population and reached the same number, for a traceable reason:

- `choose_bound` places the bound one step above the negative median.
- The negative median is 0.337891 on both populations. The two negative sets are not identical,
  but their medians coincide.
- The wide pass retrieves almost the whole corpus (median 89 candidates per query), and the
  reranker scores each pair independently (P3: 0.0039). So the strongest non-match per query is
  drawn from nearly the same documents as in FRE-1479.

The positive populations differ: 110 here against 119 in FRE-1479. The proactive episode is only
the most recent cross-session turn per entity, where broad recall reaches every turn the fused set
holds.

## Latency

One serial reranker call per proactive turn, after the raw query. It runs only when the gate is
armed. p50 278 ms and p95 740 ms were measured from the VPS against Voyage, over production-width
candidate sets (about 39 documents), through the production function the adapter calls. The p95
is about 2.7 times the p50. It is the Voyage round trip, not the document count, which varies
little (39 to 42).

On a Voyage outage, `rerank()` waits up to its 10 s primary timeout, then up to 30 s for the
fallback. The fallback's scores are on another scale, so the gate reports the bound candidates as
UNAVAILABLE. Broad recall has the same exposure today.

## Method

- Production code end to end: `generate_embedding` → `MemoryService.suggest_proactive_raw` (a
  session id no seeded turn carries) → `memory.proactive.score_proactive_relevance`.
- Labels follow FRE-1479: entity positive when its name is expected; episode positive when its
  turn `DISCUSSES` an expected entity.
- The production-width pass is a separate retrieval, not a cut of the wide pass. A wide dense
  retrieval suppresses lexical hits in dense ranks 21–100 (codex plan-review).
- Test substrate only (FRE-375).

## Scope of the gate

ADR-0148 D4's proactive clause binds only a candidate with zero entity overlap and zero topic
hits. A candidate with either carries relevance evidence other than recency and is not gated,
which is unchanged from FRE-1477. A bound candidate with no reranker value, or a value from a model
other than `rerank-2.5`, is dropped as `RECALL_RELEVANCE_UNAVAILABLE`, and the turn composes
UNAVAILABLE.
