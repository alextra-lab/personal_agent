# ADR-0098 — Memory Substrate & Lifecycle Architecture (Core/Docs topology; the living-knowledge model)

**Status:** Accepted — 2026-06-27 (owner greenlight; build wave FRE-637–642 Approved, FRE-643 Tier-3 deferred-with-trigger) · **§D1 (class-as-stored-property) + its query-time System recall filter superseded by ADR-0115 (2026-07-11); §D2 / §D4 / §D7 remain Accepted.** · **Amended 2026-08-30 (Amendment A — a provenance chain must terminate outside the agent; the retrieval tool declares its referent; provenance is written atomically with the entity or relationship as an append-only edge, not only on the Claim; entitlement follows the terminus). Trigger: FRE-1338.**
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

### D3 — Topology: Core unified; Docs an isolatable provenance layer; the seam is never hot-joined *(Amendment A distinguishes the query-shape sense of "terminal" — preserved unchanged — from the epistemic sense; see A1)*

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
5. **provenance + a timestamp** on every Claim, so it can be superseded (D2). *(Amendment A extends this obligation to entities and relationships, not Claims alone — see A4.)*

**One source yields many class items** (ADR-0097 invariant 1) — this *is* the document-triage decision. A single document is **not** a class: a medical textbook the owner is studying yields World content + a Stance edge ("learning it") + Personal facts (if annotated with their case) + possibly System (if it is an infra runbook). Triage runs **per extracted item, not per document** — "ingest must classify, not assume" (ADR-0097). Document sources are triaged by the **same contract** as conversation turns; the only Layer-0 difference is retention (D6 — documents keep verbatim bytes in R2; conversations offload the transcript). Document *chunking strategy* (how a long document is segmented into provenance anchors before extraction) is the one document-ingest detail deferred — **trigger:** the first non-conversation document source is actually wired (today there is none; all sources are conversations).

This is the contract; the extractor model/prompt is implementation. Substrate tickets that depend on Stance/Personal/System storage are **blocked on this landing** — designing Stance storage on an extractor that never emits Stance is the exact failure that makes the crown jewel look unused and get cut in a year.

### D6 — Retention: extract-and-point, not transcript hoarding *(terminus + carrier rules added by Amendment A)*

Resolves ADR-0097's Layer-0 `retention` question:

- **`conversation` source** = provenance-only. After extraction, the verbatim transcript is **offloaded to R2 (ADR-0069)** with a pointer on the source node; it does not live hot in Neo4j past a retention window. (Today `:Turn` stores full `user_message`/`assistant_response` indefinitely.)
- **`document` source** = verbatim, re-readable — bytes in R2, a keyed pointer from Core (the D3 Docs seam).
- **Co-authorship → trust** (ADR-0097 Layer-0 `co-authored?`): user-asserted Stance/Personal is trusted at face value (the owner is the authority on their own stance); **agent-derived** claims (the agent was a conversation participant) require corroboration before promotion to durable. Realized through `KnowledgeWeight.source_type` at the promotion gate. **[Amended by FRE-1020, 2026-07-27 — realization only; the decision above is unchanged.]** Co-authorship is **not** expressible through `source_type`, and attempting it is why the rule was inert for a year. `source_type` is a **channel** vocabulary (`conversation | tool_result | web_search | manual | inferred`) recording *how* a fact arrived; every extracted Claim arrives through `conversation`, so the slot was constant — and because confidence derived solely from it, D2's weaker-claim guard ("not naive last-write-wins") was **unreachable on the production path** — only the `observed_at` staleness check still discriminated (live: 94 Claims, one distinct confidence, zero rejections ever). Co-authorship is therefore a **distinct axis**, `Claim.asserted_by` (`user` | `agent`), and confidence derives from *(channel, authorship)* — the agent tier pinned to the pre-existing channel base so no existing supersession path regresses. Per **AC-9**, the value is **derived in Python from the role-partitioned captured turn and never read from the extractor's output**: were the model allowed to declare a claim user-asserted, it could mint the very credential that makes its own output authoritative. Measured on the live corpus, agent-grounded claims are the plurality (~43 %), so the discriminator has a real population. Scope: this restores the **D2 supersession guard**; D6's **corroboration/promotion gate (AC-9 (a)/(b))** — which needs a source registry with ingest-time trusted-source flags — remains unimplemented.

This also creates the first **R2↔graph join** (a typed provenance pointer), which today does not exist. *(Amendment A: this pointer terminates at the conversation transcript, which for an agent-participant turn is the agent's own prose — a hop, not a root. A1 requires the chain to be walkable to an external artifact.)*

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
6. **Provenance joins; Docs is never hot-traversed.** **Check:** the joinability probe (ADR-0074) finds **every** promoted Core Claim has a non-dangling provenance pointer to its source; and the hot recall/tutor path issues **zero** traversals *through* Docs (inspect query plans / instrumentation). *Fails if* Core Claims are orphaned from their source, or a hot query walks Docs. — D3, D6 *(Amendment A: "every promoted Core Claim" leaves entities and relationships uncovered, and the entity path is the one FRE-1338 travelled — see AC-A1.)*
7. **World-internal correlation is queryable now.** **Check:** over the gated (System-excluded) corpus, a query returns a World↔World bridge between two concepts the owner never explicitly linked (graph path or vector-similarity), and returns **no** System-class bridge. *Fails if* World is unwalkable for correlation, or System pollution dominates the bridges. — D7
8. **Transcripts are extracted-and-pointed, not hoarded.** After the retention job, a `conversation` source past the retention window carries a **pointer**, and the verbatim text is fetchable from R2 by that pointer — not stored hot in Neo4j. **Check:** the post-window `:Turn`/source node holds no full `user_message`/`assistant_response`, and the R2 fetch by pointer returns the text. *Fails if* full transcripts remain hot indefinitely (current behavior). — D6
9. **Co-authorship differentiates trust — both directions, pinned to source identity not repetition.** Using `KnowledgeWeight.source_type` + source-id at the promotion gate. **Check:** (a) an agent-derived claim asserted **twice from the same agent source/session** is **not** promoted — *repetition is not corroboration*; (b) the **same** claim corroborated by a **second distinct non-agent source** **does** promote — where that second source-id resolves to a source-type **independently recorded as non-agent in the store's source registry** (or carries a trusted-source flag **set at ingest time**), *not* a source-id the agent self-assigned; an agent-emitted claim carrying a synthetic or self-attributed second source does **not** satisfy corroboration; (c) a user-asserted Stance/Personal **is** retained/promoted at face value on first assertion. *Fails if* repeated same-source agent self-assertions auto-promote ((a) collapses into (b)), **or** an agent can manufacture corroboration with a self-assigned source-id, **or** a never-promote gate blocks the genuinely-corroborated case ((b) fails), **or** all three collapse into identical handling. — D6
10. **The ephemeral tier actually evicts — and only it, by class scope not by luck.** Set the configured ephemeral TTL `X` (the ADR-0042 freshness/eviction setting); create a System and an episodic item aged past `X`, plus contemporaneous (same-aged) Personal and Stance Claims. **Check:** (a) inspect the eviction job's match set (dry-run / `EXPLAIN` the Cypher) and confirm it selects **only** `class=System`/episodic candidates — the Personal/Stance Claims are provably **not in the match set**; (b) run the job and confirm the past-TTL System/episodic item is **gone** (evicted, not merely flagged dormant) while the same-aged Personal/Stance Claims **remain**. *Fails if* nothing is evicted (lifecycle still proposal-only — the current state), or if the match set includes any Personal/Stance Claim (the soul survived by luck, not by the class-scoped guard, D3). — D4, D3

**The assembled-ADR seam (closes only when all children land):** criteria **3 + 4 + 1 together, through one pipeline, over a two-fixture integration** — because no single real turn carries all four classes (the car-buying turn has Personal/World/Stance; System needs an operational turn). Run *both* the FRE-636 car-buying fixture (must yield a Stance edge + a Personal Claim + World, not flattened — criterion 3) **and** an operational fixture (must yield System, gated out — criterion 4) **through the same extraction→storage→promotion path**, then correct one of the emitted World/Personal Claims and confirm the update lands and supersedes (criterion 1). All three must pass together. No single ticket proves this; it is the integration criterion master holds the decomposed ADR against, and it does **not** close because the last child merged — only because both fixtures demonstrably produce the right graph end-to-end.

*ADR-0098 is **Accepted** — co-designed with the owner and greenlit 2026-06-27, as the status header and the ADR index both record. This line previously read "Proposed pending owner acceptance", which contradicted both; the elevation it describes as pending had already happened on the day the ADR was written. The integration criterion above is unaffected: it is what closes the assembled seam, not what elevates the status.*

---

## Amendment A — 2026-08-30: provenance must reach the knowledge, and it must terminate outside the agent

**Trigger:** FRE-1338, a verified cross-session incident. **Amends:** D3 (the meaning of "terminal"),
D5(5) (the obligation's subjects) and D6 (what the pointer must resolve to). **Does not disturb** D2,
D4, D7, or the FRE-1020 co-authorship realization. **Unblocks:** FRE-640 (filed 2026-06-27, parked in
`Backlog` since 2026-07-10) and FRE-1022, which explicitly deferred its design question to "an
ADR-0098 amendment or a small ADR of its own".

### The incident, verified rather than inferred

Model A researched GPSR obligations, read real pages, and named two real vendors — SafeCart and
EaseCert. Entity extraction minted both as `Organization` nodes 11 seconds later. Thirty-one seconds
after that, Model B in a **different session** recalled them through `search_memory`'s `entity_match`
path and published a bibliography listing *"SafeCart, What Is GPSR?"* and *"EaseCert, 2026 GPSR
Compliance"*. It had opened neither page.

What crossed was checked against the graph, not assumed. `matched_turns` returns `turn_id`,
`timestamp`, a `mark_truncated(user_message, 400)`, `summary` and `key_entities`
(`tools/memory_search.py:198-206`) — **not** `assistant_response`. The source turn's 7,180-character
response was never returned, and its stored summary names no document. Model B inherited two bare
nouns and composed the titles around them. The facts were true; the addresses were absent.

### Where the address actually is — corrected in review

An earlier draft of this amendment claimed the addresses were never recorded. **That is false, and
the correction matters because it makes the fix smaller.** `fetch_url`'s URL is persisted durably:
every dispatched call appends `"arguments": plan["arguments"]` to `ctx.tool_results`
(`orchestrator/executor.py:6538-6549` — put there by FRE-947 precisely because arguments "were
dropped before the capture"), and the turn's `TaskCapture` is constructed with
`tool_results=ctx.tool_results` (`executor.py:3835`), a field the capture model declares and
persists (`captains_log/capture.py:84`).

So the consolidator **already receives** every URL the turn fetched. It simply never looks: entity
extraction is handed exactly two fields — `capture.user_message` and a `<think>`-stripped
`capture.assistant_response` (`second_brain/consolidator.py:589-592`, called at
`consolidator.py:607-615`) — and nothing else from the capture reaches it, so `tool_results` sits
unread one attribute away. Entities are then written with a hardcoded
`KnowledgeWeight.from_source("conversation")` (`consolidator.py:798`).

**The defect is therefore association and propagation, not capture.** Two seams are missing:

1. **Association** — nothing links an *extracted item* to the *tool result that supported it*. A turn
   with four `fetch_url` calls and nine extracted entities has no rule assigning which address
   justifies which entity.
2. **Propagation** — the KG write does not copy any address onto the node or edge it creates. A live
   `Organization` node carries 26 properties including `confidence`, `extractor_model` and
   `originating_session_id`; **none** records where the knowledge came from.

The turn-scoped `SourceRegistry` (`executor.py:3627`) and its end-of-turn telemetry snapshot, which
omits `referent` (`executor.py:2194-2206`), are **not** part of this causal chain — the snapshot is a
log call and consolidation never reads it. That observability gap is real but incidental; it is
recorded here so a reader does not mistake it for the cause.

### A1 — A provenance chain must terminate outside the agent *(owner policy, not a deduction)*

A terminus is an **external artifact**: a fetched page, an ingested document, or a statement the owner
made. A chain whose last hop is a `:Turn` the agent authored is a hop to be walked through, never a
root to stop at.

This is stated as an **owner decision, not as something the incident proves.** The incident
demonstrates that the recall projection loses addresses; it does not by itself establish that an
agent-authored transcript carrying resolvable citations could never serve as a root. The rule comes
from the owner's stated purpose for provenance (2026-08-30): *"if the agent tells me something,
provenance is the closest I can get to verifying its truthiness — that's what I want."* A pointer to
the agent's own earlier output cannot serve that purpose; it cites the agent to justify the agent.
Recorded this way so a future reader can revisit the policy without having to re-litigate the
evidence.

**This changes what D3 means by "terminal", and D3's row is amended accordingly.** D3's table calls
`Core → fetch Docs by id` *terminal* in the **query-shape** sense — stop hot-traversing, the seam is a
keyed one-way lookup. A1 uses "terminus" in the **epistemic** sense — where a justification bottoms
out. These are different axes, and D3's keyed one-way lookup is preserved unchanged. What A1 adds is
that reaching a conversation transcript is not *epistemically* terminal: the chain continues to the
artifact that transcript's turn actually retrieved. D6's `conversation`-source pointer stands as a
hop, not as the root it was implicitly treated as.

### A2 — The retrieval tool declares its referent

`REFERENT_ARGUMENTS` (`grounding/source_registry.py:408-410`) is a one-entry dict —
`{"fetch_url": "url"}` — living in the grounding module and describing tools that do not know it
exists. The declaration moves onto `ToolDefinition` as `referent_parameter: str | None` — the name of
the parameter whose value is the thing retrieved, which is exactly what `REFERENT_ARGUMENTS` encoded,
relocated to the tool that knows it. `fetch_url` declares `referent_parameter="url"`; a tool
addressing a query rather than a referent leaves it `None`. The registry becomes a *consumer* of the
tool contract rather than an oracle about it, and `REFERENT_ARGUMENTS` is deleted.

**Scope boundary, stated deliberately.** Repairing FRE-1338 requires only that `fetch_url`-class
tools — those addressing exactly one external referent — declare it. **Per-result referents out of
`web_search` are deliberate additional scope, not a repair**: the incident's addresses came from
fetches, and ADR-0138 already records per-result search provenance as knowingly deferred
(`source_registry.py:416-424`). Sequenced after the repair, not inside it.

### A3 — Provenance travels in the same record as the content it justifies

Never a side channel with an independent lifetime.

- **Not the event bus.** Not for reliability — the bus is enabled and healthy, and ADR-0041 already
  has consumers fetch durable source data rather than trust the event payload — but because a message
  in flight is not a value in the row. Provenance is a **write-time integrity constraint**, not a
  notification. The bus may still *trigger* consolidation, exactly as ADR-0041 designs it.
- **Not a session-scoped ledger.** The fetch→consolidation window is not bounded by a session:
  `consolidate_recent_captures(days=7, limit=50)` (`brainstem/scheduler.py:930`) sweeps on-disk
  captures under min-interval, in-flight-request and resource-pressure gates, plus retry. It spans
  sessions and process restarts.
- **The capture already satisfies this rule for tool arguments** (FRE-947), which is why the repair is
  propagation rather than new storage. The obligation this amendment adds is that **extraction and
  the KG write preserve the same property**: the address moves with the item it justifies.

### A4 — Which source justifies which item is decided by containment, in Python

The association seam has no answer today, and it is the hard half. Three candidates were live.

*Rejected — turn-level association.* Every item extracted from a turn references every external
referent that turn fetched. Cheap, and worthless: a turn fetching four pages would attribute all nine
extracted entities to all four, so a provenance pointer would assert support it does not have. It
also makes the verification criterion vacuous — any address passes.

*Rejected — the extractor declares it.* Pass the tool results into the extraction prompt and have the
model emit which source supported each item. This is the exact failure ADR-0098 D6 already ruled out
under FRE-1020/AC-9: the value must be **"derived in Python from the role-partitioned captured turn
and never read from the extractor's output"**, because a model permitted to declare its own
provenance can mint the credential that makes its output authoritative. Model-declared provenance is
not provenance.

*Chosen — containment, computed in Python at write time.* An extracted item is provenanced to the
source(s) whose **retrieved content contains it**, decided by the D3(c) containment check ADR-0138
already ships (`grounding/containment.py`, normalized token presence — the same function
`verify_turn` calls). It is deterministic, derivable without the model's cooperation, reuses existing
machinery rather than inventing a second definition of "the source supports this", and it is
computable retroactively (A5) because captures retain each tool result's `output`.

Containment is evaluated **at write time, while the retrieved content is in hand** — not at read
time. The graph therefore stores the *result* of the check, and no durable copy of page bytes is
needed in Core to make provenance resolvable later.

**Attribution is not verification, and this rule buys only the first.** D3(c) containment was written
to ask "does this source support this assertion". Used here it answers a weaker question — "does this
source mention this item" — and that is deliberate. A page mentioning SafeCart justifies *"this is
where we learned of SafeCart"*; it does **not** license the entity's stored `description`, `type`, or
any claim about it. Provenance is the owner's instrument for going and checking
(A1); it is not a second grounding gate, and an implementer must not read it as one. Anything
stronger is ADR-0138's job, on the assertion, at read time.

**The string passed to the check, per item kind:** an Entity contributes its **name** only (matching
the attribution semantics above); a Claim contributes its content; a relationship contributes its
verbalization as `source-name predicate target-name`. A match is recorded on
`ContainmentOutcome.CONTAINED` only — `ENTAILMENT_REQUIRED` and `UNVERIFIABLE` do **not** create a
provenance reference, because an attribution that needed an entailment judgement is not the
mechanical, model-independent link this rule exists to provide. The consequence is recorded rather
than discovered: lowercase or stylized names (`npm`, `iPhone`) and stopword-like names fall to
`none` under `grounding/containment.py`'s entity pattern and function-word list. That is a known
false-negative class, not a silent one — it is countable via A5's reporting, and narrowing it is
follow-on work, not a blocker.

**An item containing no external source's content gets no source reference**, and falls to A5's
`none`. That is the honest outcome for a fact the agent produced rather than read, and it is what
makes the criteria below able to fail.

### A4b — The physical model, decided here because a scalar reintroduces first-write-wins

Entities are `MERGE`d and accumulate mentions across many turns and sources
(`memory/service.py:2198-2229`, where origination is preserved on-create only). A single `provenance`
property would overwrite on the second sighting or freeze on the first — the first-write-wins failure
D2 exists to kill, reintroduced on a new axis.

- **`:Source` is a Core node** carrying `referent`, `retrieved_at`, `content_hash`, and a *pointer* to
  the retained bytes. This preserves D3: Core holds the small keyed pointer, the bytes live in the
  isolatable Docs/R2 layer, and no hot query traverses into it. It also makes A4's same-transaction
  requirement trivially satisfiable, since the Core write touches only Core.
- **Two identities, deliberately separated.** *Provenance-version identity* is
  `(referent, content_hash)`: the same URL re-fetched unchanged is the same Source, a changed page
  mints a new one, and *"the page moved under the claim"* becomes detectable rather than silent.
  *Corroborating-authority identity* is the **referent's origin** (its resolved host or document
  identity), **not** the version. Two versions of one page are **one** authority. Stated separately
  because collapsing them would let a single page changing over time satisfy D6's requirement for a
  second distinct source — repetition wearing a new hash, which is precisely the failure AC-9 exists
  to prevent.
- **Nodes carry an edge**: `(:Entity|:Claim)-[:SOURCED_FROM]->(:Source)`, append-only. A canonical
  entity seen from three distinct sources carries three references, so corroboration becomes
  **countable by distinct source identity** rather than by repetition — the storage surface FRE-1022's
  gate was blocked on.
- **Relationships carry a property, not an edge.** Extracted relationships are native Neo4j
  relationships created through `apoc.merge.relationship` (`memory/service.py:3833-3860`), and a
  relationship cannot be the endpoint of another relationship. They therefore carry a `source_ids`
  list property, appended and de-duplicated on the `YIELD rel` the existing statement already
  returns; each element is the `:Source` node's own `source_id`. Stated explicitly because an earlier draft of this amendment
  specified an edge for both and was unimplementable for relationships.
- The provenance write is **in the same transaction** as the node or edge it justifies.

### A5 — Two states, never a silent third; and backfill reconstructs before it surrenders

A knowledge item carries `provenance_state` ∈ {`'provenanced'`, `'none'`} — a stored, queryable value
on the node or relationship, never an absent property to be inferred. Both values are explicit; there
is no third, and no null.

**The transition `none → provenanced` is expected and allowed**, in that direction only. Provenance
is append-only (A4b), so an item written without a source legitimately gains one later — a
subsequent turn whose fetched page contains it, or the A5 reconstruction below. An item never
returns to `none`.

**Legacy items are reconstructed, not blanket-marked.** Captures retain `tool_results` including each
call's `arguments` **and** `output` (`captains_log/capture.py:84-85`), so the A4 containment rule can
be replayed offline against the capture that minted each entity. The migration:

- applies the same containment check to each historical item against that turn's tool-result outputs;
- **multiple matches are recorded, not treated as ambiguity** — provenance is append-only, so an item
  contained in two fetched pages legitimately carries both;
- marks `none` only where no tool result contains the item, or where the minting capture is missing;
- reports reconstructed and `none` counts separately.

**Legacy *relationships* are largely not reconstructable and this is accepted, not hidden.**
Relationship writes persist neither a trace nor a session key (`memory/service.py:3849-3857`), so
there is usually no join back to the capture that minted them. They take `none` and are reported as
such. Entities, which carry `originating_session_id` and `source_turn_ids`, are the reconstructable
population.

A blanket `none` would discard recoverable provenance and is explicitly rejected.

### A6 — Entitlement follows the terminus *(this amends ADR-0138 D2, and must be recorded there)*

Recording `none` does not by itself stop the fabrication this amendment exists to prevent, so the
consuming rule is decided here:

| Terminus of the chain | Entitlement |
|---|---|
| External artifact (fetched page, ingested document) | `EXTERNAL` |
| A statement the owner made | `USER_STATED` |
| An agent-authored turn, **or** `provenance_state = 'none'` | `AGENT_DERIVED` — usable as context, **not** admissible as a citation |

**Aggregation for a mixed recall** follows the rule already in the module rather than inventing a
second one: a call is only as entitled as its **least-entitled item**, the same most-restrictive shape
`_search_memory_entitlement` applies across Claim rows (`grounding/source_registry.py:531-540`). One
`none`-terminus item in a recall drops the whole registration to `AGENT_DERIVED`, until the per-item
entitlement architecture FRE-1302 deferred actually lands.

**Provenance does not become an input to `verify_turn`, and the two must not be wired together.**
`verify_turn` runs `check_containment(span.text, source.content)` against the content the turn
actually registered (`grounding/verification.py:342-360`), and that is unchanged: a recall registers
the recalled item's content, exactly as today. The provenance chain is **the owner's** path back to
the originating artifact, per A1's stated purpose — not a second containment input. Making read-time
verification load retained bytes would put the inline path into Docs and break D3's Core-only hot
query shape, which is why it is ruled out here rather than left for an implementer to discover.
What A6 changes for `verify_turn` is only the **entitlement** the recall registers with.

**This is a narrowing of ADR-0138 D2, not a residue of it, and calling it residue was wrong.** ADR-0138
treats a typed memory retrieval as admissible with reachability vacuous for referent-less items;
A6 forbids an agent-authored or `none` terminus outright. The implementation therefore carries an
obligation to record this as an amendment to ADR-0138 D2 in the same wave — an ADR narrowed by
another ADR's amendment, without its own text changing, is exactly the drift this project keeps
paying for.

### Verification / Acceptance Criteria

The ADR-0074 joinability probe is **not** the instrument: it walks session identity across substrates
and its entity check is a `count(e)` keyed on `originating_session_id`
(`observability/joinability/walk.py:1001-1018`), with no notion of a provenance chain. These criteria
are checked by direct Cypher over the A4b structures plus the named live scenarios.

- **AC-A1 — provenance reaches the knowledge, and the reference actually supports it.** Take every
  `:Entity` and every extracted relationship created after the change **from a turn whose capture
  holds at least one external-referent tool result**. **Check:** each either carries a `SOURCED_FROM`
  reference (or `source_ids` entry) to a `:Source` whose retrieved content **contains that item's
  attribution string** under the D3(c) check at `CONTAINED` (entity → name; claim → content;
  relationship → `source-name predicate target-name`), or carries `provenance_state = 'none'`
  **and** no tool result in that capture contains that string. *Fails if* (a) an item carries only `none` while some fetched result does contain it —
  the universal-`none` implementation — **or** (b) an item references a `:Source` whose content does
  not contain it, which is the false-association shortcut of linking every entity to any URL the turn
  happened to fetch. Both vacuous implementations are rejected by construction.
- **AC-A2 — the address survives the consolidation window.** Fetch a page in session A; end session A;
  let consolidation run only *after* session end. **Check:** the entity minted from that turn
  references a `:Source` whose referent is the fetched URL. *Fails if* the reference appears only when
  consolidation runs in-session — the seeded negative for the session-ledger design A3 rejects.
- **AC-A3 — provenance is bus-independent.** Repeat AC-A2 with the event bus disabled, triggering
  consolidation directly. **Check:** the reference is still written. *Fails if* it is absent, which
  would prove it travelled by bus contrary to A3.
- **AC-A4 — the leak is closed, positively, without severing recall.** Run the same question in two
  sequential sessions. **Check (a):** session B's recall of an entity minted in session A returns
  session A's **external referent** — not a bare name, and not `none`, since the address is
  recoverable and containment holds. **Check (b):** a recall of the owner's own prior statement still
  returns and is usable at `USER_STATED`. *Fails if* (a) returns a bare noun (today's result) or
  `none` where containment holds, **or** if (b) regresses — a fix that severs memory passes a weaker
  (a) and breaks the product.
- **AC-A5 — the sentinel is complete over nodes *and* relationships, and reconstruction was
  attempted.** **Check:** `MATCH (n) WHERE (n:Entity OR n:Claim) AND n.provenance_state IS NULL
  RETURN count(n)` returns 0 — and likewise for any value outside {`'provenanced'`, `'none'`} —
  **and** the equivalent over extracted relationships
  (`MATCH ()-[r]->() WHERE type(r) IN $extracted_types AND r.provenance_state IS NULL`) returns 0.
  Separately, the migration reports reconstructed-vs-`none` counts, and a fixture of ≥10 legacy
  entities whose minting captures demonstrably contain their names must reconstruct **non-zero**.
  *Fails if* either query is non-zero, or if the reconstruction fixture yields zero — which is what a
  migration that marked `none` without replaying containment would produce.
- **AC-A6 — the tool contract is the single source of referents.** **Check:** `REFERENT_ARGUMENTS`
  no longer exists (grep returns no definition), and a fixture tool declaring
  `referent_parameter="<param>"` on its `ToolDefinition` and registered in the test registry produces
  a resolvable `:Source` end-to-end
  **with no edit inside `grounding/`**. *Fails if* the grounding module still carries a per-tool
  table, or if the fixture tool requires a grounding-side change to be recognised — the drift A2
  exists to end.
- **AC-A7 — entitlement follows the terminus, including the mixed case.** Construct four recalls: one
  terminating at a fetched page, one at an owner statement, one at an agent-authored turn, and one
  **mixed** (a fetched-page item and a `none` item in the same call). **Check:** the first three
  register `EXTERNAL`, `USER_STATED`, `AGENT_DERIVED`, the third is not admissible as a citation, and
  the mixed call registers `AGENT_DERIVED` by the least-entitled-item rule. *Fails if* a bare entity
  recall is still stamped `EXTERNAL` (today's behaviour via the no-Claims branch), or if the mixed
  call inherits the entitlement of its best item.

### Carried as open — decided by the implementation, not pretended settled here

Three review rounds closed the principles and the storage topology. These remain genuinely open, and
are listed so an implementer resolves them deliberately rather than by accident:

1. **How the content hash is computed** — over the captured (possibly truncated) tool output, or over
   the full retained bytes — and how pointer/hash integrity is checked on read. Affects whether
   "the page moved" is detectable in the truncation case.
2. **Narrowing the containment false-negative class.** Lowercase, stylized and stopword-like entity
   names fall to `none` by A4's `CONTAINED`-only rule. A5's reporting makes the rate visible; whether
   it warrants an alias or casing pass is a decision to take on measured numbers, not in advance.
3. **Whether `web_search` gains per-result referents** (A2's deliberate additional scope), and if so
   whether its snippets count as retrieved content for A4 attribution.

Each is scoped so that getting it wrong degrades coverage — more items at `none` — rather than
producing a false attribution. That asymmetry is intentional: an item honestly marked unprovenanced
is recoverable later under A5's `none → provenanced` transition; a wrong address is not.

### Scope explicitly **not** taken

Cross-arm eval contamination — a sequential run in which arm N recalls arm N−1 — is a **run-scoping**
problem, not a provenance one: a perfectly-provenanced fact still crosses. Correct provenance makes
it *visible*, never absent. Tracked separately against the FRE-375 substrate-isolation shape.

Per-item (rather than per-call) entitlement registration remains FRE-1302's deferred architecture;
A6 aggregates most-restrictively until it lands.
