# ADR-0133: The Typed Emit Envelope for the Residual Log Corpus — Enforcement by Declared Retired Spelling, and the Field Registry Declined

**Status:** Proposed
**Date:** 2026-08-06
**Deciders:** Project owner (FRE-1113, owner-directed 2026-08-06)
**Tags:** telemetry, observability, naming-convention, elasticsearch, enforcement, structlog

---

## Context

### What is being decided

ADR-0129 replaced ADR-0128's two enforcement tiers "with a weaker guarantee, stated honestly," abandoning both — the typed exclusive envelope and the ingest-pipeline normalisation — in exchange for identity that propagates. This ADR decides whether the first of those two is restored, on what surface, and by what mechanism; whether the second is restored at all; and it settles the registry question that has now been deferred three times.

It does not re-open the vocabulary. OpenTelemetry semantic conventions remain the naming standard (ADR-0093 D1/D2, restated as ADR-0128 D1 and ADR-0129 D2), and nothing here changes what a name *should* be. This ADR is about what happens when a producer writes a different one.

### The measurement that reframes the question

The obvious objection to restoring the envelope is that the ADR-0129 chain is converting the interesting telemetry to spans, leaving the envelope to govern scraps. Measured live on 2026-08-06 over `agent-logs-*`, 7-day window:

| | Documents | Share |
|---|---|---|
| **Total** | **495,375** | 100% |
| Span-bound events — everything the ADR-0129 chain converts (`model_call_*`, `tool_call_*`, `tool_execution_completed`, `step_planning_*`, `llm_step_completed`) | 1,793 | **0.36%** |
| **Residual log corpus — what remains a log record after the chain lands** | **493,582** | **99.64%** |

The scraps are the corpus. The ten highest-volume event types in that window are, in order: `budget_counter_snapshot` (126,096), `event_processed` (117,201), `event_published` (117,071), `cost_gate_reaper_swept` (21,061), `sensor_poll` (20,823), `mode_controller_evaluated` (20,630), `metrics.sampled` (19,264), `budget_counter_snapshot_emitted` (10,507), `consolidation_skipped_already_consolidated` (3,511), `slm_health_probe_completed` (2,300). Not one of them becomes a span under ADR-0129.

This is not an argument that the residual corpus is *more valuable* than traces. It is the observation that ADR-0129's own supersession table leaves it **entirely ungoverned** — *"outside the spine, log-record field naming becomes ungoverned — a real regression against ADR-0128's intent"* — and that "ungoverned" now describes 99.64% of the documents rather than a remainder.

### The seed the envelope was going to grow from is being removed

FRE-1044 (ADR-0128 A2) scopes tier one as *"generalizes the existing canonical model-call field sets"* — `CANONICAL_MODEL_CALL_STARTED_FIELDS` and `CANONICAL_MODEL_CALL_COMPLETED_FIELDS` at `src/personal_agent/telemetry/events.py:55-88`.

FRE-1067 (ADR-0129 B3, `Approved` 2026-08-06) retires them: its scope re-points `tests/personal_agent/llm_client/test_telemetry_parity.py` *"from frozen field-name sets to span attribute conformance,"* and its AC-10 removes `RequestTimer` outright. So the ticket that was going to generalise those sets is second in a queue whose head deletes them.

Tier one therefore cannot be restored as written. It has to be **re-founded** on the residual corpus rather than on the model-call spine, which is what this ADR does.

### What the existing mechanism actually guarantees, and what it does not

`CANONICAL_MODEL_CALL_*_FIELDS` are `frozenset[str]` — bare name sets — and the parity test asserts only that required keys are present (`tests/personal_agent/llm_client/test_telemetry_parity.py:100,130`). No value types, no rejection of extra keys. ADR-0128 D5 recorded this honestly and decided the upgrade; the upgrade was never built. Verified 2026-08-06: no OpenTelemetry package appears in `pyproject.toml`, `grep -rn "gen_ai" src/ docker/ config/` returns nothing, no template in `docker/elasticsearch/` carries `default_pipeline`, and the live cluster holds no ingest pipeline of ours (only Elastic's built-in `logs-apm.*`).

### The emit surface is free-form by construction, and that is not a defect to fix

`src/` contains **551 `log.info(...)` call sites** alone (ast-grep census, 2026-08-06), each passing arbitrary keyword payload. Across 15 committed templates there are **237 distinct field names, of which 178 appear in exactly one family and 59 cross families** — figures materially unchanged from ADR-0128's census (234 / 175 / 59), so the shape is stable, not drifting.

The live mapping is wider still: **716 mapped field names across `agent-logs-*`**. ADR-0128 D3 deliberately left the 178 family-private names ungoverned and granted naming governance *no presence obligation*. That freedom is the design, and any mechanism that revokes it is rejecting the emit surface rather than governing it.

### The failure class, measured rather than imagined

ADR-0128 D5 illustrates the envelope's value with *"a misspelled identifier sitting alongside a correct one."* That is a real risk but it is **not the failure this project has actually paid for**. Every divergence in the record is two producers independently choosing different words for one concept:

| Divergence | Recorded in | String similarity |
|---|---|---|
| `duration_ms` vs `latency_ms` — disjoint populations, intersection measured at 0 | ADR-0129 Context | **0.57** |
| `prompt_tokens` vs `input_tokens` | ADR-0068, 2026-05-10 | **0.72** |
| `completion_tokens` vs `output_tokens` | ADR-0068, 2026-05-10 | **0.67** |
| `event` vs `event_type` | ADR-0090 open decisions | **0.67** |
| `ts` vs `@timestamp` | ADR-0128 D2 census | **0.33** |
| *(illustrative typo)* `trace_id` vs `tarce_id` | — | 0.88 |

(Ratios are `difflib.SequenceMatcher`, computed 2026-08-06.)

This table is load-bearing and it falsifies the intuitive mechanism. **A near-miss detector tuned to catch `tarce_id` at 0.88 catches none of the five divergences that actually happened.** Any threshold low enough to catch `ts`/`@timestamp` at 0.33 would flag most of the 716-name mapping.

A near-miss scan run over the live `agent-logs-*` mapping at a 0.82 threshold returns exactly two hits, **both legitimate**: `max_latency_ms` (a genuinely different measure) and `component` (a real name-versus-id pair). So string similarity is simultaneously too weak for the observed failure and productive of false positives on the observed corpus.

### Why the ingest pipeline no longer has a case

FRE-1113 argued tier two was the near-term answer to FRE-1109's seventeen conflicting field types. Two facts closed that argument, in this order:

1. **2026-08-04 (owner ruling):** application-log history is no longer maintained. FRE-1036 closed by deleting the out-of-policy indices rather than migrating them. The sixty-one legacy indices tier two was going to normalise no longer exist.
2. **2026-08-04:** FRE-1109 reached `Done`, arbitrating the collision by pinning types forward-only in the template — the option FRE-1113's body argued against, shipped at Tier-3 cost.

The general argument survives — an ingest pipeline still generalises to the other sixteen fields where a template pin does not — but it has no forcing function, no corpus to repair, and its motivating ticket is closed.

### The registry contradiction that forces a ruling

FRE-1067 is `Approved` and carries **AC-12**: *"No field registry is introduced. Proven by: the diff adding no generated field registry, no per-field type declaration file and no template-generation step."* FRE-1048 (ADR-0128 A6) is the field registry.

Because AC-12 is scoped to FRE-1067's own diff — as ADR-0130 D6 requires of an implementation criterion — the two cannot literally fail each other, and a later registry would not retroactively break FRE-1067. The contradiction is one of recorded intent rather than of tests, and it is not resolvable by whichever ticket lands first.

There is a second, sharper ambiguity in the same sentence. Read narrowly, *"no per-field type declaration file"* forbids the registry. Read broadly, it forbids **any** file declaring a type per telemetry field — which would include this ADR's vocabulary module and would silently kill the thing FRE-1113 exists to restore. A build session picking up FRE-1067 has no ruling to consult. This ADR supplies one.

---

## Decision

### D1 — Tier 1 is restored, scoped to the residual log corpus, and re-founded

The typed emit envelope is built. Its governed surface is **Elasticsearch log records that do not become spans** — 99.64% of the corpus as measured above — and it is founded on a declared vocabulary rather than on `CANONICAL_MODEL_CALL_*_FIELDS`, which FRE-1067 retires.

Span attributes are **out of scope**. They are governed by semantic conventions and asserted by FRE-1067's AC-6, AC-7 and AC-8. This ADR governs the log path only, and the two mechanisms meet at no point — which is why restoring tier one contradicts nothing in ADR-0129 and requires no amendment to its D8 table beyond the row this ADR's Status Update records.

### D2 — Enforcement lives at the single emit seam, never at 551 call sites

The envelope is a **validating structlog processor**, registered in the same `structlog.configure` pipeline at `src/personal_agent/telemetry/logger.py:232` that FRE-1064 uses for the span-context processor. It validates the assembled record dict immediately before it reaches `es_logger`.

This is decided here rather than left to implementation because the intuitive design — a frozen dataclass constructed at each emit site — is unbuildable and would contradict a funded ticket. There are 551 `log.info` sites alone, and FRE-1064's **AC-4 forbids editing emit sites** to achieve identity injection (*"that would mean identity is still being supplied by hand, which is the practice this ADR exists to end"*). A record-construction envelope would demand exactly that edit, at every site, for the same reason ADR-0129 rejected it.

Enforcement at the seam also makes coverage a property of the pipeline rather than of author discipline: a new emit site is governed the moment it is written, with nothing to remember.

### D3 — What the envelope enforces: three rules, in priority order

**Rule 1 — Declared retired spellings are rejected, by exact match.** The vocabulary declares, for each governed name, the spellings it retires. `latency_ms` and `duration_ms` are retired in favour of intrinsic span duration; `prompt_tokens` and `completion_tokens` in favour of `input_tokens` / `output_tokens`; `ts`, `timestamp`, `started_at`, `probed_at` and `rated_at` in favour of `@timestamp`. A record carrying a retired spelling fails.

This is the rule that carries the decision. It is exact, so it produces no false positives, and per the Context table it catches **all five** divergences this project has actually paid for — every one of which a similarity threshold misses.

**Rule 2 — Near-miss of a governed name is rejected, with a declared exception list.** A key that is not itself governed, is not family-private-and-declared, and exceeds the stated similarity threshold against a governed name, fails. This catches the typo class (`tarce_id` at 0.88, `sesion_id` at 0.95, `trace_ids` at 0.94) that Rule 1 cannot see.

The exception list is closed and each entry states why. It opens with the two legitimate hits measured on the live mapping: `max_latency_ms` and `component`. An exception without a stated reason is a defect, not a configuration.

**Rule 3 — Governed names carry their declared type.** A governed name whose value does not match its declared type fails. This is the rule that would have caught FRE-1107, where the capture template mapped `threshold_violations` as `integer` while the producer wrote a list of strings and Elasticsearch rejected every affected document whole.

**What is deliberately not enforced.** There is no presence obligation — a family that has no `component_id` writes none, exactly as ADR-0128 D3 decided. There is no rejection of unrecognised keys: a genuinely-new key such as `queue_depth` passes, and the 178 family-private names keep the freedom D3 granted them. ADR-0128 D5's word "exclusivity" is therefore **not** delivered as written, and this ADR says so plainly rather than reusing the word for something weaker.

### D4 — It fails at development time, and never drops telemetry in production

The processor's behaviour is split by environment, and the split is the whole point:

- **Under test and in CI, a violation raises.** This is where the guarantee lives — ADR-0128 D5's real promise was *"it fails at development time, where a mistake is cheapest,"* and that is the property being restored.
- **In production, a violation never drops, rejects or mutates the record.** The record is indexed as emitted, and the violation increments a counter published through the existing joinability monitor (`observability/joinability/`), which already runs on a schedule and already writes a health family.

Telemetry that is deleted for being malformed cannot tell you why it was malformed — ADR-0128 D4's reasoning, unchanged and re-affirmed. A validating processor that raised in production would take the service down on a telemetry defect, which is a strictly worse failure than the one it prevents.

### D5 — Tier 2, the substrate-boundary ingest pipeline, is dropped

It is not deferred, parked or left for later evidence. The corpus it was designed to normalise was deleted by owner ruling on 2026-08-04, and the ticket that justified it (FRE-1109) closed the same day by a different remedy. FRE-1045 (ADR-0128 A3) is closed rather than left in `Backlog` implying a plan.

If a future collision makes the case again, an ingest pipeline remains available and costs nothing to hold in reserve. What is rejected is building one **now**, against no measured need, on a surface where D1–D4 already prevent the class at emit.

### D6 — The field registry is declined, and ADR-0090's deferred question is answered *no*

No registry is built. No per-field declaration generates Elasticsearch templates. No CI job diffs generated output against committed templates. Templates stay hand-written, and can drift, exactly as ADR-0129 D8 recorded.

ADR-0090 listed *"a declared field registry — a typed catalog the emit sites and templates both derive from"* under its open decisions in June. ADR-0128 D6 committed to it; ADR-0129 D8 abandoned it; FRE-1048 has sat unbuilt throughout. **This ADR answers it in the negative, which closes it by decision rather than by a fourth deferral.** FRE-1048 is closed.

**FRE-1067's AC-12 is upheld in its narrow reading and rejected in its broad one.** What AC-12 forbids is a generated registry, a template-generation step, and CI drift-gating — all of which this ADR also forbids, permanently. What it must not be read as forbidding is D3's vocabulary module, which declares roughly sixty names, their retired spellings and their types, generates nothing, and is consumed by one processor. The distinction is generation: a registry is a **source** that other artifacts are derived from; the vocabulary is a **leaf** that only the validator reads.

Because a build session reading AC-12 cold could reasonably reach the broad reading, its wording needs reconciling before FRE-1067 builds. That is recorded as doc-drift in this ADR's handoff rather than actioned here.

### D7 — Sequencing: after B1, before or alongside B3, and never blocking the chain

The ADR-0129 chain is funded and sequenced, with FRE-1064 the labelled head. This work sequences behind it:

- **After FRE-1064 (B1).** The validating processor registers in the same structlog pipeline as the span-context processor. Landing first would mean writing that registration twice and would put a second change in front of the chain's own falsification gate.
- **The vocabulary must be declared before FRE-1067 (B3) merges**, because B3 deletes `CANONICAL_MODEL_CALL_*_FIELDS`. If the vocabulary does not exist by then, the project passes through a window with no name governance at all and the parity test's guarantee is lost rather than transferred.
- **This work never gates the chain.** If B1's falsification gate fails and master stops the chain, this ADR's children stop with it — the validating processor has no value on a corpus whose identity never improved, and it must not become the reason a falsified chain continues.

---

## Alternatives Considered

### Option 1: Build tier one exactly as ADR-0128 D5 wrote it — a typed record, exclusive on unknown keys

**Description:** A frozen dataclass or `TypedDict` whose construction is the only sanctioned way to build a telemetry record, rejecting any unrecognised key.

**Pros:**
- Strongest possible statement of intent: the record shape becomes closed, and nothing can enter it unnoticed.
- Exactly what FRE-1113 records the owner asking for, twice, and what FRE-1044 is already written to build.
- Value types and required-key presence come free with the type system rather than needing a validator.

**Cons:**
- **Rejects the emit surface rather than governing it.** ADR-0128 D3 deliberately left 178 family-private names ungoverned and granted no presence obligation; exclusivity revokes that in the same document that granted it, and the tension was never resolved.
- Requires editing 551 `log.info` call sites, and **directly contradicts FRE-1064's AC-4**, which forbids emit-site edits for precisely the reason ADR-0129 exists.
- Catches the typo class it was illustrated with, and — per the Context table — none of the five divergences actually recorded.

**Why Rejected:** It is unbuildable against a funded ticket, and the property it buys is not the property the failure history calls for. D2 and D3 keep the guarantee that mattered (development-time failure on a wrong name) and drop the word "exclusivity" rather than redefining it quietly.

### Option 2: Near-miss detection alone

**Description:** Reject any key within a similarity threshold of a governed name; pass everything else. This was the mechanism proposed to the owner during this ADR's design session, before it was tested.

**Pros:**
- Preserves the free-form payload surface completely — no key is forbidden for being new.
- Cheap: one similarity function and a threshold, no per-name declaration to maintain.
- Genuinely catches the misspelling class ADR-0128 D5 describes.

**Cons:**
- **It misses every measured divergence.** `duration_ms`/`latency_ms` at 0.57, `prompt_tokens`/`input_tokens` at 0.72, `event`/`event_type` at 0.67, `ts`/`@timestamp` at 0.33.
- A threshold low enough to catch `ts`/`@timestamp` would flag a large fraction of 716 live field names.
- Already produces false positives at 0.82 on the current mapping (`max_latency_ms`, `component`).

**Why Rejected:** On measurement taken during authoring. It is retained as **Rule 2** — it is the right tool for typos and the only tool for them — but it cannot be the primary rule, because the class it misses is the class this project has repeatedly paid for.

### Option 3: Restore tier two as well — the substrate-boundary ingest pipeline

**Description:** Attach an ingest pipeline to each family's template via `default_pipeline`, renaming per owning producer as documents are written, with `_meta` rule provenance and a `normalized_by` stamp.

**Pros:**
- Reaches producers we do not own, which no in-process mechanism can — the property ADR-0128 D5 built it for.
- Rewrites stored bytes rather than patching reads: a ratchet, not a plaster.
- Generalises to the other sixteen conflicting fields, where FRE-1109's template pin resolves exactly one.

**Cons:**
- Its corpus was deleted on 2026-08-04 by owner ruling; there is nothing left to normalise retrospectively.
- Its motivating ticket closed the same day by a cheaper remedy.
- `slm_server`, the out-of-repo producer that justified it, stops writing to Elasticsearch entirely under FRE-1071 — so the one producer beyond in-process reach leaves the surface.
- A pipeline whose rules are not retired as producers are fixed becomes the debt it was built to prevent (ADR-0128's own AC-9 risk).

**Why Rejected:** Every leg of its justification has been removed by events, and the last one leaves with FRE-1071. Building it now would be a mechanism in search of a defect.

### Option 4: Build the field registry (ADR-0128 D6 / FRE-1048)

**Description:** One declaration per governed field — name, type, required-or-optional, owning families — generating both the envelope's field sets and the Elasticsearch template properties, diff-gated in CI.

**Pros:**
- Makes template drift a build failure rather than a discovery, which no other mechanism on the board does.
- Closes ADR-0090's open decision affirmatively, and would have prevented FRE-1107's producer/template type disagreement structurally.
- Roughly sixty declarations, not 237 — smaller than it sounds.

**Cons:**
- Directly contradicts FRE-1067's AC-12, an approved criterion on a funded ticket.
- The largest lift on the table, and this project has deferred exactly this twice before deferring it a third time in ADR-0129 D8.
- Delivers nothing binding until it is entirely finished, which is how both previous deferrals happened.

**Why Rejected:** Owner ruling, 2026-08-06. Recorded here as a real cost rather than a clean win: template drift stays possible and ADR-0090's fourth corner stays open. D6 answers the question *no* so that it stops being re-asked, which is worth more than a fourth deferral that keeps the option nominally alive.

### Option 5: Do nothing — semantic conventions are the governance

**Description:** Accept ADR-0129's position unchanged. Semconv governs span attributes; log-record naming stays ungoverned.

**Pros:**
- Zero build, zero maintenance, zero new surface.
- Honest about what the SDK provides, which ADR-0129 D8 already is.
- Avoids a seventh telemetry ADR in a lineage where six changed nothing.

**Cons:**
- Leaves 99.64% of the corpus with no naming mechanism of any kind.
- Semconv is silent on Elasticsearch log-document field names by construction — ADR-0128 D2 established this for the timestamp and it holds generally.
- The divergence class recurs at emit and is discovered months later in the substrate, which is the entire measured history.

**Why Rejected:** It is the status quo, and the status quo's cost is documented across five ADRs. The distinguishing property of this decision is that its mechanism is one processor in a pipeline that another funded ticket is already registering a processor into — days of work, not a programme.

---

## Consequences

### Positive Consequences

- **The residual corpus acquires a naming mechanism for the first time** — 99.64% of documents, currently governed by nothing.
- **The measured divergence class becomes structurally impossible at emit.** All five recorded instances are exact-match retired spellings under Rule 1; a sixth of the same shape fails in CI.
- **Coverage is a property of the pipeline, not of authors.** A new emit site is governed the moment it is written, with nothing to remember — the same argument ADR-0129 makes for context propagation, applied to names.
- **A three-times-deferred question is answered.** ADR-0090's registry open decision closes *no*, by decision, and stops consuming review attention.
- **FRE-1067's AC-12 acquires a ruling** before a build session has to guess at it.
- **The parity test's guarantee transfers rather than evaporates** when FRE-1067 deletes `CANONICAL_MODEL_CALL_*_FIELDS`.
- **Nothing in ADR-0129 is amended.** The two mechanisms govern disjoint surfaces, so this is additive rather than a partial supersession of an Accepted ADR.

### Negative Consequences

- **"Exclusivity" is not delivered.** An unrecognised key still passes. A genuinely novel misspelling that is neither a declared retired spelling nor similar enough to trip Rule 2 is stored silently, exactly as today. ADR-0128 D5 promised more and this ADR does not.
- **The vocabulary is a maintained artifact.** Roughly sixty names, their retired spellings and their types, kept current by hand. A name added to a template and not to the vocabulary is ungoverned and nothing says so — which is the registry's diff-gate, declined in D6.
- **Rule 2 will produce false positives**, and its exception list will grow. Two are known today; more will surface, and each is a small tax paid at CI time.
- **Templates stay hand-written and can drift.** ADR-0090's mapping corner is unimproved, and FRE-1107's failure mode — producer and template disagreeing on type — is caught by Rule 3 at emit but not reconciled against the mapping.
- **Production violations are counted, not prevented.** D4's split means a wrong name still reaches Elasticsearch in production; only the development-time path fails. A violation counter nobody reads is a real risk, mitigated only by routing it through a monitor that already has a reader.
- **This is the seventh telemetry ADR**, in a lineage of six that changed nothing observable. Its defence is that its mechanism is small and lands inside a chain already funded — not that its diagnosis is better than its predecessors', which were correct.
- **Dropping tier two forecloses out-of-repo governance.** Should a second `slm_server`-shaped producer appear, nothing in this ADR reaches it, and D5's reasoning would need revisiting rather than extending.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| This becomes the seventh ADR that changes no bytes | **High** | The chain is three tickets landing inside a funded chain, and D7 sequences them against a head that is already labelled; AC-1 and AC-2 are outcome checks over production documents, not merge checks |
| The vocabulary rots — names added to templates, never declared | **High** | AC-6 measures declared-versus-live coverage over the governed families rather than trusting the artifact; the registry's diff-gate that would have prevented this structurally is declined in D6 and the gap is stated |
| Rule 2's exception list becomes a dumping ground that disables the rule | Medium | AC-5 requires a planted typo to still fail with the list in force, so an over-broad list fails the criterion rather than quietly passing |
| The processor raises in production and takes the service down | Medium | D4 makes the environment split a decision rather than a configuration; AC-3 asserts production volume is unaffected |
| B1's falsification gate fails and this work continues regardless | Medium | D7 binds this chain to the same gate — it stops when the chain stops |
| Violation counter is published and never read | Low | It rides the existing joinability monitor, which already has a reader and an escalation path; AC-4 measures the rate's trajectory, not the counter's existence |

---

## Implementation Notes

**Files affected:**

- `src/personal_agent/telemetry/vocabulary.py` — new. The declared governed names, their retired spellings, their types, and Rule 2's exception list. Generates nothing and is read only by the validator.
- `src/personal_agent/telemetry/logger.py:232` — register the validating processor in `structlog.configure`, after FRE-1064's span-context processor.
- `src/personal_agent/telemetry/events.py:55-88` — `CANONICAL_MODEL_CALL_*_FIELDS` retire here rather than simply being deleted by FRE-1067; their content moves into the vocabulary.
- `src/personal_agent/observability/joinability/` — publish the violation counter alongside the existing health output.
- `tests/personal_agent/telemetry/` — the planted-violation tests that make D4's development-time guarantee real.

**Migration steps:** declare the vocabulary (seeded from the 59 cross-family names and the five recorded divergences) → register the validating processor behind its development-time behaviour → publish the production counter → retire `CANONICAL_MODEL_CALL_*_FIELDS` into it as FRE-1067 lands.

**Dependencies:** FRE-1064 (ADR-0129 B1 — supplies the processor pipeline this registers into; `Approved`, `stream:build1`) · FRE-1067 (ADR-0129 B3 — deletes the canonical field sets, and carries the AC-12 wording D6 rules on; `Approved`) · FRE-1037 (role-enum widening, supplying the `purpose` vocabulary) · FRE-1045 and FRE-1048 are **closed** by D5 and D6 respectively.

**Testing strategy:** every rule is proven by a planted violation that must fail and a planted legitimate value that must pass — a rule proven only by the failing half can be satisfied by a validator that rejects everything.

---

## Verification / Acceptance Criteria

**On when these are evaluated.** These are *post-implementation* invariants over real production documents across a defined window, never a synthetic probe alone. Unless stated otherwise the window is **7 complete days after the last child deploys**, and the baseline is the 2026-08-06 census reproduced in Context (495,375 documents; 0.36% span-bound; 716 live mapped names; 59 cross-family names).

**Population guard, applying to every criterion below.** Each check records its enumerated population and compares it against the pre-change volume for the same window length. A criterion whose population has collapsed **fails as inconclusive rather than passing** — a check that evaluates zero rows and reports success is the failure mode an acceptance suite is most prone to.

- **AC-1 — A retired spelling cannot reach production, and a new name is unimpeded.** · **Check:** on a scratch branch, (i) add `latency_ms` to a record emitted on a governed path and run CI; (ii) separately add `queue_depth` — a name in no vocabulary — and run CI. · *Fails if* (i) passes CI, **or** (ii) fails CI. Both halves are required: a validator that rejects everything satisfies (i) trivially, and (ii) is what proves the 178 family-private names kept the freedom D3 grants them.

- **AC-2 — No declared retired spelling survives in stored production documents.** · **Check:** for each retired spelling in the vocabulary, an `exists` query across all governed families over the post-cutover window — over documents, not mapping declarations. · *Fails if* any returns non-zero. Mapping-absence is insufficient: a retired field persists in `_source` under a dynamic type with no declaration at all. This is the criterion Rule 1 exists to satisfy, and it is stated over the corpus rather than over the validator so a validator that is registered but not reached still fails it.

- **AC-3 — Production telemetry was not reduced, dropped or mutated by the validator.** · **Check:** total indexed document count across governed families for the post-cutover window, against the pre-change count for the same window length from the recorded baseline; **and** a production record deliberately carrying a violation is present in Elasticsearch, unmodified, with its violation counted. · *Fails if* volume falls below the recorded baseline, **or** the planted violating record is absent, altered or stripped of the offending key. The second half is what discriminates D4's decision from a validator that quietly sanitises records — sanitising would satisfy AC-2 while destroying the evidence AC-4 counts.

- **AC-4 — The violation rate is published, bounded, and shrinking while it matters.** · **Check:** violation count divided by total documents, per family, per week, published by the joinability monitor, over three consecutive weeks. · *Fails if* the rate is unpublished for any week, **or** exceeds 1% in any family, **or** is at or above 0.1% and has not declined across the three weeks. A genuinely-zero rate is success and is not required to decline; a persistent sub-1% plateau is an unfixed producer and must not sit behind a ceiling forever.

- **AC-5 — Rule 2 still fires with its exception list in force.** · **Check:** with the exception list as committed, plant `sesion_id` on a governed path and run CI; separately confirm `max_latency_ms` and `component` still emit to production in the window. · *Fails if* the planted typo passes CI, **or** either excepted name stopped being emitted. The first half is what stops the exception list growing until it disables the rule; the second is what stops the rule being satisfied by suppressing legitimate fields.

- **AC-6 — The vocabulary describes the corpus it claims to govern.** · **Check:** enumerate distinct field names actually present in governed-family documents over the window; every name that crosses two or more families is either declared in the vocabulary or listed as a stated exclusion with a reason. · *Fails if* any cross-family name is neither declared nor excluded. This is the criterion that catches vocabulary rot, and it is deliberately measured **from the live corpus rather than from the templates** — the registry's template diff-gate is declined in D6, so the corpus is the only remaining witness.

- **AC-7 — No registry was built.** · **Check:** the repository contains no artifact generating Elasticsearch template `properties`, and no CI job diffing generated template output against committed templates. · *Fails if* either appears. A **guard**, not a proof of success: it asserts only that D6's ruling held and that FRE-1067's AC-12 was not defeated by a later ticket. AC-6 carries the substance.

**Seam ticket:** **FRE-1178** — *ADR-0133 SEAM — adjudicate the emit-envelope criteria*. Filed parked (`Backlog`, no `stream:` label). **Due date: 2026-10-15.** That is the earliest date all seven criteria become adjudicable: AC-4 alone needs three consecutive published weeks after the last child deploys, and the chain sequences behind FRE-1064 and FRE-1067, neither of which has started. Master activates it at the first advance-dispatch on or after that date; an `adr` session adjudicates it and records one verdict per criterion in this ADR's Status Updates. This ADR reaches `Implemented` only if every verdict is green.

---

## References

- ADR-0004 — Telemetry & Metrics Implementation Strategy (Accepted): set the telemetry model, left the field vocabulary unspecified
- ADR-0068 — Agent Self-Telemetry Data Plane (Accepted, 2026-05-10): recorded the `prompt_tokens` / `input_tokens` and `completion_tokens` / `output_tokens` divergences that Rule 1 catches and similarity misses
- ADR-0074 — End-to-End Traceability & Joinability (Accepted): the joinability monitor D4 publishes the violation counter through
- ADR-0090 — Telemetry Surface Contract (Accepted — 2026-06-21): its deferred *"declared field registry"* open decision is answered **no** by D6, closing it by decision rather than a fourth deferral
- ADR-0093 — OpenTelemetry at the Substrate Boundary (Accepted with scope change — 2026-06-21): the vocabulary standard, unchanged here; both its implementation tickets closed on 2026-08-06, leaving its status line stale
- ADR-0128 — Telemetry Naming and Structure Convention (Superseded by ADR-0129 — 2026-07-31): the source of tier one and tier two; its D5 exclusivity promise is narrowed by D3 and its D6 registry is declined by D6
- ADR-0129 — OpenTelemetry Instrumentation, with Trace Visibility as the Acceptance Bar (Accepted — 2026-07-31): governs spans; its D8 table records log-record naming as ungoverned, which is the surface this ADR governs
- ADR-0130 — Two Tiers of Acceptance Criteria (Accepted): D1/D2, why these criteria stay with the ADR and are asserted only by the seam ticket
- `src/personal_agent/telemetry/events.py:55-88` — `CANONICAL_MODEL_CALL_*_FIELDS`, the `frozenset[str]` name-sets whose content moves into the vocabulary
- `src/personal_agent/telemetry/logger.py:232` — `structlog.configure`, where both FRE-1064's span-context processor and this ADR's validating processor register
- `src/personal_agent/telemetry/es_logger.py:165-171` — the document assembly the validator runs immediately before
- `tests/personal_agent/llm_client/test_telemetry_parity.py:100,130` — the existing parity assertion, which proves required-key presence only; retired by FRE-1067 and replaced by this ADR's rules
- Linear FRE-1113 — this ADR's originating ticket
- Linear FRE-1064 — ADR-0129 B1, the processor pipeline this registers into; `Approved`, `stream:build1`
- Linear FRE-1067 — ADR-0129 B3, whose AC-12 D6 rules on and whose scope retires the canonical field sets
- Linear FRE-1044 — ADR-0128 A2, the original tier-one ticket, superseded in scope by this ADR's chain
- Linear FRE-1045 — ADR-0128 A3, the ingest pipeline, closed by D5
- Linear FRE-1048 — ADR-0128 A6, the field registry, closed by D6
- Linear FRE-1109 — the field-type collision, closed 2026-08-04 by a forward-only template pin, removing tier two's forcing function
- Linear FRE-1107 — the producer/template type disagreement Rule 3 catches at emit
- [OpenTelemetry — semantic conventions for generative AI](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
- [Python `difflib.SequenceMatcher`](https://docs.python.org/3/library/difflib.html#difflib.SequenceMatcher) — the similarity measure quoted in Context and enforced by Rule 2

---

## Status Updates

### 2026-08-06 — Proposed
**Changed By:** `/adr` session (FRE-1113)
**Reason:** Owner-directed. Two rulings were taken in session: tier one is built, re-founded on the residual log corpus; and the field registry is buried, answering ADR-0090's open decision *no*. Measurement during authoring established that the surface ADR-0129 leaves ungoverned is 99.64% of the corpus rather than a remainder, that FRE-1067 deletes the field sets tier one was to be founded on, and — decisively — that string-similarity near-miss detection catches none of the five naming divergences this project has actually recorded, which moved the primary mechanism from similarity to declared retired spellings.
