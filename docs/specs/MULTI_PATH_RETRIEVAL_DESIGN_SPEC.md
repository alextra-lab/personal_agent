# Multi-Path Retrieval Design Spec — Arm Set, RRF Parameters, Operating Point, Dedup, and the Stage-6 Seam

**Status:** Draft (design deliverable for FRE-705)
**Date:** 2026-07-01
**Backing ADRs:** ADR-0104 (Multi-Path Retrieval with Rank Fusion — Proposed) · ADR-0103 (Recall is Retrieval; No Clean Floor — Accepted)
**Owner:** Architect (adr session), with owner (the "where we meet" collaborative design)
**Linear:** FRE-705 (this spec) · children FRE-706 (operating-point sign-off), FRE-707 (structural arm)

---

## 0. What this document is

ADR-0104 fixed the **architecture** — recall becomes several independent retrieval *arms*, fused by
Reciprocal Rank Fusion (RRF), reranked, then handed to the main model — and deliberately left the
**parameters and the seam** open, to be settled here. This spec closes exactly those open questions:

1. the **v1 arm set** and its sequencing (§2),
2. the **RRF constant `k` and per-arm retrieval depth** (§3),
3. the **cross-path dedup rule** (§4),
4. the **soft, adaptive operating point** — the "return something vs. say *no prior discussions*"
   decision across arms (§5; folds in the design half of FRE-706),
5. where the pipeline **plugs into the gateway Stage-6 context-assembly seam** (§6),
6. the **numeric end-to-end recall latency ceiling and the fused-set cap** (§7),
7. the **build-phase ticket chain** FRE-705 is accountable for filing — the assembled seam owner for
   ADR-0104 (§8),
8. the **acceptance criteria** this design must satisfy, mapped to ADR-0104 AC-1…AC-6 (§9).

It ships no code. It is a design record that governs the build chain in §8.

**Scope boundary (inherited, ADR-0103 Decision).** No SOC / dual-domain work. A domain-specialized
partner is a future *fork* reusing the domain-agnostic core — never grafted on here.

---

## 1. Grounding: what exists today

Confirmed against the code, so the design binds to reality, not intent:

| Piece | Where | State today |
|-------|-------|-------------|
| Dense vector arm | `memory/service.py` `query_memory()`, `entity_embedding` ANN (`db.index.vector.queryNodes`, ~1896) | In place; the only live arm |
| Reranker | `memory/reranker.py` `rerank()`, called from `service.py:~2078` **inside `query_memory()` only** | Runs on the **single** vector path (`query_memory`); the broad and proactive paths do **not** rerank (ADR-0104 Context, FRE-699) |
| **Reranker input cap** | `reranker_input_cap = 25` (settings.py:535, FRE-672/696) via `_select_rerank_candidates()` (service.py:149) | **Already bounds** the set handed to the cross-encoder to top-25 **by vector score** — but only in `query_memory()`; the rest pass through on vector+recency score |
| Noise-guard floor | `recall_similarity_floor = 0.0` (settings.py:563, ADR-0100) | A noise guard, **not** a separating gate (ADR-0103 AC-1) |
| Lexical / full-text arm | — | **Does not exist**; no Neo4j full-text index on turns/entities |
| Structural / graph arms | closed-axis structure exists (ADR-0097/0098) | **Not in the recall query**; structural gated on FRE-637 |
| **Explicit recall path** (the lived symptom) | `TaskType.MEMORY_RECALL` → `recall_broad()` → `query_memory_broad()` (service.py:2269) | Serves *"what have I discussed about X?"* — the FRE-435 *"no prior discussions"* symptom. **Does not rerank today.** Uses ADR-0100 relevance-bounded candidacy |
| Entity-name heuristic path | `recall()` → `query_memory()` (service.py:1832) | Fires for analysis/other intents on capitalised entity hints. **This is the only path that reranks today** (holds `_select_rerank_candidates` + the cap) |
| Proactive path | `suggest_relevant()` | Separate; proactive suggestions |
| Stage-6 seam | `request_gateway/context.py` `assemble_context()` → `_query_memory_for_intent()` (~168–207) | **Three siloed calls**, not one (FRE-699): `recall_broad` for MEMORY_RECALL, `recall` for entity-name, `suggest_relevant` for proactive |

**Two load-bearing consequences of this reality:**

1. **The cap mechanism already exists — it is generalized, not invented.** The "fused-set cap handed
   to the reranker" ADR-0104 AC-6 demands is the existing `reranker_input_cap` + `_select_rerank_candidates()`.
   Today it lives *only inside `query_memory()`*. Multi-path lifts it into a shared core and re-points
   the selection from raw vector score to **RRF fused rank**. The cross-encoder — the ~0.11 s/candidate
   bottleneck — still sees **at most 25 candidates**, no matter how many arms feed the union. (This
   bounds the *reranker* only, not upstream retrieval — see §7.)

2. **v1's primary target is the explicit recall path (`query_memory_broad`), not `query_memory`.** The
   lived *"no prior discussions"* symptom (FRE-435) fires on `TaskType.MEMORY_RECALL`, which routes
   through `recall_broad`/`query_memory_broad` — a path that does **not rerank today at all**. So
   multi-path on that path is a *strict* improvement (it gains rerank-on-fused it never had), and it is
   where the symptom actually lives. The three siloed paths (FRE-699) converge onto the shared
   multi-path core; §6 fixes which path v1 wires first and how the others follow.

---

## 2. The v1 arm set

**Decision (owner-confirmed): v1 ships three arms — Dense, Lexical, Multi-query.** Structural and
graph arms are v2 (FRE-707), because structural is gated on FRE-637 (`type` closed by contract) and
graph traversal has no lived failure-mode pressure yet.

Each arm earns its place by the **distinct failure mode** it recovers — the ADR-0104 test that an arm
is not decorative:

| Arm | Failure mode it uniquely covers | Mechanism | New substrate? |
|-----|--------------------------------|-----------|----------------|
| **Dense vector** (exists) | In-vocabulary semantic match; the general case | `entity_embedding` ANN, ADR-0100 relevance-bounded candidacy | No |
| **Lexical full-text** | Rare tokens, IDs, proper names, code identifiers the dense embedder blurs into its topical neighbourhood | Neo4j full-text index over `Turn` content + `Entity.name`; BM25-style ranked hits | **Yes** — new FT index |
| **Multi-query paraphrase** | The open-vocabulary miss — the lived "vision" vs. "perception" symptom (FRE-435) — *without* requiring write-side canonicalization | Generate a bounded paraphrase set, fan each variant through the dense arm, union | No |

**Why these three and not the other two, now:**

- **Structural predicate — deferred to FRE-707.** A hard predicate on `type` silently drops rows whose
  `type` is `""`/`"Unknown"` until FRE-637 closes the axis by contract (ADR-0103 §4 / ADR-0104 AC-4).
  Shipping it in v1 would either block v1 on FRE-637 or ship an unsafe arm. Neither is acceptable.
- **Graph traversal — deferred to FRE-707.** Real value (multi-hop reach), but no current lived miss
  demands it, and it shares FRE-707's structure-wiring surface. Sequencing it with the structural arm
  keeps the substrate-touching work in one build slice.

**Sequencing within v1:** Dense is already live. Lexical and Multi-query are additive and independent
of each other; they can be built in either order (§8 pairs them in one build ticket). The **assembled**
three-arm pipeline is the seam-owner ticket (§8, Build 3).

**The v1 arm set proves the discriminating acceptance criterion (AC-3, §9):** Lexical and Multi-query
are precisely the two routes that recover an item the dense arm alone misses. If v1 shipped Dense-only
(a renamed single path), AC-3 could not pass.

---

## 3. RRF parameters

### 3.1 Fusion is by rank, never by score

Non-negotiable, inherited from ADR-0104 §2 / FRE-695: arm score scales are **arbitrary and not
comparable** (a dense-cosine 0.62, a BM25 8.3, a reranker 0.62 are not one axis). Fusion combines
**rank positions** only:

```
RRF_score(item) = Σ_arms  1 / (k + rank_arm(item))
```

where `rank_arm(item)` is item's 1-based position in that arm's ranked list (absent ⇒ that arm
contributes 0). This gives the **agreement bonus for free**: an item surfaced by two arms at rank *r*
outscores an item surfaced by one arm at rank *r* — the antifragile, self-validating signal ADR-0103
calls for when no single score can be trusted.

### 3.2 The constant `k`

**`k = 60`.** The Cormack et al. (2009) default — the value the RRF literature uses precisely because
results are insensitive to it across a wide band, which is the right property for a constant we
explicitly refuse to fit to n≈54 probe data. Exposed as `multipath_rrf_k` (config-driven, ADR-0031),
tunable **only** via the FRE-489/670 probe **as a regression instrument, never an optimization target**
(ADR-0103 §7). A larger `k` flattens the rank-position weighting (later ranks matter more relative to
the top); 60 keeps a healthy top-rank emphasis without a cliff.

### 3.3 Per-arm retrieval depth

**Each arm returns its top `multipath_arm_top_k = 50`** candidates for fusion. Rationale: the fusion
input needs enough depth that an item ranked, say, 30th by lexical but 3rd by dense still contributes;
50 is comfortably deeper than the 25-candidate reranker cap it feeds, so the cap — not arm depth — is
the latency bound. Depth beyond ~50 buys little (RRF weight at rank 50 with k=60 is ~1/110, already
small) and costs arm-side work. Config-driven; identical default across arms for symmetry.

**Multi-query paraphrase count: `multipath_paraphrase_count = 3`** query variants total (the original +
2 paraphrases). Bounded to hold the added embedding cost small (§7). Paraphrases are generated by the
local model (a cheap generation, not the primary); an empty/failed paraphrase set degrades gracefully
to the dense arm alone (fail-open on the *arm*, never fail-closed on recall).

---

## 4. Cross-path dedup rule

The same real item surfaced by multiple arms must be **one** node before fusion, or RRF's agreement
bonus is lost (the item would compete against itself) and the reranker would waste cap slots on
duplicates.

**Identity rule — two results are the same item iff:**

- **Turns:** same `Turn.turn_id`.
- **Entities:** same `Entity` `elementId` (Neo4j internal id). Free-text `name` is **not** an identity
  key — "vision" and "perception" are deliberately different nodes (ADR-0103 §4, the open axis); dedup
  must not merge them.

**Merge semantics:** dedup is applied **within** each arm first (keep the best — lowest — rank if an
arm somehow returns a node twice), producing one `(item_id → rank)` map per arm. RRF then sums
`1/(k+rank)` across arms keyed by `item_id`. Output is deduped **by construction**: each `item_id`
appears once in the fused ranking with its summed score.

This rule is a **pure function** — `(list[ranked_arm_result]) → fused_ranking` — with no substrate
dependency, hence unit-testable in isolation (§8 Build 1, AC-2).

---

## 5. The soft, adaptive operating point

This is the design half of FRE-706, settled here (owner-confirmed); FRE-706 becomes the **sign-off +
recorded-values** enactment ticket (§8) that satisfies ADR-0103 AC-5.

**The decision being made:** at the recall surface there is still one binary act — *return retrieved
material*, or *emit "no prior discussions on this topic."* ADR-0103 §5 forbids this from being a static
calibrated cutoff. It is a **dial several soft signals vote on**, and the **main model is the final
arbiter of truth** (ADR-0104 §3) — recall delivers *material*, judgement happens one layer up.

**The three signals that vote (none is a hard gate):**

1. **Fused-set occupancy after the noise-guard floor.** The ADR-0100 `recall_similarity_floor` stays a
   **noise guard** — it drops pure no-record noise that sits *below* the true-match distribution; it
   does **not** sit in the positive/near-miss overlap trying to separate (ADR-0103 AC-1). If the fused
   set is empty *after* the noise guard, there is genuinely nothing to return.
2. **RRF agreement.** An item surfaced by ≥2 arms is a stronger soft relevance signal than any lone
   arm's top hit. This is **new evidence multi-path provides** that single-path never had — it raises
   confidence in "there is prior discussion," and is surfaced to the main model as a confidence cue,
   not used to filter.
3. **Reranker ordering in its soft operating region.** The reranker orders the fused set; it **never
   filters to empty** (ADR-0103 AC-2). FRE-695 measured its soft operating region (~88% recall @ ~9%
   FP at the best reranker) — this informs how confidently material is framed to the main model, not a
   cutoff.

**The rule:** recall returns the reranked fused material whenever the fused set is non-empty after the
noise guard. "No prior discussions" is emitted **only** when the fused set is empty after the noise
guard — i.e. no arm found anything above pure noise. The reranker and agreement signals shape
**ordering and the confidence framing handed up**, never a drop-to-empty. The main model then decides
whether the material actually answers the query.

**What FRE-706 signs off (its recorded values):**

- the `recall_similarity_floor` value operated as a **noise guard** (must remain below the true-match
  distribution — provable on the FRE-489 probe: no true positive is dropped by it, AC-1);
- whether/how RRF agreement (≥2 arms) is surfaced as a confidence cue to the main model;
- confirmed against the pedagogical bar with owner sign-off, mirroring the FRE-655 sign-off pattern.

No hard separating floor is chosen anywhere — that framing is retired (ADR-0103 AC-5).

---

## 6. The Stage-6 seam

Stage 6 does **not** call one recall method — it makes **three siloed calls** by intent (FRE-699).
Naming them precisely is the correction that keeps the design honest:

```
request_gateway/context.py  →  _query_memory_for_intent()  (~168–207)
  ├─ TaskType.MEMORY_RECALL   → memory_adapter.recall_broad()   → query_memory_broad()  ← v1 TARGET
  ├─ proactive_memory_enabled → memory_adapter.suggest_relevant()
  └─ entity-name heuristic    → memory_adapter.recall()          → query_memory()  (reranks today)
```

**v1 wires the explicit recall path first**, because that is where the lived *"no prior discussions"*
symptom fires and it does not rerank today (so multi-path is a strict gain there):

```
recall_broad(query_text, …)  →  query_memory_broad()
     │  (behind multipath_recall_enabled; flag off → today's broad path, byte-for-byte)
     ▼
   ┌───────────── shared multi-path retrieval core ─────────────┐
   │  arms (parallel, asyncio.gather):                          │
   │    • dense   → entity_embedding ANN (exists)                │
   │    • lexical → Neo4j full-text index (new)                  │
   │    • multiq  → paraphrase × dense, unioned                  │
   │  → cross-path dedup (§4)                                    │
   │  → RRF fuse (§3, memory/fusion.py — pure)                   │
   │  → _select_rerank_candidates() by RRF rank (generalized)    │
   │  → rerank() on fused top-reranker_input_cap (=25)           │
   │  → operating point (§5) → material or "no prior discussions"│
   └────────────────────────────────────────────────────────────┘
```

**Concrete change surface:**

- **New pure module `memory/fusion.py`** — RRF combiner + cross-path dedup (§3, §4). No substrate;
  unit-tested rank-in → fused-rank-out.
- **New shared multi-path core** in the service — arms → dedup → RRF → rerank-on-fused → operating
  point. Introduced as an internal method both explicit paths can route through.
- **New lexical arm** — a Neo4j full-text index (`CREATE FULLTEXT INDEX` over `Turn` content and
  `Entity.name`) + a query function returning ranked hits. Index creation follows the existing
  `entity_embedding` index-management pattern (service.py:~1232).
- **New multi-query wrapper** — generates the bounded paraphrase set, fans through the dense (and,
  optionally, lexical) arm, unions.
- **Generalize `_select_rerank_candidates()`** — today it lives inside `query_memory()`; lift it into
  the shared core, selection key moves from vector score to RRF fused rank; the `reranker_input_cap`
  (25) and the pass-through-on-non-reranker-score behaviour are unchanged. This also brings
  rerank-on-fused to the explicit recall path, which has no reranker today.
- **Route `query_memory_broad()` through the core** behind `multipath_recall_enabled` (default off →
  the current broad path, byte-for-byte).
- **Path convergence (FRE-699).** v1's seam owner (§8 Build 3) wires the **explicit recall path
  (`query_memory_broad`)** through the core and proves AC-1/AC-3/AC-5/AC-6 there. Converging the
  entity-name path (`query_memory`) and the proactive path (`suggest_relevant`) onto the *same* core is
  named as an explicit follow-on within Build 3's scope (cheap once the core exists) — **not** left
  implicit, so a build cannot wire `recall()` alone and leave the primary MEMORY_RECALL path
  single-path.
- **Telemetry** — emit on the recall trace: `arms_ran` (the set of arms that executed), `fused_set_size`
  (the count handed to the reranker, which must be ≤ `reranker_input_cap`), per-arm candidate counts,
  and the recall path (`broad`/`entity`) the core served. This is the AC-1 and AC-6 instrumentation.

**Rollout discipline (FRE-433 standard):** flag-gated, default off; A/B on the FRE-489/670 probe as a
**regression instrument**; live verification is master-owned and deploy-gated. Default off reproduces
single-path recall exactly.

---

## 7. Latency ceiling and fused-set cap

**Posture (owner-confirmed): no-regression.**

- **Numeric end-to-end recall latency ceiling: p50 ≤ 17 s**, anchored on the FRE-679 reranker-dominated
  baseline. Multi-path must **not** make recall slower; its value is proven on coverage (AC-3), not
  speed.
- **Fused-set cap = `reranker_input_cap` (default 25).** The set handed to the cross-encoder is the
  fused top-25 by RRF rank — the **same bound** as today, now fed by fusion instead of vector score.

**What the cap does and does not bound.** `reranker_input_cap` bounds **only the cross-encoder** — the
~0.11 s/candidate bottleneck (FRE-696) — because it sits *after* fusion. It does **not** bound the
upstream cost multi-path adds: the extra paraphrase embeddings, the lexical full-text query (which does
not exist today), the extra ANN/Cypher work, dedup, and fusion all happen **before** the cap. A build
that only satisfies `fused_set_size ≤ 25` could still regress latency upstream. So the no-regression
claim rests on **two** things — a bounded upstream, and a measured gate:

**Upstream cost is explicitly bounded, not open-ended:**

1. **Arms run in parallel** (`asyncio.gather`): wall-clock arm time is the *slowest* arm, not the sum.
2. **Per-arm depth ≤ `multipath_arm_top_k` (50)** and **paraphrase count ≤ `multipath_paraphrase_count`
   (3)**: the union before fusion is ≤ ~150 entries; the added embeddings are ≤ 2; the lexical FT query
   is index-backed. Each is a configured, enforced bound — not "as many as the arm returns."
3. **Fusion + dedup** are pure in-memory operations over ≤ ~150 ranked entries — negligible.

**The binding check is measurement, not the argument.** AC-6 conjunct (c) — **measured p50 recall
latency with the flag on ≤ 17 s in the FRE-489/670 A/B** — is the falsifiable gate that catches any
upstream regression the argument above missed. **Rollout gate:** if the A/B shows p50 > 17 s, the arms
are gated / tuned (reduce paraphrase count, tighten arm depth, or drop the slowest arm) **before**
rollout; the flag does not ship on a measured regression. This is the FRE-433 "flag → verified →
rollout" discipline, master-owned.

**AC-6 has three falsifiable conjuncts (§9):** (a) emitted `fused_set_size ≤ reranker_input_cap` in
telemetry; (b) this spec states a numeric ceiling — **17 s** (done, here) — anchored on FRE-679;
(c) **measured** p50 with the flag on ≤ 17 s in the A/B, else the flag is held.

---

## 8. Build-phase ticket chain (the assembled seam owner)

FRE-705 is **accountable for filing** the build chain that lands the arms + fusion + rerank-on-fused
and proves ADR-0104 AC-1…AC-6 together, live, behind the flag. ADR-0104 reaches **Implemented** only
when the seam-owner ticket shows those criteria holding with the flag on (master gates). Naming the
chain here so the assembled ADR does not close on the three authored children alone.

| Build ticket | Scope | Proves | Depends on |
|--------------|-------|--------|-----------|
| **Build 1 — RRF fusion + cross-path dedup** | `memory/fusion.py`: pure RRF combiner (§3) + dedup rule (§4). Unit tests: agreement property, dedup by canonical id, k=60 behaviour. | AC-2 | — |
| **Build 2 — Lexical + Multi-query arms** | Neo4j full-text index over `Turn`/`Entity.name` + ranked query fn; multi-query paraphrase wrapper (§2, §3.3). Integration-tested vs. test substrate; **flag-dark** (not yet wired into live recall). | (feeds AC-1/AC-3) | — |
| **Build 3 — Assemble the pipeline (SEAM OWNER)** | Build the shared multi-path core (dense + lexical + multi-query → dedup → RRF → `_select_rerank_candidates` by RRF rank → rerank-on-fused); route the **explicit recall path `query_memory_broad`** (the MEMORY_RECALL / lived-symptom path) through it behind `multipath_recall_enabled`; **converge the entity-name path (`query_memory`) and proactive path onto the same core** (FRE-699), not left single-path; soft operating point (§5, values from FRE-706 sign-off); telemetry (`arms_ran`, `fused_set_size`, path); enforce fused-set cap. **Prove AC-1, AC-3, AC-5, AC-6 live, flag on**, via the FRE-489/670 A/B (incl. AC-6(c) measured p50 ≤ 17 s, else hold the flag). | AC-1, AC-3, AC-5, AC-6 | Build 1, Build 2, FRE-706 |

**Existing siblings that plug into this chain:**

- **FRE-706** (Approved) — operating-point **sign-off + recorded values** (§5). Gates Build 3's
  operating-point implementation. Satisfies ADR-0103 AC-5.
- **FRE-707** (Approved, blocked on FRE-637) — the **structural + graph arms** (v2). Plugs into the
  *same* `memory/fusion.py` combiner as two more ranked lists. Delivers ADR-0104 AC-4. Not part of the
  v1 seam.

**The named seam owner for ADR-0104 is Build 3.** FRE-705 (this spec) is accountable for its existence;
FRE-705 files Builds 1–3 as a Needs-Approval chain under the Memory Recall Quality project.

---

## 9. Acceptance criteria (design-altitude slice of ADR-0104)

**Two altitudes, kept distinct so neither is faked:**

- **Design altitude (S-1…S-6, below).** FRE-705's *own* acceptance — proven by **review of this
  document against ADR-0104**. Passing these means the spec closed the open questions correctly. It
  does **not** mean ADR-0104 is delivered, and it does **not** close ADR-0104.
- **Live altitude (ADR-0104 AC-1…AC-6).** The behavioral proof — measured, flag-on, on the substrate —
  carried by the §8 build chain (seam owner Build 3) and **master-verified**. This is the hard gate on
  ADR-0104 reaching Implemented. The S-criteria below deliberately point at it so a reader cannot
  mistake "the spec is good" for "the pipeline works."

Each S-criterion is falsifiable — a broken or half-finished *design* fails it.

| # | Criterion | How proven | Fails if |
|---|-----------|-----------|----------|
| **S-1** | Names the v1 arm set and justifies each arm by the distinct failure mode it covers | §2 table | An arm is listed with no unique failure mode (decorative), or the set is Dense-only (a renamed single path) |
| **S-2** | States fusion is by **rank position**, never by comparing raw arm scores across arms, and gives the reason (scales incomparable — FRE-695) | §3.1 | The spec fuses by weighted score, or omits the reason |
| **S-3** | Sets a numeric latency ceiling **bound to a measured live gate**, and a fused-set cap **bound to an enforced mechanism** | §7: ceiling p50 ≤ 17 s anchored on FRE-679 **and** carried as AC-6(c) measured-p50-else-hold; cap = `reranker_input_cap` (25) enforced by `_select_rerank_candidates` + asserted in telemetry (`fused_set_size ≤ cap`) | Either number is absent; **or** the ceiling is stated with no live measured check behind it (a declared "17 s" that nothing verifies); **or** the cap names no enforcing mechanism + telemetry assertion (a number that constrains nothing) |
| **S-4** | Defines the cross-path dedup rule (canonical identity + merge semantics) | §4 | No identity key stated, or `name`-equality used to merge open-axis entities |
| **S-5** | Files the build-seam chain whose **seam-owner ticket carries AC-1…AC-6 as live acceptance gates** | §8; tickets filed in Linear and linked on FRE-705; Build 3's acceptance = AC-1/AC-3/AC-5/AC-6 proven flag-on live | No build ticket exists; **or** tickets exist but the seam owner's acceptance is "wire it up" rather than the live ACs (filing paperwork without carrying the behavioral proof); **or** no single ticket is accountable for the assembled AC-1…AC-6 |
| **S-6** | The operating point is soft/adaptive, no hard separating floor | §5 | Any signal is used as a drop-to-empty gate; a hard cosine/rerank cutoff appears |

**Mapping to ADR-0104's live AC (proven by the §8 build chain, not this spec):** S-1→AC-1/AC-3,
S-2→AC-1, S-3→AC-6, S-4→(dedup, feeds AC-1), S-6→AC-5. ADR-0104 AC-2 (agreement) is Build 1; AC-4
(structural arm on enforced `type`) is FRE-707. ADR-0104 is **Implemented** only when Build 3 shows
AC-1/AC-3/AC-5/AC-6 holding together, flag on, live — master gates. Filing this spec and its tickets
does **not** advance ADR-0104 past Proposed.

---

## 10. References

- ADR-0104 — Multi-Path Retrieval with Rank Fusion (Proposed; the architecture this spec details).
- ADR-0103 — Recall is Retrieval; No Clean Similarity Floor; Separation is Structural (Accepted; the
  principle — soft operating point, no hard gate, structure-where-closed).
- ADR-0100 — Relevance-Bounded Candidate Generation (the dense arm's candidacy; the noise-guard floor).
- ADR-0097 / ADR-0098 — Ingested-Knowledge Taxonomy / Memory Substrate (the closed-axis structure the
  v2 structural arm uses; FRE-637 closes `type`).
- ADR-0035 — Reranker integration (the soft ordering signal, now on the fused set).
- ADR-0031 — Model/config identity (arm and RRF knobs are config-driven, not hardcoded).
- `docs/research/2026-06-30-fre-695-reranker-separation.md` — arm scales not comparable → RRF.
- `docs/research/2026-06-30-recall-as-retrieval-and-the-dual-domain.md` — §5 (recall = multi-path), §7
  (the open design questions this spec closes).
- FRE-494 — the authoring ticket. FRE-705 — this spec. FRE-706 — operating-point sign-off. FRE-707 —
  structural/graph arms (v2, blocked on FRE-637). FRE-679 — ~17 s recall baseline. FRE-696 —
  reranker_input_cap 50→25, ~0.11 s/candidate. FRE-672 — `_select_rerank_candidates` input cap.
- Code: `memory/service.py` (`query_memory`, `entity_embedding` ANN ~1896, `_select_rerank_candidates`
  :149, rerank call ~2078), `memory/reranker.py` (`rerank`), `config/settings.py:535`
  (`reranker_input_cap=25`), `:563` (`recall_similarity_floor`), `request_gateway/context.py:207`
  (Stage-6 `recall()` seam).

---

## 11. Change log

- **2026-07-01** — Initial draft (FRE-705). Owner-confirmed decisions: v1 arms Dense+Lexical+Multi-query;
  operating point designed here, FRE-706 = sign-off; latency posture no-regression (p50 ≤ 17 s,
  fused-set cap = existing `reranker_input_cap` = 25).
- **2026-07-01 (codex round 1)** — Corrected the Stage-6 seam: the explicit MEMORY_RECALL path is
  `recall_broad`/`query_memory_broad` (no rerank today), not `recall()`; named it v1's primary target
  and the three-path convergence (FRE-699). Made the no-regression argument bound upstream cost
  explicitly and rest on the measured-p50 gate (AC-6c), not the reranker cap alone. Tightened S-3/S-5
  from existence-checks to discriminating checks and split design-altitude vs live-altitude acceptance.
