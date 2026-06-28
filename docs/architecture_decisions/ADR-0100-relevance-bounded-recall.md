# ADR-0100: Memory Recall — Relevance-Bounded Candidate Generation

**Status:** Proposed
**Date:** 2026-06-28
**Deciders:** Owner, Architect (adr session)
**Tags:** memory, retrieval, neo4j, recall-quality, performance

---

## Context

**What is the issue we're addressing?**

The owner's standing symptom (FRE-435, 2026-06-02): the agent answers *"No prior
discussions on this topic"* when prior context demonstrably exists. ADR-0087's Phase-1
measurement program quantified the cause; FRE-491 (PR #264, merged) delivered the baseline
and routed the recommendation (`docs/research/2026-06-27-memory-recall-quality.md`). This
ADR is the routed Phase-2 fix (FRE-494), scoped by that evidence.

**The verified root cause is in the retrieval *query layer*, not the write path or the
embedder.** Recall is recency-first, relevance-second. The automatic recall paths build
their candidate set by **time**, then let the vector index and reranker only *re-score the
survivors* — they never *expand* the candidate set. Two distinct recency gates sit upstream
of any semantic step:

- **Gate A — hard cutoff.** `MemoryRecallQuery.recency_days` defaults to **30**
  (`memory/protocol.py`); `query_memory` appends `AND c.timestamp >= $cutoff_date`
  (`memory/service.py:1471`). Any turn older than 30 days is excluded *before* relevance is
  consulted → the literal "no prior discussions" denial. The broad recall path
  (`query_memory_broad`, used for the `MEMORY_RECALL` intent) applies the same gate at
  **90** days.
- **Gate B — recency-ordered LIMIT.** The candidate Cypher ends
  `RETURN DISTINCT c ORDER BY c.timestamp DESC LIMIT $limit` (`service.py:1478-1482`). Even
  within the window, the *most recent N* turns are selected; recent chatter crowds out the
  relevant older turn → **wrong-but-recent** context.

The vector index then runs (`db.index.vector.queryNodes('entity_embedding', …)`,
`service.py:1539`) but only populates a `vector_scores` dict used to *re-rank the turns the
recency Cypher already returned*. FRE-491 measured the right entity ranked **#1 at 0.82
cosine** — semantically perfect, yet unrecallable because it was never a candidate.
Embeddings and the write path are **not** the bottleneck.

**What needs to be decided:** how to make automatic recall select candidates by *relevance*
rather than *recency*, without (a) unbounded candidate-set growth as the KG accumulates,
(b) admitting low-relevance noise now that time no longer filters, or (c) surfacing stale
facts. The fix must be simple, measurable, and reversible.

**A correct pattern already exists in-repo.** `MemoryService.suggest_proactive_raw`
(`service.py:237`) generates candidates vector-first (`queryNodes(entity_embedding, $top_k)`,
bounded by `proactive_memory_vector_top_k`) with **no recency gate**. This ADR brings the
recency-gated paths into line with that pattern rather than inventing a new mechanism.

**Adjacent decision context.** ADR-0098 (Accepted 2026-06-27) made facts *living Claims*
with bitemporal supersession + contradiction detection — that layer now owns
correctness-over-time. The recency gate was a crude proxy for "don't surface stale facts";
post-0098 that proxy is redundant *and* harmful to recall, so recency can be safely demoted
from a hard filter to a ranking signal.

---

## Decision

**Replace recency-keyed candidate generation with relevance-keyed candidate generation in
the automatic recall paths (`query_memory`, `query_memory_broad`), gated behind a
default-off flag.** Four coupled changes, converging on the existing
`suggest_proactive_raw` pattern:

1. **Candidate generation by relevance.** The candidate set becomes the **union** of (a) the
   existing entity-name match and (b) **vector top-k over `entity_embedding` across all
   time** (the index already exists). Candidate generation is no longer keyed on
   `c.timestamp`.

2. **Drop the hard recency pre-filter.** Remove `AND c.timestamp >= $cutoff_date` from the
   automatic recall candidate Cypher. `recency_days` no longer gates candidacy.

3. **Recency becomes a ranking signal, not a gate.** Recency is folded into
   `_calculate_relevance_scores` as a *weight* alongside the vector and reranker scores, so
   recent-and-relevant wins ties while old-and-highly-relevant is no longer denied. `LIMIT`
   is applied **after** relevance ranking, not after `ORDER BY timestamp DESC`.

4. **A calibrated similarity floor as the safety gate.** A new config value
   `recall_similarity_floor` (cosine threshold) drops candidates below a relevance bar — the
   guard that replaces the time filter so a relevance-keyed set does not admit junk. The
   floor is **config-driven and embedder-calibrated, never hardcoded**, because score
   distributions shift per embedding model (forward-compat for any future embedder swap —
   see ADR References, the embedder-quality follow-on).

**Scale safety.** Candidate-set size is bounded by `proactive_memory_vector_top_k` (ANN
top-k), so cost is **invariant to KG growth** — we replace a time-bound with a relevance-
bound of the *same* cardinality, not an unbounded scan. The existing Stage-7 token-aware
budget trimming still caps recalled context.

**Staleness correctness is delegated to ADR-0098** (Claims + bitemporal supersession +
contradiction detection). Recall no longer carries a staleness responsibility.

**Rollout discipline (FRE-433 standard).** The change ships behind
`relevance_bounded_recall_enabled` (default **off**) → A/B on the FRE-489 probe set → live
verification → rollout. Flag off reproduces legacy behaviour exactly.

**Observability.** A live `memory_recall` telemetry event is emitted per recall:
candidate-set size, top/median vector score, recency span (oldest/newest hit), an
**empty-result counter** (the "no prior discussions" event made measurable in prod),
latency, and recalled-token count — mirroring the FRE-435 harness metrics into the live path
so regressions are visible.

**Why this over the alternatives:** raising the window only postpones the cliff and leaves
candidate generation recency-keyed; removing the filter outright keeps recency-keying *and*
unbounds the scan. Only relevance-keyed candidate generation fixes the actual selection
axis, and it does so by reusing a pattern already proven in the codebase — minimal new
surface, fully reversible.

---

## Alternatives Considered

### Option 1: Raise the recency window (30→365, 90→730 days)
**Description:** Bump the `recency_days` defaults; change nothing else.
**Pros:**
- One-line change; near-zero risk; instantly recalls more.
**Cons:**
- Only moves the cliff — any turn older than the new window is still denied.
- Candidate generation stays **recency-keyed**; the vector index still only re-ranks
  survivors, so a relevant old turn outside the window remains unrecallable.
- Gate B (recency-ordered LIMIT crowding) is untouched.
- The window value is arbitrary and will rot as the corpus ages.

**Why Rejected:** Treats the symptom, not the cause. The selection axis is still time.

### Option 2: Remove the recency filter, keep the timestamp-ordered LIMIT
**Description:** Delete the `c.timestamp >= cutoff` clause but leave
`ORDER BY c.timestamp DESC LIMIT $limit`.
**Pros:**
- Trivial; removes the hard denial.
**Cons:**
- Candidate set becomes "the most recent N turns across *all* time" — still recency-keyed
  selection, just with no window; vector/reranker still only re-rank.
- Removes the only bound on the candidate scan → cost grows with KG size (the scaling
  failure the owner flagged).
- Gate B crowding gets *worse*, not better.

**Why Rejected:** Strictly worse scaling and still the wrong selection axis.

### Option 3: Relevance-bounded candidate generation (vector top-k + similarity floor, recency as weight)
**Description:** The chosen decision above.
**Pros:**
- Fixes the selection axis: candidates by relevance, bounded by top-k (scale-invariant).
- Reuses the in-repo `suggest_proactive_raw` pattern — low new surface, low risk.
- Recency preserved as a ranking signal; staleness delegated to ADR-0098.
- Flag-gated, A/B-measured, reversible.

**Cons:**
- Without a floor, a relevance-keyed set could admit weak matches → mitigated by
  `recall_similarity_floor`.
- Slightly more rerank/scoring work per call → bounded by top-k + token trimming.
- Behaviour change on the recall hot path → mitigated by flag + A/B.

**Why Rejected:** Not rejected — chosen.

### Option 4: Upgrade the embedder (cloud or higher-parameter local model)
**Description:** Swap `Qwen3-Embedding-0.6B` for a larger local model or a cloud embedding API
to raise retrieval quality.
**Pros:**
- Higher raw embedding quality; offloads CPU (cloud).
**Cons:**
- Orthogonal to the measured defect — the embedder already ranks the right entity #1 @0.82;
  the bug is candidate *generation*, not *scoring*.
- Cloud adds a network round-trip on the recall hot path, per-call cost, and ships private
  memory/query text off-box (privacy cost for a personal-memory system).
- A local upgrade is RAM-bound on the GPU-less VPS and requires a full KG re-embed.

**Why Rejected:** Does not address the defect. Deferred to a separate, evidence-gated
research ticket (benchmark on FRE-489 *after* the de-gate exposes the residual retrieval
ceiling). See References.

---

## Consequences

### Positive Consequences
- Recall reaches across all history — the "no prior discussions" false-negative is removed
  for in-corpus relevant turns regardless of age.
- Gate B crowding is eliminated: relevant-old beats recent-irrelevant.
- Model-independent and deterministic — no model change required.
- Cost is scale-invariant (top-k bound) as the KG grows.
- The "no prior discussions" event becomes measurable in production for the first time.

### Negative Consequences
- New tunable (`recall_similarity_floor`) that must be calibrated per embedder; a wrong floor
  trades recall against precision.
- Recall behaviour changes on a hot path used by every turn — requires careful A/B + rollout.
- Slightly higher per-call scoring work (more candidates reach the reranker).

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Floor set too low → low-relevance noise surfaced | Medium | Calibrate on FRE-489 negatives; AC-3 asserts negatives return empty; config-driven, tunable without redeploy of logic |
| Floor set too high → relevant turns dropped (regression) | Medium | A/B recall@k on FRE-489 before rollout; default-off flag; AC-1/AC-2 gate |
| Candidate set grows with KG (scaling) | Medium | Hard top-k bound (`proactive_memory_vector_top_k`); AC-4 asserts `candidate_set_size ≤ top_k` independent of corpus size |
| Stale facts surfaced now that time no longer filters | Low | Correctness owned by ADR-0098 Claims (supersession + contradiction); recency retained as ranking weight |
| Hot-path latency regression from more reranking | Low | top-k cap + Stage-7 token trimming; `memory_recall` latency telemetry watched post-rollout |
| Embedder swap later silently breaks a hardcoded floor | Low | Floor is config-driven + embedder-calibrated by contract (Decision §4) |

---

## Implementation Notes

**Files affected:**
- `src/personal_agent/memory/service.py` — `query_memory` (candidate Cypher: union vector
  top-k + entity match, drop `cutoff_date` clause, LIMIT-after-ranking) and
  `query_memory_broad` (same de-gate); `_calculate_relevance_scores` (recency weight); emit
  `memory_recall` event.
- `src/personal_agent/memory/protocol.py` — `recency_days` semantics note (no longer a hard
  gate on the automatic path; retained for explicit time-scoped queries).
- `src/personal_agent/config/settings.py` — new `relevance_bounded_recall_enabled: bool`
  (default False) and `recall_similarity_floor: float`.
- Telemetry: `memory_recall` event + Elasticsearch mapping (audit every field against the
  index template — float vs long, keyword `ignore_above`).
- Probe/harness: `scripts/eval/fre435_memory_recall/` reused for the A/B (no new infra).

**Migration steps:** none destructive — no schema change, no re-index. The `entity_embedding`
vector index already exists. Behaviour is flag-gated; rollout is config-only.

**Testing strategy:** unit tests assert the de-gated Cypher and the floor; the FRE-489 probe
provides the A/B (recall@k, false-negative, distractor-invariance). No new test
infrastructure.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

- **AC-1 — A relevant memory older than the legacy window is recalled.** With the flag on, a
  FRE-489 probe positive whose source turn is **>30 days old** (and **>90 days** for the
  broad/`MEMORY_RECALL` path) appears in `recall()` / `recall_broad()` results.
  **Check:** harness run on the bespoke probe; assert the old positive is present.
  *Fails if* the >window positive is absent — i.e. the exact pre-fix false-negative. A
  fix that merely raised the window to *N* days still fails for a positive older than *N*.

- **AC-2 — Candidate selection is relevance-bounded, not time-bounded (no crowding).**
  Inserting *M* recent distractors does not evict an old relevant positive.
  **Check:** FRE-489 run sweeping `distractor_background_n`; assert recall@5 for an old
  positive stays ≥ baseline as *M* grows. *Fails if* recall@5 degrades with more recent
  distractors (the Gate-B symptom). A timestamp-ordered LIMIT cannot pass this.

- **AC-3 — The similarity floor keeps junk out.** A query with no semantically-relevant
  memory returns empty, not low-similarity noise.
  **Check:** FRE-489 negative case (query unrelated to corpus); assert 0 entities above
  `recall_similarity_floor`. *Fails if* below-floor matches are surfaced. Guards against
  "relevance-keyed = surface anything".

- **AC-4 — Candidate-set size is bounded by top-k regardless of corpus size.**
  **Check:** the emitted `memory_recall` event's `candidate_set_size` ≤
  `proactive_memory_vector_top_k` across probe runs at two corpus sizes (e.g. 1× and 5×).
  *Fails if* `candidate_set_size` grows with corpus size — the scaling failure Option 2
  would exhibit.

- **AC-5 — The empty-recall ("no prior discussions") rate is observable live.**
  **Check:** query Elasticsearch for the `memory_recall` event; it carries an
  empty-result flag/counter distinguishing empty from non-empty recall. *Fails if* no
  emitted field lets you count empty recalls in prod. This is the standing regression watch.

- **AC-6 — Default-off and exactly reversible.** With `relevance_bounded_recall_enabled` off,
  recall reproduces legacy behaviour.
  **Check:** run the FRE-489 baseline with the flag off; assert the pre-fix false-negative
  (AC-1's positive absent) reproduces and metrics match the FRE-491 baseline. *Fails if* the
  off-state diverges from legacy — meaning the flag does not cleanly gate the change.

**Seam owner (decomposed ADR):** the assembled intent — *relevance-bounded recall delivers
across both live recall paths with the floor and telemetry in place, verified on FRE-489 with
the flag on* — is asserted by the **verification/rollout ticket (FRE-NEW-3 below)**, and
master gates it at integration. The ADR does **not** close when the last child merges; it
closes when FRE-NEW-3 shows AC-1…AC-6 holding together with the flag on.

---

## References

- ADR-0087 — Memory-Recall Quality: A Measurement-First Program (Phase-1 backing; PR #166).
- ADR-0098 — Memory Substrate & Lifecycle (Accepted 2026-06-27; owns staleness via living
  Claims + bitemporal supersession; PR #263).
- ADR-0096 — Memory Access Model (coordinated hybrid; access posture, unchanged here).
- ADR-0042 — Memory access-tracking / freshness (the recency-as-signal precedent).
- ADR-0035 — Reranker integration (the downstream re-scoring step this ADR feeds).
- FRE-491 — Baseline run + hypothesis-table resolution (H4 dominant; PR #264, merged).
- FRE-493 — Research doc + routed recommendation (`docs/research/2026-06-27-memory-recall-quality.md`).
- FRE-489 — Bespoke live-corpus probe set (the A/B yardstick for every AC here).
- FRE-433/434 — Methodology precedent (measure-don't-assert, flag → A/B → rollout).
- Code: `memory/service.py` (`query_memory:1400`, `query_memory_broad:1676`,
  `suggest_proactive_raw:237`, `_calculate_relevance_scores`), `memory/protocol.py`
  (`recency_days:108`), `request_gateway/context.py` (`_query_memory_for_intent:138`).
- Embedder-quality follow-on (separate, evidence-gated): benchmark Qwen3-Embedding-4B /
  BGE-M3 / a cloud reference on FRE-489 after the de-gate exposes the residual retrieval
  ceiling; VPS is GPU-less (8 vCPU Haswell/AVX2, ~10 GiB shared RAM).

---

## Status Updates

### 2026-06-28 - Proposed
**Changed By:** Architect (adr session)
**Reason:** Routed Phase-2 fix for the memory-recall symptom, scoped by FRE-491 evidence
(H4 — query-layer recency gating). Authored for FRE-494.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
