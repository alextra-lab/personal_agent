# ADR-0126: Reading the Living-Knowledge Substrate — Stance-First Push, Pull-Only Claims

**Status:** Proposed — 2026-07-27
**Date:** 2026-07-27
**Deciders:** Project owner; adr session (Opus); master session (independent verification)
**Tags:** memory, recall, knowledge-substrate, context-assembly, boundary, criteria-quality
**Backing ticket:** FRE-1012
**Implements the read half of:** ADR-0098 (Memory Substrate & Lifecycle Architecture — Accepted 2026-06-27; §D1 and its query-time System recall filter superseded by ADR-0115)

---

## Context

**What is the issue we're addressing?**

ADR-0098 D2 replaced first-write-wins with a living-knowledge substrate: durable facts as
provenance-bearing, bitemporally-valid **Claims**, and the owner's affect toward World concepts as
native **Stance** edges. Both shipped. **Neither has ever been read into a turn.**

A word-boundary search for `Claim | HAS_FACT | Stance | HAS_STANCE | assert_claim | assert_stance`
across `src/` returns exactly six files — `memory/service.py`, `memory/supersession.py`,
`memory/models.py`, `second_brain/consolidator.py`, `second_brain/entity_extraction.py`,
`brainstem/scheduler.py`. All write-side. There are **zero** references in `request_gateway/`,
`orchestrator/`, `gateway/`, or `tools/`. The system has two read surfaces — automatic context
assembly, and the model-callable `search_memory` tool — and **both are blind to both structures.**
`search_memory` returns entities, turns and session metadata only, on both its entity-match and broad
paths (`tools/memory_search.py:157-212`). *(This verifies the absence of direct references to those
six symbols; it does not exclude some hypothetical generic consumer, and none was found.)*

Recall still runs entirely on the legacy Entity layer. The capability ADR-0098 shipped — that a wrong
fact is correctable, that a superseded original is retained rather than overwritten, that the owner's
stance toward a concept is a first-class edge — has never reached the model.

### Why nobody noticed: the criteria could not detect it

This is the part that generalises. **Not one of ADR-0098's ten acceptance criteria requires a Claim
or a Stance to be *read* into a turn.** AC-1 and AC-2 check the store by Cypher. AC-5 says
*"Check (single Cypher query)"* — a human runs the traversal by hand. AC-7, the only positive read
criterion in the set, sits on the World/Entity layer, not on Claims or Stance.

**AC-4 is the sharp case.** It requires *"a tutor/recall query for a domain prompt returns **zero**
System items."* That passes **vacuously**: recall returns zero System claims because recall returns
zero claims. A negative criterion with no positive companion is satisfied most completely by a
component that does nothing at all.

So ADR-0098's criteria set is **satisfiable end to end by a write-only implementation**. That is the
mechanism behind the ticket's framing question — *why did a correct capability sit unconsumed without
anything noticing.* It was not carelessness. It was a criteria set that was rigorously outcome-level
about writing and silent about reading. This ADR treats that as a reusable lesson, not an incident
(D7).

### The state of the two layers, measured

Measured live against the deployed graph on 2026-07-27.

**Claims — present, embedded, and not trustworthy enough to push.**

| Property | Distinct values across 91 Claims |
|---|---|
| `class` | `Personal` — **1** |
| `source_type` | `conversation` — **1** |
| `confidence` | `0.8` — **1** |
| `update_kind` | `new` (89) · `correction` (1) · `evolution` (1) — 3 |

All 91 carry embeddings (FRE-768 backfilled them), and every claim's `trace_id` resolves to a real
`:Turn` — **zero dangling provenance**. There is no `Claim` vector index; the only vector index is
`entity_embedding` on `:Entity`. So the substrate is one index creation away from being
vector-searchable, not a from-scratch embedding job. *(FRE-1012's text says there is no index and no
embedding; the index half is right, the embedding half is not. Corrected on the ticket.)*

Against ADR-0098 D1's own definition of System — *agent infra / telemetry / healthcheck /
test-scaffold, assigned on the turn's subject/intent* — a classification of all 91 finds roughly **48
that are System-subject**, all carrying `class=Personal`. The clusters: nine claims asserting facts
about "the user's knowledge graph"; three Elasticsearch cluster-health rows (`yellow status`,
`one unassigned primary shard` — the literal healthcheck example ADR-0098 names); two SLM/Cloudflare
failure rows; two `prompt_cache_test_status` rows whose own text reads *"initiated this conversation
to test prompt cache behavior"*; container/mount/pip rows; ADR-hunting rows; `gateway_uptime`;
`tool_call_counts`.

That is **≈53%** — the FRE-636 spike's ~46% entity-pollution figure reproducing on the new substrate.
And it means **AC-4 of ADR-0098 does not merely pass vacuously on its recall half; it fails on its
extraction half today**, one class over, which is AC-4's own stated failure mode.

The harm is not token waste. Those nine knowledge-graph claims assert *"about 16 nodes"*,
*"sub-20ms queries"*, *"search is keyword-based on entity names"*, *"lacks an automatic entity and
fact extraction pipeline"* — durable, current, `confidence=0.8`, and **false about Seshat** (7,233
entities; vector-plus-lexical recall; a live extraction pipeline). Injected, the agent would assert
false facts about itself carrying recall's authority.

**Stance — smaller, cleaner, and structurally one hop from the existing recall path.**

27 `HAS_STANCE` edges. `affect` populated **27/27**. Every target is a single-label `:Entity` and
every target carries an embedding — so stance targets are **already in the entity vector index and
already traversed by the existing recall path**. Two edges are superseded (`Autofiction`, `Sorbet`),
both correctly: `Sorbet`'s vague *"prefers"* was superseded by the specific *"prefers a
sorbet-leaning texture"*, keyed on `(owner, target)`.

`mastery` is null on all 27, and this is **correct output, not a gap**. The populator chain is
complete — extractor prompt (`entity_extraction.py:156`) → `_coerce_mastery` (`:598`) →
`_build_stance` (`consolidator.py:84`) → `assert_stance` Cypher (`service.py:2240`) — and the prompt
instructs the model to emit null for a pure preference. All 27 affects are preferences or intentions
(*"prefers over Java"*, *"wants to understand"*, *"wants to learn"*), for which null is the right
answer. Forcing it non-null would require fabricating skill levels the owner never stated.
`review_due` is genuinely unwired (0/27), but it is ADR-0098 **D4** lifecycle work explicitly scoped
out of the shipping ticket, not a D2 defect.

ADR-0098 **AC-3 requires a stance edge "with affect/mastery"** — read conjunctively it is unmet, read
disjunctively it is met. That ambiguity is the same defect class as AC-4's vacuity: a criterion whose
verdict depends on how it is read rather than on what the system does. This ADR therefore decides
affect-only sufficiency explicitly rather than inheriting the ambiguity (D1).

**One defect is live in the data.** `Barrage républicain` carries `affect=""` with `mastery` null —
an edge that conveys nothing. That is the third occurrence this week of the same pattern (FRE-1010's
entities rendering as empty bullets), which is why D6 makes non-emptiness a decision rather than an
implementation nicety.

### A finding that lands outside this ADR's scope but changes a neighbour's premise

Because `source_type` is constant, `confidence` is constant: provenance hard-codes
`"source_type": "conversation"` (`entity_extraction.py:588-595`), `_build_claim` sets
`confidence=KnowledgeWeight.from_source(source_type).confidence` (`consolidator.py:106-120`), and
`conversation` maps to `0.8` (`weight.py:15-24`). In `adjudicate()` (`memory/supersession.py:180-194`)
the guard `if new_confidence < candidate.confidence: return REJECT` — the line whose own comment reads
*"not naive last-write-wins"* — **is unreachable on the production producer path**, and so is the `>`
label branch. Only the `new_observed_at < candidate.observed_at` staleness check survives. *(Precisely
stated: this is a property of the current producer path, not of `adjudicate()` itself — the function
still discriminates correctly when called with differing confidences, as tests and any future
non-conversation producer would.)*

Supersession therefore degenerates to **newer-wins-with-a-staleness-guard**, which is the naive
last-write-wins model ADR-0098 D2 names and rejects. The `correction`/`evolution` labels that do
appear come from the extractor's explicit-signal branch, which the code's own docstring confirms
*"drives the label only, never the FRESH/REJECT safety decision."* **The label works; the safety
guard is dead.** FRE-1012's assertion that the write side "appears to work correctly, including the
supersession adjudication" is falsified; master has corrected the ticket.

This matters to a neighbour: **ADR-0100 demoted the hard recency gate on the explicit premise that
"ADR-0098 … now owns correctness-over-time."** On live data it does not. That premise needs a note
(D8), and the degeneracy needs its own ticket against ADR-0098 — it is not this ADR's to fix.

### What is measured and what is inferred

Stated so a reviewer can attack the inference without re-deriving the data.

- **Measured** (live graph and tree, verified independently by the adr and master sessions): every
  count, cardinality, index, population figure, and code path cited above. That `mastery` is wired end
  to end is measured from source; that all 27 nulls are *correct* is not (below).
- **Inferred** (judgment, open to challenge): the ~48/91 System-subject classification is *this
  session's reading* of ADR-0098 D1 applied by hand, not a classifier output — the exact number is
  arguable, the order of magnitude is not, and it is corroborated by the independent ~46% entity
  figure from FRE-636. That `mastery=null` is correct for all 27 is a reading of the 27 affect texts
  against the prompt's own rule; source proves the field is wired and the rule exists, not that the
  extractor never missed an implied skill statement. The behavioural/topic-scoped split of the 27
  stances (D2) is likewise a reading, which is precisely why D3 makes the cut revisable rather than
  baking it in.
- **Not independent:** the adr and master sessions agreeing is *one* reading reached twice from the
  same measurements. The data is the independent part, and was re-verified by the owner.

---

## Decision

Eight decisions. The governing principle: **a read surface's safety is set by what it costs when it
is wrong, not by how valuable it is when it is right.** ADR-0125 D2 established the asymmetry — a
miss on an injected surface is invisible, a false positive taxes every turn it fires on. Everything
below is that rule applied per surface.

### D1 — Stance is ADR-0098's first consumer, and affect-only is sufficient

The Stance layer is made readable first. It depends on **nothing the claim classifier does**: its
targets are `:Entity` nodes already in the vector index and already traversed, so surfacing stance
requires no new relevance decision and inherits none of the claim layer's classification risk.

**`affect` alone is sufficient for a first consumer.** `mastery` is correctly null for a pure
preference and is not a prerequisite; `review_due` belongs to ADR-0098 D4's spaced-repetition
lifecycle and is out of scope here. This resolves ADR-0098 AC-3's `affect/mastery` ambiguity as a
decision rather than leaving it to the reader.

### D2 — Stance has two distinct surfaces, because one mechanism cannot serve both

A single entity-gated surface **cannot produce this decision's motivating examples** — that much is
demonstrable. The stances *"prefers explicit request before creation"* (`Artifact`) and *"prefers by
default for follow-up data"* (`Plain text responses`) are standing instructions about how the agent
should behave. Entity-gated, they fire only once the user has already raised artifacts or output
format — which is after the behaviour they govern has occurred. Shipping one mechanism would reproduce
the exact pattern this ADR exists to end, one level down.

What is demonstrated is that **one entity-gated surface is insufficient**, not that an always-present
layer is the unique remedy — an action-gated policy surface is a genuine alternative and is weighed as
Option 6. Therefore, on the balance of those options:

- **Standing behavioural preference → an always-present profile layer.** Not gated on entity recall.
  Present on every turn, because its entire purpose is to govern behaviour before the topic arises.
- **Topic-scoped stance → push-as-enrichment on entity selection.** Attached to `:Entity` nodes the
  existing recall path has *already* selected. This is not a new relevance-guessing surface; it
  enriches one that has already fired and already paid for its guess — which is why it does not incur
  ADR-0125 D2's asymmetric cost.

The measured split makes this affordable: of 27 stances, roughly **four to six are standing
behavioural** (`Artifact`, `Plain text responses`, `production transactions`, `Health Issues`) and
~21 are topic-scoped. The always-present layer is small by nature, so its per-turn cost is bounded by
construction rather than by a trimmer.

### D3 — The behavioural/topic-scoped split is a read-time facet, first realised as a curated set

The substrate carries no field marking the distinction: `Artifact` and `Plain text responses` are
ordinary `:Entity` nodes. Per ADR-0125 D7 — *write-time dispatch only for isolation boundaries that
should never be crossed; read-time facets for anything retrieval-related, where the cut may be wrong
and must stay revisable* — this is retrieval, so it is a **read-time facet**. No new extractor field,
no write-time routing.

Its **first implementation is an owner-curated set**, not a classifier. Two reasons: the always-present
layer is small and high-stakes, so precision matters more than coverage; and the alternative — a
classifier deciding what is always injected — is a third bet on a mechanism measured wrong twice in
one day (~46% on entities, ~53% on claims). Curation is revisable at any time, which is the property
D7 requires.

### D4 — Claims are pull-only

Claims are reachable **only** through the model-callable `search_memory` tool. They are never injected
into assembled context.

On today's data push is unsafe at any threshold: ≈53% of the layer is System-subject, and **there is
no field to gate on** — `class`, `source_type`, and `confidence` each hold exactly one distinct value
across all 91 rows. A pull surface has symmetric cost: a wrong claim arrives as one tool result the
model can weigh in context, and a miss is equally visible. A push surface does not.

This ends the write-only state **this cycle**, without staking recall safety on the classifier and
without waiting for the write-side fix (D8) — which is the failure mode that produced the current
situation.

Claims may acquire a narrow push path later. That is a separate decision, gated on a measured
classification-precision bar, and it inherits D5 and D6 without renumbering.

### D5 — Current-only on every push surface; the supersession chain only through pull

**Pre-committed to any push surface, present or future.** A superseded fact surfacing as current is a
high-cost silent error repeated on every turn it fires on. *"You used to prefer X, now Y"* is a
deliberate teaching act and belongs behind an explicit request.

- **Push** — current items only (`valid_to IS NULL AND invalid_at IS NULL`). Superseded originals are
  never enriched into context.
- **Pull** — the supersession chain is reachable on demand, including superseded originals retained
  for history, with the supersession link and reason.

This is D4's asymmetry one level down, and it composes with it rather than adding a new rule.
Retention is unchanged: **superseded ≠ deleted** (ADR-0098 D2). This decision governs *surfacing*,
never *storage*.

### D6 — No empty item on a push surface

An item pushed into assembled context must carry non-empty content. A stance with an empty or
whitespace-only `affect` and no `mastery` conveys nothing, and rendering it produces the empty-bullet
artefact FRE-1010 fixed on the entity layer. `Barrage républicain` is that case live today.

Filtering happens **before render**, so an empty item is absent rather than rendered blank. This is a
decision and not an implementation detail because it is now the third occurrence of one failure mode
across three layers.

### D7 — Criteria-quality rule: a producer needs a criterion that fails when nothing reads it

> **An ADR that ships a producer must carry at least one acceptance criterion that fails if nothing
> reads its output.**

Scoped deliberately as a **rule about how criteria are written**, applied at authoring time — not a
standing process gate, not a review checkpoint, not a new artifact. As a gate it would become
recurring ceremony; as a sentence applied when writing criteria it costs nothing after the ADR is
written.

The evidence for it is four unconsumed capabilities found in a single week — the Claim substrate, the
Stance layer, structured output wired and unused, and the prompt manifest built per turn and
discarded — plus two criteria inside ADR-0098 alone (AC-4's vacuity, AC-3's ambiguity) that a working
and a non-working implementation satisfy identically.

The practical form: pair every negative criterion with a positive one. *"No System item is recalled"*
is satisfied perfectly by recalling nothing; *"a domain item **is** recalled and no System item is"*
is not. This ADR applies the rule to itself — AC-8 is its self-test.

### D8 — Scope boundary: what this ADR does not fix

- **The supersession degeneracy is out of scope** and carries its own ticket against ADR-0098.
  Folding a write-path fix into a read-path ADR would make this one unshippable, and it is ADR-0098's
  problem.

  **What D5 does and does not buy under it, stated precisely.** D5 keys on `valid_to`/`invalid_at`, so
  it reliably prevents a row *marked* non-current from being pushed. It does **not** make the surviving
  current row trustworthy: the degeneracy governs *which* claim receives those flags — `adjudicate()`
  chooses `SUPERSEDE` versus `REJECT`, and `assert_claim` then marks either the prior rows or the
  incoming one non-current — so with confidence comparison inert, the row the graph calls current is
  whichever arrived later, not necessarily the correct fact. **D5 filters the adjudicator's chosen
  winner; it does not adjudicate.** This is a real residual exposure on the push surface, bounded today
  because only Stance is pushed (2 supersessions, both manually verified correct) and Claims are
  pull-only, where a wrong current value arrives as a weighable tool result rather than an assertion.
  It is a further reason the write-side ticket should not linger.
- **The claim classifier is out of scope.** D4 routes around it rather than depending on it.
- **ADR-0100 gets an appended note**, because it demoted the hard recency gate on the premise that
  ADR-0098 owns correctness-over-time, and on live data that premise does not hold. A note, not a
  reversal — the demotion may still be right for other reasons, but it must not rest on a stated
  premise that is false.

---

## Alternatives Considered

### Option 1: Make Claims push-recallable now, behind a fixed classifier

**Description:** Create a `Claim` vector index, gate on `class != System`, and inject surviving
claims into assembled context alongside entities.

**Pros:**
- Delivers the ticket's literal request in one step.
- The embeddings already exist; the index is a near one-liner mirroring `ensure_vector_index`.
- Highest-value facts (a cardiology follow-up, a scheduled procedure) reach the model unprompted.

**Cons:**
- `class` holds one distinct value across all 91 rows — the gate has nothing to gate on.
- ≈53% of the layer is System-subject, including nine claims that are factually false about Seshat.
- It stakes recall safety on a classifier measured wrong twice in one day.

**Why Rejected:** It would push dimension-1 material into user-facing context, which ADR-0125 D2
forbids outright. The gate does not exist in the data, and manufacturing one from a classifier is the
third attempt at a mechanism with two measured failures.

### Option 2: Fix the write side first; defer all recall until Claims are trustworthy

**Description:** Make `source_type` vary along ADR-0098 D6's agent-derived vs user-asserted line,
repair classification, restore confidence-weighted adjudication, then revisit recall.

**Pros:**
- Honest ordering — trust the substrate before reading it.
- Repairs the supersession degeneracy, which is a real defect.
- Nothing user-facing changes while the substrate is unreliable.

**Cons:**
- Leaves both layers write-only for another cycle, which is precisely the diagnosed pattern.
- Stance needs **nothing** from the write-side fix; it is blocked only by association.
- A write-path repair with no reader would again ship without a criterion able to detect it.

**Why Rejected:** It treats one substrate as one problem. Stance is clean, structurally adjacent to
the existing recall path, and independently valuable — deferring it buys nothing. The write-side fix
proceeds in parallel under its own ticket (D8).

### Option 3: One Stance surface, entity-gated only

**Description:** Attach all stances to recalled entities uniformly; no always-present layer.

**Pros:**
- One mechanism, materially simpler.
- Cost scales with recall; no per-turn floor.
- Never fires on an irrelevant topic.

**Cons:**
- **Provably cannot produce the motivating examples.** *"Prefers explicit request before creation"*
  fires only once artifacts are already under discussion.
- Standing instructions would appear to work in testing — the entity is present when you test for it.
- It would reproduce the write-only pattern one level down: a mechanism shipped, and the class of
  case it exists for unreachable.

**Why Rejected:** The owner's catch, and decisive. A mechanism that cannot produce its own motivating
examples is not a partial solution.

### Option 4: Carry the behavioural/topic-scoped split as a write-time property on the stance edge

**Description:** Add a `scope` field to the extractor's stance contract; the model classifies each
stance as behavioural or topic-scoped at write time.

**Pros:**
- The distinction travels with the data; no read-time computation.
- No curated list to maintain.
- Consistent with how `class` and `facet` are already carried.

**Cons:**
- ADR-0125 D7 reserves write-time dispatch for isolation boundaries; this is retrieval.
- **Write-time routing is first-write-wins for location** — correcting a misclassified stance requires
  a migration or a recompute pass, and the always-present surface is the one where being wrong is
  expensive in the meantime.
- It is a third classifier bet, on the highest-stakes surface.

**Why Rejected:** The cut is a judgment likely to be revised as the stance corpus grows, and D7 exists
to keep revisable cuts revisable. A read-time facet is re-derived for free on the next turn; a stored
property is recoverable only by migration or recompute. *(Not "permanent" — that would overstate it —
but the asymmetry in cost of being wrong is the deciding factor, on top of the classifier risk.)*

### Option 6: An action-gated behavioural policy surface

**Description:** Instead of an always-present layer, retrieve behavioural stances at the moment an
action they govern is about to be taken — inject *"prefers explicit request before creation"* when the
model is about to call an artifact-creating tool.

**Pros:**
- Zero per-turn floor; cost is paid only when the governed action actually occurs.
- Precisely targeted — the stance arrives exactly where it applies, with no irrelevant firing.
- Scales cleanly as the behavioural corpus grows, unlike a curated always-present set.

**Cons:**
- It requires knowing which action is imminent, and that is decided by the model *mid-turn*, after
  context assembly has already run — so the surface would have to live in the tool-dispatch path, not
  in recall.
- It governs only preferences expressible as tool-call preconditions. *"Prefers plain text by default
  for follow-up data"* constrains prose the model writes directly, where there is no action to gate on.
- It is a materially larger build touching the orchestrator loop, for a subset of the cases.

**Why Rejected — for now, and explicitly not on principle.** It is the better long-run shape for the
subset it covers, and it is the natural home for a preference that maps to a specific tool call. It is
rejected here because it cannot cover response-style preferences at all, and because it moves the work
from the recall path into the orchestrator loop, which is a much larger change than the one this ADR
is sized for. The always-present layer is bounded (AC-7) and revisable (D3); if the curated set ever
approaches its ceiling, action-gating is the first thing to reconsider — that is the stated trigger.

### Option 5: Curate nothing — inject all 27 stances on every turn

**Description:** Skip the split; the corpus is small enough to inject wholesale.

**Pros:**
- No facet, no curation, no classifier — trivially correct about coverage.
- Guarantees the behavioural stances are always present.
- 27 short strings is a small per-turn cost today.

**Cons:**
- Scales with the wrong quantity: the stance corpus grows with every session, the always-present
  budget does not.
- Injects *"prefers Comté as a cheese to keep eating"* into a turn about Kafka — the exact
  guess-and-tax failure ADR-0125 D2 names.
- It would satisfy a naive always-present criterion while making topic-scoping meaningless, which is
  why AC-2 and AC-3 must be checked as a pair.

**Why Rejected:** Correct today, structurally wrong, and it removes the distinction D2 exists to
make. Rejected on scaling, not on present cost.

---

## Consequences

### Positive Consequences

- ADR-0098 acquires its first reader after shipping write-only; the substrate stops being a store
  nothing consumes.
- Standing behavioural preferences the agent currently violates — *explicit request before artifact
  creation*, *plain text by default for follow-up data* — become present before the behaviour they
  govern rather than after.
- Claims become reachable without staking recall safety on a classifier with two measured failures,
  and without waiting on the write-side repair.
- The push/pull split turns a taste question into a consequence of an invariant already accepted
  (ADR-0125 D2), so future surfaces inherit the reasoning rather than re-litigating it.
- D5 is pre-committed, so a later narrow Claims push path inherits current-only without renumbering.
- D7 converts a four-times-repeated failure into one sentence applied at authoring time.
- ADR-0100's false premise is recorded rather than left to be discovered a third time.

### Negative Consequences

- The always-present layer is a genuine per-turn token cost on **every** turn — the first surface in
  the system with that property. AC-7 caps it at 12 curated stances and 1,500 bytes, so growth beyond
  that requires amending this ADR rather than a quiet curation decision.
- A curated set is manual work that will drift as the stance corpus grows; D3 accepts this in exchange
  for revisability and precision, and the trigger for revisiting is the curated set becoming large
  enough that maintaining it is the dominant cost.
- Pull-only Claims mean the highest-value facts (a scheduled procedure, a cardiology follow-up) still
  do **not** reach the model unprompted. This is a real capability deferred, deliberately, and it is
  the main thing the owner gives up under D4.
- Two stance surfaces are more mechanism than one, and the split rests on a read-time judgment that
  may be wrong at the margin.

### Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| The curated behavioural set drifts stale as stances accumulate | High | Medium | D3 makes it a read-time facet, revisable without migration; the set is small by construction and AC-2 fails loudly if a curated stance stops arriving |
| The always-present layer becomes a per-turn cost nobody notices growing | Medium | Medium | AC-7 fixes the ceiling in this ADR (≤12 stances, ≤1,500 bytes) and fails on breach; raising it requires an amendment, and Option 6 (action-gating) is the named alternative once the ceiling is approached |
| The current row D5 pushes is the wrong fact, because adjudication is degenerate | Medium | Medium | D5 filters the adjudicator's winner but does not adjudicate (D8); exposure is bounded to Stance today (2 supersessions, verified) since Claims are pull-only, and the ADR-0098 write-side ticket is the real fix |
| Pull-only Claims means the tool is never called, so Claims remain effectively unread | Medium | High | AC-4(b) proves reachability, not usage; usage is a tool-description and routing question the implementation ticket carries — and AC-8 fails if the pull path is removed |
| Topic-scoped enrichment injects an irrelevant stance | Medium | Low | It rides on an entity the recall path already selected; a wrong stance implies a wrong entity, which is the existing recall path's failure, not a new one |
| D5 relies on `valid_to`/`invalid_at` while supersession adjudication is degenerate | Low | Medium | The degeneracy affects *which* claim wins, never whether the loser is marked superseded; D5's filter is unaffected. Tracked separately (D8) |
| The behavioural/topic split is simply the wrong cut | Medium | Medium | Read-time facet (D3) — re-derivable at any time with no write-path migration and no stored state to unwind |

---

## Implementation Notes

- **Two independent chains.** Stance-read (D1/D2/D3/D5/D6) and Claims-pull (D4/D5). Neither blocks the
  other; Stance leads because it is the shortest path from *"ADR-0098 has no consumer"* to *"ADR-0098
  has a consumer."*
- **A `Claim` vector index is likely unnecessary at current scale.** 91 nodes with embeddings already
  present; the supersession path already scans the user's current claims and compares in Python. The
  implementation ticket decides index-versus-scan on measurement, not by default. `ensure_vector_index`
  (`memory/service.py:2463`) is the pattern if an index is wanted.
- **Claim scoping does not use the existing visibility filter.** Claims carry no `visibility`
  property, so `_build_visibility_filter` does not apply. Scoping runs through the owning
  `:Person.user_id` on the `HAS_FACT` edge (ADR-0107: a claim anchors to the acting authenticated
  user). The implementation ticket must not assume the entity-path filter transfers.
- **Files most likely affected:** `request_gateway/context.py` (both push surfaces),
  `tools/memory_search.py` (pull), `memory/service.py` (claim + stance read queries),
  `memory/protocol.py` (signatures).
- **Doc drift for master, on acceptance:** append the ADR-0100 note (D8); file the ADR-0098
  supersession-degeneracy ticket; ADR-0098's own status line stays Accepted — this ADR implements its
  read half and does not supersede it.
- **`source_code_location` is a live data-quality signal, not an action item here.** One claim stores
  a host filesystem path as a durable user fact. Under D4 it is pull-only and therefore not injected,
  but it is evidence for the System-gate ticket.

---

## Verification / Acceptance Criteria

Each is outcome-level and discriminating; a broken or half-built implementation must fail it. Per D7,
every criterion that could be satisfied by a component doing nothing is paired with a positive
companion, and **AC-8 is the suite's self-test.**

**Observation point, fixed for the whole suite.** Every "reaches the model" assertion below is made
against the **actual serialized provider request** — the bytes sent to the model — and **not** against
`prompt_manifest` or any other component manifest. ADR-0125 D3 records `prompt_manifest` as a *likely
but unconfirmed* satisfier for "the assembled context actually sent to the model," and until that
fidelity is independently proven a half-wired implementation could populate a manifest while omitting
the content from the request. A manifest may be substituted **only** once an invariant or test proving
manifest-to-request fidelity exists and is cited by identifier. *(This ADR checks affect strings, which
appear in rendered text — unlike ADR-0125 AC-3, which needs the manifest precisely because recall
identifiers need not appear in rendered content. The two are not in tension.)*

- **AC-1 — A topic-scoped stance reaches the model when its target entity is recalled, and does not
  when it is not.** · **Check:** run a turn whose message is about a concept carrying a stance (e.g.
  `Python`, affect *"prefers over Java"*) and assert the affect string is present in the **serialized
  provider request**. Run a second turn on an unrelated topic with no stance-bearing entity in its
  recall set and assert the same string is absent. · *Fails if* the affect is absent in the first
  case, or present in the second. *(Vacuity for this criterion is covered systematically by AC-8's
  mutation matrix rather than restated here.)*

- **AC-2 — A standing behavioural stance is present on a turn that mentions none of its targets.**
  · **Check:** issue a probe message with no lexical or semantic overlap with `Artifact` or
  `Plain text responses`, and confirm the recall set for that turn contains **neither** entity. Assert
  the curated behavioural affects are nonetheless present in the serialized provider request.
  · *Fails if* a behavioural stance appears only when its target entity was independently recalled —
  that is Option 3 shipped under D2's name, and it is the specific failure this criterion exists to
  catch.

- **AC-3 — A topic-scoped stance does *not* become always-present.** · **Check:** on the same
  entity-free probe turn used by AC-2, assert a topic-scoped affect (e.g. `Comté` — *"prefers it as a
  cheese to keep eating"*) is **absent**. · *Fails if* it is present — which would mean the corpus is
  injected wholesale (Option 5) and the D2 split is decorative. **AC-2 and AC-3 must be evaluated as a
  pair on the same turn**; either alone is satisfiable by a degenerate implementation, and only
  together do they force the split to be real.

- **AC-4 — Claims are reachable by pull and unreachable by push.** · **Check:** two halves, both
  required. **(a)** Seed a claim engineered to be maximally recallable for a probe message (high
  embedding similarity, current, `class=Personal`), assemble context for that probe with every recall
  toggle at its most permissive setting, and assert the claim's content is **absent** from the final
  serialized model input. **(b)** Call `search_memory` with a query matching that same claim and
  assert the claim's content **is** returned, distinguishable from entity and turn rows. · *Fails if*
  the claim appears in assembled context under any configuration, **or if (b) returns only entity/turn
  rows.** Half (b) is the anti-vacuity partner: (a) alone is satisfied perfectly by implementing
  nothing, which is exactly how ADR-0098's AC-4 passed.

- **AC-5 — A superseded item never surfaces on push, and its chain is retrievable on pull.**
  · **Check:** use the live superseded pair — `Sorbet`, where *"prefers"* was superseded by *"prefers
  a sorbet-leaning texture"*. On a sorbet-topic turn, assert the **current** affect is present in the
  final serialized model input and the **superseded** string is absent. Then request the chain through
  the pull path and assert it returns **both** entries plus the supersession link. · *Fails if* the
  superseded text reaches assembled context, if the current one does not, or if the pull path cannot
  return the superseded original — retention without retrievability is not the audit trail ADR-0098 D2
  decided on.

- **AC-6 — An empty item is filtered before render, and a non-empty one still renders.** · **Check:**
  run a turn recalling `Barrage républicain` (`affect=""`, `mastery` null) and assert the rendered
  stance section contains **no** entry for it — no empty bullet, no bare label. Repeat with a
  synthetic whitespace-only affect. Then run a turn recalling a populated stance and assert the
  section **is** rendered with its content. · *Fails if* an empty or whitespace-only stance renders as
  a bullet, **or if the section is never rendered at all** — suppressing everything passes the first
  half and fails the decision.

- **AC-7 — The always-present layer's per-turn cost is bounded by a limit fixed *here*, not chosen
  after observing output.** The bound is decided by this ADR so it cannot be back-fitted: **the
  curated behavioural set holds at most 12 stances, and its contribution to the serialized provider
  request is at most 1,500 bytes.** (Sized from the measured corpus — 4–6 behavioural stances today at
  ~120 bytes each rendered — with headroom, deliberately far below any trimmer threshold.)
  · **Check:** measure the byte length the behavioural layer contributes to the serialized provider
  request; assert it is **non-zero** and **≤ 1,500**. Assert the curated set's cardinality is **≤ 12**.
  Then add one stance to the curated set and assert the measured contribution rises. · *Fails if* the
  contribution is zero (the layer is not injected — AC-2's failure by another route), exceeds 1,500
  bytes, if the set exceeds 12 entries, if the measurement is taken anywhere other than the serialized
  request, or if it does not respond to the curated set changing — which would mean the measurement is
  reading something other than the layer. **Raising either limit requires amending this ADR**, which is
  the point: unbounded curation growth is the acknowledged risk, and a bound the implementation may set
  for itself does not constrain it.

- **AC-8 — SEAM: each consumer removal turns *named* assertions red, from a green baseline.** *This
  ADR's self-application of D7, and the criterion ADR-0098 lacked.* A generic "at least one criterion
  fails" is not sufficient — an unrelated pre-existing failure would satisfy it — so the expected
  failures are named per mutation. · **Check:** first establish a **green baseline**: AC-1 through
  AC-7 all pass unmutated. An unrelated failure invalidates the run and must be fixed before
  proceeding. Then apply each mutation independently, restoring between runs:

  | Mutation | Assertions that MUST fail |
  |---|---|
  | Remove topic-scoped stance enrichment from context assembly | AC-1 positive half · AC-5 current-stance-present half |
  | Remove behavioural-profile injection | AC-2 · AC-7 non-zero-contribution half |
  | Remove the Claims path from `search_memory` | AC-4(b) · AC-5 supersession-chain-on-pull half |

  · *Fails if* any named assertion still passes under its mutation, if the baseline is not green
  before mutating, or if a failure under mutation is traceable to a cause other than the removed
  consumer. **Green with no consumer present is precisely ADR-0098's shipped condition** — the
  condition this ADR exists to make impossible to reach undetected.

**Seam owner (assembled intent):** **AC-8.** It can only be run once both surfaces exist, and it is
the assembled proof that the write-only state has actually ended — not that fields are populated, not
that children merged. AC-1 through AC-7 are asserted independently by their own tickets; AC-8 is the
only criterion no single child can satisfy, and it is master's to hold. **This ADR does not close
because its last child merged; it closes when removing each reader turns its named assertions red.**

---

## References

- ADR-0098 — Memory Substrate & Lifecycle Architecture (Accepted 2026-06-27; §D1 and its query-time System recall filter superseded by ADR-0115) — the substrate whose read half this ADR supplies
- ADR-0100 — Memory Recall: Relevance-Bounded Candidate Generation (Accepted 2026-06-28) — its recency demotion rests on a premise falsified here; note appended per D8
- ADR-0125 — The Two Quality Dimensions and the Turn Evidence Contract (Accepted 2026-07-27) — D2's asymmetry rule and D7's write-time-vs-read-time facet rule govern this decision
- ADR-0115 — Knowledge Class Axis: Emission, Persistence, Dispatch (Implemented 2026-07-12) — supersedes ADR-0098 §D1's class-as-stored-property
- ADR-0107 — User Identity Resolution and Log Propagation (Accepted 2026-07-02) — a Claim anchors to the acting authenticated user, which is how D4's pull path scopes
- ADR-0097 — Ingested-Knowledge Taxonomy (Proposed — hypothesis, held loosely)
- ADR-0052 — Seshat Owner Identity Primitive (Accepted, amended 2026-05-09) — the `is_owner` anchor the Stance edge originates from
- ADR-0087 — Memory Recall Quality Measurement Program (Accepted 2026-06-27; reconciled with ADR-0098) — the pillar this lands under
- FRE-1012 — this ADR's backing ticket (text corrected 2026-07-27: claim embeddings exist; the write side is not sound)
- FRE-1005 — ADR-0125 T3, the usage edge; blocked on this decision
- FRE-1006 — ADR-0125 T5 seam; inherits the same premise
- FRE-1010 — Task-assist recall renders entities as empty bullets — the empty-render pattern D6 generalises
- FRE-636 — taxonomy-validation spike (`docs/research/2026-06-27-fre-636-taxonomy-validation.md`) — the ~46% entity-pollution figure this ADR's ~53% claim figure corroborates
- FRE-768 — Claim embedding backfill — why all 91 claims are already vector-ready

---

## Status Updates

### 2026-07-27 - Proposed
**Changed By:** adr session (Opus), owner-directed
**Reason:** Authored from the FRE-1012 exploration. The ticket's finding was verified, extended to the
Stance layer it had missed, and corrected on two points (claim embeddings exist; the write side is not
sound). The owner ruled five open calls: pull-only Claims, Stance leads, the supersession degeneracy to
its own ADR-0098 ticket, current-only on push pre-committed to future surfaces, and the criteria-quality
rule scoped as an authoring rule rather than a process gate. The behavioural-versus-topic-scoped stance
split (D2) was the owner's catch and is the decision without which this ADR would ship a mechanism
unable to produce its own motivating examples.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
