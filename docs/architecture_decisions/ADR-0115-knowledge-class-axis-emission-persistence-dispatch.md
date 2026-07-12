# ADR-0115: The Knowledge Class Axis — Two-Axis Emission, Entity Persistence, and Output-Kind Dispatch

**Status:** Implemented — 2026-07-12 (assembled seam proven live on a sanctioned turn; existing-corpus cleanup FRE-865 backfill + FRE-868 eviction outstanding for the ADR-0114 de-confound)
**Date:** 2026-07-11
**Deciders:** Owner (architect) · master (integration gate) · adr session (Opus)
**Tags:** memory, knowledge-graph, extraction, taxonomy, dispatch

**Supersedes:** ADR-0106 in full (its `output_kind` dispatch is subsumed here); ADR-0098 §D1 (class-as-stored-property) and its query-time System recall filter.
**Refines:** ADR-0097 — removes `System` from the *class* vocabulary; System becomes an `output_kind` outcome, not a subject class.
**Preserves (explicitly NOT superseded):** ADR-0098 §D2 (Claims / bitemporal), §D4 (class-aware lifecycle), §D7 (world correlation) remain Accepted.
**Enables / de-confounds:** the *data* for a future `class` predicate in ADR-0104's structural arm — adding the predicate is **unowned follow-up** (see D6) · ADR-0114's associative-memory study, de-confounded **by construction** (System absent from Core, not filtered).
**Reconciles with:** ADR-0105 — `sysgraph` is the home for `finding` items.

---

## Context

**What is the issue we're addressing?**

The *knowledge-class axis* — labelling every extracted item by subject/ownership — was meant to
answer "whose knowledge is this, and does it belong in the user's memory?" Three ADRs touched it and
none finished it, so the axis today is smeared across four documents and dark in the substrate:

- **ADR-0097** (Proposed) births the vocabulary `{Personal, World, Stance}` as pure taxonomy and
  punts all storage to 0098.
- **ADR-0098** (Accepted) adds a fourth value `System`, makes `class` a stored property on every
  item, and gates `System` out of recall with a **query-time exclusion filter**. Its build wave was
  FRE-637–642; the Entity-persistence write was **FRE-639 — canceled**.
- **ADR-0106** (Accepted, merged) then *decomposes* `System` off the class axis into a **separate
  routing axis** `output_kind ∈ {knowledge, ephemeral, finding}`, and replaces the recall filter with
  **write-time dispatch** (isolation by absence-of-write). `output_kind` was never built.

**Verified ground truth (2026-07-11, live substrate):**

- All ~7,992 `:Entity` nodes carry `class = None` (live Neo4j query, 2026-07-11; MASTER_PLAN records
  0/7992 classified). Only the 30 `Claim` nodes carry a class (all `Personal`).
- Extraction **does** compute a class — the extractor validates every entity to
  `{World, Personal, System}`, fail-open to `World`: vocabulary at `entity_extraction.py:410`,
  defaulting in `_normalize_entity_class` (`:521`) applied per item in `_finalize_extraction` (`:570`)
  (FRE-637). But the Entity write **drops it** at two points: `consolidator.py:682-688` builds
  `Entity(...)` and never reads `entity_data["class"]`; the `Entity` model (`models.py:31`) has no
  class field; and `create_entity`/the MERGE (`service.py:1236-1447`) has no class param or `SET`.
  Claims persist it (`service.py:1861/1875`) and Stances persist it on the `HAS_STANCE` edge
  (`service.py:1707/1709`) — which is exactly why only those substrates carry a class.
- `output_kind` has **0 hits in `src/`** — the ADR-0106 dispatch is entirely a design-doc concept.
- ADR-0104's structural recall arm (`service.py:3038`) ranks by `entity_type`/recency/hops — **never
  by class** — is flag-dark (`structural_arm_enabled=False`), and is not wired into either fusion path.

**Consequences of the gap:** ADR-0104's class/structural recall arm is coded-but-dark (there is no
class on entities to rank on); ADR-0114's associative-memory study is **confounded** — it cannot
filter the System-class noise it cannot see.

**Root cause of the smear:** the extractor's per-entity emission is a *single conflated field*
`class ∈ {World, Personal, System}`. It jams the **routing** decision ("this is System — don't keep
it") into the same slot as the **subject** decision ("this is World/Personal knowledge"). One field,
two questions. That conflation is why the axis fractured across three ADRs, none of which owns the
end-to-end persistence seam.

**What needs to be decided:** consolidate the axis into one coherent, buildable contract that owns the
seam `extractor emission → Entity write → dispatch → recall read-side` end-to-end, and settle whether
`System` is a stored class value or a routing outcome.

---

## Decision

Adopt a **two-axis emission contract** and own the persistence + dispatch seam end-to-end in this one
ADR. The class axis stops being three overlapping half-decisions and becomes one contract with a
single owner.

### D1 — Two orthogonal axes, emitted per item by the extractor

- **`output_kind ∈ {knowledge, ephemeral, finding}`** — the *nature / routing* decision: is this
  durable user knowledge, transient noise, or a system self-observation?
- **`class ∈ {Personal, World, Stance}`** — the *subject / ownership* decision, meaningful **only**
  for `output_kind = knowledge` items. **On the Entity node the class is `{World, Personal}` only** —
  a Stance is not an entity; it is the owner↔World `HAS_STANCE` edge (ADR-0098 §D2), emitted as a
  separate stance item carrying `class = Stance` on the edge. So `{Personal, World, Stance}` is the
  *overall* vocabulary; the *Entity* enum is `{World, Personal}`.
- **`System` ceases to be a class value.** "System-ness" is expressed as
  `output_kind ∈ {ephemeral, finding}`. Nothing that reaches Core carries `class = System`. Harness-
  as-studied-*subject* durable content is `class = World` with an owner Stance edge (per ADR-0106),
  not a System label.

### D2 — Persistence seam (the gap this ADR closes)

For `output_kind = knowledge` items, the entity `class ∈ {World, Personal}` is **written onto the
Entity node**, bringing Entities to parity with the Claim/Stance substrate that already persists it
(Stance itself lives on the `HAS_STANCE` edge, not the Entity — the FRE-863 emission contract restricts
the Entity enum to `{World, Personal}`):

- add a `class` field to the `Entity` model (`models.py`),
- carry `entity_data["class"]` through the consolidator's `Entity(...)` construction
  (`consolidator.py:682`),
- add the param + `SET e.class` to `create_entity`/the MERGE (`service.py:1236`), **indexed** so
  recall can predicate on it.

### D3 — Dispatch: isolation by absence-of-write (Option B)

`output_kind` routes each item at emission time:

- `knowledge` → **Core** (Neo4j user KG), carrying its P/W/S `class`.
- `ephemeral` → **observed in ES only**, never written to Core.
- `finding` → **`sysgraph`** (ADR-0105), never the user KG.

System-natured material never enters Core **by construction — not by a read-time filter.** This
supersedes ADR-0098's query-time System exclusion and directly de-confounds ADR-0114: the noise is
*absent*, not *hidden*.

### D4 — Fail-open default (owner's call, in session)

When the classifier cannot confidently classify an item, it defaults to
`output_kind = knowledge, class = World` — preserving today's FRE-637 fail-open posture (never
silently lose a candidate fact). The raw turn is captured in ES regardless, so a misclassification is
recoverable and measurable. **Accepted cost:** uncertain items may admit some System-ish noise into
Core; we measure the leak rate before considering any tightening (observable-first, not clamp-first).

### D5 — Supersession is decision-level, not doc-level

This ADR supersedes only the class-axis *decisions*, leaving unrelated ones authoritative:

- **ADR-0106** — superseded in full (it is purely this boundary/dispatch).
- **ADR-0098 §D1** (class-as-stored-property) and its **query-time System filter** — superseded.
- **ADR-0098 §D2/§D4/§D7** (Claims/bitemporal, class-aware lifecycle, world correlation) —
  **preserved, still Accepted.** Wholesale-superseding 0098 would reopen settled decisions.
- **ADR-0097** — refined: the class vocabulary is `{Personal, World, Stance}` only, of which the
  **Entity** node carries `{World, Personal}` (Stance persists on the `HAS_STANCE` edge).

### D6 — Read-side invariant, and the scope boundary on ranking

Because System never reaches Core, recall over Core needs **no** System-exclusion predicate. This ADR
guarantees `class` is queryable on the Entity node and that System is absent — that is its read-side
deliverable.

It **does not build class-aware ranking**, and — importantly — **no existing ADR owns it yet.**
ADR-0104's structural arm today ranks by `entity_type`/recency/hops (not class), and ADR-0114
benchmarks recall without defining class-ranking ownership. So consuming the now-persisted `class` in
scoring is **genuinely unowned future work**: this ADR *enables* it (the data exists and is indexed)
and files a **follow-up ticket** for the recall project to decide *whether and how* to add a `class`
predicate to the ADR-0104 arm — it does **not** pretend an existing ADR already owns it. Bundling
ranking into this ADR would re-conflate *storing the subject class* with *scoring by it* — the exact
conflation this ADR exists to undo.

---

## Alternatives Considered

### Option 1: Class-value + query-time recall filter (ADR-0098's original)
**Description:** Persist `class ∈ {Personal, World, Stance, System}` on the Entity and exclude
`System` at read time.
**Pros:**
- Smallest build — one recall predicate, no dispatch consumer.

**Cons:**
- System noise physically enters the graph; it is only *masked* at read.
- Every current and future consumer must remember to apply the filter — a standing footgun.
- Contradicts the already-Accepted ADR-0106.
- Leaves ADR-0114 confounded: the noise is present and must be filtered by every study.

**Why Rejected:** masks the problem instead of removing it. Owner chose isolation-by-construction (B).

### Option 2: Surgical gap-filler ADR (persist class on Entity; leave three docs authoritative)
**Description:** A narrow ADR that only wires the Entity write and points to 0097/0098/0106 as still
the sources of truth.
**Pros/Cons:** Smaller doc, touches least — but **perpetuates the documentation smear** the owner
flagged: the axis's current state stays spread across four docs, reconstructable only by reading all
of them, with no single owner of the seam.
**Why Rejected:** owner explicitly chose one consolidating ADR.

### Option 3: Fail-closed default (uncertain → ephemeral, parked in ES)
**Description:** When unsure, route the item `ephemeral` — observed in ES, never written to Core.
**Pros/Cons:** Keeps Core cleaner and reduces System leakage — but risks **silently dropping genuine
user knowledge** on a bad classification, the knowledge-loss failure mode FRE-637 deliberately avoids.
**Why Rejected:** owner chose fail-open; transient noise is measurable and recoverable from ES,
whereas dropped knowledge is not.

### Option 4: Wholesale-supersede ADR-0098
**Description:** Replace 0098 entirely with this ADR.
**Pros/Cons:** One doc instead of two — but 0098 also owns Claims/bitemporal (D2), class-aware
lifecycle (D4), and world correlation (D7), none of which are the class axis; superseding it wholesale
reopens settled decisions.
**Why Rejected:** supersede at the decision level (D1 + the System filter) only.

---

## Consequences

### Positive Consequences
- **One source of truth** for the class axis — the current design is one doc, not four.
- The **persistence gap closes**: Entities reach parity with Claims/Stances; `class` becomes queryable
  and rankable.
- **ADR-0104 gains the data it lacked** — there is finally a `class` on entities a future predicate
  could rank on (adding that predicate is unowned follow-up, D6).
- **ADR-0114 is de-confounded by construction** — System noise never enters Core.
- The two-axis split **removes the subject/routing conflation** that caused the smear in the first
  place.

### Negative Consequences
- Two consumers (persistence + dispatch) must both be built, and **dispatch is 0% implemented today** —
  the larger lift.
- The extractor **emission contract changes** (one 3-value field → two axes) — a prompt + schema +
  normalization change carrying re-classification risk on the boundary.
- Fail-open **admits some System-ish noise** into Core on uncertain items (accepted, measured).

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Emission-contract change misclassifies at the boundary | Medium | Raw item kept in ES; boundary fixtures in the AC set; fail-open preserves knowledge; measure leak/miss rate before tightening |
| Dispatch build slips — persistence live but System still entering Core | Medium | Sequence persistence first (immediate value, unblocks 0104), dispatch second (de-confounds 0114); each is independently shippable and verifiable |
| Superseded ADRs leave dangling/stale cross-references | Low | Supersession map + Status-line updates on 0097/0098/0106; grep for stale System-as-class references at the gate |
| Existing ~7,992 `class=None` entities stay unclassified | Medium | This ADR governs **new** writes; a **backfill** (re-run classification over the existing corpus) is a separate, named ticket — flagged, not silent |

---

## Implementation Notes

**Files affected:**
- `second_brain/entity_extraction.py` — emit two axes (`output_kind` + P/W/S `class`); drop
  `System`-as-class from `_VALID_ENTITY_CLASSES`; keep fail-open to `World`.
- `second_brain/consolidator.py:682` — carry `class` into `Entity(...)`; route by `output_kind`.
- `memory/models.py:31` — add `Entity.class` field.
- `memory/service.py:1236` (`create_entity`) — class param + `SET e.class`; index on `class`.
- `events/` + `consolidator.py` — the `output_kind` dispatch consumer (per ADR-0106).
- `sysgraph/` — the `finding` home; `settings.py` — any gating flag.

**Sequence (dependency, not just preference):**
1. **Emission** — extractor emits the two-axis contract (`output_kind` + P/W/S `class`); System stops
   being a class value. **Head** — persisting or dispatching on today's conflated 3-value emission
   would write `System` as a stored class (interim bad data); correcting emission first prevents that.
2. **Persistence** — Entity write carries P/W/S `class`. Depends on (1).
3. **Dispatch** — `output_kind` consumer routes to Core / ES / sysgraph, de-confounding 0114. Depends
   on (1); parallel to (2). *The assembled seam (AC-5) requires both (2) and (3).*
4. **Recall follow-up** — file a ticket for the recall project to decide a `class` predicate in the
   ADR-0104 arm (unowned today; out of this ADR's scope).
5. **Backfill** — re-run classification over the existing ~7,992 `class=None` entities (separate
   ticket, gated on 1 + 2).

**Backfill** of the existing corpus is a **separate ticket**, not part of this seam.

**Testing strategy:** boundary fixtures (Personal / World / Stance / System-natured items) asserting
both the right axis *values* and the right *home*.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

- **AC-1 — class persists with the *right* value.** A Personal fixture ("Dr. Chen is my
  cardiologist") produces a `:Entity` with `class=Personal`; an impersonal-know-how fixture produces
  `class=World`. **Check:** run extraction on the fixtures, `MATCH (e:Entity {name:$n}) RETURN
  e.class`. *Fails if* `class` is `None` (the drop is not fixed) **or** the Personal fixture yields
  `class=World` (value wrong, not merely present).
- **AC-2 — System never lands in Core, by absence-of-write.** Feed a System-natured fixture (a
  healthcheck/telemetry/test-scaffold snippet). **Check:** zero new `:Entity` nodes attributable to it
  **and** the raw item present in ES. *Fails if* any `:Entity` is written from it (dispatch degenerated
  into a filter) **or** the item is absent from ES (lost, not observed).
- **AC-3 — `output_kind` routes to exactly one home.** `knowledge`→Core only; `finding`→`sysgraph`
  only; `ephemeral`→ES only (no Core, no sysgraph). **Check:** per fixture, assert presence in the
  intended store and absence in the other two. *Fails if* any item multi-homes or lands in the wrong
  store.
- **AC-4 — fail-open preserves the uncertain item.** An item the classifier returns unclassifiable-for
  is written to Core as `class=World`, not dropped. **Check:** inject a fixture that forces the
  normalization default; assert a `:Entity` exists with `class=World`. *Fails if* the item is absent
  from Core (fail-closed drift).
- **AC-5 — recall needs no System filter (the de-confound), proven over a mixed corpus.** Build Core
  through the new path from a mixed fixture set (Personal + World + Stance knowledge items *and*
  System-natured items). Assert **both**: (a) `MATCH (e:Entity) WHERE e.class IS NULL OR NOT e.class IN
  ['Personal','World','Stance'] RETURN count(e)` = 0 — every entity carries a real subject class, none
  unclassified, none `System`; **and** (b) the known System-natured fixtures produced **zero**
  `:Entity` nodes. A recall query may then trust `class` with no System guard. *Fails if* any entity is
  `class IS NULL` (today's broken state — persistence not built), **or** carries a non-P/W/S value,
  **or** a System fixture produced an entity (System leaked in as `None`/`World`). It is discriminating
  precisely because absence-of-a-`System`-value **alone is not enough** — the null-and-leak legs catch
  a half-built implementation that AC-5's earlier form would have passed.
- **AC-6 — the consolidation is clean.** ADR-0106 `Status = Superseded by ADR-0115`; ADR-0098 §D1 +
  the System filter marked superseded **while §D2/§D4/§D7 remain Accepted**; ADR-0097's class
  vocabulary lists only `Personal/World/Stance`. **Check:** read the three Status lines / decision
  sections. *Fails if* any of the three still presents `System` as a stored class, or if 0098's
  §D2/§D4/§D7 were superseded by accident.

**Seam owner (assembled-ADR intent):** the assembled claim — *"the class axis is real,
correctly-scoped, and System-free in Core, end-to-end"* — holds only once persistence (AC-1/AC-4),
dispatch (AC-2/AC-3/AC-5), and consolidation (AC-6) **all** land. **Master** asserts this at the
integration gate; the ADR does **not** close when its last child ticket merges. The class-aware
*ranking* arm is explicitly outside this seam — it is **unowned follow-up work** (D6), tracked by a
separate ticket, and is **not** part of this ADR's Done.

---

## References

- ADR-0097 — Ingested-Knowledge Taxonomy (class vocabulary; refined here to P/W/S)
- ADR-0098 — Memory Substrate & Lifecycle (§D1 + System filter superseded; §D2/§D4/§D7 preserved)
- ADR-0106 — System/User Knowledge Boundary (`output_kind` dispatch; superseded/subsumed here)
- ADR-0104 — structural recall arm (downstream consumer; unblocked by class persistence)
- ADR-0114 — associative-memory study (de-confounded by construction)
- ADR-0105 — self-improvement / `sysgraph` (the `finding` home)
- ADR-0100 — relevance-bounded recall (fusion context for the recall handoff)
- `src/personal_agent/second_brain/entity_extraction.py:410,521,570` — class vocabulary / fail-open normalize / per-item finalize (FRE-637)
- `src/personal_agent/second_brain/consolidator.py:682` — Entity construction drop point
- `src/personal_agent/memory/models.py:31,141` — Entity model / `Claim.knowledge_class`
- `src/personal_agent/memory/service.py:1236,1707,1861` — `create_entity` (no class) vs `HAS_STANCE` edge / Claim write (both persist class)
- FRE-637 — extraction emits class (shipped) · FRE-639 — Entity persistence write (canceled)

---

## Status Updates

### 2026-07-11 - Proposed
**Changed By:** adr session (Opus), owner-driven
**Reason:** Consolidates the knowledge-class axis smeared across ADR-0097/0098/0106 into one two-axis
emission contract (subject `class` P/W/S + routing `output_kind`). Owner selected Option B (dispatch,
isolation-by-absence-of-write) and the fail-open default in session. Emission gates persistence and
dispatch (correcting the axis before writing avoids interim `System`-as-class data); class-aware
ranking left as **unowned follow-up** (a ticket for the recall project), not handed to an existing ADR.

### 2026-07-11 - Accepted
**Changed By:** master (integration gate), on owner acceptance
**Reason:** Owner accepted the consolidated two-axis design after 2 codex rounds (AC-5 rewritten to fail a half-built impl; class-ranking ownership corrected to an unowned follow-up). Supersession into force: ADR-0106 → Superseded; ADR-0098 §D1 + its query-time System recall filter → superseded (§D2/§D4/§D7 preserved); ADR-0097 class vocabulary refined to Personal/World/Stance (System moves to the `output_kind` axis). Implementation chain filed Needs-Approval (Entity class persistence · `output_kind` write-time dispatch, folding FRE-728 · corpus backfill · structural-arm wiring · class-aware ranking recall follow-up). ADR reaches *Implemented* only when the assembled seam — AC-1/AC-4 persistence + AC-2/AC-3/AC-5 dispatch + AC-6 consolidation — is proven live (master-held).

### 2026-07-12 - Implemented
**Changed By:** master (integration gate), assembled-seam live proof
**Reason:** Seam proven live on a sanctioned owner turn (trace `2564b7c5`) after the batched gateway deploy (SHA `c51a7486`; migration 0019 applied as admin role). **AC-1:** 5 new knowledge entities persisted `class=World` (Four-Stroke Engine, Otto Cycle, Crankshaft, Spark Plug, Flywheel) — pre-deploy the corpus was 0/7992 classed. **AC-2/AC-3:** the turn's System-natured items produced **4 `sysgraph.stat` `dispatch_finding_observed` rows** (Yellow Status, Unassigned Primary Shard, Single-Node Cluster, Elasticsearch — each carrying trace_id/session_id) and were **not written to Core** (`entities_dispatched_finding=4`, `entities_created=5`, `relationships_dispatch_skipped=3`); the two finding names that DO have `:Entity` nodes in Core (`Elasticsearch`, `Single-Node Cluster`) are pre-deploy historical nodes — `class=NULL`, `last_seen` predates the deploy — untouched by this turn. **AC-6** consolidation (supersession status lines) landed at acceptance. FRE-863/864/728/860 all Done + live. **Residual (tracked separately, NOT part of this ADR's write-time mechanism):** the *existing* corpus is not yet de-noised — FRE-865 (backfill) classifies existing knowledge + marks existing System, then FRE-868 evicts the marked existing System from Core (e.g. the 720-mention historical `Elasticsearch` node). Those two authorized ops runs complete the ADR-0114 unblock for the historical corpus.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
