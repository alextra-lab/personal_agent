# ADR-0114: Heterarchical Associative Memory — a Decoupled Research Study (learn-at-ingest, consolidate-offline, recall-from-store)

**Status:** Proposed
**Date:** 2026-07-07 (revised 2026-07-10)
**Deciders:** Project owner (adr session, Opus)
**Tags:** memory, knowledge-graph, categorization, associative-memory, self-organizing, consolidation, research-study, neo4j, gds
**Backing tickets:** FRE-837 (umbrella); implementation sub-issues sequenced in Linear (see Status Updates)

**Scope discipline:** this ADR decides to **run an isolated research study**, seeded from a frozen snapshot of live Seshat data. It does **not** change the production KG, does **not** amend ADR-0109, and does **not** decide how (or whether) any of this returns to Seshat — that is a **future ADR, gated on this study's results**. Every "the system does X" below describes the *study sandbox*, never prod.

**Posture:** this is **exploratory, amateur research**, and it reads that way on purpose. The cognitive-science material is *inspiration* for a design, not evidence that the design is brain-faithful; the claims are hypotheses with pre-registered ways to be wrong, not results. Where a component is speculative it is labelled speculative. No inflated ego — the study earns its conclusions one falsifiable arm at a time.

---

## Context

**What is the issue we're addressing?**

A forensic read of the live prod KG (2026-07-07, read-only, method per ADR-0087/FRE-636) surfaced a cluster of symptoms that all trace to **one** root:

- **Type-scatter under a single-exclusive taxonomy.** Real entities (`Arterial calcification`, `Hypertension`, `Halitosis`, …) scatter across ADR-0109's `Phenomenon` / `DomainOrTopic` / `QuantityMeasure` — none fits cleanly, and each entity is forced into exactly one.
- **Case-variant self-disagreement.** `Arterial calcification` typed `Phenomenon` while `Arterial Calcification` typed `DomainOrTopic`; `halitosis` vs `Halitosis` likewise. The same concept disagrees with itself because single-typing has nowhere to put "both," and there is no concept-hub / dedup mechanism.
- **Flattened relations.** A clinical directional link (hypertension → arterial-calcification) survives only as an **untyped, symmetric `RELATED_TO`** — direction and predicate lost.
- **Abstract-query recall miss.** Multipath recall (ADR-0104 multipath retrieval, enabled in the owner's live config as of 2026-07-07 — a runtime observation, not the ADR's status) still misses on abstract cues ("health issues") because it is embedding-similarity with **no categorical backstop** to enter through.

**The root, stated precisely (revised 2026-07-10).** An earlier draft named the disease as "the single-exclusive-*type* axiom." That is the wrong altitude — it conflates two different questions that do not need the same representation:

1. **What *kind* of entity is this?** (a stable, mostly-exclusive ontological fact — `Phenomenon`, `MethodOrConcept`, `DomainOrTopic`, …)
2. **Through which semantic *subjects* should this entity be recalled?** (a graded, context-grown, many-valued index)

The real root is that Seshat currently forces question 2 to be answered by question 1's machinery: **the entity *kind* is being used as the system's only categorical retrieval index.** `Liver dysfunction` does not stop being a `Phenomenon`; it simply also needs to be reachable through *health issue*, *liver health*, *organ dysfunction*, *clinical concern* — and the kind axis has exactly one slot. Type-scatter, case-variant disagreement, and the abstract-query miss are all symptoms of overloading one axis with two jobs, not evidence that the kind is wrong. (This is consistent with fine-grained entity typing being a *multi-label* problem in the literature, with corpus-level and context-level evidence as complementary signals.)

**The three-axis reframe.** The design this study tests therefore keeps three things orthogonal:

- **Entity identity** — the concept hub.
- **Stable entity kind** — the ADR-0109 taxonomy value, *preserved as-is and carried as a control* (the study does not delete or re-decide it).
- **Dynamic associative category memberships** — the new, graded, multi-parent, context-grown retrieval index.

So the study's question is **not** "should `Phenomenon` / `MethodOrConcept` / `DomainOrTopic` disappear?" It is "does adding an orthogonal associative-category index *improve recall*, on top of a preserved entity kind?" That reframe is what keeps the study additive to ADR-0109 rather than a covert repeal of it.

**The stance finding (why the write-side and read-side are one problem).** Reading the taxonomy and recall ADRs together (0097/0109 and 0100/0103/0104) shows an **unargued axiom**: every categorization axis in Seshat is modeled as *either* **closed-single-valued** *or* **open-free-text**, never as **multi-valued graded membership** *carried on a separate axis*.

- ADR-0109 **closed** the `type` axis to a single exclusive value (each type definition is written as inclusion **+ exclusion**; conflicts are resolved by *picking a winner*). Clean for *kind*, but useless as a *retrieval index* — it forces the unanswerable *"is a `trie` a tool or an idea?"* when that question was never the point.
- ADR-0103 **refused to close** the `topic` axis (topic is free-text with no field) *because* closing it as a single-valued hard predicate is brittle (`topic="vision"` drops the note filed under `"perception"`). Correct — but it left no *structured* categorical backstop, which is the abstract-query miss.

Both sides hit the *same wall* from opposite ends. Cognitive models motivate the missing third option — a concept belongs to **several** superordinate categories at once, with graded strength (Rosch 1975; hub-and-spoke, Patterson/Ralph 2007) — carried on its *own* axis rather than smuggled into the kind. The study tests whether an inspectable property-graph approximation of that idea yields the operational benefit; it does not claim the graph *is* how a brain works.

**The reframe that scopes this ADR (owner, 2026-07-07).** Seshat is a domain-general "liberal-arts collaborator," **not** a health app and **not** a records store — health was merely the conversation that exposed the gap; no domain is privileged and there is no data-custody problem to design around (this closed-VPS point corrected an earlier over-cautious assumption). The owner does **not** want to rearchitect the production KG on an unproven bet. He wants to **explore an alternative memory model** — a self-organizing, multi-parent associative memory — as a **decoupled side project**, seeded from real Seshat data, that **may find its way back into Seshat if the research proves fruitful.**

**What needs to be decided.** Whether to run that study; the representational primitive it tests; where the plastic "learning" lives vs. where stable "recall" lives; how categories stay usable rather than becoming a snowflake of unusable connections; its isolation contract; how it can *continue* under its own momentum if v0 succeeds; and — the load-bearing part — a **falsifiable** success signal, so this is a *study* and not a nice-looking graph.

---

## Decision

**Run an isolated research study of a heterarchical associative memory, built on three separated stages — learn at ingest, consolidate offline, recall from the store — with a falsifiable core and a pre-built continuation path.** The study is domain-general, seeded from a frozen snapshot of live Seshat data, physically isolated from prod, and reversible (delete the sandbox). Integration-back is out of scope and deferred to a future ADR.

### D1 — Isolation & corpus (the study is a sandbox, not a prod change)

A **separate Neo4j instance** (its own container/volume) + the Graph Data Science plugin + Neo4j's native vector index. The corpus is a **one-time frozen export** of the prod KG **and conversation traces** — frozen so results are reproducible and re-runnable across parameter sweeps. The study is **read-only against prod and writes only to its own store**; it must never touch prod Neo4j/ES/Postgres (the FRE-375 substrate-isolation line applies unchanged). No prod code path is modified.

### D2 — The representational primitive: an evidence layer beneath a derived multi-parent index

A graph stores this natively — it is painful in SQL, first-class in a labeled property graph. The single most important revision (2026-07-10) is to **separate immutable evidence from the derived, disposable index** so that "accrete, never overwrite" (D4) and "decay/prune weak edges" (D5) stop contradicting each other.

**Evidence layer (immutable — never decays, always auditable):**

- `(:Episode)-[:HAS_MENTION]->(:Mention)-[:REFERS_TO]->(:Concept)` — each time a concept is used, in one conversation, is a `Mention`. Mentions are append-only.
- `(:Mention)-[:PRODUCED]->(:MembershipAssertion)` where `(:MembershipAssertion)-[:ABOUT]->(:Concept)` and `-[:PROPOSES]->(:Category)`, carrying `{proposed_confidence, model, prompt_version, seed, when}`. This is the categorizer's *raw claim*, frozen with its encoding provenance. It is never rewritten or pruned.

**Concept + preserved kind:**

- `(:Concept {id, canonical_name, embedding, kind, valence, arousal})` — the hub (hub-and-spoke). `kind` is the **preserved ADR-0109 entity type**, carried as a control, not re-decided. Case-variants collapse here: `(:Surface)-[:ALIAS_OF]->(:Concept)` binds `Arterial Calcification` and `arterial calcification` to **one** hub, so self-disagreement is structurally unrepresentable.

**Derived layer (recomputable — a projection of the evidence, safe to suppress/recompute):**

- `(:Concept)-[:MEMBER_OF {membership_confidence, support_count, last_supported_at}]->(:Category)` — **the thesis on one edge**: multi-valued (many edges), graded (`membership_confidence`), multi-parent (N distinct parents). It is a *materialised projection* recomputed from the underlying `MembershipAssertion`s — `support_count` = how many distinct-context assertions back it, `membership_confidence` = the epistemic strength derived from them. Because it is derived, the consolidator may drop or recompute it **without destroying evidence**.
- `(:Category)-[:SUBSUMES {strength}]->(:Category)` — categories form a **DAG (heterarchy)**, not a tree (Rosch's superordinate/basic/subordinate). Populated in v1 (see D9); the edge type exists in the schema from day one so v1 needs no migration.
- `(:Concept)-[:MENTIONED_IN {when}]->(:Episode)` — the episodic index (Tulving 1972; Eichenbaum relational memory), so recall can enter by *when/where* as well as by content.

**Typed relations (a separate concern — see D9 arm and Risks):**

- `(:RelationAssertion {predicate_surface, canonical_predicate, polarity, modality, confidence, provenance})` linking two concepts, **directional**, replacing symmetric `RELATED_TO`. Typed-relation extraction is *not* part of the core category-recall hypothesis; it is scoped to its own arm with its own gold set, precisely so relation gains cannot leak into recall and be misattributed to categories.

**Why the evidence layer earns its keep.** If three conversations independently classify `Liver dysfunction` (medication context → *adverse effect* 0.81; results context → *liver health* 0.94; general-review context → *health issue* 0.88), those are **three immutable assertion records**. The `MEMBER_OF` edge is a *derived projection* of them. Without the split, the system would either overwrite provenance, grow arrays of provenance-ids on one edge, create duplicate edges with unclear aggregation, or destroy historical evidence during pruning. The rule that resolves all of it: **evidence never decays; retrieval activation may decay; derived membership edges may be suppressed or recomputed, but their supporting assertions remain auditable.**

### D3 — Learn at ingest: categorize the encoding event, not the bare entity

Categorization is decided **in-context, from the full conversation the term was used in** — never as a decoupled post-hoc pass. This is **encoding specificity** (Tulving & Thomson 1973) and *meaning-is-use* (Firth 1957: "you shall know a word by the company it keeps"): a term's categories are a function of its use-in-context; deciding them stripped of that context is *why* prod scatters. The ingest categorizer (an LLM reading the conversation) emits **`MembershipAssertion`s** — graded multi-parent proposals that carry their encoding provenance (which conversation, when, how used, and the `model/prompt_version/seed` that produced them).

**Honest naming:** the "small fast net at ingest" is a *learning system whose memory is the graph* — an LLM categorizing in-context **plus** assertion-accumulation in the evidence layer **plus** the offline consolidator (D5). There are **no opaque neural-net weights anywhere**; the plasticity lives in the *edge dynamics*, which keeps the whole thing **inspectable** — the point of using a graph at all. (This deliberately rejects the fully-distributed "deep" store; see Alternatives.)

### D4 — Accrete, don't overwrite (this is what escapes the "old-dog" trap)

Each conversation is an **encoding event that adds a `Mention` + its `MembershipAssertion`s** — never an overwrite. A fresh conversation finds the existing hub (via `ALIAS_OF`) and **appends** new assertions; the derived `MEMBER_OF` edges are then recomputed from the enlarged evidence set (first-seen creates a parent, re-seen deepens `membership_confidence`/`support_count`). A concept's richness = the **union of every context it was used in**. Because the *evidence* is additive and immutable:

- **Plasticity:** a genuinely new context can *always* add a new assertion → a new parent — the dog can still learn a new trick.
- **Stability:** old assertions are never destroyed — no catastrophic forgetting of *evidence* by construction.
- **Worn-in:** `membership_confidence` deepens with reinforcing assertions; grooves dominate recall but the thin new thread still exists. Set-in-its-ways **and** teachable at once — the stability–plasticity dilemma (Grossberg) that an accreting *evidence* store is designed to sidestep (a design aim the study tests, not a solved result).
- **Forgetting is deliberate and reversible:** the consolidator may **suppress a derived `MEMBER_OF` edge** that is weak+stale+incoherent, but the assertions that backed it remain — so a later reinforcing context can resurrect the membership. Recall-time *activation* decays; evidence does not.

This gives the distributional richness of embeddings ("meaning = sum of contexts") **while keeping every context as a traceable, provenance-bearing assertion** — the thing pure embeddings throw away.

### D5 — Consolidate offline: the anti-snowflake engine (and where GDS actually lives)

A **slow, offline** process (runs on a cadence, **never in the recall path** — this is "sleep") keeps the fast, messy ingest output usable. This is also **the only place GDS integrates** — every GDS algorithm here is batch (project a subgraph, run, write back), so none of them sit in the online recall path. Full pipeline: (1) **canonicalize categories** — merge scattered parent nodes whose names are near-synonyms and/or whose member-sets overlap; (2) **detect emergent structure** — GDS community detection proposes categories bottom-up as a *check* on free-text names; (3) **prune the incoherent tail** — suppress derived memberships that are weak **and** stale **and** incoherent with the concept's dominant neighborhood (evidence retained); (4) **schematize** *(later)* — write a gist onto stable categories, promote a recurrent pattern to a durable semantic edge (ADR-0105 episodic→semantic); (5) **reinforce/decay** — the worn-in clock, on derived edges only.

**Canonicalization is a two-stage decision, not one threshold (revised 2026-07-10).** A single merge threshold cannot tell a synonym from a broader/narrower pair. So:

- **Stage 1 — candidate generation (this is GDS's v0, load-bearing job).** **GDS Node Similarity / KNN** over category nodes proposes top-*k* candidate pairs by **member-set overlap** (Jaccard on shared members), combined with cosine on category-name embeddings. *(Honest scale note: at v0 sandbox size a plain Cypher Jaccard aggregation would also work; GDS earns its place by returning top-k candidates directly and by scaling past the sandbox — it is the designated mechanism, not a strict requirement at small n.)*
- **Stage 2 — typed decision.** An LLM/rule step labels each candidate `ALIAS_OF` (merge), `SUBSUMED_BY` (a hierarchy edge — **do not merge**), `RELATED` (associate), `DISTINCT` (keep separate), or `uncertain` (manual-review bucket). v0 fires **only** `ALIAS_OF` merges and records the rest; the distinction still matters in v0 because *merging a broader parent into a narrower one is a correctness error that corrupts the plateau metric*, not a tuning artefact.

**GDS in v1 (proposer, not oracle).** **Leiden** at 2–3 resolutions (with intermediate communities) on the Concept↔Concept co-membership graph proposes categories bottom-up as an independent check on the LLM's names and as candidate `SUBSUMES` edges — approximating Rosch's levels. **Caveat, stated plainly:** *Leiden community IDs are not a valid `SUBSUMES` taxonomy by themselves* — they are a hypothesis generator, LLM/human-validated before adoption, never ground truth. (Note the symbol clash this avoids: Leiden's own parameters include `theta`, `gamma`, `randomSeed`; the study's merge knob is renamed accordingly — below.)

**GDS candidate signals (speculative, labelled).** **FastRP** structural node embeddings (topology-derived, orthogonal to text embeddings) are a *possible* recall-fusion arm (v1, not committed). **PageRank/centrality** is a *diagnostic* (which concepts are structural hubs) — explicitly **not** the salience source (D6); conflating structural centrality with the owner's imposed salience prior would be exactly the overclaim this revision strips.

**v0 stub (built first, deliberately minimal):** **only** (1) canonicalize-categories (two-stage, alias-merges only) and (3′) decay+prune (suppress derived edges, retain evidence). The *hypothesis* (which AC-3 tests, not a foregone result) is that op 1 alone removes most of the snowflake and decay+prune prevents it regrowing. Community detection, schematization, and semantic promotion are v1+.

**The knob and the cheap metric.** The consolidator has **one knob** — a **merge threshold τ_merge** (Grossberg's *vigilance* reincarnated — high τ_merge under-merges/scatter survives, low τ_merge over-merges/collapses to "stuff"; renamed from θ to avoid the Leiden parameter clash). The cheap health metric is the **canonical-category-count-vs-conversations curve** (no consolidator → linear growth = the snowflake; healthy → the curve **plateaus**). The study **sweeps τ_merge** and reads that curve — but the plateau is a *health signal*, not the falsifiable core (see D8).

**Freeze the proposal ledgers across the sweep (revised 2026-07-10).** The corpus is frozen, but the categorizer's *outputs* must be frozen too, or the τ_merge sweep confounds extraction stochasticity with the knob. Procedure: run the categorizer *N* times with fixed model/prompt/version at fixed seeds, store the resulting `MembershipAssertion` ledgers immutably (the evidence layer *is* this ledger), then run **every** consolidation configuration against the **same** ledgers. Report within-seed and across-seed variance. Only τ_merge varies within a seed.

### D6 — Separate epistemic strength from recall priority (frequency alone is an echo chamber)

An earlier draft defined edge `strength = frequency × salience`, which couples two different meanings. The revision (2026-07-10) **stores them separately**:

- **Epistemic (on the derived `MEMBER_OF` edge):** `membership_confidence`, `support_count`, `last_supported_at` — *is this membership well-supported by the evidence?* A frequently-discussed but low-salience membership can be epistemically **certain**.
- **Recall-time activation (computed at query time, not stored on the membership):** `salience`, `recency`, `novelty`, `goal_relevance` — *how much should this surface for this user, now?* A one-time high-salience event can deserve recall **priority** without earning a stronger *ontological* membership.

Salience remains the owner's **survival/valence → goal-relevance** axis (Nairne 2007 survival-processing advantage; LeDoux 2012; Russell's valence×arousal circumplex) — an imposed prior, the one axis the study does not leave to emerge (and *not* GDS PageRank). Recall combines the two families explicitly:

```
S = w_s·S_semantic + w_m·S_membership + w_r·S_recency + w_i·S_salience + w_n·S_novelty
```

so membership stays an **epistemic** claim while recall priority stays **user/context-sensitive** — and the two can be tuned independently. The novelty term (surface a thin thread sometimes — exploration, not pure exploitation) directly serves the pedagogical North Star (ADR-0084): high-salience ≈ needs-revisiting.

### D7 — Recall from the settled store, benchmarked against the real system

Recall reads the **consolidated** store and can **enter through any parent** (the categorical backstop prod lacks), any episode, or content. It is scoped **honestly as an exploration / abstract-recall win** — for cues like "health issues" / "what have I discussed about X" where prod **misses entirely**. It is **not** a precision fix: ADR-0103 proved the "on-the-topic vs is-the-answer" separation is *structural* and it still lives *inside* each category.

**The baseline is the actual production system, not a strawman (revised 2026-07-10).** The comparator is the **current production multipath recall (ADR-0104 behaviour)** reproduced on the frozen snapshot — not a stripped embedding-only baseline. The ADR itself frames this study as *additive to ADR-0104*, so the honest question is "does the associative index add recall *on top of* what production already does?" Beating an embedding-only baseline production already surpasses would prove nothing. Overclaiming precision would be the lie the No-BS criteria exist to catch (AC-6).

### D8 — The falsifiable core

The study succeeds or fails on one question, not on vibes: **does the associative-category index improve abstract-cue Recall@20 over the production-multipath baseline on the same frozen corpus, without breaching a pre-registered nDCG@20 non-inferiority margin?** The plateau/legibility result (AC-3) and the accretion/canonicalization mechanisms (AC-1/AC-2) are **supporting quality gates** — necessary, but the *primary falsifiable outcome is the recall win* (AC-4), with membership *quality* (AC-7) guarding against a mechanism that emits labels no human would trust. A clean **null** (the index ties the multipath baseline) is a valid, budgeted outcome.

Narrowed thesis (one line): *On a frozen Seshat corpus, does adding context-derived, graded, multi-parent category membership as an **orthogonal** retrieval index — with offline canonicalization — improve abstract-cue **Recall@20** over the **production-multipath** baseline, without breaching a pre-registered **nDCG@20 non-inferiority margin**?* Concept hubs, relation typing, salience, novelty, and hierarchy are evaluated through the separate ablation rungs (D9), not folded into this core.

### D9 — Staged ablation ladder + continuation gate (how the study keeps its momentum)

An earlier draft changed many variables at once (canonicalization + contextual categorization + multi-parent + grading + free-form categories + consolidation + typed relations + episodic links + salience + novelty + graph-assisted recall) against a single baseline. **If that wins, you cannot say why.** The revision makes the study a **ladder of arms with a pre-registered continuation gate**, engineered so **v1 runs on the same substrate, schema, harness and frozen ledgers as v0 — continuation needs no migration and no rearchitecture.** That is the momentum guarantee, and it is an *infrastructure* claim, stated precisely: the arms are **not** all strict super-sets of one another. Arms D and E are **diagnostic decompositions** that explain *why* the v0 result (arm C) holds — D branches back to B to isolate the damage open vocabulary does, E adds consolidation to re-derive C from that open side as a control. Arms F and G are **additive rungs** on top of E. What every one of them shares — and all the momentum promise needs — is that each writes only to schema/edge-types v0 already created.

**Forward-compatible by construction.** The sandbox schema is the *full-analysis* schema from day one (evidence layer, preserved `kind`, `SUBSUMES` edge type, `membership_confidence`-vs-activation split, the alias/subsumes/uncertain decision slot) even though v0 only exercises part of it. So running any v1 arm = writing to columns and edge-types that already exist — no migration.

| Arm | Configuration | Question it answers | Stage |
|-----|---------------|---------------------|-------|
| **A** | Production multipath recall (ADR-0104) on the snapshot | the real baseline | v0 |
| **B** | A + concept-hub / alias canonicalization | does dedup alone explain any gain? | v0 |
| **C** | B + consolidated multi-parent categories (fixed candidate→typed-decision, alias-merges) | **does categorical entry improve recall?** (the core) | v0 |
| **D** | B + open LLM categories, **no** consolidation | what damage does open vocabulary do? | v1 |
| **E** | D + canonicalization/consolidation | does consolidation control the scatter? (re-derives C from the open side) | v1 |
| **F** | E + `SUBSUMES` hierarchy traversal (GDS Leiden proposer) | does the heterarchy add value beyond flat categories? | v1 |
| **G** | F + salience/novelty reranking | does personalized activation add value? | v1 |

Typed **directional relation extraction** is a **separate study/arm** with its **own gold set** — never mixed into A–G, so relation gains cannot be misattributed to categorization.

**Continuation gate (pre-registered, decided before v0 numbers are seen).** Climb from v0→v1 iff: *C beats A by the pre-registered Recall@20 margin (AC-4) **and** B alone does not already close most of that gap* (i.e. the win comes from categories, not merely dedup). Pass → run the v1 rungs on the *same* sandbox/corpus/harness/ledgers. Fail or null → write the null and stop. **No second ADR is needed to continue the study** — the future ADR is gated only on *integration back into prod*, exactly as today.

---

## Alternatives Considered

### Option 1: Rearchitect the production KG directly (amend ADR-0109 to multi-parent in place)
**Description:** Make the live KG multi-parent now — change extraction, storage, and recall in prod.
**Pros:**
- If it works, no second migration; benefits land immediately.
- One system to maintain, not two.
**Cons:**
- High blast radius on the owner's live memory, on an **unproven** representational bet.
- The very thing in doubt (does the associative index help?) would be risked against real data before it is answered.
- Irreversible-ish; rollback is a second migration.
**Why Rejected:** The owner explicitly wants **test-and-learn, decoupled** — not a live rewrite on a hypothesis. Study-first is lower-risk, reversible (delete the sandbox), and answers the question *before* betting prod on it.

### Option 2: Do nothing structural — extend the existing taxonomy as needed (add types to ADR-0109)
**Description:** Treat the scatter as a missing-category problem and add entity *kinds* (e.g., a `MedicalCondition` type) when evidence demands, per ADR-0097's "add a class when evidence demands it."
**Pros:**
- Cheapest; stays inside a validated, measured paradigm.
- No new substrate.
**Cons:**
- Adds slots to the **kind** axis while the actual gap is a **missing retrieval axis**. More kinds still cannot make one entity reachable through *several* subjects at once.
- Case-variant self-disagreement and abstract-query miss are **structural to using the kind as the sole index**; more kinds do not fix them.
- Invites domain-specific type sprawl the domain-general design rejects.
**Why Rejected:** It optimizes the wrong axis. The problem is not *too few kinds*; it is *the kind carrying the whole retrieval burden*. This ADR tests adding an orthogonal associative index, not patching the kind axis.

### Option 3: The "deep" fully-distributed representation (a vector/Hopfield associative net *is* the store)
**Description:** Drop discrete types entirely; memory is a learned vector space, recall is pattern-completion, categories are emergent directions.
**Pros:**
- The most distributed / least discrete option; multi-parent falls out as superposition; genuinely self-organizing.
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
**Why Rejected:** Too timid to be a real test; it would "pass" without proving anything discriminating. (The ladder in D9 is the disciplined middle path between this and the everything-at-once study the earlier draft implied.)

---

## Consequences

### Positive Consequences
- **Tests Seshat's most foundational, unexamined memory axiom** cheaply and reversibly, before betting prod on it.
- If it holds, **one primitive** (graded multi-parent membership on an orthogonal axis) addresses *both* the write-side scatter/self-disagreement *and* the read-side abstract-query miss — while the ADR-0109 entity *kind* is preserved intact.
- **Fully inspectable** — no opaque net; the graph is the ledger of the learning, with an immutable per-assertion evidence layer under a recomputable index (distributional richness *with* audit).
- **Domain-general** — no privileged domains; the health cases are just fixtures.
- **A pre-built continuation path** — v1 shares v0's substrate, schema, harness and frozen ledgers, so a positive v0 climbs to the v1 arms with no migration or rearchitecture.
- Directly feeds the **pedagogical North Star** (ADR-0084): salience ≈ needs-revisiting; multi-parent enables cross-thread correlation.
- A clean, reusable **baseline harness** (production-multipath recall on the frozen snapshot) that other memory work can reuse.

### Negative Consequences
- **A second substrate to stand up and run** (isolated Neo4j + GDS) for the study's duration.
- The **consolidator is unproven and the riskiest component** — canonicalization can over- or under-merge, and the two-stage decision adds an LLM/rule step to get right.
- Multi-parent buys **no precision**; it must be reported honestly as an exploration/recall result only.
- **Typed-relation extraction is a separate study** — its cost and gold-set work are additional, not covered by the core arms.
- A study can legitimately return a **null result** (the index ties the multipath baseline) — that is a valid, budgeted outcome, not a failure to hide.
- Integration-back is a *separate, later* cost not covered here.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Category scatter merely **relocates to the parents** (free-text category names) | High | The two-stage canonicalizer (D5) is *designed for exactly this*; AC-3 makes plateau-without-collapse-with-legibility the pass condition, swept over τ_merge, and AC-7 checks membership quality. |
| Consolidator **over-merges** ("stuff") or **under-merges** (snowflake), or **merges a broader parent into a narrower one** | High | Sweep τ_merge; AC-3 requires a τ_merge with plateau *and* legibility *and* a non-collapse floor *and* alias-merge precision (a bad τ_merge fails, not passes); the alias-vs-subsumes decision keeps hierarchy out of merges. |
| Frequency entrenches an **echo chamber** at recall | Medium | Epistemic strength split from recall-time salience/novelty (D6); AC-4 checks abstract-recall *quality* at a fixed operating point, not raw frequency. |
| **Strawman baseline** inflates the result | High | AC-4 benchmarks against **production multipath (ADR-0104)** reproduced on the snapshot, not embedding-only; arm B isolates the dedup-only contribution. |
| **τ_merge sweep confounded** by categorizer stochasticity | Medium | Freeze seeded `MembershipAssertion` ledgers; every consolidation config runs on the *same* ledgers; report within/across-seed variance (D5). |
| **Leiden communities adopted as a taxonomy** they are not | Medium | Leiden is a *proposer*, LLM/human-validated before any `SUBSUMES` adoption; never treated as ground truth (D5, v1). |
| **Predicate snowflake** — free-text relation predicates recreate `RELATED_TO` | Medium | Typed relations use a `canonical_predicate` with polarity/modality; relation-typing is a **separate arm/study with its own gold set**, never mixed into A–G. |
| Study **writes to prod substrate** by accident | High | Separate instance + FRE-375 line; AC-5 asserts zero prod writes, two-sided. |
| Study becomes a **rabbit hole** with no verdict | Medium | Falsifiable core (D8) + v0 stub (2 ops) + pre-registered continuation gate (D9); the study answers AC-4 at a single τ_merge\*, not "build a product." |
| **Null result** read as wasted effort | Low | A clean null (with the baseline harness + reproducible corpus + frozen ledgers) is an accepted, valuable outcome and informs whether to stop. |

---

## Implementation Notes

- **Substrate:** isolated Neo4j instance (own volume) + GDS plugin + native vector index; separate compose service, never wired to prod URIs (FRE-375).
- **Corpus:** one-time frozen export of prod `:Entity`/relationships + conversation traces into the sandbox; document the snapshot SHA/date for reproducibility.
- **Schema (forward-compatible from day one):** evidence layer (`Episode`→`Mention`→`MembershipAssertion`), `Concept` with preserved ADR-0109 `kind`, derived `MEMBER_OF {membership_confidence, support_count, last_supported_at}`, `SUBSUMES` edge type (populated v1), `RelationAssertion` (separate arm). v0 leaves v1 edge-types unused but present.
- **Ingest categorizer:** LLM reads full conversation → emits `MembershipAssertion`s with provenance + `model/prompt_version/seed`; writer appends (accretes); derived `MEMBER_OF` recomputed from assertions (D2/D3/D4).
- **Consolidator v0:** offline job — (1) two-stage canonicalize: **GDS Node Similarity/KNN** candidate generation (member overlap + name-embedding cosine) → LLM/rule typed decision, alias-merges only; (2) decay+prune (suppress derived edges below floor, retain evidence). v1+: Leiden proposer, schematize, semantic promotion, FastRP arm.
- **Ledgers:** run categorizer N times at fixed seeds; store immutable assertion ledgers; run every τ_merge config on the same ledgers.
- **Baseline harness:** reproduce **production multipath recall (ADR-0104)** on the same snapshot for head-to-head scoring (arm A).
- **Metrics:** category-count-vs-conversation curve across τ_merge (per ordering); abstract-query Recall@20 + nDCG@20 (study vs baseline) on the pre-registered frozen abstract-cue set; membership-precision on a stratified human-rated sample; legibility spot-check of top-20 + tail sample.
- **Decomposition (the D9 ladder):** substrate+snapshot → schema+writer (evidence layer + accretion) → production-multipath baseline harness (arm A) → canonicalization (arm B) → consolidator v0 + τ_merge sweep + categories (arm C) → v0 synthesis + continuation-gate readout → *(gated)* v1 arms D–G + separate relation study. Sequenced with dependencies in Linear (Step 5).

---

## Verification / Acceptance Criteria

**How will we know this study actually delivered — not just merged?** Every criterion is **testable, population-scale (not fixture-scale), and able to fail**. They split into three roles, and the distinction matters — a blanket "every criterion is outcome-level" would be false and is not claimed:

- **Evidence (AC-3, AC-4, AC-7) — outcome-level; a broken or half-built study must NOT pass.** These are what say the study delivered.
- **Mechanism / isolation gates (AC-1, AC-2, AC-5) — the instrument works; necessary but not sufficient.** A broken categorizer *can* pass AC-1 (it only checks that labels are emitted with backing) — which is exactly why AC-7 exists to check the labels are *good*. These gate the evidence; they are not themselves evidence of success.
- **Honesty guard (AC-6) — prevents overclaim, not counted as delivery evidence.**

All numeric bars are **pre-registered** — fixed *before* the run and recorded in the study plan; they may be tuned only before results are seen, never after (this is what defeats cherry-picking). The frozen snapshot's own statistics set the eligible populations, so the bars are checkable without new infrastructure.

- **AC-1 — Multi-parent accretion holds across the population (MECHANISM GATE — necessary, not evidence of success).** Define the **eligible set** E = concepts mentioned in ≥2 conversations whose contexts differ (computable from the snapshot). Pass = **median `MEMBER_OF` degree over all of E is ≥2**, *and* **≥60% of E carry ≥2 provenance-distinct memberships** (each derived edge is backed by `MembershipAssertion`s from a different source conversation). · **Check:** one Cypher aggregation over E. · *Explicitly not success evidence* — a broken categorizer emitting `["Health", "Things discussed", "Important subjects"]` would pass this; AC-7 is what catches that. · *Fails if* the median degree over E is 1, or the ≥60% bar is missed, or memberships lack backing assertions.
- **AC-2 — Canonicalization is real entity resolution, not lowercasing (with hard negatives).** Build V⁺ = case-/near-variant surface pairs that *should* resolve to one hub, and **V⁻ = hard-negative pairs that must NOT merge** (homonyms/polysemes: `Python`(language)/`python`(animal), `Mercury`(planet/element/software), `Apple`(company/fruit)). Pass = **pairwise precision ≥0.95 and recall ≥0.90 over V⁺∪V⁻** — i.e. ≥90% of true variants share a hub **and** ≥95% of merges are correct (hard negatives stay separate). The two named trigger pairs are spot-checks, *not* the bar. · **Check:** enumerate V⁺ and V⁻ from the snapshot; measure pairwise precision/recall. · *Fails if* precision or recall misses — lowercasing every label would tank precision on V⁻ (catastrophic homonym merges), so it cannot pass.
- **AC-3 — The consolidator plateaus without collapse, and the categories are coherent, distinct, and stable (supporting quality gate).** Conversations processed in **chronological snapshot order**, and the result must hold under **≥2 additional pre-registered permutations** (robustness to front-loading category-rich conversations). Sweep τ_merge. For each τ_merge: (a) **plateau** — marginal new-canonical-categories-per-100-conversations in the final tertile is **≤25% of the first-tertile rate**, and the whole curve sits strictly **below the no-consolidator control** (≈linear), under every ordering; (b) **head legibility** — the **top-20** categories by member count, rated by **2 independent judges** on a 3-point rubric; "coherent" only when **both judges agree**; pass = **≥70% of top-20 coherent AND Cohen's κ ≥ 0.4**; (c) **tail coherence** — a **random 20-category sample from below the top-20**, same rubric, pass = **≥50% coherent** (guards against a clean head over an unusable long tail); (d) **distinctness** — **<10% of category pairs exceed a pre-registered member-overlap ceiling** (guards against overlapping duplicate categories, not just incoherent ones); (e) **non-collapse** — total canonical-category count **≥ a pre-registered floor**; (f) **stochastic stability** — across the seeded ledgers (D5), category-count and top-20 membership are stable (pre-registered variance bound). Pass = **∃ τ_merge satisfying (a)∧(b)∧(c)∧(d)∧(e)∧(f)**. · **Check:** the τ_merge-sweep plots (per ordering/seed) + the rated top-20 and tail tables + the overlap-pair histogram. · *Fails if* every τ_merge is ≈linear (snowflake), order-/seed-dependent, collapses (floor), has an incoherent tail, or is riddled with duplicate categories.
- **AC-4 — Abstract-query recall beats the PRODUCTION-MULTIPATH baseline by a pre-registered, significant, and materially-sized margin — at the same operating point as AC-3.** A **single τ_merge\*** is selected (by a pre-registered rule) from the τ_merge that pass AC-3 and **fixed before any AC-4 scoring** (the plateau point and the recall-win point must be the *same* config — no re-sweeping for recall). Cue set = **≥30 abstract cues** spanning ≥4 snapshot domains; **cues AND gold neighborhoods are pre-registered and frozen before any scoring**; annotation (one annotator + a second adjudicating disagreements) is **blind to both systems' outputs**. Systems: **study** (enter-through-membership, arm C) vs **baseline** (**production multipath recall, ADR-0104**, reproduced on the snapshot — *not* embedding-only). Metrics: **Recall@20 (primary)** and **nDCG@20 (ranking guardrail)**. Pass = **all three**: (i) **relative** — study Recall@20 ≥ **1.10 ×** baseline; (ii) **absolute** — study Recall@20 − baseline ≥ a **pre-registered floor (default 0.05, set by a power/utility analysis before the run)**; (iii) **significance** — a **pre-registered paired test (Wilcoxon signed-rank or a paired bootstrap CI over the ≥30 cues)** with the effect size and 95% CI reported, not just p<0.05. **AND** the nDCG guardrail is a **non-inferiority test**: the **lower 95% CI bound of ΔnDCG@20 (study − baseline) must exceed −δ**, for a pre-registered margin δ (a non-significant regression is *not* evidence of equivalence). · **Check:** the frozen cue+gold set, the scored table, the paired test with CI/effect size, and the ΔnDCG CI. · *Fails if* the win misses relative OR absolute OR significance, or the ΔnDCG lower bound falls below −δ. Also fails the study's **primary** claim (this is the falsifiable core, per D8).
- **AC-5 — Isolation is actively verified, not just grepped.** Two-sided proof: (1) **the study env cannot reach prod** — prod credentials are absent from the study environment and a deliberate connection attempt to the prod bolt/ES/PG URIs from the study env **fails** (no route/creds); (2) **prod shows zero attributable writes** — either (i) the study runs against the frozen snapshot during a **quiesced prod window** (no live turns), so prod Neo4j node+relationship counts (and ES/PG equivalents) before/after show **exact zero deltas**; or (ii) if prod cannot be quiesced, prod's **write-audit log filtered by client identity** over the study window shows **zero writes from the study's client/credentials**. · **Check:** the failed-connection log + (i) exact-zero prod deltas over the quiesced window **or** (ii) the client-attributed write-audit. · *Fails if* prod creds are reachable from the study env, or any prod write is attributable to the study (or the noise cannot be attributed away).
- **AC-6 — No precision overclaim (honest-scope guard, not delivery evidence).** The study report **separates** abstract-cue results (AC-4) from precise-answer-cue results, shows the precise-query numbers, and does **not** assert a precision improvement over ADR-0103's structural limit. · **Check:** both result sets present in the writeup. · *Note:* a guard, not counted as evidence the study worked. · *Fails if* the two are conflated or a precision win is claimed.
- **AC-7 — Membership quality: the emitted memberships are ones a human would endorse (the evidence AC-1 cannot give).** On a **stratified random sample** of `MEMBER_OF` edges (strata by `membership_confidence` band and by concept frequency), 2 judges rate each membership *endorsed / dubious / wrong*. Pass = **membership precision ≥80% (both-judges-endorsed) at the operating τ_merge\***, **redundant-parent-pair rate ≤15%** (near-duplicate parents on the same concept), and **unsupported-membership rate ≤5%** (memberships whose backing assertions do not actually justify the category). · **Check:** the rated sample table + a Cypher scan for near-duplicate parents and for edges whose assertions fail a support test. · *Fails if* precision is below 80%, or redundant/unsupported rates exceed bounds — i.e. the "Health / Things discussed / Important subjects" failure mode that slips past AC-1.

**Seam owner (decomposed-ADR seam):** the **v0-synthesis ticket** owns the assembled intent — **AC-4 holding at a single pre-registered τ_merge\*, end-to-end** (ingest → accretion → consolidator → production-multipath baseline), with **AC-3 and AC-7 as quality gates**, **AC-1/AC-2/AC-5 as mechanism/isolation gates**, and **AC-6 honored** — *plus* the **pre-registered continuation-gate readout** (does v0 warrant climbing to v1 arms D–G?). The ADR does **not** close because the last child (e.g. the consolidator) merged; it closes only when the synthesis ticket shows that **at one τ_merge\*** the whole loop plateaus-without-collapse, the memberships are human-endorsed, **and** recall beats the production-multipath baseline by the pre-registered significant, materially-sized margin within the nDCG non-inferiority band — or returns a clean, documented null.

---

## References

- `docs/architecture_decisions/ADR-0109-entity-taxonomy-redesign.md` — the single-exclusive 10-type taxonomy this study reframes and *preserves as a control* (entity kind), not amends
- `docs/architecture_decisions/ADR-0097-ingested-knowledge-taxonomy.md` — "prefer fewer classes; add on evidence" stance; orthogonal class axis
- `docs/architecture_decisions/ADR-0098-memory-substrate-and-lifecycle-architecture.md` — P/W/S class + substrate the snapshot is drawn from
- `docs/architecture_decisions/ADR-0100-relevance-bounded-recall.md` — the false-negative recall problem this addresses from the category side
- `docs/architecture_decisions/ADR-0103-recall-no-clean-floor-structural-separation.md` — "structure-where-closed, semantic-where-open"; the precision limit this study does NOT repeal
- `docs/architecture_decisions/ADR-0104-multi-path-retrieval-rank-fusion.md` — production multipath recall; **the baseline this study benchmarks against** (additive backstop, not a replacement)
- `docs/architecture_decisions/ADR-0105-convergent-self-improvement-pipeline-and-system-graph.md` — episodic→semantic consolidation this study's D5-op4 extends
- `docs/architecture_decisions/ADR-0106-system-user-knowledge-boundary-dispatch-observe-ground.md` — provenance boundary (scope-distinct: this ADR is about categorization, not System/User routing)
- `docs/architecture_decisions/ADR-0084-pedagogical-architecture-socratic-tutor-layer.md` — pedagogical North Star; salience ≈ needs-revisiting
- `docs/architecture_decisions/ADR-0087-memory-recall-quality-measurement-program.md` — measurement-first recall methodology (used for the read-only forensic)
- FRE-375 test/eval substrate isolation policy — the never-write-prod line the study inherits
- Rosch (1975) graded categories; Patterson, Nestor & Rogers (2007) hub-and-spoke — *inspiration* for multi-parent, multi-dimensional concepts (motivation, not validation of the graph model)
- Tulving (1972) episodic/semantic; Tulving & Thomson (1973) encoding specificity — categorize the encoding event
- Firth (1957) "know a word by the company it keeps" — meaning-is-use / distributional richness
- Collins & Loftus (1975) spreading activation; Anderson ACT-R — associative reach
- Marr (1971) / hippocampal CA3 — pattern completion
- McClelland, McNaughton & O'Reilly (1995) Complementary Learning Systems — fast/slow, replay, consolidation (motivates the learn/consolidate/recall split; does not validate it)
- Grossberg (Adaptive Resonance Theory, 1980s) — stability–plasticity, the vigilance (τ_merge) knob
- Nairne (2007) survival-processing advantage; LeDoux (2012) survival circuits; Panksepp (1998); Russell circumplex — the salience prior
- Bartlett (1932) schema/schematization — compression as consolidation
- Fine-grained entity typing as multi-label classification (Ling & Weld 2012; Choi et al. 2018) — kind-vs-category as separable axes with corpus- and context-level evidence
- Non-inferiority testing (Piaggio et al., CONSORT 2012) — the ΔnDCG margin method (AC-4)
- Neo4j Graph Data Science — **Node Similarity/KNN** (v0 canonicalization candidate generation), **Leiden** (v1 hierarchy proposer — communities are *not* a taxonomy), **FastRP** (speculative recall arm), **PageRank** (diagnostic, not salience)
- Peer-review analysis of ADR-0114 (owner-provided, 2026-07-10) — the orthogonal-axes / evidence-layer / ablation-ladder / AC-rigor revision integrated here (session artifact)
- `/tmp/.../adr-kickoff-memory-kg.md` — owner-directed kickoff framing (session artifact)

---

## Status Updates

### 2026-07-07 - Proposed
**Changed By:** Project owner (adr session, Opus)
**Reason:** Discussion-first design settled with the owner across a multi-round Remote Control session; owner ratified the shape ("this represents my mental model") and directed write-up. Recorded as a **research study proposal**; the integration-back decision is explicitly a future ADR gated on AC-3/AC-4. Implementation tickets to be filed (Needs Approval) and sequenced per Step 5.

### 2026-07-10 - Revised (still Proposed)
**Changed By:** Project owner (adr session, Opus)
**Reason:** Integrated an owner-provided peer-review analysis. Substantive revisions, all additive to the original decision: (1) **orthogonal axes** — root cause restated as "entity *kind* used as the sole retrieval index," not "single-typing is wrong"; ADR-0109 kind is *preserved as a control*, associative categories are a *third axis*; (2) **evidence layer** — immutable `Mention`/`MembershipAssertion` records beneath a *derived, recomputable* `MEMBER_OF`, resolving the accrete-vs-prune contradiction (evidence never decays; derived edges/activation may); (3) **production-multipath baseline** — AC-4 now benchmarks against ADR-0104 as reproduced on the snapshot, not an embedding-only strawman; (4) **staged ablation ladder + continuation gate (D9)** — arms A–G on one substrate/schema/harness so v1 continues with no migration (D/E are diagnostic decompositions, F/G additive rungs — infrastructure-additive, not a strict arm-ordering prefix); relation-typing split into a separate study; (5) **AC rigor** — AC-1 demoted to a mechanism gate; new **AC-7 membership quality**; AC-2 gains hard negatives + pairwise P/R; AC-3 gains tail coherence, distinctness, stochastic stability; AC-4 gains a named paired test + effect size/CI + a nDCG **non-inferiority** margin + an **absolute** recall floor; (6) **GDS made concrete** — Node Similarity/KNN as v0 canonicalization candidate generation, Leiden as a v1 *proposer* (not a taxonomy), FastRP/PageRank labelled speculative/diagnostic; (7) **epistemic vs recall-priority split** (D6) — `membership_confidence` separated from recall-time salience/recency/novelty; (8) **frozen seeded proposal ledgers** so the τ_merge sweep (renamed from θ to avoid the Leiden parameter clash) isolates the knob; (9) **tempered tone** — cognitive science framed as inspiration, not fidelity, per the exploratory-research posture. Status remains **Proposed**; the falsifiable core is now AC-4 (recall beat) with AC-3/AC-7 as quality gates.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
