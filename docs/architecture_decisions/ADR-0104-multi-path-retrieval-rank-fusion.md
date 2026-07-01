# ADR-0104: Multi-Path Retrieval with Rank Fusion

**Status:** Proposed
**Date:** 2026-07-01
**Deciders:** Owner, Architect (adr session)
**Tags:** memory, retrieval, recall-quality, multi-path, rrf, architecture

---

## Context

**What is the issue we're addressing?**

ADR-0103 settled the *posture*: recall is retrieval over a living typed KG; no single similarity
score gives a clean separation floor (measured, FRE-694/695); separation comes from
**structure-where-closed, semantic-where-open**; the operating point is soft and adaptive. This ADR
records the **architecture** that follows — the constructive answer to "so how do we reach the
knowledge reliably." It is the design the owner named as *"where we meet"*: the direction is
decided here; the detailed spec is a dedicated child (see Verification → Seam owner).

**Today recall is single-path and fuzzy-cosine-first.** The closed-axis predicates (`type`,
recency-as-predicate, relationship hops) exist in the substrate (ADR-0097/0098) but are **not** in
the recall query. The reranker fires on **only one** path — the vector `search_memory` path
(`memory/service.py:1818`); the topic and proactive paths do not rerank, and the paths are
**siloed, not fused** (FRE-699). There is no lexical/full-text arm at all (no Neo4j full-text index
on turns today). So a query whose answer is out-of-vocabulary for the dense embedder — filed under
"perception" when the query says "vision" — has **no second route** to recover it. That
open-vocabulary tail miss is the direct, lived form of the "no prior discussions" false-negative
ADR-0100 attacked from the candidacy angle; single-path retrieval leaves the other half on the
table.

**Why fusion by rank, not score, matters up front.** FRE-695 established that arm score-scales are
**arbitrary and not comparable across arms** — a dense-cosine 0.62 and a reranker 0.62 and a BM25
score of 8.3 are not on one axis. Any architecture that *combines* arms must therefore fuse on
something scale-free. That constraint is load-bearing and is why this ADR commits to Reciprocal Rank
Fusion (RRF) rather than a weighted score blend.

**What needs to be decided:** whether recall becomes a **multi-strategy, rank-fused** retrieval
system, and the shape of that commitment at the architecture altitude — leaving arm parameters, the
adaptive operating point, cross-path dedup, and the gateway seam to the design spec.

---

## Decision

**Make recall a multi-path retrieval pipeline: several independent retrieval *arms*, fused by
Reciprocal Rank Fusion, then reranked, then handed to the main model as the final arbiter.** This is
a **recall play** — it raises the odds we retrieved the item *at all* (attacking the false-negative
tail) — not a floor fix; the final sieve remains the soft reranker of ADR-0103, never a hard gate.

1. **Multi-strategy arms.** Recall issues the query against several arms with different failure
   modes, and takes the **union**:
   - **Dense vector** — the existing `entity_embedding` ANN top-k (in place today).
   - **Lexical / full-text** — exact- and near-token matching (a Neo4j full-text index on turn/entity
     text; not built today). Catches rare tokens, IDs, names the embedder blurs.
   - **Structural predicate** — closed-axis filters (`type`, recency-as-predicate, relationship).
     **Gated on FRE-637** (ADR-0098 `type` enforcement): a predicate on a soft-closed `type` is
     unsafe until `type` is closed by contract — ADR-0103 §4.
   - **Graph traversal** — relationship hops from a matched anchor entity (multi-hop reach a single
     vector query cannot).
   - **Multi-query (paraphrase) expansion** — ask in several vocabularies ("vision" / "perception" /
     "eyesight") and union the results. This is the **direct** mitigation for the open-vocabulary miss
     without requiring canonicalization on the write side.

2. **Fuse by rank, not score — Reciprocal Rank Fusion.** Each arm returns a ranked list; RRF combines
   them by rank position (`score = Σ 1/(k + rank_i)`), sidestepping the incomparable-scale problem
   entirely. RRF also **rewards agreement**: an item surfaced by several independent arms ranks above
   an item surfaced by one — a stronger (soft) relevance signal than any lone cosine, and exactly the
   antifragile property called for when no single signal can be trusted (ADR-0103).

3. **Rerank the fused set, then hand it to the main model.** The reranker (ADR-0035) runs on the
   fused candidate set as a **soft ordering signal** (never a hard cutoff — inherits ADR-0103 AC-1),
   and the reranked material goes to the main model, which is the final arbiter of *truth*. Retrieval
   delivers *material*; judgement happens one layer up.

**Honest framing (stated in the ADR so it is not lost):** because aggregate recall is already high
(R@5 ≈ 0.98–1.00 at the production 0.6B embedder on the FRE-489 probe — a small-sample, n≈54
signal, not a proven ceiling), a **metric will not validate multi-path up front**. Its payoff is in
the **lived tail** (out-of-vocabulary, multi-hop queries a single path whiffs on) and in
**antifragility** on small, non-stationary, unlabeled data. So this ADR's acceptance is
**structural + tail-case**, not an aggregate recall lift — see Verification.

**What is deferred (deliberately) to the design-spec child:** the exact arm set for v1 (which arms
ship first), the RRF constant `k`, per-arm top-k, the adaptive operating point (how the soft
"return vs. no-prior-discussions" decision is made across arms), cross-path dedup (turns vs.
entities surfaced by multiple arms), and where the pipeline plugs into the gateway / context-assembly
seam (Stage 6). This ADR fixes the **architecture**; the spec fixes the **parameters and the seam**.

---

## Alternatives Considered

### Option 1: Single-path retrieval (status quo — dense-vector-first, one arm)
**Description:** Keep recall as the ADR-0100 relevance-bounded vector path, reranked, single arm.
**Pros:**
- Simplest; already shipped; one code path to reason about.
- Adequate for in-vocabulary queries (probe recall already saturates).

**Cons:**
- **No recovery for the open-vocabulary tail** — the "perception" vs "vision" miss has no second
  route; this is the lived residue of the "no prior discussions" symptom.
- No structural precision even though the substrate has the structure (ADR-0097/0098).
- Fragile by construction on non-stationary data: one signal, no ensemble.

**Why Rejected:** Leaves the false-negative tail — the very thing ADR-0103 says multiple paths
exist to close — unaddressed.

### Option 2: Weighted-score fusion (linear combine of arm scores)
**Description:** Run multiple arms, normalize and linearly combine their scores
(`w1·cosine + w2·bm25 + w3·rerank`).
**Pros:**
- Familiar; a single tunable weight vector.

**Cons:**
- **Requires comparable scales** — FRE-695 proved arm scales are arbitrary and not comparable;
  normalization across arms is itself an unstable calibration on n≈54 data (the ADR-0103 §7 trap).
- Re-introduces exactly the "calibrate a constant" fragility ADR-0103 rejects, one level up.
- No natural agreement bonus without extra machinery.

**Why Rejected:** Depends on the comparability FRE-695 disproved; RRF gets the agreement property for
free without calibrating incompatible scales.

### Option 3: Multi-query expansion only (paraphrase, keep the single dense arm)
**Description:** Address the open-vocabulary miss by paraphrasing the query into several vocabularies
and unioning the dense-arm results — no lexical/structural/graph arms.
**Pros:**
- Directly targets the "vision"/"perception" miss; small, additive change to the existing path.
- No new indexes or substrate dependencies.

**Cons:**
- Covers **only** the open-vocabulary failure mode; misses the rare-token (lexical), structural
  (`type`/recency), and multi-hop (graph) failure modes — each a distinct way single-path whiffs.
- Costs N embeddings/queries per recall for one arm's worth of coverage.

**Why Rejected:** A proper subset of the chosen architecture. Multi-query is *one arm* here, not the
whole answer.

### Option 4 (chosen): Multi-path arms fused by RRF, then reranked
**Description:** The decision above.
**Why Rejected:** Not rejected — chosen. It is the superset that covers each arm's distinct failure
mode, fuses scale-free, and rewards agreement.

---

## Consequences

### Positive Consequences
- **Covers more failure modes than any single arm** — the union of dense/lexical/structural/graph/
  multi-query directly attacks the open-vocabulary, rare-token, structural, and multi-hop misses.
- **Antifragile on exactly the data we have** — small, non-stationary, unlabeled; ensembling several
  signals is the correct response when no single signal can be trusted (ADR-0103).
- **RRF rewards cross-path agreement** without calibrating incompatible score scales — a stronger,
  self-validating relevance signal.
- **Uses the structure the substrate already has** (ADR-0097/0098) instead of leaving it inert.
- **No hard gate anywhere** — inherits ADR-0103's soft-operating-point posture end to end.

### Negative Consequences
- **More retrieval work per turn → latency.** Live recall is *already* ~17s, dominated by a ~15.7s
  VPS-CPU reranker pass (FRE-679) now that recall returns real candidates; adding arms increases both
  fan-out and the fused-set size the reranker must score. Latency budget is a first-class design
  constraint for the spec (reranker input cap, parallel arm execution, arm gating by primary model —
  FRE-699).
- **Cross-path dedup complexity** — the same turn/entity surfaced by multiple arms must be merged
  before fusion; a real (bounded) design problem.
- **No up-front metric validation** — probe recall saturates, so the win is tail-case + structural,
  which is harder to demo than an aggregate lift (stated honestly; see Verification).
- **Partial dependency** — the structural arm cannot ship until FRE-637 closes the `type` axis; v1
  will be a subset of arms.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Latency regression from more arms + larger fused set | High | Spec sets reranker input cap + parallel arms + per-model arm gating (FRE-699); AC measures fused-set size and rerank input bound |
| Structural arm built on soft-closed `type` → predicate silently drops rows | High | Structural arm **gated on FRE-637**; ADR-0103 §4 / AC-3; until then the arm is not enabled |
| RRF `k` / per-arm top-k mis-set → agreement bonus dominated by a noisy arm | Medium | Deferred to spec with the FRE-489/670 probe as a **regression instrument** (not a target — ADR-0103 §7) |
| Multi-query expansion multiplies embed/query cost | Medium | Bounded paraphrase count; arm is optional per operating point; measured in the spec |
| Adding arms yields no measurable recall gain (saturated probe) | Medium | Acceptance is **structural + tail-case**, not aggregate lift; a constructed OOV tail probe is the discriminator (AC-3) |
| Fusion re-introduces a hard cutoff by the back door | Low | Inherits ADR-0103 AC-1 (floor is a noise guard) + AC-2 (reranker orders, never gates); reranker stays soft |

---

## Implementation Notes

**Design-first, then build.** This ADR is Proposed; the immediate deliverable is the **design-spec
child**, not code. Sketch of the surface the spec will touch:

- **Arms.** Today only the dense-vector arm exists (`memory/service.py`, `entity_embedding` ANN).
  Lexical needs a Neo4j full-text index (none today). Structural needs FRE-637's closed `type`.
  Graph traversal reuses existing relationship edges. Multi-query is an orchestration wrapper over
  the dense (and lexical) arms.
- **Fusion.** A new RRF combiner over the arms' ranked lists — small, pure, unit-testable in
  isolation (rank-in → fused-rank-out; no substrate needed for the fusion unit tests).
- **Rerank + seam.** The reranker (`service.py:1818`) moves from "runs on the vector path" to "runs
  on the fused set." The pipeline plugs into the gateway Stage-6 context-assembly seam
  (`request_gateway/context.py`); the exact seam is the spec's to fix.
- **Rollout discipline (FRE-433 standard).** Flag-gated, default off; A/B on FRE-489/670 as a
  **regression instrument**; live verification is master-owned and deploy-gated.
- **Dependencies:** ADR-0103 (principle), ADR-0097/0098 + FRE-637 (structure), ADR-0035 (reranker),
  ADR-0100 (relevance-bounded candidacy the dense arm already uses), FRE-679/699 (latency + path
  load).

**Testing strategy:** the RRF combiner and dedup are pure functions with unit tests; the arms are
integration-tested against the test substrate; the end-to-end tail-case win is proven on a
constructed FRE-489/670 probe fixture (no new test infrastructure).

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

Architecture-altitude, falsifiable. Because probe recall saturates, acceptance is **structural +
tail-case**, deliberately **not** an aggregate recall lift (a bad implementation could hit an
aggregate number and still be single-path).

- **AC-1 — Recall runs ≥2 independent arms and fuses them by RRF rank.** **Check:** a recall call
  emits (telemetry / trace) the set of arms that ran and produces a fused ranking; the fusion code
  combines arms by **rank position**, not by comparing raw arm scores. *Fails if* only the dense arm
  runs, or fusion linearly combines raw scores (the Option-2 regression). Distinguishes multi-path
  from a renamed single path.

- **AC-2 — RRF agreement property holds.** **Check:** a unit test on the fusion combiner — an item
  ranked *r* by two arms outranks an item ranked *r* by one arm; and an item ranked highly by one
  arm but absent from others does not automatically top an item with broad multi-arm support.
  *Fails if* multi-arm agreement confers no rank benefit — then fusion is decorative.

- **AC-3 — The lived-tail win: multi-path recovers a single-path miss.** **Check:** a constructed
  probe case whose answer is out-of-vocabulary for the dense arm but reachable by a second arm
  (lexical / structural / multi-query) is **present** in results with multi-path on and **absent**
  with it off (single dense arm). *Fails if* the item is missed in both, or found in both (then the
  extra arms added nothing). This is the discriminating outcome — the "no prior discussions" tail,
  closed. It replaces an aggregate-recall AC precisely because the aggregate is saturated.

- **AC-4 — The structural arm never runs on an unenforced `type`.** **Check:** the structural
  predicate arm is disabled (feature-gated) until FRE-637 closes the `type` axis; when enabled, a
  `type`-predicate recall does not silently drop entities whose `type` is `""`/`"Unknown"`.
  *Fails if* a `type = X` predicate excludes rows with unenforced/empty `type` (the ADR-0103 §4
  open-vocabulary-drop failure, one axis over). This **extends** ADR-0103 AC-3 (no hard predicate on
  the *open* axis) to the closed axis: the structural arm runs only on an **enforced** `type`.

- **AC-5 — No arm's raw score is used as a hard gate (inherits ADR-0103 AC-1 floor + AC-2 reranker).** **Check:** no arm
  or the fused/reranked set is filtered-to-empty on a score threshold; the reranker orders, it does
  not gate. *Fails if* a hard cutoff re-appears.

- **AC-6 — Multi-path fan-out is latency-bounded, not open-ended.** Three conjuncts, each
  falsifiable: **(a)** the emitted **fused-set size handed to the reranker ≤ a configured cap**
  (reranker input is bounded, not the union of every arm's top-k); **(b)** the design spec states a
  **numeric** end-to-end recall latency ceiling (anchored on the FRE-679 ~17s reranker-dominated
  baseline) — a spec that ships without a number fails this AC on its face; **(c)** measured p50
  recall latency with the multi-path flag on does **not exceed** that ceiling. **Check:** read the
  spec for the number; assert the emitted `fused_set_size ≤ cap` in telemetry; run the A/B and
  compare p50 to the ceiling. *Fails if* the reranker input is uncapped, the spec omits a numeric
  ceiling, or measured p50 exceeds it. (The number is the spec's to set — this AC binds it to *have*
  one, cap the fan-out, and hold both.)

**Seam owner (decomposed ADR):** the assembled intent — *recall is a live multi-path, RRF-fused,
reranked pipeline across ≥2 arms, delivering the tail-case win (AC-3), within the latency bound
(AC-6)* — is **not** closed by the three authored children (FRE-705/706/707). The accountable owner
is **FRE-705 (the multi-path retrieval design spec)**: it is a design deliverable whose first
follow-on task is to **file the build-phase integration ticket** (or ordered chain) that lands the
arms + RRF + rerank-on-fused-set and proves AC-1…AC-6 together, live, behind a flag. That
build-seam ticket — not yet filed; FRE-705 creates it — is the true seam owner; **FRE-705 is
accountable for its existence**. FRE-707 (structure-wiring) delivers the *structural-arm* slice and
is blocked on FRE-637; FRE-706 delivers the *operating-point* slice. ADR-0104 reaches
**Implemented** only when the FRE-705-filed build-seam ticket shows AC-1…AC-6 holding together with
the flag on — master gates it. It does **not** close when its last authored child (a design spec, a
re-scope, a gated arm) merges.

---

## References

- ADR-0103 — Recall is Retrieval: No Clean Similarity Floor; Separation is Structural (the principle
  this architecture rests on and inherits guardrails from; authored in the same PR; Accepted).
- ADR-0100 — Memory Recall: Relevance-Bounded Candidate Generation (the dense arm's current
  candidacy logic; PR #267).
- ADR-0097 — Ingested-Knowledge Taxonomy (the closed-axis structure the structural + graph arms use).
- ADR-0098 — Memory Substrate & Lifecycle (Accepted 2026-06-27; typed structure + `type` enforcement;
  build chain FRE-637; PR #263).
- ADR-0035 — Reranker integration (the soft ordering signal, now on the fused set).
- ADR-0087 — Memory-Recall Quality: A Measurement-First Program (measurement backing).
- `docs/research/2026-06-30-recall-as-retrieval-and-the-dual-domain.md` — §5 (recall = multi-path)
  and §7 (the open design questions this ADR opens and its design-spec child closes).
- `docs/research/2026-06-30-fre-695-reranker-separation.md` — arm scales not comparable → RRF, not
  weighted-score fusion.
- FRE-494 — the authoring ticket (two ADRs sequenced + three children).
- FRE-705 — Multi-path retrieval design spec (child #1; the accountable seam owner — files the
  build-phase integration ticket).
- FRE-706 — Recall operating-point re-scope (child #2; supersedes FRE-655's hard-floor framing).
- FRE-707 — Wire closed-axis predicates into recall (child #3; the structural-arm slice; blocked on
  FRE-637).
- FRE-637 — ADR-0098 `type` extraction/emission contract (gates the structural arm; AC-4).
- FRE-655 — the closed measure-and-propose ticket whose hard-floor framing FRE-706 supersedes.
- FRE-679 — live recall ~17s, reranker-dominated (the latency constraint for AC-6).
- FRE-699 — recall path-frequency / rerank load (reranker fires only on the vector path today;
  per-model arm gating).
- Code: `memory/service.py:1818` (`rerank(...)`, single-path today), `memory/service.py`
  (`entity_embedding` ANN — the dense arm), `request_gateway/context.py` (Stage-6 seam).

---

## Status Updates

### 2026-07-01 - Proposed
**Changed By:** Architect (adr session)
**Reason:** Records the multi-path + RRF retrieval architecture that follows from ADR-0103, per
FRE-494 (ADR-B of two). Direction decided; detailed design (arm set, RRF params, operating point,
dedup, Stage-6 seam) deferred to the design-spec child. Kept **Proposed** — not Accepted — because
the design is the owner-collaborative "where we meet" and the structural arm is gated on FRE-637.
