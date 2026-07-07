# ADR-0114: Heterarchical Associative Memory — a Decoupled Research Study (learn-at-ingest, consolidate-offline, recall-from-store)

**Status:** Proposed
**Date:** 2026-07-07
**Deciders:** Project owner (adr session, Opus)
**Tags:** memory, knowledge-graph, categorization, associative-memory, self-organizing, consolidation, research-study, neo4j, gds
**Backing tickets:** FRE-837 (umbrella); implementation sub-issues sequenced in Linear (see Status Updates)

**Scope discipline:** this ADR decides to **run an isolated research study**, seeded from a frozen snapshot of live Seshat data. It does **not** change the production KG, does **not** amend ADR-0109, and does **not** decide how (or whether) any of this returns to Seshat — that is a **future ADR, gated on this study's results**. Every "the system does X" below describes the *study sandbox*, never prod.

---

## Context

**What is the issue we're addressing?**

A forensic read of the live prod KG (2026-07-07, read-only, method per ADR-0087/FRE-636) surfaced a cluster of failures that all trace to **one** root:

- **Type-scatter under a single-exclusive taxonomy.** Real entities (`Arterial calcification`, `Hypertension`, `Halitosis`, …) scatter across ADR-0109's `Phenomenon` / `DomainOrTopic` / `QuantityMeasure` — none fits cleanly, and each entity is forced into exactly one.
- **Case-variant self-disagreement.** `Arterial calcification` typed `Phenomenon` while `Arterial Calcification` typed `DomainOrTopic`; `halitosis` vs `Halitosis` likewise. The same concept disagrees with itself because single-typing has nowhere to put "both," and there is no concept-hub / dedup mechanism.
- **Flattened relations.** A clinical directional link (hypertension → arterial-calcification) survives only as an **untyped, symmetric `RELATED_TO`** — direction and predicate lost.
- **Abstract-query recall miss.** Multipath recall (ADR-0104 multipath retrieval, enabled in the owner's live config as of 2026-07-07 — a runtime observation, not the ADR's status) still misses on abstract cues ("health issues") because it is embedding-similarity with **no categorical backstop** to enter through.

**The stance finding (why these are one problem, not four).** Reading the taxonomy and recall ADRs together (0097/0109 and 0100/0103/0104) shows an **unargued axiom**: every categorization axis in Seshat is modeled as *either* **closed-single-valued** *or* **open-free-text**, never as **multi-valued graded membership**.

- ADR-0109 **closed** the `type` axis to a single exclusive value (each type definition is written as inclusion **+ exclusion**; conflicts are resolved by *picking a winner* — "ruled by disambiguation, not waived"). Clean, but it forces the unanswerable *"is a `trie` a tool or an idea?"* Multi-parent / multi-label was **never enumerated as an alternative** in either ADR.
- ADR-0103 **refused to close** the `topic` axis (topic is free-text with no field) *because* closing it as a single-valued hard predicate is brittle (`topic="vision"` drops the note filed under `"perception"`). Correct — but it left no categorical backstop, which is the abstract-query miss.

Both sides hit the *same wall* from opposite ends. The brain's answer to that wall — a concept belongs to **several** superordinate categories at once, with graded strength (Rosch 1975; hub-and-spoke, Patterson/Ralph 2007) — is the **missing third option** neither ADR put on the table. The write-side scatter and the read-side miss are the *same* root seen twice.

**The reframe that scopes this ADR (owner, 2026-07-07).** Seshat is a domain-general "liberal-arts collaborator," **not** a health app and **not** a records store — health was merely the conversation that exposed the gap; no domain is privileged and there is no data-custody problem to design around (this closed-VPS point corrected an earlier over-cautious assumption). The owner does **not** want to rearchitect the production KG on an unproven bet. He wants to **explore an alternative memory model** — a self-organizing, multi-parent associative memory — as a **decoupled side project**, seeded from real Seshat data, that **may find its way back into Seshat if the research proves fruitful.**

**What needs to be decided.** Whether to run that study; the representational primitive it tests; where the plastic "learning" lives vs. where stable "recall" lives; how categories stay usable rather than becoming a snowflake of unusable connections; its isolation contract; and — the load-bearing part — a **falsifiable** success signal, so this is a *study* and not a nice-looking graph.

---

## Decision

**Run an isolated research study of a heterarchical associative memory, built on three separated stages — learn at ingest, consolidate offline, recall from the store — with a falsifiable core.** The study is domain-general, seeded from a frozen snapshot of live Seshat data, physically isolated from prod, and reversible (delete the sandbox). Integration-back is out of scope and deferred to a future ADR.

### D1 — Isolation & corpus (the study is a sandbox, not a prod change)

A **separate Neo4j instance** (its own container/volume) + the Graph Data Science plugin + Neo4j's native vector index. The corpus is a **one-time frozen export** of the prod KG **and conversation traces** — frozen so results are reproducible and re-runnable across parameter sweeps. The study is **read-only against prod and writes only to its own store**; it must never touch prod Neo4j/ES/Postgres (the FRE-375 substrate-isolation line applies unchanged). No prod code path is modified.

### D2 — The representational primitive: multi-valued, graded, multi-parent membership

A graph stores this natively — it is painful in SQL, first-class in a labeled property graph.

- `(:Concept {id, canonical_name, embedding, valence, arousal})` — the hub (hub-and-spoke). Case-variants collapse here: `(:Surface)-[:ALIAS_OF]->(:Concept)` binds `Arterial Calcification` and `arterial calcification` to **one** hub, so self-disagreement is structurally unrepresentable.
- `(:Concept)-[:MEMBER_OF {strength, source, provenance}]->(:Category)` — **the whole thesis on one edge**: multi-valued (many edges), graded (`strength`), multi-parent (N distinct parents), each carrying the encoding context that produced it.
- `(:Category)-[:SUBSUMES {strength}]->(:Category)` — categories form a **DAG (heterarchy)**, not a tree; a category may have several parents and levels (Rosch's superordinate/basic/subordinate).
- `(:Concept)-[:REL {predicate, weight}]->(:Concept)` — **typed and directional**, replacing symmetric `RELATED_TO` (hypertension →*causes*→ calcification).
- `(:Concept)-[:MENTIONED_IN {when}]->(:Episode)` — the episodic index (Tulving 1972; Eichenbaum relational memory), so recall can enter by *when/where* as well as by content.

### D3 — Learn at ingest: categorize the encoding event, not the bare entity

Categorization is decided **in-context, from the full conversation the term was used in** — never as a decoupled post-hoc pass. This is **encoding specificity** (Tulving & Thomson 1973) and *meaning-is-use* (Firth 1957: "you shall know a word by the company it keeps"): a term's categories are a function of its use-in-context; deciding them stripped of that context is *why* prod scatters. The ingest categorizer (an LLM reading the conversation) emits **graded multi-parent membership proposals that carry their encoding provenance** (which conversation, when, how used).

**Honest naming:** the "small fast net at ingest" is a *learning system whose memory is the graph* — an LLM categorizing in-context **plus** edge-accumulation in the graph **plus** the offline consolidator (D5). There are **no opaque neural-net weights anywhere**; the plasticity lives in the *edge dynamics*, which keeps the whole thing **inspectable** — the point of using a graph at all. (This deliberately rejects the fully-distributed "deep" store; see Alternatives.)

### D4 — Accrete, don't overwrite (this is what escapes the "old-dog" trap)

Each conversation is an **encoding event that adds** — a fresh conversation finds the existing hub and **MERGEs** new memberships/relations (first-seen creates, re-seen accumulates). A concept's richness = the **union of every context it was used in**; one conversation frames it broadly (a parent), another uses it differently (a *different* parent), a third co-mentions peers (siblings). Because learning is **additive, never overwriting**:

- **Plasticity:** a genuinely new context can *always* add a new parent — the dog can still learn a new trick.
- **Stability:** old parents are never destroyed to make room — no catastrophic forgetting by construction.
- **Worn-in:** `strength` deepens with reinforcement; grooves dominate recall but the thin new thread still exists. Set-in-its-ways **and** teachable at once — the stability–plasticity dilemma (Grossberg) dissolved by an accreting store.
- **Forgetting is deliberate:** unreinforced thin edges **decay by disuse** (a controlled TTL/floor), never accidental overwrite.

This also gives the distributional richness of embeddings ("meaning = sum of contexts") **while keeping every context as a traceable, provenance-bearing edge** — the thing pure embeddings throw away.

### D5 — Consolidate offline: the anti-snowflake engine (without it, unusable connections)

A **slow, offline** process (runs on a cadence, **never in the recall path** — this is "sleep") keeps the fast, messy ingest output usable. Full pipeline: (1) **canonicalize categories** — merge scattered parent nodes whose names are near-synonyms and/or whose member-sets overlap (`heart stuff` / `Cardiovascular health` / `cardiovascular` → one node — entity-resolution applied to *parents*); (2) **detect emergent structure** — GDS community detection (Leiden) at 2–3 resolutions proposes categories bottom-up as a check on free-text names, yielding Rosch's levels; (3) **prune the incoherent tail** — drop memberships that are weak **and** stale **and** incoherent with the concept's dominant neighborhood; (4) **schematize** *(later)* — write a gist onto stable categories, promote a recurrent pattern to a durable semantic edge (ADR-0105 episodic→semantic); (5) **reinforce/decay** — the worn-in clock.

**v0 stub (built first, deliberately minimal):** **only** (1) canonicalize-categories and (3′) decay+prune. Op 1 alone kills most of the snowflake; decay+prune keeps it from regrowing. Community detection, schematization, and semantic promotion are v1+.

The consolidator has **one knob and one cheap metric**: the knob is a **merge threshold θ** (Grossberg's *vigilance* reincarnated — high θ under-merges/scatter survives, low θ over-merges/collapses to "stuff"); the metric is the **category-count-vs-conversations curve** (no consolidator → linear growth = the snowflake; healthy → the curve **plateaus**). The study **sweeps θ** and reads that curve.

### D6 — Salience is the counterweight (frequency alone is an echo chamber)

Edge `strength` is **frequency × salience**, not raw frequency — otherwise the worn-in grooves entrench what is *talked about most*, not what *matters most*, and rich-get-richer crowds thin new threads out at recall (functionally-unteachable even while technically-teachable). Salience is the owner's **survival/valence → goal-relevance** axis (Nairne 2007 survival-processing advantage; LeDoux 2012; Russell's valence×arousal circumplex) — an imposed prior, the one axis the brain does not leave to emerge. Recall additionally carries a **novelty/diversity term** (surface a thin thread sometimes — exploration, not pure exploitation), and salience directly serves the pedagogical North Star (ADR-0084): high-salience ≈ needs-revisiting.

### D7 — Recall from the settled store, scoped honestly

Recall reads the **consolidated** store and can **enter through any parent** (the categorical backstop prod lacks), any episode, or content. It is scoped **honestly as an exploration / abstract-recall win** — for cues like "health issues" / "what have I discussed about X" where prod **misses entirely**. It is **not** a precision fix: ADR-0103 proved the "on-the-topic vs is-the-answer" separation is *structural* and it still lives *inside* each category. This study is **additive to ADR-0104** (a candidate arm/backstop) and does **not** repeal ADR-0103. Overclaiming precision would be the lie the No-BS criteria exist to catch (AC-6).

### D8 — The falsifiable core

The study succeeds or fails on one question, not on vibes: **can a swept-θ consolidator make the category-count curve plateau while keeping categories legible and abstract-query recall beating the baseline on the same frozen corpus?** The baseline is the *current* model reproduced on the snapshot: single-exclusive type + embedding-similarity recall. Everything in AC-1…AC-6 discriminates against a broken or half-built implementation.

---

## Alternatives Considered

### Option 1: Rearchitect the production KG directly (amend ADR-0109 to multi-parent in place)
**Description:** Make the live KG multi-parent now — change extraction, storage, and recall in prod.
**Pros:**
- If it works, no second migration; benefits land immediately.
- One system to maintain, not two.
**Cons:**
- High blast radius on the owner's live memory, on an **unproven** representational bet.
- The very thing in doubt (does multi-parent help?) would be risked against real data before it is answered.
- Irreversible-ish; rollback is a second migration.
**Why Rejected:** The owner explicitly wants **test-and-learn, decoupled** — not a live rewrite on a hypothesis. Study-first is lower-risk, reversible (delete the sandbox), and answers the question *before* betting prod on it.

### Option 2: Do nothing structural — extend the existing taxonomy as needed (add types to ADR-0109)
**Description:** Treat the scatter as a missing-category problem and add types (e.g., a health/`MedicalCondition` type) when evidence demands, per ADR-0097's "add a class when evidence demands it."
**Pros:**
- Cheapest; stays inside a validated, measured paradigm.
- No new substrate.
**Cons:**
- Leaves the **single-exclusive-type axiom untouched** — the actual root. Adding types cannot represent "both parents at once."
- Case-variant self-disagreement and abstract-query miss are **structural to single-typing**; more types do not fix them.
- Invites domain-specific type sprawl the domain-general design rejects.
**Why Rejected:** It optimizes the wrong axis. The problem is not *too few types*; it is *one-type-per-entity*. This ADR exists to test that axiom, not to patch around it.

### Option 3: The "deep" fully-distributed representation (a vector/Hopfield associative net *is* the store)
**Description:** Drop discrete types entirely; memory is a learned vector space, recall is pattern-completion, categories are emergent directions.
**Pros:**
- Most brain-faithful; multi-parent falls out as superposition; genuinely self-organizing.
**Cons:**
- **Fuses learning and recall** — a store that re-learns on every write drifts and forgets (the category error the owner caught: a constantly-mutating net is *learning*, not *recall*).
- Opaque (categories are directions you must probe for), **catastrophic forgetting**, capacity limits / spurious attractors, ungovernable provenance — maintenance is model-ops, not DB-ops.
- Worst path back into a graph-shaped Seshat.
**Why Rejected:** Its good idea (associative encoding) is **kept and relocated** to the ingest stage (D3), while recall reads a stable, inspectable graph (D7). We take the association without the opacity.

### Option 4: Shallow enrichment — soft category labels layered on the existing entities, no accretion/consolidation
**Description:** Keep entities as-is; attach soft cluster labels once; no ingest-time contextual learning, no offline consolidator.
**Pros:**
- Simplest to build; trivially composes back.
**Cons:**
- Barely challenges the axiom — a one-shot label is still an afterthought decoupled from conversation (the exact mistake in D3).
- No developmental arc, no worn-in dynamics, no anti-snowflake engine — so it cannot answer the interesting question.
**Why Rejected:** Too timid to be a real test; it would "pass" without proving anything discriminating.

---

## Consequences

### Positive Consequences
- **Tests Seshat's most foundational, unexamined memory axiom** cheaply and reversibly, before betting prod on it.
- If it holds, **one primitive** (graded multi-parent membership) fixes *both* the write-side scatter/self-disagreement *and* the read-side abstract-query miss.
- **Fully inspectable** — no opaque net; the graph is the ledger of the learning, with per-edge provenance (distributional richness *with* audit).
- **Domain-general** — no privileged domains; the health cases are just fixtures.
- Directly feeds the **pedagogical North Star** (ADR-0084): salience ≈ needs-revisiting; multi-parent enables cross-thread correlation.
- A clean, reusable **baseline harness** (single-type + embedding recall on the frozen snapshot) that other memory work can reuse.

### Negative Consequences
- **A second substrate to stand up and run** (isolated Neo4j + GDS) for the study's duration.
- The **consolidator is unproven and the riskiest component** — canonicalization can over- or under-merge.
- Multi-parent buys **no precision**; it must be reported honestly as an exploration/recall result only.
- A study can legitimately return a **null result** (multi-parent ties the baseline) — that is a valid, budgeted outcome, not a failure to hide.
- Integration-back is a *separate, later* cost not covered here.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Category scatter merely **relocates to the parents** (free-text category names) | High | The consolidator's canonicalize step (D5-op1) is *designed for exactly this*; AC-3 makes plateau-without-collapse the pass condition, swept over θ. |
| Consolidator **over-merges** (everything collapses to "stuff") or **under-merges** (snowflake persists) | High | Sweep θ; AC-3 requires a θ with *both* plateau *and* human-legible top-N categories — a bad θ fails, not passes. |
| Frequency entrenches an **echo chamber** at recall | Medium | Salience-weighted strength + novelty term at recall (D6); AC checks abstract-recall *quality*, not raw frequency. |
| Study **writes to prod substrate** by accident | High | Separate instance + FRE-375 line; AC-5 asserts zero prod writes. |
| Study becomes a **rabbit hole** with no verdict | Medium | Falsifiable core (D8) + v0 stub consolidator (2 ops) + one metric; the study is scoped to answer AC-3/AC-4, not to build a product. |
| **Null result** read as wasted effort | Low | A clean null (with the baseline harness + the reproducible corpus) is an accepted, valuable outcome and informs whether to stop. |

---

## Implementation Notes

- **Substrate:** isolated Neo4j instance (own volume) + GDS plugin + native vector index; separate compose service, never wired to prod URIs (FRE-375).
- **Corpus:** one-time frozen export of prod `:Entity`/relationships + conversation traces into the sandbox; document the snapshot SHA/date for reproducibility.
- **Ingest categorizer:** LLM reads full conversation → emits graded multi-parent membership proposals with provenance; writer MERGEs (accretes) into the sandbox graph (D2/D3/D4).
- **Consolidator v0:** offline job — (1) canonicalize categories (cosine on names + member-set overlap, threshold θ); (2) decay+prune (multiply unreinforced strengths, drop below floor). v1+: Leiden communities, schematize, semantic promotion.
- **Baseline harness:** reproduce the current model on the same snapshot — single-exclusive type + embedding-similarity recall — for head-to-head scoring.
- **Metrics:** category-count-vs-conversation curve across θ; abstract-query recall (study vs baseline) on a held-out abstract-cue set; a legibility spot-check of top-N categories.
- **Decomposition:** substrate+snapshot → schema+writer (ingest/accretion) → baseline harness → consolidator v0 + θ sweep → study synthesis/writeup. Sequenced with dependencies in Linear (Step 5).

---

## Verification / Acceptance Criteria

**How will we know this study actually delivered — not just merged?** Each criterion is outcome-level, **population-scale (not fixture-scale)**, and able to fail; a broken or half-built implementation must not pass. All numeric bars are **pre-registered** — fixed *before* the run and recorded in the study plan; they may be tuned only before results are seen, never after (this is what defeats cherry-picking). The frozen snapshot's own statistics set the eligible populations, so the bars are checkable without new infrastructure.

- **AC-1 — Multi-parent accretion holds across the population, not on a fixture.** Define the **eligible set** E = concepts mentioned in ≥2 conversations whose contexts differ (computable from the snapshot). Pass = **median `MEMBER_OF` degree over all of E is ≥2**, *and* **≥60% of E carry ≥2 provenance-distinct memberships** (each edge's `provenance` names a different source conversation). · **Check:** one Cypher aggregation over E (not a hand-picked node). · *Fails if* the median degree over E is 1, or the ≥60% bar is missed — i.e., a few showcased concepts accrete while the population stays single-parent, or memberships lack provenance.
- **AC-2 — Canonicalization holds corpus-wide, not on the two trigger strings.** Build V = the full set of case-/near-variant surface pairs in the snapshot (normalized-string match). Pass = **≥95% of V resolve to a single shared `:Concept` hub** (both surfaces `ALIAS_OF` the same hub). The two named trigger pairs are reported as spot-checks but are *not* the bar. · **Check:** enumerate V from the snapshot; measure the shared-hub resolution rate. · *Fails if* corpus-wide resolution is <95% — special-casing the two fixtures cannot move a population bar.
- **AC-3 — The consolidator plateaus without collapse (the falsifiable core), operationalized.** Conversations are processed in **chronological snapshot order** (the order they actually occurred); the plateau result must **also hold under ≥2 additional pre-registered fixed permutations** (robustness against front-loading category-rich conversations). Sweep θ. For each θ compute (a) **plateau**: fit the canonical-category count N(c) vs conversations-processed c; the marginal new-categories-per-100-conversations in the **final tertile** is **≤25% of the first-tertile rate**, *and* the whole curve sits strictly **below the no-consolidator control** (which grows ≈linearly) — under chronological order and each permutation; (b) **legibility**: the **top-20** categories by member count, rated by **exactly 2 independent judges** on a 3-point rubric (coherent / mixed / incoherent); a category counts "coherent" only when **both judges rate it coherent (unanimous)**; pass = **≥70% of the top-20 coherent AND Cohen's κ ≥ 0.4** between the two judges; (c) **non-collapse**: total canonical-category count is **≥ a pre-registered floor** (so "one bucket called *stuff*" fails legibility *and* this floor). Pass = **∃ θ satisfying (a) ∧ (b) ∧ (c)**. · **Check:** the θ-sweep plots (per ordering) + the rated top-20 table. · *Fails if* every θ is either ≈linear vs the control (snowflake/under-merge), order-dependent (plateau only under one ordering), or trips the collapse floor / legibility bar (over-merge).
- **AC-4 — Abstract-query recall beats the reproduced baseline by a pre-registered, significant margin — at the same operating point as AC-3.** A **single θ\*** is selected (by a pre-registered rule) from the θ that pass AC-3, and **fixed before any AC-4 scoring**; AC-4 is evaluated **only at that θ\*** (no re-sweeping θ for recall — the plateau/legibility point and the recall-win point must be the *same* configuration). Cue set = **≥30 abstract cues** spanning ≥4 snapshot domains; **both the cues AND their gold neighborhoods are pre-registered and frozen before any study/baseline scoring**, and annotation (one annotator + a second adjudicating disagreements) is **blind to both systems' outputs** (labels cannot be adjusted after seeing results). Systems: **study** (enter-through-membership) vs **baseline** (single-exclusive-type + embedding similarity) on the **same** frozen corpus. Metrics: **Recall@20 (primary)** and **nDCG@20 (ranking guardrail)** vs gold. Pass = study beats baseline by **≥10% relative Recall@20, paired across the ≥30 cues with p<0.05** (paired test), **AND study nDCG@20 does not significantly regress** (study nDCG@20 ≥ baseline, no significant decrease) — so a win cannot come from dumping gold items low in the top-20. · **Check:** the frozen cue+gold set, the scored table, and both significance tests. · *Fails if* the Recall win is absent / below 10% / not significant, or nDCG@20 significantly regresses — a single-query or badly-ranked marginal win does not pass.
- **AC-5 — Isolation is actively verified, not just grepped.** Two-sided proof: (1) **the study env cannot reach prod** — prod credentials are absent from the study environment and a deliberate connection attempt to the prod bolt/ES/PG URIs from the study env **fails** (no route/creds); (2) **prod shows zero attributable writes** — either (i) the study runs against the frozen snapshot during a **quiesced prod window** (no live turns), so prod Neo4j node+relationship counts (and ES/PG equivalents) before/after must show **exact zero deltas**; or (ii) if prod cannot be quiesced, prod's **write-audit log filtered by client identity** over the study window shows **zero writes from the study's client/credentials** (every observed prod write is positively attributed to a non-study client). · **Check:** the failed-connection log + (i) exact-zero prod deltas over the quiesced window **or** (ii) the client-attributed write-audit. · *Fails if* prod creds are reachable from the study env, or any prod write is attributable to the study (or the noise cannot be attributed away).
- **AC-6 — No precision overclaim (honest-scope guard, not delivery evidence).** The study report **separates** abstract-cue results (AC-4) from precise-answer-cue results, shows the precise-query numbers, and does **not** assert a precision improvement over ADR-0103's structural limit. · **Check:** both result sets present in the writeup. · *Note:* this guard prevents overclaiming; it is **not** counted as evidence the study worked (that is AC-1..AC-5). · *Fails if* the two are conflated or a precision win is claimed.

**Seam owner (decomposed-ADR seam):** the **study-synthesis ticket** owns the assembled intent — **AC-3 ∧ AC-4 holding together at a single pre-registered θ\*, end-to-end** (ingest → accretion → consolidator → baseline), with AC-1/AC-2/AC-5 as gates and AC-6 honored. The ADR does **not** close because the last child (e.g. the consolidator) merged; it closes only when the synthesis ticket shows that **at one θ\*** the whole loop plateaus-without-collapse **and** beats baseline by the pre-registered significant margin on the frozen corpus.

---

## References

- `docs/architecture_decisions/ADR-0109-entity-taxonomy-redesign.md` — the single-exclusive 10-type taxonomy this study reframes (not amends)
- `docs/architecture_decisions/ADR-0097-ingested-knowledge-taxonomy.md` — "prefer fewer classes; add on evidence" stance; orthogonal class axis
- `docs/architecture_decisions/ADR-0098-memory-substrate-and-lifecycle-architecture.md` — P/W/S class + substrate the snapshot is drawn from
- `docs/architecture_decisions/ADR-0100-relevance-bounded-recall.md` — the false-negative recall problem this addresses from the category side
- `docs/architecture_decisions/ADR-0103-recall-no-clean-floor-structural-separation.md` — "structure-where-closed, semantic-where-open"; the precision limit this study does NOT repeal
- `docs/architecture_decisions/ADR-0104-multi-path-retrieval-rank-fusion.md` — multipath recall; this study is a candidate additive backstop, not a replacement
- `docs/architecture_decisions/ADR-0105-convergent-self-improvement-pipeline-and-system-graph.md` — episodic→semantic consolidation this study's D5-op4 extends
- `docs/architecture_decisions/ADR-0106-system-user-knowledge-boundary-dispatch-observe-ground.md` — provenance boundary (scope-distinct: this ADR is about categorization, not System/User routing)
- `docs/architecture_decisions/ADR-0084-pedagogical-architecture-socratic-tutor-layer.md` — pedagogical North Star; salience ≈ needs-revisiting
- `docs/architecture_decisions/ADR-0087-memory-recall-quality-measurement-program.md` — measurement-first recall methodology (used for the read-only forensic)
- FRE-375 test/eval substrate isolation policy — the never-write-prod line the study inherits
- Rosch (1975) graded categories; Patterson, Nestor & Rogers (2007) hub-and-spoke — multi-parent, multi-dimensional concepts
- Tulving (1972) episodic/semantic; Tulving & Thomson (1973) encoding specificity — categorize the encoding event
- Firth (1957) "know a word by the company it keeps" — meaning-is-use / distributional richness
- Collins & Loftus (1975) spreading activation; Anderson ACT-R — associative reach
- Marr (1971) / hippocampal CA3 — pattern completion
- McClelland, McNaughton & O'Reilly (1995) Complementary Learning Systems — fast/slow, replay, consolidation
- Grossberg (Adaptive Resonance Theory, 1980s) — stability–plasticity, the vigilance (θ) knob
- Nairne (2007) survival-processing advantage; LeDoux (2012) survival circuits; Panksepp (1998); Russell circumplex — the salience prior
- Bartlett (1932) schema/schematization — compression as consolidation
- Neo4j Graph Data Science — community detection, PageRank, node embeddings (the self-organization engine)
- `/tmp/.../adr-kickoff-memory-kg.md` — owner-directed kickoff framing (session artifact)

---

## Status Updates

### 2026-07-07 - Proposed
**Changed By:** Project owner (adr session, Opus)
**Reason:** Discussion-first design settled with the owner across a multi-round Remote Control session; owner ratified the shape ("this represents my mental model") and directed write-up. Recorded as a **research study proposal**; the integration-back decision is explicitly a future ADR gated on AC-3/AC-4. Implementation tickets to be filed (Needs Approval) and sequenced per Step 5.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
