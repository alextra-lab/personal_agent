# ADR-0098 — Memory Substrate & Lifecycle Architecture (Core/Docs topology; the living-knowledge model)

**Status:** Accepted — 2026-06-27 (owner greenlight; build wave FRE-637–642 Approved, FRE-643 Tier-3 deferred-with-trigger)
**Implements:** ADR-0097 (Ingested-Knowledge Taxonomy — *vocabulary*; this ADR is the *how*: storage, joins, aging, scale)
**Supersedes:** ADR-0071 (the architecture half — "two-source one-gate"; the taxonomy half went to ADR-0097)
**Related:** ADR-0052 (Owner Identity Primitive — the `is_owner` anchor + dedup-exclusion invariant this ADR extends to protect the soul subgraph), ADR-0087 (Memory Recall Quality — the pillar this lands under; a de-polluted, correctly-classified store is a recall-quality lever), ADR-0096 (Memory Access Model — *how* memory is retrieved; this ADR decides *what is stored and how it lives*, 0096 decides the access posture over it), ADR-0042 (KG freshness — the decay/access primitive the class-aware lifecycle consumes), ADR-0073 (cross-fact constraint layer — the thin contradiction-handling slice this ADR generalizes into correction), ADR-0069/0070 (R2 artifact substrate + output channels — the cold store transcripts and documents offload to), ADR-0035 (entity dedup at ingest — kept and hardened), the pedagogical north star (Socratic tutor: World know-how + the owner's Stance toward it + cross-thread insight).
**Validation:** FRE-636 taxonomy-validation spike (`docs/research/2026-06-27-fre-636-taxonomy-validation.md`); the per-class acceptance criteria below; the joinability probe (ADR-0074) for provenance integrity.

> ADR-0097 committed to a *vocabulary* (Layer-0 Source; Layer-1 Personal / World / Stance) and deferred every architectural question to here. This ADR answers them — and corrects ADR-0097 on one empirical point the spike forced: the three classes do **not** cover everything ingested. It also makes one thing non-negotiable that the current substrate gets exactly backwards: **knowledge must be living — updatable and correctable — not frozen on first write.**

---

## Context

ADR-0097 is a vocabulary, not a design. Two pieces of evidence shape the architecture that consumes it.

**The FRE-636 spike (measurement-first, read-only against the live KG).** It pressure-tested Personal/World/Stance against 7,366 real entities and returned three findings that bind this ADR:

1. **The three classes hold** on genuine user-knowledge (97.5% blind inter-rater agreement) and should **not** be simplified — Stance looks rare (~3% of genuine entities) only because the extractor never emits it, not because it is unused.
2. **The taxonomy has a hole.** ~46% of extracted entities are not user-knowledge at all — they fit none of the three classes. ADR-0097's "every ingested item is Personal/World/Stance" is **false on real data**.
3. **Extraction is the binding constraint, not storage.** A single car-buying turn contained, in the user's own words, all three classes at once; the pipeline kept the World specs densely, **flattened** the explicit Stance ("I love the Rafale") into a World-entity description clause, and **dropped** the Personal situational fact ("my lease expires in October") entirely. Any substrate built on this extractor is starved of Personal and Stance.

**The live substrate (verified 2026-06-27 against `/opt/seshat/.claude/worktrees/adrs`).** The architecture must design against what exists:

- **One Neo4j graph.** Labels `:Turn` / `:Session` / `:Entity` / `:Person`; the owner is `is_owner=true` on a `:Person` (ADR-0052), not a label. No `:Core`, `:Fact`, `:Claim`, `:Community`, `:Stance` label exists.
- **No knowledge-class axis.** Only `Visibility` (public/group/private) and `KnowledgeWeight` (confidence + source_type). Nothing carries Personal/World/Stance.
- **Extraction emits 7 entity types** (Person/Organization/Location/Technology/Concept/Event/Topic) + 6 edge types — **no slot for a stance-relation or an owner-situational fact.**
- **Promotion is a property flag.** Episodic→semantic sets `memory_type='semantic'`; it creates no `:Fact`/`:Claim` node and no edge.
- **First-write-wins freezes facts.** Once an `:Entity` exists, later extractions cannot overwrite its type/description/properties (`service.py` MERGE `CASE WHEN … IS NULL THEN $new ELSE existing`); they only bump `mention_count`. A wrong or thin first description is permanent.
- **No lifecycle execution.** Freshness (ADR-0042) is wired but **default-off**; the review job is proposal-only; there is no TTL, eviction, or community/topic tier; the ADR-0071 gate was never built.
- **Three disjoint stores, no join.** Neo4j (graph) · Postgres (artifact metadata) · R2 (artifact bytes). No edge links a knowledge node to its source document.

**What the "~46%" actually is (source identified — a deliberate gate the owner set before this could be decided).** It is **not *automated* test traffic**: only 26 of 2,133 turns carry `eval_mode:true` (~1%), and FRE-375 isolation already routes automated test writes to a separate database. The FRE-636 spike's "test/dev/agent-operational noise" label refers to this **dev-phase owner activity**, not eval runs — **genuine owner sessions whose *subject* is the system** — frequent healthchecks ("Postgres healthy, ES degraded"; the owner runs one after most harness updates), log/telemetry review (`cost_gate_reaper_swept`, `sensor_poll`, DEBUG counts becoming `:Entity` nodes), harness-architecture explainers (executor.py, ToolLoopGate), plus a handful of connectivity pings. ~23% of all entities additionally have NULL/empty descriptions — extraction junk. So the material is real, recurring, dev-phase activity — **it cannot be filtered as "test traffic"; the gate must key on the subject/intent of the turn.** And it is intrinsically **ephemeral** ("Postgres healthy at 09:13" has no durable value), yet it is currently calcifying into permanent entities and inflating infra terms as corpus-dominant.

---

## Decision

A single coherent substrate built on the taxonomy, with one governing principle and seven decisions. The governing principle: **knowledge is living.** Durable knowledge is stored as first-class, provenance-bearing, temporally-valid **Claims** that are updated and corrected over time — never frozen on first write. Everything below serves that.

### D1 — Knowledge class is a first-class axis: Personal / World / Stance / **System**

Every knowledge item carries a `class` (a label or indexed property), orthogonal to `MemoryType` (ADR-0097 invariant 3 — class is *subject/ownership*, MemoryType is *lifecycle/derivation*; they compose). The spike forces a fourth value ADR-0097 lacked:

- **Personal / World / Stance** — the three pedagogical classes, unchanged from ADR-0097. Kept, not simplified.
- **System** — the negative space: non-user-knowledge (agent infra/telemetry/healthcheck/test-scaffold). It is **not a pedagogical peer** of the other three; it is the explicit home for the ~46% so it stops being silently mislabeled World. It is **assigned by the classifier on the turn's subject/intent** (not a test flag — see Context), is **excluded from all tutor/recall queries**, and is **born ephemeral** (D4): episodic, fast-decay, **never promoted** to durable World/Core.

This makes ADR-0097's partition *total and honest*: every ingested item has a class, and exactly the first three reach the tutor corpus.

### D2 — Facts are first-class Claims, and knowledge is living (kills first-write-wins)

Stance and Personal-situational facts have **no structural home** in today's entity-property model (finding #3's flattening/dropping is a direct consequence). They become first-class **Claims** — provenance-bearing assertions, modeled as nodes or typed edges:

- **Stance** = an owner↔World edge carrying affect / mastery / spaced-repetition state (`(owner)-[:HAS_STANCE {affect, mastery, review_due}]->(:WorldConcept)`). The pedagogical crown jewel; a native edge inside Core (D3).
- **Personal-situational facts** = Claims about the owner's life/relationships/events (`(owner)-[:HAS_FACT]->(:Claim)` or an owner-anchored Claim node). **[Amended by [ADR-0107](ADR-0107-user-identity-resolution-and-log-propagation.md) (Accepted 2026-07-02): a Claim anchors to the *acting authenticated User* (via `user_id`, per ADR-0052), not the `is_owner` singleton — so a claim asserted by a non-owner user attaches to that user. The Stance clause above is unchanged.]**
- **World facts** = Claims/SPO over the entity spine (consistent with the GraphRAG Claim/Covariate/Statement consensus the FRE-635 evidence documents).

Because facts are Claims with provenance and **temporal validity**, knowledge is **updatable** — two distinct modes, deliberately separated so updating one does not corrupt the other:

- **Correction** (the stored fact is *wrong*): resolved by **contradiction-detection + provenance/confidence weighting**, not naive last-write-wins (which would let a bad later extraction clobber a good earlier one). Generalizes ADR-0073's cross-fact constraint slice and realizes the Karpathy-wiki "lint-for-contradictions" idea on the entity spine (a `:Core` curation concern, per the FRE-635 evidence — **not** a markdown substrate, whose superiority claims failed verification).
- **Evolution** (the fact *was* true and *changed*): **bitemporal validity** — invalidate the old Claim (`valid_to` / `invalid_at`), assert the new, **retain the old for history**. This is the Zep/Graphiti edge-invalidation model (arXiv 2501.13956, cited in FRE-635). **Superseded ≠ deleted** — the audit trail is itself a learnable signal ("you used to prefer X, now Y").

**First-write-wins is explicitly retired** for durable knowledge. A Claim's value can change; the entity it hangs off persists.

### D3 — Topology: Core unified; Docs an isolatable provenance layer; the seam is never hot-joined

Two storage tiers along the *curation* axis (distinct from the *subject* axis of D1):

- **Core** — one unified graph holding the entity spine + all Personal / World / Stance Claims and edges. **Not split by subject.** The Stance edge literally joins the owner to World, and the highest-value queries re-cross that boundary at every hop — so it must stay native. Core is small, precious, curated, slow-changing.
- **Docs** — the raw-source provenance layer (chunks-as-provenance, *never* as a retrieval unit — the GraphRAG consensus). High-churn, large, append-heavy. **Physically isolatable** (its own store or database).

The split is driven by the **hypothesized access pattern**, not by aesthetics. Three read workloads:

| Workload | Touches Core | Touches Docs | Shape |
|---|---|---|---|
| Per-turn recall (hot) | yes | no | retrieval/rank — Core only |
| Tutor / mastery / thread-pulling (north star) | yes | no | owner-anchored multi-hop **inside Core** |
| **World-internal correlation / insight** (north star) | yes | no | scan/bridge **inside Core**, *not* owner-seeded |
| Citation / verbatim re-read | yes | yes (keyed) | Core → **fetch Docs by id** (terminal) |
| Global sensemaking (Tier-3 — **deferred**, D7) | yes | yes (broad) | batch — not hot |

So **no hot query interleaves Core and Docs.** Core is the retrieval and traversal target; Docs is *pointed into by id* for provenance and verbatim re-read. Consequences:

- **Core stays unified** — the Stance traversal and the insight scan are native and cheap; a subject-split would tax both speculatively.
- **Docs is the natural isolation boundary** — isolating it costs hot queries nothing (the seam is a keyed one-way lookup), and it is where scale, churn, and aggressive eviction actually live. Isolation buys **blast-radius safety** (a botched doc reingest/purge physically cannot reach Core) and per-tier lifecycle — the one real win of separation, obtained without the cost of splitting Core.
- **The soul is protected inside Core** by extending the ADR-0052 invariant: destructive jobs (eviction, reingest) are **class-scoped and structurally unable to match Personal/Stance** (as dedup already excludes the owner node), so Core unification does not put the soul at risk.

*World-scale correction:* World-internal correlation **does** scan Core's World (it is not owner-seeded), so World's size and edge density matter for query cost — but at one-owner-one-year (~10⁴–10⁵ nodes) this is well within single-graph Neo4j with the entity-embedding vector index and typed-edge spine already present.

### D4 — Lifecycle is class-aware: durable-but-living vs born-ephemeral

One-size aging is wrong; the taxonomy *is* the lifecycle policy:

| Class | Lifecycle |
|---|---|
| **World** | durable · **curated + updatable** (contradiction-resolved, bitemporally superseded) · **never bulk-evicted** (its value compounds — more clean World = more correlation surface) · history retained |
| **Personal** | durable · updatable · bitemporal (lease expires; cardiologist changes) · never evicted |
| **Stance** | the *most* temporal class — mastery moves (spaced-repetition schedule), preferences flip; updating is its normal operation, not decay |
| **Episodic / System** | ephemeral · decays (ADR-0042, turned on for this tier) · **evicts** — System is born-ephemeral and **never promoted** |

ADR-0042 freshness graduates from default-off-everywhere to **on for the ephemeral tier**, off (or inverted into review-scheduling) for the durable tiers. Eviction is **execution, not proposal** — for the ephemeral/System tier only, and class-scoped (D3).

### D5 — The extraction-emission contract (the binding constraint — sequenced FIRST)

Per finding #3, no substrate is worth building until the extractor can feed it. This ADR **owns the emission contract** the substrate requires (the implementation is sequenced ahead of substrate-dependent work, D-seq). The redesigned extractor MUST emit, **per source unit** — a conversation turn *or* a document (ADR-0097 Layer-0 `document | conversation | observation`):

1. a **class** for every item (Personal / World / Stance / System);
2. **Stance** as a structured owner↔World relation with affect/mastery — not a description clause;
3. **Personal situational facts** as Claims (the dropped "lease expires October" case);
4. a **System determination** for operational/infra/telemetry subjects;
5. **provenance + a timestamp** on every Claim, so it can be superseded (D2).

**One source yields many class items** (ADR-0097 invariant 1) — this *is* the document-triage decision. A single document is **not** a class: a medical textbook the owner is studying yields World content + a Stance edge ("learning it") + Personal facts (if annotated with their case) + possibly System (if it is an infra runbook). Triage runs **per extracted item, not per document** — "ingest must classify, not assume" (ADR-0097). Document sources are triaged by the **same contract** as conversation turns; the only Layer-0 difference is retention (D6 — documents keep verbatim bytes in R2; conversations offload the transcript). Document *chunking strategy* (how a long document is segmented into provenance anchors before extraction) is the one document-ingest detail deferred — **trigger:** the first non-conversation document source is actually wired (today there is none; all sources are conversations).

This is the contract; the extractor model/prompt is implementation. Substrate tickets that depend on Stance/Personal/System storage are **blocked on this landing** — designing Stance storage on an extractor that never emits Stance is the exact failure that makes the crown jewel look unused and get cut in a year.

### D6 — Retention: extract-and-point, not transcript hoarding

Resolves ADR-0097's Layer-0 `retention` question:

- **`conversation` source** = provenance-only. After extraction, the verbatim transcript is **offloaded to R2 (ADR-0069)** with a pointer on the source node; it does not live hot in Neo4j past a retention window. (Today `:Turn` stores full `user_message`/`assistant_response` indefinitely.)
- **`document` source** = verbatim, re-readable — bytes in R2, a keyed pointer from Core (the D3 Docs seam).
- **Co-authorship → trust** (ADR-0097 Layer-0 `co-authored?`): user-asserted Stance/Personal is trusted at face value (the owner is the authority on their own stance); **agent-derived** claims (the agent was a conversation participant) require corroboration before promotion to durable. Realized through `KnowledgeWeight.source_type` at the promotion gate.

This also creates the first **R2↔graph join** (a typed provenance pointer), which today does not exist.

### D7 — Insight now; heavy summarization deferred on a clean corpus

Splitting a conflation:

- **World-internal correlation / bridge-finding** (the curiosity/serendipity engine — "two things you know connect in a way you haven't noticed") is a **first-class read pattern, built now.** Its primitives already exist in Core (typed edges + the `entity_embedding` vector index); it is central to the north star, not a global-query nicety.
- **Tier-3 community/topic *summarization*** (precompute clusters into theme/summary nodes — the expensive, operationally-heavy layer) is **additive-deferred.** **Trigger:** the operational/System gate (D1) has landed and produced a **de-polluted World corpus** — running expensive summarization over a corpus that is ~46% noise would only surface garbage themes. The substrate is built forward-compatible (communities compute *on top of* the entity spine — the GraphRAG evidence confirms no migration is needed to add the tier later).

### D-seq — Implementation sequence (the critical path)

1. **Extraction-emission contract (D5)** — first. Until the extractor emits class + Stance + Personal + System + provenance, nothing downstream has data.
2. **Class axis + Claims model + first-write-wins retirement (D1, D2)** — the storage shape the contract feeds.
3. **System gate + class-aware lifecycle (D1, D4)** — de-pollute; turn on ephemeral eviction.
4. **Retention offload + R2↔graph pointer (D6)**; **Docs isolation (D3)**.
5. **Insight/correlation read pattern (D7a)**.
6. **(Deferred)** Tier-3 summarization (D7b), gated on a clean corpus.

---

## Open decisions (deferred — each with a named trigger)

- **Tier-3 summarization tier** — build trigger: D1's System gate landed + a measured de-polluted World corpus exists (D7).
- **Physical Core split (subject isolation)** — *not now.* Trigger: a class-scoped destructive job has actually reached soul data **despite** the D3 guard, **or** World churn measurably degrades owner-anchored Core queries. Until then, one Core.
- **Where exactly the System gate sits** — pre-extraction intent filter (skip durable extraction for system-subject turns) vs. post-extraction class assignment. The contract (D5) requires the *determination*; which side of extraction it executes is an implementation A/B, gated on which yields the cleaner World corpus.
- **Quantified extraction-loss rate** — the spike gives a directional probe, not a measured per-class survival number. A source-vs-entity survival audit (N turns, measured survival per class) is the trigger-able follow-up if a hard baseline is wanted before/after D5.
- **Last-write-wins vs. always-contradiction-resolve for low-stakes World corrections** — D2 mandates contradiction-resolution for facts in tension; whether trivial description improvements take a cheaper last-write path is a tuning decision, gated on measured curation cost.

---

## What this is deliberately NOT

- **Not a subject-split substrate.** Personal/Stance/World live in one Core; only the curation tier (Core vs Docs) separates physically.
- **Not a markdown wiki.** The Karpathy-wiki *contradiction-linting idea* is adopted (D2); its markdown-substrate superiority claim is not (it failed verification — FRE-635 evidence).
- **Not a chunk-retrieval store.** Docs chunks are provenance anchors, never the retrieval unit (D3).
- **Not a fourth pedagogical class.** System is the negative space, not a peer of P/W/S; goals/intentions still classify as Stance/Personal (ADR-0097, confirmed by the spike).
- **Not the access model.** *What is stored and how it lives* is here; *how it is retrieved* is ADR-0096.

---

## Alternatives Considered

- **Keep first-write-wins / adopt naive last-write-wins.** Rejected — the former freezes wrong facts (the live bug); the latter lets a bad later extraction clobber a good one. Correction-by-contradiction + bitemporal evolution is the only model that keeps knowledge both *living* and *trustworthy* (D2).
- **Physically isolate the soul (Personal/Stance) from World now.** Rejected — the Stance edge and the insight scan are the highest-value workloads and both re-cross or live inside that boundary; isolation taxes them for a blast-radius win that the D3 class-scoped-guard delivers inside one Core. Deferred with a trigger, not adopted.
- **Drop/merge Stance because it is rare (~3%).** Rejected — the rarity is extraction loss (finding #3), not a taxonomy signal; at the source level Stance is loud, explicit, and pedagogically central.
- **Filter the ~46% as test traffic.** Rejected — it is genuine dev-phase owner activity, not test (≈1% eval turns); a test filter removes almost none of it. A subject/intent class (System) is required (D1).
- **Build Tier-3 summarization up front.** Rejected — most expensive piece, run over a ~46%-polluted corpus it would surface garbage; deferred on a clean-corpus trigger (D7).
- **One undifferentiated store with one lifecycle.** Rejected — World must not decay, System must, Stance must be review-scheduled; the taxonomy *is* the lifecycle policy (D4).

---

## Consequences

**Positive.** Stance and Personal finally have a structural home, so the binding constraint (extraction) is addressed before storage rather than after. Knowledge becomes living — correctable and evolvable with an audit trail — killing the first-write-wins freeze. The tutor corpus is de-polluted (System gated), a direct Memory-Recall-Quality win (ADR-0087). The crown-jewel Stance traversal and the insight scan stay native (Core unified). Docs isolation gives blast-radius safety and per-tier lifecycle without splitting the soul. The first R2↔graph provenance join appears. The design is forward-compatible with Tier-3.

**Negative / risk.** This is a large, multi-ticket change to a live substrate; the extraction-first sequence (D5) means visible value lags the first tickets. The Claims/bitemporal model adds write-path complexity and must not regress recall latency (ADR-0096 hot path). Contradiction-resolution can mis-adjudicate and corrupt a correct fact — it must keep the superseded original (D2) so any bad merge is recoverable. The System classifier can mislabel a genuine World item as System and starve the tutor, or vice-versa pollute it — its precision is itself an acceptance criterion. Turning on eviction (D4) is destructive execution: it must be class-scoped and soul-excluded (D3) or it is a data-loss risk. Transcript offload (D6) moves bytes out of the hot store — the pointer must never dangle (joinability probe).

---

## Verification / Acceptance Criteria

Outcome-level and discriminating — each states the observable result and how it is checked; a broken or half-built implementation must fail it. These are the criteria the implementation tickets (D-seq) carry, sliced below.

1. **First-write-wins is dead — a wrong first fact is correctable.** Write a World/Personal Claim with a thin/wrong value, then re-assert the correct value (higher confidence / in contradiction). **Check:** querying the current fact returns the **corrected** value, and the original is retained as superseded (not gone). *Fails if* the first value is still returned (freeze persists) or the original is destroyed (no audit trail). — D2
2. **Evolution is bitemporal, not destructive.** Change a Personal fact that *was* true (e.g. a lease end-date). **Check (Cypher):** the prior Claim has `valid_to`/`invalid_at` set and is still present; the current-valid query returns only the new Claim; the two validity intervals do not overlap. *Fails if* the update overwrites in place or deletes history. — D2
3. **Stance and Personal survive extraction as structured items.** Run the redesigned extractor over the known car-buying turn (spike session `6b0e7d46`, seq 1) or an equivalent fixture. **Check:** ≥1 `HAS_STANCE` edge from the owner node to a World concept *with* affect/mastery, **and** ≥1 Personal situational Claim (the lease fact) — **neither** flattened into a World entity's `description`. *Fails if* the output is still only the 7 World-ish entity types (the current flattening/dropping reproduces). — D5
4. **System material is gated from recall and never promoted — across the operational breadth, not just healthchecks.** Ingest a fixture set spanning all four System subjects D1 names: a healthcheck, a telemetry/log-review turn, a harness/tooling turn, and a connectivity ping. **Check:** every extracted item from all four carries `class=System`; a tutor/recall query for a *domain* prompt returns **zero** System items; a graph query for `class=System AND memory_type=semantic` (promoted) returns **zero**. *Fails if* a classifier that only keys on the word "healthcheck" lets telemetry/harness entities (e.g. `sensor_poll`, executor.py, Neo4j) through as World, or any System item is promoted to durable. — D1, D4
5. **The Stance traversal is native (Core unified).** **Check (single Cypher query):** `owner -[:HAS_STANCE]-> WorldConcept -[:RELATED_TO]-> WorldConcept` returns results in **one** graph query with no cross-store hop. *Fails if* the Stance edge spans two physical stores and the walk requires an application-side join. — D3
6. **Provenance joins; Docs is never hot-traversed.** **Check:** the joinability probe (ADR-0074) finds **every** promoted Core Claim has a non-dangling provenance pointer to its source; and the hot recall/tutor path issues **zero** traversals *through* Docs (inspect query plans / instrumentation). *Fails if* Core Claims are orphaned from their source, or a hot query walks Docs. — D3, D6
7. **World-internal correlation is queryable now.** **Check:** over the gated (System-excluded) corpus, a query returns a World↔World bridge between two concepts the owner never explicitly linked (graph path or vector-similarity), and returns **no** System-class bridge. *Fails if* World is unwalkable for correlation, or System pollution dominates the bridges. — D7
8. **Transcripts are extracted-and-pointed, not hoarded.** After the retention job, a `conversation` source past the retention window carries a **pointer**, and the verbatim text is fetchable from R2 by that pointer — not stored hot in Neo4j. **Check:** the post-window `:Turn`/source node holds no full `user_message`/`assistant_response`, and the R2 fetch by pointer returns the text. *Fails if* full transcripts remain hot indefinitely (current behavior). — D6
9. **Co-authorship differentiates trust — both directions, pinned to source identity not repetition.** Using `KnowledgeWeight.source_type` + source-id at the promotion gate. **Check:** (a) an agent-derived claim asserted **twice from the same agent source/session** is **not** promoted — *repetition is not corroboration*; (b) the **same** claim corroborated by a **second distinct non-agent source** **does** promote — where that second source-id resolves to a source-type **independently recorded as non-agent in the store's source registry** (or carries a trusted-source flag **set at ingest time**), *not* a source-id the agent self-assigned; an agent-emitted claim carrying a synthetic or self-attributed second source does **not** satisfy corroboration; (c) a user-asserted Stance/Personal **is** retained/promoted at face value on first assertion. *Fails if* repeated same-source agent self-assertions auto-promote ((a) collapses into (b)), **or** an agent can manufacture corroboration with a self-assigned source-id, **or** a never-promote gate blocks the genuinely-corroborated case ((b) fails), **or** all three collapse into identical handling. — D6
10. **The ephemeral tier actually evicts — and only it, by class scope not by luck.** Set the configured ephemeral TTL `X` (the ADR-0042 freshness/eviction setting); create a System and an episodic item aged past `X`, plus contemporaneous (same-aged) Personal and Stance Claims. **Check:** (a) inspect the eviction job's match set (dry-run / `EXPLAIN` the Cypher) and confirm it selects **only** `class=System`/episodic candidates — the Personal/Stance Claims are provably **not in the match set**; (b) run the job and confirm the past-TTL System/episodic item is **gone** (evicted, not merely flagged dormant) while the same-aged Personal/Stance Claims **remain**. *Fails if* nothing is evicted (lifecycle still proposal-only — the current state), or if the match set includes any Personal/Stance Claim (the soul survived by luck, not by the class-scoped guard, D3). — D4, D3

**The assembled-ADR seam (closes only when all children land):** criteria **3 + 4 + 1 together, through one pipeline, over a two-fixture integration** — because no single real turn carries all four classes (the car-buying turn has Personal/World/Stance; System needs an operational turn). Run *both* the FRE-636 car-buying fixture (must yield a Stance edge + a Personal Claim + World, not flattened — criterion 3) **and** an operational fixture (must yield System, gated out — criterion 4) **through the same extraction→storage→promotion path**, then correct one of the emitted World/Personal Claims and confirm the update lands and supersedes (criterion 1). All three must pass together. No single ticket proves this; it is the integration criterion master holds the decomposed ADR against, and it does **not** close because the last child merged — only because both fixtures demonstrably produce the right graph end-to-end.

*ADR-0098 is Proposed pending owner acceptance. It was co-designed with the owner (2026-06-27) and the design is settled; status elevation to Accepted is master's call at the integration gate.*
