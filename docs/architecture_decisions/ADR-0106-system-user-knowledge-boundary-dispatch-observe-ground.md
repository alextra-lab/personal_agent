# ADR-0106: The System/User Knowledge Boundary — Dispatch by Output Kind, Observe, and Ground (no reasoning cage)

**Status:** Superseded by ADR-0115 — 2026-07-11 (its `output_kind` dispatch is subsumed into ADR-0115's two-axis emission/persistence/dispatch; the FRE-728 implementation is re-pointed to ADR-0115)
**Date:** 2026-07-02
**Deciders:** Project owner (adr session, Opus)
**Tags:** memory, knowledge-graph, system-boundary, extraction, dispatch, observability, self-improvement, sysgraph, event-bus, grounding

**Reconciles:** FRE-639 (ADR-0098 T3 in-Core System gate) with ADR-0105 (isolated `sysgraph`) — see D3
**Refines:** ADR-0098 D1 (the System *class*) — this ADR decomposes the single `System` class value into an **`output_kind`** axis (`ephemeral` vs `finding`) and reclassifies the harness-as-studied-subject to `World`, and replaces the query-time class *filter* with write-time *dispatch*; ADR-0105 D1 (the `source` discriminator) — adds a third, owner-interactive source
**Terminology:** the routing axis is **`output_kind`** (the *nature* of an extracted item: `knowledge` / `ephemeral` / `finding`). "Subject" means *what an item is about* and is a distinct thing — this ADR routes by kind, never by subject.
**Backing tickets:** FRE-727 (this ADR); coordinates with FRE-639 (parked pending this reconciliation), FRE-708 / ADR-0105
**Prescriptive, not descriptive:** the invariants below (write-time dispatch, `output_kind` emission, `sysgraph`, the `owner_diagnostic` source) are the *target* this ADR decides to build. The live code today is subject-based with no dispatch seam and no persisted `class` (see Context); every "never/always/by construction" statement is the invariant the implementation must *establish*, not a property the current codebase already has.

---

## Context

**What is the issue we're addressing?**

One line runs through recent memory work but was never designed whole: the boundary between the **System domain** (the agent's own machinery — code, config, health, telemetry) and the **User domain** (Personal / World / Stance about the owner). It has been approached from three sides that do not agree:

1. **Write side (FRE-639).** Classify System-subject turns and keep their extracted entities out of recall via a **query-time class filter**, leaving them in the Core graph.
2. **Storage side (ADR-0105).** The opposite instinct — **physically isolate** System data into a separate store (`sysgraph`), *because a query-time filter can be forgotten*. 0105 explicitly asked to coordinate with FRE-639.
3. **The self-referential observation (owner, 2026-07-02, to *consider*).** After a health check the owner often asks self-referential questions ("explain your decomposition," "why did you do X"). Today these are indistinguishable from user turns, so they (a) may be answered from a parametric guess with no visibility into whether ground truth was consulted, (b) get extracted as entities that pollute the User KG, and (c) are invisible — nothing tags, counts, or monitors them.

**The framing correction that shapes everything below (owner, 2026-07-02).** An earlier draft over-committed to a *deterministic gate* that detects a self-referential query and routes it down a fixed handler. That is an explicit **non-goal**. We do not want a rigid process that constrains the model's reasoning. The goal is to **isolate the System domain and observe it** at the substrate and telemetry layers, while the model stays free to think.

**Two pieces of evidence ground the design** (read-only against the live prod KG, 2026-07-02, method per FRE-636 / ADR-0087):

- **There is no `class` axis in prod at all.** All **7,581** `:Entity` nodes carry `class=None`; **2,172** turns. FRE-637's class emission and FRE-639's gate are **not deployed** — the System-vs-User machinery today lives *only in the extraction prompt* and nowhere in storage or query. **We are designing this boundary before it is built** — the ideal time, not a reconciliation of two live systems. (~23%, 1,718, also have empty descriptions — extraction junk, orthogonal to this decision.)
- **The "~46% System noise" (FRE-636) is three different things conflated.** Breaking the operational bucket open on the live KG:

  | Kind | Live examples (mention count) | Correct home |
  |---|---|---|
  | **(a) Ephemeral machine state** | `Elasticsearch` "status yellow" (719), `Health Check` (20), `System RAM` "usage %" (8), `approval_ui_disabled_proceeding` (28), `web_search_connect_failed` (6) | ephemeral → **observe + drop** |
  | **(b) The harness as a studied subject** | `ToolLoopGate` "a gate mechanism… dedupes tool calls…" (42), `MCP Gateway` "must be started so tools appear" (17), `Tool Execution` phase (4) | durable knowledge → **user KG** |
  | **(c) Generic tech, ops-framed** | `DNS-based service discovery` (12), `PgBouncer` (14), `TCP` (26), `UTC` (12) | World know-how → **user KG** |

  The tell is in the descriptions: (a) is *state at a moment* ("status yellow," "usage %") — worthless tomorrow; (b) is *a durable fact about how a thing works* — structurally identical to any World topic the owner studies (ADR-0098 D5's own "medical textbook the owner is studying" example, with "the harness" substituted). The current extraction prompt would stamp (b) `class=System` and discard it — throwing away the pedagogical crown jewel. So the boundary is **miscut today**: it keys on *subject* ("is it about the machine") when it should key on *output kind* (durable knowledge vs ephemeral state vs system finding).

**What needs to be decided.** Where the System/User line actually falls; how it is enforced (storage isolation, tagging, both) without a forgettable filter; how a self-referential concern is made observable without a rigid classifier that boxes the model in; what grounding-without-restriction looks like; and how this reconciles FRE-639 with ADR-0105.

---

## Decision

**Governing principle.** The System/User line is **not** "is the subject the machine." It is the **kind of output** a producer emits: durable *knowledge*, ephemeral machine *state*, or a system self-improvement *finding*. Producers (extraction, reflection) reason and emit **freely**; their **outputs** are dispatched by `output_kind` onto the event bus to the right home. **Nothing gates the model's reasoning path.** Isolation is achieved at write time by *where an item is stored*, not at read time by a filter that must be remembered.

### D1 — The boundary is an `output_kind` axis; three routes

Every produced item carries an **`output_kind`** ∈ `{knowledge, ephemeral, finding}` that selects exactly one home:

- **`knowledge` → the User KG (Core).** World / Personal / Stance — *including the harness as a studied subject.* "How decomposition works" is World know-how plus an owner Stance ("learning it"), no different in kind from a scattering law or a leasing concept. It is thread-pullable and spaced-repetition-eligible; it belongs with the rest of the owner's knowledge. `knowledge` items retain their ADR-0098 P/W/S `class`; **the harness-as-studied-subject is `class=World` (with an owner Stance edge), not `System`.**
- **`ephemeral` → observe + drop.** Health/logs/metrics/error-events *at a moment* ("ES yellow," "RAM 62%," "429s now"). No durable value. It **stays in Elasticsearch/telemetry** (already retrievable, and cited by any ticket as evidence) and **never enters the KG**. "Drop" means *drop from the KG*, not *delete from ES*.
- **`finding` → `sysgraph`.** A durable, actionable fact about the agent's own machinery that *should change* ("no connection pooling → reaper exhausts Postgres"). Not user knowledge; not ephemeral. Its home is the ADR-0105 self-improvement pipeline (D4).

**Why a new axis and not just ADR-0098's `class`.** ADR-0098 D1 gave items a single `class` ∈ `{Personal, World, Stance, System}`. That one `System` value **cannot drive this three-way route** — it collapses "ephemeral state to drop" and "actionable finding to `sysgraph`" into one bucket, and it mis-files the harness-as-studied-subject (which is durable World knowledge). So this ADR **refines ADR-0098 D1**: the emission contract (ADR-0098 D5) additionally emits `output_kind`; the old `System` class **decomposes** into `output_kind ∈ {ephemeral, finding}`, and what the old rubric would have called "System because the subject is the harness" is re-decided as `knowledge`/`class=World`. The P/W/S classes are unchanged for `knowledge` items.

This **corrects the current extraction rubric** (`entity_extraction.py`), which today instructs "judge by the *subject* of the turn" and marks harness internals `System`. Under this ADR a harness *explainer* is `knowledge`/World (route 1); only *ephemeral machine state* is `ephemeral` (route 2); an actionable *defect finding* is a `finding` (route 3).

### D2 — Dispatch at the producer's output, over the event bus (isolate by construction, not by filter)

Extraction emits each item with its `output_kind` (D1) **per item, never per-turn** — a mixed turn carries several kinds. The producer emits its items on the event bus; a **dispatch consumer routes by `output_kind`** to the home in D1. The target invariants this establishes:

- **The design's isolation invariant: `ephemeral`/`finding` items are never *written* into the User KG.** Isolation is by construction — a recall query cannot return a System item because it was *never written* to Core, not because a read-path filter hid it. This is strictly stronger than a query-time filter (ADR-0105's stated concern; FRE-639's residual weakness): there is no filter to forget because there is nothing to exclude. *(This is the invariant to build — the current write path MERGEs every extracted entity into Neo4j with no dispatch branch and does not persist `class`; establishing this invariant is exactly what T1 does.)*
- **Per-item dispatch splits mixed turns correctly.** "I'm leasing a Rafale — also is your KG healthy?" routes the vehicle knowledge to Core and the health reading to drop, in one pass. Whole-turn tagging cannot do this and edges toward a cage.
- **The turn itself is untouched.** The `:Turn`/`:Session` record stays in the turn stream; only *derived knowledge* dispatches. This is why the boundary does not fracture turn integrity (the reason ADR-0105 Option 5 was rejected) — nobody relocates a turn.

### D3 — Reconcile FRE-639 ↔ ADR-0105: distinct scopes, and a user turn can feed the pipeline

The two "System"s are structurally different data, and this ADR names both homes and the bridge between them:

- **ADR-0098 System** = a *user conversation turn whose subject is infra.* Under this ADR its items dispatch by `output_kind` (D1): `ephemeral` drops, durable harness-knowledge goes to Core as World+Stance, a `finding` goes to `sysgraph`. **This ADR directs that FRE-639's in-Core query-time class filter be replaced by write-time dispatch (D2)** — FRE-639 is re-scoped (its query-filter/eviction work is superseded; see the consequence below), because dispatch is the strictly stronger enforcement (isolation by construction vs a filter that can be omitted). The supersession is a decision this ADR makes and its tickets enact, not a change already in the code.
- **ADR-0105 System** = the *self-improvement pipeline's own relational model* (proposals/findings/tickets/outcomes), born isolated in `sysgraph`. **Unchanged**, except that this ADR adds a new producer to it (D4).
- **The bridge (the sharp case).** A user turn *can feed the pipeline scope*: a health check that finds a problem emits a **finding** into `sysgraph` (route 3). This is where ADR-0098-System and ADR-0105-System meet — the health-check finding — and the bus-dispatch is the seam that lets a turn's finding cross into the pipeline while its ephemeral readings and the turn itself go nowhere near the User KG.

### D4 — The health-check finding is a third proposal source (extends ADR-0105 D1)

ADR-0105 D1's `source` discriminator (`Literal["statistical_detector", "reflection"]`) is explicitly extensible; both existing sources are *background* producers. This ADR adds an **interactive** source — `owner_diagnostic` — for findings surfaced during an owner-driven diagnostic turn (health check, log analysis). The finding flows the **same** machinery already accepted in 0105:

- it becomes a **Finding/Proposal node** in `sysgraph` (0105 D2), **captured even when the owner creates the ticket by hand** — otherwise the background reflector cannot know the problem is already actioned;
- it promotes to a **ticket with bidirectional linkage** (0105 D4), which cites the ES telemetry as evidence;
- its **outcome closes the loop** (0105 D7);
- **generation-time dedup** (0105 D9) then prevents the background reflector from **re-proposing** a problem already found and ticketed.

### D5 — Grounding without restriction: always-available, governed self-knowledge sources

Curated self-knowledge sources — the agent's **own code, redacted config, health, telemetry, trace** — live in the toolset **unconditionally**, governed per-tool like any other tool. The model reaches for them **when it judges relevant**; no detection enables them, and no route forces them. This fully decouples *capability* from *classification*: the model can ground itself when it chooses, and nothing narrows its reasoning to make it. **Config is served redacted** (secrets never surfaced) — the one security guard on the new capability.

### D6 — Observability by post-hoc derivation, not a gate

The System domain is made observable from signals that **already exist**, read *after* the turn — so nothing new sits in the reasoning path:

- **Volume** — how many turns produced System-scoped output (from the dispatched scope of D2).
- **Grounding** — *did the model consult a real self-knowledge source* when it self-analyzed (derived from the **tool-call trace**, which already records tool invocations).

Both surface on a monitor. Grounding is a **truth signal**, not just ops hygiene: a **finding** produced with `grounded=false` is a *suspect* finding — a plausible diagnosis nobody verified against ground truth (a ticket born from a guess); a **harness-lesson** produced with `grounded=false` is *maybe-false learning* (worse for a tutor than for an ops turn). The signal is derived, never a gate — it observes what happened; it does not steer what happens.

### D7 — `sysgraph` scope: system self-improvement only

`sysgraph` holds **improvements to the system, full stop.** Reflection *about the owner's world, actions, and conversations* — the pedagogical/tutor reflection ("you've circled game theory from three angles; your stance on X shifted; you haven't revisited optics in six weeks") — lives in **Core**, native to the World and Stance it reflects on (the ADR-0098 D7 World-internal-correlation read pattern).

This is not a preference; the **isolation invariant requires it.** ADR-0105 isolates `sysgraph` by engine *precisely so self-improvement data can never touch the User KG.* World/tutor reflection must **traverse** Core (Stance edges, World↔World bridges). Put it in `sysgraph` and `sysgraph` would have to traverse Core — the isolation that justifies its existence is gone. Keep it in Core and `sysgraph` stays clean. So the **same dispatch-by-`output_kind` principle applies one layer up, to the reflection producer**: reflect freely, dispatch each reflection output by its kind (a machine-improvement `finding` → `sysgraph`; a world/tutor insight → Core). The pedagogical reflector itself is a **separate future ADR** — today's reflection engine is system-improvement-only (ADR-0105's own categories: performance 43% / observability 23% / reliability 11%).

### Explicit non-goal (stated, and made checkable)

There is **no deterministic self-referential classifier that routes the model into a canned handler, narrows its context, or restricts its toolset.** The "self-referential turn" **dissolves into D1** — it is just a turn whose *outputs* dispatch by kind; there is no special handler and no special path. This non-goal is not merely asserted: AC-5 makes it falsifiable (a code-path scan must find no reasoning-path stage that branches on a self-referential/System classification).

---

## Alternatives Considered

### Option 1: Keep FRE-639's in-Core query-time class filter as-is
**Description:** Leave System items in the Core graph; exclude them from recall with a query-time `class` filter (plus never-promote + eviction).
**Pros:**
- No new dispatch seam; the class axis is the only new machinery.
- Consistent with ADR-0098's single-Core topology.
**Cons:**
- The recall-read exclusion is a **forgettable filter** — one query that omits it leaks System into the tutor corpus (ADR-0105's stated reason for physical isolation).
- It keys on *subject*, so it still **mislabels the harness-as-studied-subject as System** and discards durable learning (the (b) bucket, empirically ~evident on the live KG).
**Why Rejected:** Write-time dispatch (D2) is strictly stronger — isolation by construction, no filter to forget — and D1 fixes the mis-cut the filter can't. The filter is the weaker half of the exact problem this ADR exists to solve.

### Option 2: A deterministic self-referential router + fixed handler
**Description:** Detect a self-referential query and route it down a dedicated handler (grounding forced, context narrowed).
**Pros:**
- Guarantees grounding on self-referential turns; makes them trivially countable.
**Cons:**
- **Cages the model's reasoning** — the owner's explicit non-goal.
- A brittle classifier boundary (what counts as "self-referential"?) that mis-routes mixed turns and evolves poorly.
**Why Rejected:** The non-goal, by name. The design instead dissolves the self-referential case into ordinary output-kind dispatch (D1) and observes grounding post-hoc (D6) — same visibility, zero cage.

### Option 3: Relocate whole user-turn System into an isolated store
**Description:** Physically move System-subject *turns* (not just derived items) out of Core into a separate store (ADR-0105 Option 5 extended to user turns).
**Pros:**
- Strong physical isolation of everything System.
**Cons:**
- **Fractures turn integrity** — the `:Turn`/`:Session` stream can't be split without breaking session reconstruction; high blast radius, no functional gain (ADR-0105 rejected exactly this).
**Why Rejected:** D2 achieves the isolation win by dispatching *derived knowledge*, leaving the turn record untouched — all of the benefit, none of the fracture.

### Option 4: Whole-turn System tagging
**Description:** Classify the *turn* as System or User and route wholesale.
**Pros:**
- One decision per turn; cheap.
**Cons:**
- **Mixed turns mis-route** (the Rafale-plus-health-check turn): either the vehicle knowledge is lost or the health reading pollutes Core.
- A whole-turn classifier the reasoning passes through is closer to the cage of Option 2.
**Why Rejected:** Per-item dispatch (D2) is both more correct (splits mixed turns) and further from a reasoning gate.

### Option 5: Delete ephemeral state entirely (drop from ES too)
**Description:** Treat ephemeral machine state as pure garbage — never store it anywhere.
**Pros:**
- Simplest; nothing to retain.
**Cons:**
- Loses the **debugging/evidence trail** — a health-check ticket cites the very telemetry that motivated it; deleting it orphans the ticket's evidence.
**Why Rejected:** "Drop" means *drop from the KG*, not *delete from ES*. ES retention is the evidence layer (route 2 keeps it there).

### Option 6: `sysgraph` holds both system-improvement and world/tutor reflection
**Description:** One reflection store for everything the reflector emits, machine and world alike.
**Pros:**
- One store, one producer path.
**Cons:**
- **Breaks the isolation invariant** — world/tutor reflection must traverse Core (Stance, World↔World), so `sysgraph` would have to reach into the User KG, destroying the engine-level isolation that is its entire justification.
**Why Rejected:** D7 — reflection about the owner's world is Core-native; `sysgraph` stays strictly the machine's self-improvement.

---

## Consequences

### Positive Consequences

- **The boundary is cut correctly, and once.** Output-kind, not subject: harness-as-studied-subject reaches the tutor corpus (recovered crown-jewel knowledge that today is discarded), ephemeral state never pollutes it, findings are captured for self-improvement.
- **Isolation by construction.** System items are never written to the User KG, so there is no forgettable recall filter — the strongest form of the isolation both FRE-639 and ADR-0105 were reaching for.
- **The self-referential problem is solved without a cage.** Grounding is a capability the model may use; observability is derived post-hoc; there is no handler the reasoning is forced through — and the non-goal is *checkable*.
- **A new, high-value insight source.** The owner running a health check becomes a first-class self-improvement producer (`owner_diagnostic`), and the loop closes on it — the background reflector stops re-proposing already-ticketed problems.
- **Grounding becomes a truth signal.** Suspect findings (ungrounded diagnoses) and maybe-false learning (ungrounded self-explanations) become visible rather than silent.
- **`sysgraph` and Core each stay clean**, by the same dispatch principle applied at both the extraction and reflection layers.
- **No *steady-state* System-eviction job is needed** — once dispatch is live, System items are never written to the KG (D2), so FRE-639's ongoing class-aware System eviction (which ADR-0098 D4 required) becomes moot going forward. The **already-accreted** System material still requires the one-time cleanup (below); this claim is "no recurring eviction after dispatch + cleanup," not "no eviction ever." Episodic-tier eviction, unrelated to System, is out of scope here.

### Negative Consequences

- **A dispatch seam is new machinery** on the extraction output path (the bus consumer that routes by scope) — it must be correct per-item, or a mis-dispatch mislabels knowledge.
- **The extraction prompt/contract must be corrected** (harness-as-subject is World+Stance, not System) — a behavioral change to a live producer.
- **The existing ~46% System material is already in the KG** and must be cleaned once (see Risks — a guarded, owner-excluded one-time job; blast radius is real).
- **Grounding adds always-available self-knowledge tools** — a small, governed capability surface, with config redaction as the guard.
- **Per-item scope depends on the class axis existing** — which is *not deployed* (all prod entities are `class=None`); this ADR's dispatch presupposes the ADR-0098 D5 emission contract landing.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| The one-time cleanup of existing System entities deletes soul data (Personal/Stance) | High | Class-scoped, **owner-excluded** match set (extend the ADR-0052 dedup-exclusion / ADR-0098 D3 guard); dry-run/`EXPLAIN` the match set and prove Personal/Stance are not in it **before** executing; retain a snapshot for rollback |
| Mis-dispatch routes durable knowledge to drop (or ephemeral state to Core) | Medium | Per-item scope with a fail-safe default to **Core** (never silently drop on uncertainty — losing knowledge is worse than a stray low-value entity); the observability monitor surfaces dispatch mix so drift is visible |
| The `owner_diagnostic` finding dangles (orphan node or null back-reference to the ticket) | Medium | Joinability probe (ADR-0074) over the finding↔ticket linkage; linkage written with the finding node; AC-3 asserts both directions resolve |
| An ungrounded finding creates a false ticket | Medium | The grounding signal (D6) tags it `grounded=false`; the funnel/monitor surfaces ungrounded findings as suspect (D6 is the detector, not a blocker) |
| Always-available config tool leaks secrets | Medium | Config is served **redacted** (D5); a test asserts secret keys never appear in the tool's output |
| Dispatch presupposes an undeployed class axis (`class=None` in prod today) | Medium | Sequence T1 on the ADR-0098 D5 emission contract landing; until then the dispatch consumer treats missing scope as Core (fail-safe) and the monitor reports coverage |

---

## Implementation Notes

**Files affected (primary):**
- `src/personal_agent/second_brain/entity_extraction.py` — correct the rubric (today it says "judge by the *subject* of the turn" and marks harness internals `System`): harness-as-studied-subject → `knowledge`/World + owner Stance; emit **`output_kind`** ∈ `{knowledge, ephemeral, finding}` per item (the ADR-0098 D5 contract extended).
- `src/personal_agent/second_brain/consolidator.py` — today writes every extracted entity through `MemoryService` with no branch; becomes the dispatch point (or emits to the bus consumer) that routes by `output_kind`.
- `src/personal_agent/memory/service.py` — persist the `class` property on `:Entity` at write (today `create_entity` does not); `knowledge`-only write path.
- `src/personal_agent/events/` — the extraction-output event(s) carrying per-item scope; the bus is the dispatch seam.
- a **dispatch consumer** (new; likely under `second_brain/` or `memory/`) — routes items by scope: durable → `MemoryService`/Core, ephemeral → ES/telemetry only (no KG write), finding → `sysgraph`.
- `src/personal_agent/sysgraph/` (ADR-0105) — accept the `owner_diagnostic` source; capture the finding node + bidirectional ticket linkage even for hand-created tickets.
- `src/personal_agent/captains_log/models.py` — extend the `source` discriminator with `owner_diagnostic`.
- self-knowledge tools (new; `tools/`, Tier-1 native per ADR-0028) — own-code read, **redacted** config read, health/telemetry/trace read; governance entries in `config/governance/tools.yaml`.
- telemetry derivation for the System-domain volume + grounding signals (from dispatched scope + tool-call trace); **explicit ES field mappings** (FRE-704 discipline, so conversion fields are not dropped at the 300-field cap); a Kibana monitor (built in the UI, Playwright-verified — never hand-authored Lens ndjson).
- a **guarded one-time cleanup** script for the existing System material (dry-run match set + owner-exclusion + snapshot; `docker/postgres/migrations/`-style discipline for any schema, no Alembic).

**Dependencies / coordination:**
- **Presupposes** the ADR-0098 D5 emission contract (per-item `class`) — not deployed today (`class=None` across prod).
- **Extends** ADR-0105 (the `sysgraph` store + `source` discriminator + loop-close).
- **Supersedes** FRE-639's query-time filter approach (D3); FRE-639 is re-scoped/unparked accordingly.
- Coordinates with **FRE-704** (explicit ES field mappings) and **FRE-708** (the insights-engine observability this rides).

**Testing strategy:** unit tests for per-item dispatch routing (a fixture turn with all three kinds routes each correctly); an integration test over the test substrate for the health-check-finding → `sysgraph` → ticket-linkage arc; joinability probe for the finding↔ticket linkage; a redaction test on the config tool; a code-path scan (AC-5) asserting no reasoning-path stage branches on a self-referential/System classification; a Playwright render-check for the monitor; a dry-run assertion on the cleanup match set (owner-excluded).

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

Each check names the concrete artifact it inspects. Where Core is checked, the expected shape is: a `knowledge` item is a `:Entity`/`:Claim` reachable from the owner node, carrying the persisted `class` property (`World`/`Personal`/`Stance`) this ADR adds to the write path (today `create_entity` persists no `class`); Stance is a `HAS_STANCE` edge. "Not in the KG" means **no such node exists** (verified by a node-existence query), distinct from "present but read-filtered."

- **AC-1 — The `output_kind` boundary holds across a fixture *set*, not two prompts.** *Outcome:* varied harness-explainer turns produce durable World+Stance in Core; varied ephemeral turns produce no KG node and are retrievable in ES. · **Check:** run a fixture set of **≥4 distinct** harness-explainer phrasings (decomposition, tool-loop gating, gateway stages, memory recall) and **≥4 distinct** ephemeral turns (healthcheck, log-tail, RAM/CPU, connectivity ping); **every** explainer yields ≥1 `class=World` `:Entity` **and** an owner `HAS_STANCE` edge (none `class=System`, none dropped); **every** ephemeral turn yields **zero** new KG nodes **and** its reading is queryable in the corresponding ES index. Vary the wording so a per-prompt special-case cannot pass. · *Fails if* any harness-explainer item is stamped `System`/dropped, any ephemeral reading becomes a KG node, or any ephemeral reading is *not* found in ES (silently lost, not merely dropped-from-KG).
- **AC-2 — Dispatch is per-item and isolates by absence-of-write, across *all* read paths.** *Outcome:* a mixed turn routes each item to its home in one pass; the dropped item is absent because never written. · **Check:** ingest "I'm leasing a Rafale — also is your KG healthy?"; the vehicle World/Stance items exist in Core, the health reading has **no node** (node-existence query returns empty); then confirm **no user-facing KG read path** can surface it — enumerate the recall/read entrypoints (`MemoryService.query_memory`, `recall_context`/broad-recall, and any tutor traversal) and assert none returns the health item, *and* that none relies on a `class`-exclusion filter for that correctness (there is nothing to exclude). · *Fails if* the turn routes wholesale, the health item exists as a node, or any one read path could return a System item (isolation that holds on one path but leaks on another).
- **AC-3 — A health-check finding becomes a linked, dedup-aware `sysgraph` proposal.** *(Depends on ADR-0105's `sysgraph` + `source` discriminator landing; this is the acceptance test for the T2 work, not a check against today's code.)* *Outcome:* a real diagnostic finding is captured and closes the loop even when the owner tickets by hand. · **Check:** run a health-check turn that finds a problem and create a ticket; a Finding/Proposal node exists in `sysgraph` with `source=owner_diagnostic`, resolves **both** ways to the ticket (finding→ticket id, ticket→finding id, neither null — verified by the ADR-0074 joinability probe over the linkage), and a subsequent background reflection run over the same symptom **does not** create a new proposal (dedup sees it decided). · *Fails if* the finding is dropped or lives only in ES, either linkage direction is null/dangling, or the reflector re-proposes the ticketed problem.
- **AC-4 — Grounding is discriminating against a *defined* self-knowledge allowlist.** *Outcome:* the signal is true only when the model consulted a **governed self-knowledge source**, false otherwise. · **Check:** the self-knowledge sources are a **named allowlist** in `config/governance/tools.yaml` (D5: own-code / redacted-config / health / telemetry / trace); the `grounded` signal is derived from the tool-call trace (`route_traces.tools_used` / ES `tool_name`) as *"≥1 call to a tool on the allowlist."* Replay: (a) a turn that called an allowlisted source → `grounded=true` naming the source(s); (b) a turn that answered from memory → `grounded=false`; (c) a turn that called a **non-allowlisted** tool (e.g. `web_search`) → `grounded=false` (a generic tool call does **not** count). · *Fails if* the allowlist is undefined, the signal counts any tool call as grounded (so (c) reads true), or the signal is constant regardless of the trace.
- **AC-5 — No reasoning cage — a concrete forbidden dataflow + a positive test.** *Outcome:* grounding is a capability, not a route; no named pre-LLM stage selects model/context/toolset from a self-referential/System label. · **Check:** (a) *positive* — a self-knowledge tool is invocable on a turn that is **not** System-scoped, because `tools.yaml` gates it by **mode only, not by TaskType/subject** (assert the governance entry has no task-type/subject condition); (b) *forbidden dataflow (named, not a blanket scan)* — the classification fields (`route_traces.task_type` and any self-referential/System label) are **not read** by the model-selection, context-assembly, or tool-exposure stages of `request_gateway/pipeline.py` / `orchestrator/executor.py` — asserted by an import/reference check on those specific modules against those specific fields, so it is a bounded, reproducible check rather than "prove absence everywhere"; (c) *deterministic no-special-handling* — with the self-knowledge tools **stubbed unavailable** (so the model cannot ground), a System-scoped turn still completes and is tagged `grounded=false`, not blocked or rerouted. · *Fails if* the self-knowledge tool carries a task-type/subject gate, any of the three named stages reads a self-referential/System label to alter model/context/tools, or the stubbed-unavailable turn is blocked/handled specially instead of completing with `grounded=false`.
- **AC-6 — `sysgraph` holds no world/tutor reflection — and Core *does*.** *Outcome:* the self-improvement store is all machine-subject **and** world-correlation demonstrably runs against Core. · **Check:** (mandatory both halves) (i) scan `sysgraph` finding/proposal `subject` fields — **zero** resolve to an owner-world/Stance concept (all are machine components); (ii) produce a world↔world correlation insight over the **Core** graph and confirm it was computed by a Core traversal/query, **not** by any `sysgraph` code path (inspect the call path). · *Fails if* any world/Stance insight node is found in `sysgraph`, **or** world-correlation is unimplemented/implemented over `sysgraph` (half (ii) is not optional — a `sysgraph` that is merely empty passes (i) vacuously).
- **AC-7 — Cleanup removes `ephemeral`/`finding` residue *and preserves the reclassified crown jewel* and the soul.** *Outcome:* the one-time cleanup deletes only genuine System residue, **reclassifies** harness-as-studied-subject to World (does not delete it), and provably leaves Personal/Stance intact. · **Check:** on a labeled sample of today's mislabeled material — (a) `ephemeral`-residue entities (healthcheck/telemetry readings) are **gone** after the run; (b) **harness-as-studied-subject** entities (e.g. the `ToolLoopGate`/`MCP Gateway` "how it works" items) are **retained and reclassified `class=World`** (present after the run, not deleted) — this is the D1 crown-jewel guarantee; (c) the cleanup match set is dry-run/`EXPLAIN`'d and contains **no** Personal/Stance Claim (owner-excluded, ADR-0052/0098-D3), a contemporaneous Personal/Stance sample **remains**, and a rollback snapshot exists. · *Fails if* harness-as-studied-subject entities are deleted (crown jewel lost — the failure mode a delete-all-System cleanup would hit), any Personal/Stance item is in the match set or deleted, ephemeral residue survives, or no rollback snapshot was taken.

**Seam owner (assembled intent).** The **three-route dispatch (AC-1 + AC-2 + AC-3)** is the primary seam: it holds only when `knowledge` reaches Core, `ephemeral` stays out of the KG (present in ES), **and** a `finding` crosses into `sysgraph` linked and dedup-aware — one real turn of each kind demonstrated end-to-end through the corrected extractor and the bus consumer. No single child ticket proves this. **Master holds the decomposed ADR against AC-1+AC-2+AC-3** and it does not close because the last child merged — only because a real harness-explainer, a real mixed turn, and a real health-check finding each route correctly. The no-cage (AC-5), grounding (AC-4), scope (AC-6), and cleanup (AC-7) seams are asserted independently.

---

## References

- FRE-727 — this ADR's originating ticket (the System-domain boundary; isolate + observe, no reasoning cage)
- ADR-0097 — Ingested-Knowledge Taxonomy (Personal / World / Stance; System is the negative space)
- ADR-0098 — Memory Substrate & Lifecycle Architecture (D1 the System class; D5 the per-item emission contract; D7 the Core-native world-correlation read pattern this ADR keeps in Core)
- ADR-0105 — Convergent Self-Improvement Pipeline & Isolated System Graph (the `sysgraph` store, the `source` discriminator this ADR extends, and the loop-close the `owner_diagnostic` finding rides)
- ADR-0052 — Owner Identity Primitive (the `is_owner` anchor + dedup-exclusion invariant the one-time cleanup extends)
- ADR-0074 — Joinability probe (provenance integrity for the finding↔ticket linkage)
- ADR-0040 — Linear as Async Feedback Channel (the human-in-loop ticket path the `owner_diagnostic` finding promotes through)
- ADR-0028 — Tool Integration Tiers (the self-knowledge grounding tools are Tier-1 native)
- FRE-639 — ADR-0098 T3 System gate (its query-time filter is superseded by write-time dispatch; re-scoped/unparked by this ADR)
- FRE-636 — taxonomy-validation spike (`docs/research/2026-06-27-fre-636-taxonomy-validation.md`) — the ~46% figure whose bucket this ADR breaks into three
- FRE-704 — ES 300-field-cap dynamic-field drop (explicit mappings for the observability fields)
- FRE-708 — Refine the Insights Engine and make it observable (ADR-0105's originating ticket; the observability surface this rides)
- Live KG read-only breakdown, 2026-07-02 — `docs/research/2026-07-02-fre-727-system-noise-breakdown.md` (`class=None` across 7,581 entities; the (a)/(b)/(c) System-bucket split, with reproducible Cypher)

---

## Status Updates

### 2026-07-02 - Proposed
**Changed By:** Project owner (adr session, Opus)
**Reason:** Authored from FRE-727 (Approved). Design settled with the owner across a discussion session: the boundary is by *output kind* (durable knowledge / ephemeral state / system finding), not turn subject; isolation is by write-time dispatch over the event bus (D2), not a query-time filter; the self-referential case dissolves into D1 with grounding as an always-available capability (D5) and observability derived post-hoc (D6), with the reasoning-cage explicitly a non-goal (checkable via AC-5); the health-check finding is a third `owner_diagnostic` proposal source extending ADR-0105 (D4); `sysgraph` stays system-improvement-only (D7). Awaiting Codex review + owner acceptance.

### 2026-07-02 - Revised (still Proposed) — Codex round 1
**Changed By:** Project owner (adr session, Opus)
**Reason:** Folded Codex round-1 findings. **The routing signal is now explicit** — a first-class `output_kind` axis (`knowledge`/`ephemeral`/`finding`) that decomposes ADR-0098's single `System` class, resolving the "one `class` value can't drive a 3-way route" gap (finding #1). Terminology standardized on `output_kind` (title + D1/D2/D3/D7), "subject" reserved for *what an item is about*. Present-tense over-claims reframed as **target invariants** the implementation establishes, with a "prescriptive not descriptive" header and honest current-state notes (findings #2/#11/#12). **AC-7 now requires the cleanup to reclassify-and-retain the harness-as-studied-subject** (the crown jewel), not just delete System — closing a data-loss hole (finding #9). ACs tightened: AC-1 uses a ≥4×2 fixture set + ES-retention check (not two prompts); AC-2 covers **all** user-facing read paths; AC-4 defines the governed self-knowledge **allowlist** and rejects generic tool calls; AC-5 replaced the brittle "no branch anywhere" scan with a **named forbidden dataflow** on specific gateway/orchestrator stages + a deterministic stubbed-unavailable fixture; AC-6's Core-world-correlation half made mandatory (findings #3/#4/#6/#7/#8/#10). Live-KG numbers committed as a citable artifact (`docs/research/2026-07-02-fre-727-system-noise-breakdown.md`, nit #5). Re-submitting to Codex.

### 2026-07-02 - Accepted
**Changed By:** master (integration gate)
**Reason:** Owner co-designed the full spine across the adr session and approved it; codex 2 rounds, no blocking findings; merged as PR #324. Elevated Proposed → Accepted at the gate (consistent with ADR-0098 / ADR-0105 acceptance-at-gate). The design is settled; implementation is FRE-728–732 (Needs Approval). ADR reaches *Implemented* only when the assembled seam AC-1+AC-2+AC-3 is proven live (master-held). Reconciles FRE-639 (its query-time filter + class-aware System eviction are superseded by write-time dispatch, D3); refines ADR-0098 D1; extends ADR-0105 D1.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
