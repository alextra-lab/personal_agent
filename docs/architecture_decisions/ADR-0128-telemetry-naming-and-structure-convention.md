# ADR-0128: One Telemetry Naming and Structure Convention Across Every Substrate — Enforced at Emit and at the Substrate Boundary

**Status:** Proposed
**Date:** 2026-07-29
**Deciders:** Project owner (FRE-1038, owner-directed 2026-07-28)
**Tags:** telemetry, observability, naming-convention, elasticsearch, opentelemetry, enforcement

---

## Context

### What is being decided

Six different names exist for one concept — the moment a telemetry record describes — and no rule says which is right. This ADR decides the naming and structure convention for telemetry across every substrate, and, more importantly, decides **where that convention is enforced** so it changes the bytes on disk rather than only the documentation.

### The measured divergence

The record timestamp, read directly from the committed index templates and from live documents:

| Spelling | Families |
|---|---|
| `@timestamp` | `agent-logs`, `agent-topology`, `agent-monitors-projector-health`, `agent-captains-funnel-events`, `agent-monitors-cache-reset-cadence` |
| `timestamp` | `agent-captains-captures`, `agent-captains-reflections`, `agent-captains-captures-subagents`, `agent-insights` |
| `started_at` | `agent-monitors-joinability`, `agent-monitors-joinability-substrate` |
| `probed_at` | `agent-monitors-slm-health` |
| `rated_at` | `user-turn-ratings` |
| `ts` | `slm-requests` — **not declared in its template at all**; dynamically mapped |

Six spellings, thirteen families. FRE-1038 reported four; the census above adds `rated_at` and `ts`, the latter found only by reading a live document rather than the template.

The same divergence exists beyond the date field, and beyond Elasticsearch:

- **Token counts.** `slm-requests` writes `prompt_tokens` / `completion_tokens`; `agent-logs` writes `input_tokens` / `output_tokens`. **ADR-0068 documented exactly this mismatch on 2026-05-10** and filed FRE-351 and FRE-353 to fix it. Both remain open, fourteen weeks later.
- **Event key.** `telemetry/es_handler.py:121` reads structlog's `event` key; `telemetry/es_logger.py:167` writes it as `event_type`. A single rename at a single seam, papered over downstream by dual-key fallback logic. ADR-0090 lists resolving it under *Open decisions*, unresolved.
- **Work taxonomy.** The Postgres cost ledger records a `purpose` per call (captains-log, entity-extraction, skill-routing, embedding, reranker, main-inference); Elasticsearch records a `role`, and 93% of calls report the single value `primary`. Two vocabularies, two granularities, for the same work. The implementation defect is FRE-1037; the absence of a canonical vocabulary is this ADR's.
- **Other substrates.** Neo4j entity nodes carry a `class` property, not the entity-class name a reader would guess; Redis stream keys require a prefix that is easy to omit. Any fix scoped to Elasticsearch leaves these untouched.

### Why it is expensive: a wrong name is indistinguishable from absent data

Every one of these divergences fails the same way — an empty result, never an error. FRE-1038 records five distinct field-name failures in one working session across two actors: a sort on a timestamp field the index does not carry; a read of a `checks` key on a field named `substrate_checks`; a read of an entity `class_name` property actually named `class`; a Redis key queried without its prefix; and an index name manufactured through a shell substitution and then reported to the owner as fact. None raised an error. All were reported as findings.

This is the property that makes naming an architectural concern rather than a style preference: **the system cannot distinguish a correct query over absent data from a misspelled query over present data**, so the failure is silent by construction and reaches the owner as a confident wrong answer.

### Identity fields are absent far more often than present

Measured live over all `agent-logs-*` (3,205,531 documents, 2026-07-29):

| Field | Present | Share |
|---|---|---|
| `@timestamp` | 3,205,531 | 100% |
| `event_type` | 3,205,531 | 100% |
| `trace_id` | 365,070 | **11.4%** |
| `session_id` | 67,562 | **2.1%** |
| `user_id` | 36,179 | **1.1%** |

(FRE-1038 cites 16% / 9% / 9%; that was measured over a narrower window. The all-time figures are worse.) So even where names agree, presence does not — a query cannot assume the join key exists, and nothing objects when it does not.

### The corpus is overwhelmingly already conformant — the migration is 1%

Measured live, 2026-07-29 (per-family `_count`, not `_cat/indices`, which inflates counts via nested sub-documents):

- **`agent-logs` holds 3,198,739 documents — 98.8% of the ~3.24M-document corpus — and already uses `@timestamp`.**
- Every family on a divergent spelling, summed: captures 1,889 · subagents 69 · reflections 1,963 · insights 3,684 · joinability 1,733 · joinability-substrate 9,394 · slm-health 15,868 · ratings 1,958 — **36,558 documents, 1.1% of the corpus.**

The governed surface is likewise small. Across 14 committed templates there are 363 property declarations covering **234 distinct field names — of which 175 appear in exactly one family**. Only **59 field names cross families at all**; 32 appear in three or more. `trace_id` is the most widely shared, declared in 11 of 14 templates.

### The diagnosis: two disciplined producers, no shared vocabulary

The failure is *not* carelessness at the emit sites. Both major producers independently built the same safeguard:

- `telemetry/events.py:54-67` defines `CANONICAL_MODEL_CALL_STARTED_FIELDS` / `CANONICAL_MODEL_CALL_COMPLETED_FIELDS` — frozen field sets a parity test imports as single source of truth, forcing every model client to emit one shape (ADR-0074 / FRE-376 Phase 2).
- `slm_server`'s `router.py` builds every `request_complete` document through one builder whose docstring reads: *"Single source of the schema so every slm-server request_complete event is identical regardless of model or backend (chat, rerank, ...). Fields that don't apply to a given request type are left None, never dropped."*

Both are internally perfect. Both guarantee that every event *they* emit is identical. Neither says anything about the other — so one emits `ts` / `prompt_tokens` / `completion_tokens` and the other emits `@timestamp` / `input_tokens` / `output_tokens`, and the same LLM call is described two ways depending on which side of the tunnel is asked.

**What is missing is not discipline at the emit sites. It is a vocabulary above them.**

### Why a fifth diagnosis is the real risk

Four ADRs already touch this ground, and this ADR must not become a fifth overlapping voice:

- **ADR-0004** (Accepted, 2025-12-28) — established structured logs plus minimal OTel-compatible trace semantics. It set the telemetry *model* and never specified a field vocabulary. Not superseded; this ADR supplies the vocabulary it left open.
- **ADR-0068** (Accepted, 2026-05-10) — diagnosed emit-site divergence precisely, including the `prompt_tokens` / `input_tokens` split and dead template declarations, and filed FRE-351 / FRE-352 / FRE-353. **All still open.** This ADR subsumes its D3/D4 by making those corrections fall out of a convention instead of per-site fixes.
- **ADR-0090** (Accepted) — owns the *surface* contract: emit ↔ mapping ↔ dashboard three-way reconciliation, mapping discipline, the trap classes. It explicitly defers **"a declared field registry — a typed catalog the emit sites and templates both derive from"** to its *Open decisions*, along with the `event` / `event_type` resolution. **This ADR closes both of those open decisions.** The two are complements with a clean seam: 0090 governs whether a field lands correctly typed and surfaces faithfully; 0128 governs *what it is called* and *who guarantees it is present*.
- **ADR-0093** (Accepted, 2026-06-21) — already chose the standard: adopt the OpenTelemetry data model and `gen_ai.*` semantic conventions at the emission boundary (D1/D2), sequenced as FRE-583. **`grep -rn "gen_ai" src/ docker/ scripts/ config/` returns nothing, and FRE-583 has sat in `Needs Approval` since 2026-06-21, never approved, never dispatched.**

That last point is the load-bearing lesson. The standard-selection decision has already been taken once and produced **zero change in the data**, because it ended in a ticket rather than a mechanism. Three ADRs correctly diagnosed this problem and none of them changed a byte. Choosing a second standard now would repeat the pattern exactly.

**Therefore this ADR does not re-open the choice of standard. It adopts ADR-0093's and supplies the half that was missing: enforcement.**

### The constraint that shapes enforcement: producers we do not own

`slm-requests` is written by `slm_server` — a separate repository, on a separate machine, with its own configuration and lifecycle. Verified against `origin/main` (the local checkout was 33 commits stale; five of those commits touch this code):

- **Four** emit paths now ship telemetry (`router.py:528, 696, 971, 1175` — chat, responses, rerank, streaming), up from one.
- `telemetry.py:38` POSTs to `{ES_URL}/{prefix}-{YYYY.MM.DD}/_doc` — **the index name is string-formatted client-side.**
- The shipper is deliberately fail-soft (`telemetry.py:32,52`): it swallows every exception so the request path is never affected.
- It is live — 12 documents on 2026-07-29, most recent 05:24Z.

Three consequences. First, no in-process mechanism in `personal_agent` can bind it. Second, **FRE-1036's consolidation cannot fix it either**: that work replaces daily indices with size-based rollover behind a write alias, but `slm_server` picks its own URL and will keep minting one index and one shard per day regardless — 38 shards so far, roughly one per day, immune to the consolidation. Third, if the substrate ever rejects a non-conforming document, `slm_server` discards it silently and the only trace is a warning in a log file on another machine.

`slm_server` is the first out-of-repo producer. It will not be the last. A convention that cannot reach such a producer is not a convention.

### Sequencing against FRE-1036

FRE-1036 (Approved) replaces daily indexing with size-based rollover, against a real deadline: 589 active shards against a 1,000-per-node ceiling, ~7–8 new indices per day. It explicitly rules out data streams, because three write paths use explicit document identifiers for idempotency.

FRE-1038 argues consolidation is the cheapest moment to normalise. Half of that is weak and half is strong:

- **Weak:** editing a template renames nothing in existing data. Mappings are append-only; a template governs indices created after it. Changing field names in a template alone yields new indices with new names beside old indices with old ones — the same mixed state, freshly created.
- **Strong:** consolidation almost certainly reindexes, since 480+ indices cannot be reduced by settings alone. A reindex is exactly where a rename is nearly free — `_reindex` accepts a `pipeline`, and an ingest pipeline's `rename` processor renames fields as documents pass through. Miss that window and normalising later means paying for a whole reindex again for nothing but names.

The resolution is to couple one small artefact, not the whole ADR: FRE-1036 needs only this ADR's **rename table**, a days-scale deliverable. The deadline-driven work — lifecycle policy, rollover alias, killing daily indices — touches no field names and must not wait. If the table is late, the fallback is a second reindex: expensive in machine time, cheap in risk, on 635 MB total.

Retention is itself part of the migration path. The owner has ruled that deleting indices older than May 2026 is acceptable — *"we are still littering, the foundations settle"*. Anything retention deletes needs no rename at all. Measured, the deletion curve is:

| Delete through | Indices removed | Remaining |
|---|---|---|
| pre-May 2026 | 61 (11%), 73 MB | 471 |
| through May 2026 | 171 (32%) | 361 |
| through June 2026 | 314 (59%) | 218 |

(Retention depth is FRE-1036's decision, not this ADR's; it is recorded here because it sets how much data the rename must touch.)

---

## Decision

### D1 — OpenTelemetry is the vocabulary. This is adopted, not re-litigated

Field names come from OpenTelemetry semantic conventions where a convention exists (`gen_ai.operation.name`, `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`), and from a clearly-namespaced project key where none does. This is **ADR-0093 D1/D2 restated, not a new choice** — that ADR is Accepted and its selection stands.

ADR-0093 D5 governs stability: pin a named semconv version and record it; any attribute not yet stable rides under a namespaced project key until it stabilises. The events area (including `event.name`, below) is explicitly evolving and is therefore governed by that rule.

Elastic Common Schema is **not** adopted as a separate standard. ECS was donated to OpenTelemetry in April 2023 and the two are converging by design — deliberately not merged, and convergence is acknowledged as unachievable in some areas. Adopting ECS now would re-open a settled decision in order to pick the side being merged into the other.

### D2 — `@timestamp` is the record timestamp, stored concretely, in every family. No aliases

Every governed family stores its record timestamp as a concrete `@timestamp` field. `timestamp`, `started_at`, `probed_at`, `rated_at` and `ts` are retired.

This is **not** a deviation from D1. OpenTelemetry's `Timestamp` is a field on the log-record *envelope*, not a semantic-convention attribute — semconv does not name a stored document's date field — and every OpenTelemetry-to-Elasticsearch serialization writes it as `@timestamp`. The choice is therefore conformant, and 98.8% of the corpus already satisfies it.

**No field aliases are added for any governed field.** An alias was tested end to end and does work for queries, sorting, aggregations and existence checks, but it fails for `_source` filtering — which is how the skill documents retrieve fields, so the alias would preserve the exact silent-empty failure this ADR exists to eliminate — and it rejects writes. Decisively, it is a *permanent* addition: Elasticsearch mappings are append-only, so an alias added to an index can never be removed. Buying that permanence to avoid reindexing 1.1% of the corpus is a workaround becoming technical debt.

So that this is a principle rather than a one-off veto, an alias is justified only when **all four** of these hold. A field must be:

1. universal across every family (so it is not a per-family plaster);
2. read-only in practice, so write-rejection costs nothing;
3. already dominant in one spelling, so no vocabulary argument is being suppressed;
4. divergent only in history that will not be rewritten.

The timestamp passes (1)–(3) and fails (4), because the history is 36,558 documents and will be rewritten. `role` / `purpose` fails (2) and (3) — it is two vocabularies, not two spellings, and an alias would map a name onto data that does not exist. `event` / `event_type` fails (4) — it is a live one-line rename, not history.

### D3 — The governed surface is the shared spine, not every field

A field is **governed** by this convention when any of these hold:

- it appears in two or more families (59 fields today), **or**
- it is a join key (`trace_id`, `session_id`, `span_id`, `task_id`, `user_id`, any `*_id`), **or**
- it is trap-class per ADR-0090 D2 (numeric/float/ratio/cost/duration, or long-text/error/digest).

Everything else — the 175 field names appearing in exactly one family — stays declared in that family's own template and is **not** governed. This keeps the convention roughly sixty declarations rather than 234, and keeps family authors free where freedom costs nothing.

Two governed names are settled here, closing ADR-0090's open decisions:

- **Event key:** the canonical stored key is `event.name` (OTel), mapped once at the emit seam. structlog's in-process `event` key is unchanged — it is structlog's message field — and `es_logger.py` performs the single translation. The dual-key fallback logic is then deleted, not retained. Because the OTel events area is still evolving, this name is subject to D1's version-pin rule.
- **Work taxonomy:** the Postgres `purpose` vocabulary is canonical, because it expresses distinctions Elasticsearch's `role` cannot (FRE-1037 is widening the enum to carry them). Elasticsearch adopts that vocabulary; the two stop being separate taxonomies.

### D4 — Mandatory means *present*, never *absent*. Telemetry is never dropped for non-conformance

A governed identity field is mandatory on every emitted record. Mandatory means the field is **present with an explicit value** — never omitted. Where no real value exists, an explicit sentinel is written; `telemetry/trace.py` already carries the shape for this in its user-versus-`system:<source>` split. There is no such thing as "no trace"; there is a system trace.

This is deliberately **not** fail-closed-by-rejection. With `trace_id` present on 11.4% of `agent-logs` documents today, dropping non-conforming records would delete most of our telemetry, and would delete it hardest exactly when something is broken and no request context exists — scheduler ticks, startup, module-level logs. Worse, `slm_server`'s shipper is fail-soft by design and would discard rejected documents in silence.

Instead, a record that cannot be made conformant is **indexed and tagged**: it carries `telemetry.convention_violation` naming the offending field, so violations are loud, queryable and countable rather than silent or lost. The invariant this buys is the one that matters for querying: **absence stops being representable**, so the joinability probe (ADR-0074) can be upgraded from "does the field exist" to "is this identity real or a sentinel" — a question actually worth measuring.

### D5 — Enforcement lives in two tiers, because one tier cannot reach every producer

- **Tier 1 — the typed envelope, for producers we own.** A shared field-set module that emit sites import, generalising `CANONICAL_MODEL_CALL_*_FIELDS` from one event family to the governed spine, with the existing parity-test pattern as the gate. This tier already exists in miniature in both repositories; it needs pointing at a shared list, not inventing. It fails at development time, where a mistake is cheapest.
- **Tier 2 — the substrate boundary, for every producer including ones we do not own.** An ingest pipeline attached to each family's index template via `settings.index.default_pipeline`, which normalises names **as the document is written** (`ts` → `@timestamp`, `prompt_tokens` → the canonical token field, and so on) and stamps `telemetry.convention_violation` where it cannot.

Tier 2 is categorically different from the alias rejected in D2, and the difference is exactly the technical-debt test: an alias patches the read path and leaves heterogeneous bytes on disk permanently; **a pipeline rewrites the document so the stored bytes are canonical**, and once every producer conforms at source the pipeline is deleted and nothing changes. It is a ratchet, not a plaster.

**Tier 2 carries an explicit exit condition** (see AC-9): each normalisation rule is removed once its producer is fixed at source. A pipeline rule that outlives its producer's fix becomes the debt it was built to prevent.

### D6 — The registry is the committed destination; templates are generated, not hand-written

One declaration — name, type, required-or-optional, owning families — for each governed field, from which **both** the Tier-1 envelope field sets **and** the Elasticsearch template `properties` are generated. CI diffs generated output against committed templates; drift becomes a build failure rather than a discovery, and a hand-edit to a generated template is a job that failed to run.

This closes ADR-0090's deferred *"declared field registry"* open decision. It is sequenced **after** Tier 1 rather than before: the envelope delivers binding behaviour in days and is the registry's first half, while the registry is the larger lift that this project has now deferred twice. Committing to it as the destination — with the envelope as its first phase — is what prevents a third deferral.

Family-private fields (D3) stay hand-declared in their own templates and are outside the generator.

### D7 — Migration is reindex plus retention. There is no alias tier and no read-time shim

- Anything retention deletes needs no rename; retention depth is FRE-1036's decision.
- Whatever survives is reindexed through a rename pipeline — 36,558 documents at most, seconds of machine time.
- This ADR's first deliverable is the **rename table** (old name → canonical name, per family), published early enough for FRE-1036's reindex to consume as a pipeline. It is not gated on the rest of this ADR.
- Per project history — *"you always get the mappings wrong first pass"* — every reindex verifies field counts and mappings afterwards rather than assuming them (ADR-0090's 300-field-cap and dynamic-mapping traps apply unchanged).

### D8 — Out-of-repo producers are in scope; `slm_server` is the first, and it blocks FRE-1036

`slm_server` requires three changes in its own repository, filed as its own ticket:

1. rename `ts` → `@timestamp`;
2. adopt the canonical token field names in its `request_complete` builder;
3. **POST to the write alias (`slm-requests`) instead of a client-formatted `slm-requests-{YYYY.MM.DD}`.**

Item 3 is a one-line change and is the one Tier 2 cannot cover, because a pipeline can rewrite a document's contents but not the index its author chose. **It is a genuine dependency of FRE-1036**: without it, consolidation completes while one family continues minting a shard a day, and the shard-ceiling metric will not improve as predicted.

Until those land, Tier 2 normalises `slm-requests` documents on write, so the naming convention binds that producer today without waiting on another repository's release cycle.

---

## Alternatives Considered

### Option 1: Field aliases on the divergent families

**Description:** Add an Elasticsearch `alias`-type field so `@timestamp` resolves on families storing `timestamp` / `started_at` / `probed_at` / `rated_at` / `ts`, leaving stored data unchanged.

**Pros:**
- No reindex, no producer changes, no coordination.
- Verified end to end by the owner: works for queries, sorting, aggregations and existence checks.
- Immediate — the read surface unifies the moment the mapping is applied.

**Cons:**
- **Does not resolve in `_source` filtering**, which is how the skill documents retrieve fields — so the silent-empty failure mode this ADR exists to kill survives in the retrieval path, while *appearing* fixed.
- Rejects writes, so new data still has to be correct anyway.
- Permanent: mappings are append-only, so every alias is a forever-entry in the mapping.
- Unifies the read surface while leaving the data heterogeneous; does nothing for the other substrates, and must be added per family per field.

**Why Rejected:** The migration it avoids is 36,558 documents — about 1.1% of the corpus and seconds of reindexing. Paying a permanent mapping entry, and keeping the retrieval path broken, to avoid a one-minute job is the definition of a workaround becoming technical debt. D2 records the four-test criterion under which an alias *would* be justified, so this is a reasoned boundary rather than a blanket prohibition.

### Option 2: Adopt Elastic Common Schema instead of OpenTelemetry

**Description:** Standardise on ECS, which exists for precisely this problem and whose `@timestamp` the highest-volume family already matches.

**Pros:**
- Purpose-built for Elasticsearch; `@timestamp`, `event.*`, `error.*` land natively.
- Removes bikeshedding — a published catalogue gives every new field an obvious home.

**Cons:**
- Re-opens a settled decision: ADR-0093 is Accepted and chose OpenTelemetry.
- ECS was donated to OpenTelemetry in April 2023 and is converging into it; adopting ECS selects the side being merged into the other.
- ECS says nothing about the model-call attributes (`gen_ai.*`) that dominate our highest-value telemetry.

**Why Rejected:** The two are converging, not competing, so the choice is far less consequential than FRE-1038 assumed — and the one thing that would be consequential is re-litigating an Accepted ADR to arrive somewhere adjacent. D1 adopts OTel and takes `@timestamp` on its own merits.

### Option 3: A documented convention with no enforcement mechanism

**Description:** Publish the vocabulary in `docs/reference/` and the skill documents; rely on review to uphold it.

**Pros:**
- Cheapest by a wide margin; ships in a day.
- No runtime surface, no generator, no pipeline to operate.

**Cons:**
- It is what has already been tried, four times.
- The skill documents have twice encoded a field-level fact that was wrong and been believed — they are not a reliable enforcement surface.

**Why Rejected:** ADR-0004 set the model, ADR-0068 diagnosed the divergence and filed three tickets (all still open), ADR-0090 deferred the registry, ADR-0093 chose a standard whose realization ticket was never approved. Four correct diagnoses, zero bytes changed. A fifth document is the single most likely failure mode of this ADR, and the mechanism in D5/D6 exists specifically to avoid it.

### Option 4: Registry first — generate everything before shipping anything

**Description:** Build the declaration and generator up front; derive envelope and templates from it in one change.

**Pros:**
- One mechanism, no interim state, no half-migrated vocabulary.
- Drift becomes impossible immediately rather than after a phase.

**Cons:**
- The largest lift on the table, and this project has deferred exactly this twice.
- Delivers nothing binding until it is entirely finished.

**Why Rejected:** Sequencing only — D6 commits to the registry as the destination. The envelope is the registry's first half, delivers binding behaviour in days, and creates the field list the generator will consume. Deferring *all* value behind the biggest component is how the previous two deferrals happened.

### Option 5: Fail closed — reject non-conforming records at the substrate

**Description:** Have the ingest pipeline reject any document missing a mandatory field, forcing producers to conform.

**Pros:**
- Strongest possible guarantee: everything stored is conformant, by construction.
- No violation-tagging machinery, no tolerated bad state.

**Cons:**
- With `trace_id` at 11.4% presence, it would discard most telemetry on day one.
- It discards hardest exactly when the system is broken and no request context exists.
- `slm_server`'s shipper is fail-soft and would drop rejections silently, on another machine.

**Why Rejected:** Telemetry that is deleted for being malformed cannot tell you why it was malformed. D4 takes tag-don't-drop instead, which preserves the signal and makes violations countable — a stronger position for diagnosis and a strictly larger dataset.

---

## Consequences

### Positive Consequences

- **One query answers a question across every family.** A single time filter over the telemetry surface stops silently missing families, which is the failure that generated this ADR.
- **The silent-empty class shrinks structurally**, rather than per-incident: a wrong name is caught at development time (Tier 1), corrected at write time (Tier 2), or surfaced as a violation count (D4) — three chances where there were none.
- **The convention binds producers we do not own**, today, without waiting on another repository — and binds future producers by default.
- **Two long-open defects close as by-products**: ADR-0068's `prompt_tokens` / `input_tokens` split (FRE-351/353, open fourteen weeks) and ADR-0090's `event` / `event_type` open decision.
- **ADR-0090's deferred field registry gets an owner and a phase** instead of a third deferral.
- **Absence becomes unrepresentable for identity fields**, upgrading the joinability probe from an existence check to a real-versus-sentinel measurement.
- **FRE-1036 gains a dependency it did not know it had** — `slm_server`'s client-side index naming — before it reports success against a metric that would not have moved.

### Negative Consequences

- **A generator and a pipeline to operate.** The registry (D6) is real build and maintenance surface, and hand-editing a generated template becomes forbidden — a workflow change for anyone used to editing template JSON directly.
- **Tier 2 is a translation layer with a lifetime.** If its rules are not removed as producers are fixed, it silently becomes permanent — the exact debt it was built to prevent. AC-9 exists to make that visible, but it depends on someone acting on it.
- **Dotted OpenTelemetry names nest as objects in Elasticsearch**, so `gen_ai.usage.input_tokens` changes query syntax in skill documents and dashboards. Every consumer of those fields needs updating in step.
- **Cross-repository coordination.** `slm_server` has its own approval and release cycle on a separate machine; item 3 of D8 gates part of FRE-1036's claimed outcome.
- **Reindex risk on a project with a documented history of first-pass mapping errors** — mitigated by D7's post-reindex verification, not eliminated.
- **Interim heterogeneity.** Between the rename table and the last producer fix, some families are canonical at source and others canonical only after the pipeline. Queries work throughout; the *stored* shape converges family by family.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| This becomes the fifth diagnosis that changes no bytes | **High** | D5 Tier 1 ships a binding mechanism before any registry work; AC-5 makes drift a build failure; every AC below is an outcome check, not a merge check |
| Tier 2 pipeline outlives its producers and becomes permanent | Medium | AC-9 counts live normalisation rules against unfixed producers; each rule names the ticket that retires it |
| Rename table lands after FRE-1036's reindex window | Medium | D7 makes the table a standalone first deliverable, decoupled from the rest of the ADR; fallback is a second reindex, cheap in risk on 635 MB |
| Reindex loses or mistypes fields (project history) | Medium | D7 mandates post-reindex field-count and mapping verification; ADR-0090 trap classes apply |
| `slm_server` change does not land, FRE-1036 reports false success | Medium | D8 names it an explicit FRE-1036 dependency; AC-8 measures index-creation rate, not merge status |
| Violation tagging becomes noise nobody reads | Low | AC-4 requires the violation count to be queryable and to fall over time, not merely to exist |

---

## Implementation Notes

**Files affected (this repository):**

- `src/personal_agent/telemetry/events.py` — generalise `CANONICAL_MODEL_CALL_*_FIELDS` into the governed-spine field module (Tier 1).
- `src/personal_agent/telemetry/es_logger.py:167`, `es_handler.py:121` — single `event` → `event.name` translation at the seam; delete dual-key fallback.
- `docker/elasticsearch/*.json` (14 templates) — canonical `@timestamp`, governed properties, `default_pipeline` setting.
- `scripts/setup-elasticsearch.sh` — apply ingest pipelines alongside templates (the single sanctioned mapping path, ADR-0090 D2).
- Ingest pipeline definitions — new, one per family needing normalisation.
- `config/kibana/dashboards/*.ndjson`, `docs/skills/*.md` — consumers of renamed fields.

**Files affected (`slm_server`, separate repository, separate ticket):** `src/slm_server/router.py` (builder field names), `src/slm_server/telemetry.py:38` (write alias).

**Migration steps:** publish rename table → FRE-1036 retention deletes what it deletes → reindex survivors through the rename pipeline → verify field counts and mappings → Tier 1 envelope → Tier 2 pipelines → `slm_server` source fixes → retire pipeline rules → registry and generated templates.

**Dependencies:** FRE-1036 (index consolidation — consumes the rename table; depends on D8 item 3), FRE-1037 (role enum widening — supplies the `purpose` vocabulary D3 makes canonical), FRE-1035 (field-existence check — the safety net for whatever the convention does not cover), FRE-583 (ADR-0093 D1/D2, currently unapproved — this ADR's D1 depends on it being dispatched or absorbed).

---

## Verification / Acceptance Criteria

- **AC-1 — One time-filtered query returns rows from every governed family.** · **Check:** a single `_search` across `agent-*,slm-requests-*,user-turn-ratings-*` with a `range` filter on `@timestamp` and a `terms` aggregation on `_index`, over a window in which every family is known to have written. · *Fails if* any governed family returns zero buckets — which is exactly what happens if one is left on `timestamp`, `ts`, `started_at`, `probed_at` or `rated_at`.

- **AC-2 — A document from a producer we do not own is stored canonically.** · **Check:** POST a document carrying `ts` and `prompt_tokens` to the `slm-requests` write path, then `GET` it back and inspect `_source`. · *Fails if* `_source` still contains `ts`, or lacks `@timestamp`. An alias-based implementation fails this by construction, since aliases do not appear in `_source` — this criterion is specifically chosen to discriminate against the rejected alternative.

- **AC-3 — Identity is present on every record, and the corpus did not shrink to achieve it.** · **Check:** two counts over the same post-change window on `agent-logs-*`: (a) `exists` on `trace_id` as a share of total documents, and (b) total document count compared against the pre-change daily baseline (~3.2M documents; today's presence is 11.4%). · *Fails if* presence is below 100%, **or** if total document volume falls more than 5% against baseline — the second half is what stops "conformance" being achieved by discarding non-conforming records.

- **AC-4 — A non-conformant record is loud, not silent and not lost.** · **Check:** write a record missing a mandatory identity field; confirm it is indexed, carries `telemetry.convention_violation` naming that field, and is returned by a violation query; then confirm the violation count over a rolling window declines as producers are fixed. · *Fails if* the record is rejected (violating AC-3), indexed without the tag, or the violation count is static — a tag nobody drives to zero is decoration.

- **AC-5 — Naming drift fails the build.** · **Check:** rename a governed field in a committed template on a scratch branch and run CI. · *Fails if* CI passes, or fails without naming the offending field and family. A report-only checker fails this criterion deliberately — ADR-0090's D5 shipped report-only and the baseline it was meant to burn down is still present.

- **AC-6 — No field aliases exist for governed fields.** · **Check:** `GET /<governed families>/_mapping` filtered for properties of `"type": "alias"`. · *Fails if* any governed field resolves through an alias — this asserts the rejected alternative was not quietly reintroduced as a shortcut during migration.

- **AC-7 — No governed family carries a retired date spelling.** · **Check:** for each governed family, intersect the mapping's `date`-typed properties with `{timestamp, ts, started_at, probed_at, rated_at}`. · *Fails if* the intersection is non-empty for any family that survived retention.

- **AC-8 — `slm-requests` stops creating an index per day.** · **Check:** count `slm-requests-*` indices on two dates at least seven days apart, after D8 item 3 ships. · *Fails if* the count grows by roughly one per day — measuring the actual index-creation behaviour, not whether the `slm_server` PR merged. This is also the criterion that protects FRE-1036 from reporting a shard-ceiling improvement it did not fully deliver.

- **AC-9 — The Tier-2 pipeline shrinks as producers are fixed.** · **Check:** count active normalisation rules across all ingest pipelines, and cross-reference each against its producer's fix ticket. · *Fails if* a rule remains live after its producer has been fixed at source — the translation layer outliving its purpose is precisely the debt this ADR refuses elsewhere.

- **AC-10 — Both prior open items are closed in code, not in prose.** · **Check:** `grep` for dual-key `event` / `event_type` fallback logic in `src/`; and confirm the Elasticsearch work-taxonomy field carries the Postgres `purpose` vocabulary values, not only `primary`. · *Fails if* the fallback still exists, or if the taxonomy field still reports a single value for the overwhelming majority of calls (93% today).

**Seam owner:** the **`/adr` session that authored this ADR** owns the assembled-intent criterion. AC-1, AC-6 and AC-9 hold only once *every* child ticket has landed — no individual child can prove them — so this ADR does not close when its last child merges. Master's acceptance gate asserts AC-1 across all families, AC-6 across all mappings, and AC-9 against the pipeline, before ADR-0128 moves to Implemented.

---

## References

- ADR-0004 — Telemetry & Metrics Implementation Strategy (Accepted): set the telemetry model; left the field vocabulary unspecified
- ADR-0068 — Agent Self-Telemetry Data Plane (Accepted): diagnosed the `prompt_tokens` / `input_tokens` divergence on 2026-05-10; FRE-351 / FRE-352 / FRE-353 still open
- ADR-0074 — End-to-End Traceability & Joinability (Accepted): the identity tuple and join-key discipline this ADR makes mandatory-and-present
- ADR-0090 — Telemetry Surface Contract (Accepted): the emit ↔ mapping ↔ dashboard surface; this ADR closes its deferred field-registry and `event` / `event_type` open decisions
- ADR-0093 — OpenTelemetry at the Substrate Boundary (Accepted, with scope change): the standard this ADR adopts rather than re-chooses; FRE-583 unapproved since 2026-06-21 is its unrealized half
- `src/personal_agent/telemetry/events.py:54-67` — `CANONICAL_MODEL_CALL_*_FIELDS`, the Tier-1 pattern in miniature
- `src/personal_agent/telemetry/es_handler.py:121`, `src/personal_agent/telemetry/es_logger.py:167` — the single `event` → `event_type` rename seam
- `slm_server` `src/slm_server/router.py` (four emit paths; shared `request_complete` builder), `src/slm_server/telemetry.py:38` (client-formatted daily index) — verified against `origin/main`, 2026-07-29
- Linear FRE-1038 — this ADR's originating ticket
- Linear FRE-1036 — index consolidation (consumes D7's rename table; gated on D8 item 3)
- Linear FRE-1037 — role-enum widening, supplying the canonical `purpose` vocabulary
- Linear FRE-1035 — skills field-resolution check, the safety net beneath this convention
- [OpenTelemetry / ECS semantic-convention convergence announcement](https://opentelemetry.io/blog/2023/ecs-otel-semconv-convergence/)
- [Elastic — ECS contributed to OpenTelemetry, FAQ](https://www.elastic.co/blog/ecs-elastic-common-schema-otel-opentelemetry-faq)
- [Elasticsearch — `alias` field type and its limitations](https://www.elastic.co/docs/reference/elasticsearch/mapping-reference/field-alias)
- [OpenTelemetry — event attributes registry (`event.name`)](https://opentelemetry.io/docs/specs/semconv/registry/attributes/event/)
- [OpenTelemetry — logs data model](https://opentelemetry.io/docs/specs/otel/logs/data-model/)

---

## Status Updates

### 2026-07-29 — Proposed
**Changed By:** `/adr` session (FRE-1038)
**Reason:** Owner-directed. Field aliases were tested and rejected as a quick fix in favour of deep uniformity; measurement during authoring established that the migration is 1.1% of the corpus, that both major producers already run internally-consistent emit envelopes with no shared vocabulary between them, and that `slm_server`'s client-side index naming is an unrecognised dependency of FRE-1036.
