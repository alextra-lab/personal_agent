# FRE-655 — Relevance-Bounded Recall: A/B + Floor Calibration

**Date:** 2026-06-28 · **ADR:** ADR-0100 · **Ticket:** FRE-655 (assembled seam) · **Backing:** FRE-489 probe, FRE-491 baseline
**Scope of this doc:** the build-session *measurement + proposal* (owner-bounded). The flag-flip rollout, live gateway verification, and AC-6 live ES-queryability are master-owned and deferred. The floor value below is **proposed for owner sign-off**, not yet set.

## Method

The FRE-489 probe set (21 curated live-corpus cases) was driven through **both** prod recall paths on the isolated **test substrate** (Neo4j :7688), flag off vs on, with the production embedder/reranker (Qwen3-Embedding-0.6B / Qwen3-Reranker-0.6B, same models as cloud, on localhost). Driver: `scripts/eval/fre435_memory_recall/ab_relevance_bounded.py`.

A fidelity fix was required first: the FRE-435 baseline harness drove recall with `query_text` only (no `entity_names`), so the FRE-653 relevance-bounded branch (gated on `entity_recall`) was never exercised. The driver mirrors the prod gateway — entity hints via `_capitalized_entity_hints(query)` for the `query_memory` path, and `recall_broad(query_text=…)` for the `MEMORY_RECALL` path.

Two passes:
- **A/B** (`--mode ab`, 40 live-corpus distractors, wipe per case): recall@5, flag off vs on, both paths.
- **Calibration** (`--mode calibrate`, all 21 cases co-seeded): per query, positive = the case's own expected-entity cosine; negative = the strongest *other-case* (co-resident, embedded, unrelated) entity cosine. This is the distribution AC-4's floor must separate.

## Results — the recall recovery (A/B, 40 distractors)

| metric | flag OFF | flag ON |
|--------|---------:|--------:|
| **entity-path** recall@5 (18 scored) | **0.00** | **0.72** |
| recovered (denied off → surfaced on) | — | **13 / 21** |
| **broad-path** expected-entity hit | **0 / 21** | **18 / 21** |
| empty-hint cases (broad-path only) | — | 4 / 21 |

The headline: **the broad path was fully broken flag-off (0/21) and recovers 18/21 flag-on**; the entity path goes from total denial (recall 0.00) to 0.72. This is the "no prior discussions" false negative being removed, measured end-to-end on the live-corpus probe. Observed cosine range 0.655–0.874 (confirms the index `score` is [0,1] cosine).

The 4 empty-hint cases (queries with no capitalized entity token) cannot fire the entity path — by design they are the `MEMORY_RECALL` broad-path cases, and they recover there.

## Results — floor calibration (co-resident, cross-case negatives)

| distribution | n | min | median | max |
|--------------|--:|----:|-------:|----:|
| positives (expected entity) | 21 | 0.655 | 0.823 | 0.874 |
| negatives (top other entity) | 21 | 0.625 | 0.710 | 0.807 |

Sweep (floor → recall / false-positive-rate):

```
0.00–0.60:  1.00 / 1.00      0.75:  0.76 / 0.14   ← Youden-optimal (J=0.62)
0.65:       1.00 / 0.90      0.80:  0.62 / 0.05
0.70:       0.76 / 0.62      0.85:  0.19 / 0.00
```

**The positive and negative distributions overlap heavily** (both span ~0.63–0.87). There is **no floor that cleanly separates** relevant from irrelevant entities at this embedder. The Youden-optimal floor (0.75) costs recall (0.76) — unacceptable for a recall-first system whose entire purpose is removing false negatives.

## Interpretation & proposal

1. **The similarity floor is a weak separator at the 0.6B embedder.** The real noise control is the relevance *ranking* (vector + reranker + recency), not a hard cosine gate — the A/B at floor 0.0 already surfaces positives into top-5 (recall 0.72) and recovers the broad path (18/21). A high floor throws away recall the ranking would otherwise deliver.
2. **Proposed `recall_similarity_floor` (for owner sign-off):** a **low** value — `0.0` (recall-first, rely on ranking) or at most `0.65` (drops only the weakest ~10% of negatives while keeping recall 1.0). The Youden-balanced `0.75` is documented but **not recommended** for this recall-first decision.
3. **Recency-weight cutoffs:** the current ADR-default recency weighting (0.20–0.40 in `_calculate_relevance_scores`) already delivers the recovery above without re-tuning — proposed **unchanged**, flagged for owner confirmation against the pedagogical bar.
4. **Routes evidence to FRE-656 (embedder ceiling):** the positive/negative overlap *is* the residual retrieval ceiling ADR-0100's embedder follow-on targets. A higher-quality embedder would separate the distributions and make a meaningful floor possible.

## Owner decision (2026-06-28 sign-off)

Presented the floor options against the evidence. **Owner's call:** floor `0.0` *if forced to choose* (the recall-first recommendation) — **but the indecisive separation is itself the signal: do not roll the flag out now on scores this overlapping.** Verbatim: *"this shows we need to test a better embedding model first. Why implement now on scores that are indecisive."*

**Resulting sequencing decision:**
- **FRE-656 (embedder benchmark/upgrade) is promoted to a prerequisite of the FRE-655 rollout**, not a follow-on. The 0.6B embedder's positive/negative overlap is the binding ceiling; a floor calibrated on it would be arbitrary.
- **The flag stays default-off; no rollout, no floor change is made now.** The relevance-bounded code (FRE-653/654) is shipped and inert behind the flag — zero prod risk while FRE-656 runs.
- When a better embedder lands, **re-run this exact A/B + calibration** (the driver is the reusable instrument) on the separated distributions, then set the floor and roll out.
- Recorded fallback floor if a rollout were ever forced before FRE-656: **0.0** (recall-first; ranking does the work).

This is the assembled-seam verdict: the recall *mechanism* is proven to recover the false negatives (entity 0.00→0.72, broad 0/21→18/21), but the *rollout* is gated on the embedder, by owner decision.

## AC coverage (measured here vs deferred to master rollout)

| AC | status | evidence |
|----|--------|----------|
| AC-1a (entity recall across time) | ✅ measured | entity recall 0.00 → 0.72; 13/21 recovered |
| AC-1b (broad recall across time) | ✅ measured | broad hit 0/21 → 18/21 |
| AC-2 (relevance-ordered) | ✅ measured (implied) | positives surface into top-5 flag-on (recall@5 0.72) where flag-off gave 0.00 |
| AC-3 (no recency crowding) | ✅ measured | recall recovered *with* 40 recent distractors loaded |
| AC-4 (floor keeps junk out) | ⚠️ measured + caveat | sweep computed; floor is a weak separator at this embedder (overlap) → low floor + ranking |
| AC-5 (5× scale invariance + `candidate_set_size ≤ top_k`) | ⏳ partial | recall held at 40 distractors; strict 5× + the live `memory_recall` count → master rollout verify |
| AC-6 (empty-result signal queryable in ES) | ⏳ deferred | needs the flag live in prod ES — master rollout |
| AC-7 (flag off = baseline) | ✅ measured | flag-off entity recall 0.00 reproduces the FRE-491 denial |

## Reproduce

```bash
make test-infra-up
AGENT_MODEL_CONFIG_PATH=config/models.yaml \
  uv run python -m scripts.eval.fre435_memory_recall.ab_relevance_bounded --run-id ab --distractor-background 40
AGENT_MODEL_CONFIG_PATH=config/models.yaml \
  uv run python -m scripts.eval.fre435_memory_recall.ab_relevance_bounded --run-id cal --mode calibrate
```
(`config/models.yaml` points the embedder/reranker at localhost — same models as cloud, host-reachable. Raw run JSON lands under the gitignored `out/`.)
