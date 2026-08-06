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

This table is load-bearing and it falsifies the intuitive mechanism. **A near-miss detector tuned to catch `tarce_id` at 0.875 catches none of the five divergences that actually happened.** Lowering the threshold until it does is not available either — scanned against the 716 field names live in `agent-logs-*` on 2026-08-06:

| Threshold | Names flagged | Share of live mapping |
|---|---|---|
| 0.85 | 1 | 0.1% |
| 0.82 | 2 | 0.3% |
| 0.60 | 77 | 10.8% |
| 0.33 — the threshold needed to catch `ts` / `@timestamp` | **675** | **94.3%** |

The two hits at 0.82 are **both legitimate**: `component` (0.857 against `component_id` — a real name-versus-id pair) and `max_latency_ms` (0.833 against `latency_ms` — a genuinely different measure). So string similarity is simultaneously too weak for the observed failure class and productive of false positives on the observed corpus, and there is no threshold that escapes both.

### The write path is singular, but only for one family

`es_logger.log_event` has exactly **one** production caller in `src/` — `telemetry/es_handler.py:205`, the structlog-to-Elasticsearch bridge. Every `agent-logs` document therefore passes through one seam, which is what makes seam-level enforcement viable at all rather than requiring 551 emit-site edits.

That property does **not** extend to the other families. Captures, reflections, insights, joinability and slm-health are written by their own code paths and never touch the structlog pipeline. This bounds what any structlog-registered mechanism can honestly claim, and D1 is scoped accordingly rather than asserting reach it does not have.

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

### D1 — Tier 1 is restored, scoped to the `agent-logs` write path, and re-founded

The typed emit envelope is built. Its governed surface is **the records this repository writes to `agent-logs` through the structlog seam** — of which 99.64% remain log records after the ADR-0129 chain lands — and it is founded on a declared vocabulary rather than on `CANONICAL_MODEL_CALL_*_FIELDS`, which FRE-1067 retires.

**The scope is the write path, not the corpus, and the difference is stated rather than blurred.** `agent-logs` is ~98.8% of all telemetry documents (ADR-0128's census), so this reaches the overwhelming majority — but captures, reflections, insights, joinability and slm-health have their own writers, never traverse the structlog pipeline, and are **not governed by this decision**. Claiming otherwise would repeat the overclaim ADR-0128 criticised in its predecessors, and it would make every criterion below unprovable.

Span attributes are also **out of scope**. They are governed by semantic conventions and asserted by FRE-1067's AC-6, AC-7 and AC-8. This ADR governs the log path only, and the two mechanisms meet at no point — which is why restoring tier one contradicts nothing in ADR-0129 and requires no amendment to its D8 table beyond the row this ADR's Status Update records.

### D2 — Enforcement lives at the single emit seam, never at 551 call sites

The envelope is a **validating structlog processor**, registered in the same `structlog.configure` pipeline at `src/personal_agent/telemetry/logger.py:232` that FRE-1064 uses for the span-context processor. It validates the assembled record dict immediately before it reaches `es_logger`.

This is decided here rather than left to implementation because the intuitive design — a frozen dataclass constructed at each emit site — is unbuildable and would contradict a funded ticket. There are 551 `log.info` sites alone, and FRE-1064's **AC-4 forbids editing emit sites** to achieve identity injection (*"that would mean identity is still being supplied by hand, which is the practice this ADR exists to end"*). A record-construction envelope would demand exactly that edit, at every site, for the same reason ADR-0129 rejected it.

Enforcement at the seam also makes coverage a property of the pipeline rather than of author discipline: a new emit site is governed the moment it is written, with nothing to remember.

### D3 — What the envelope enforces: three rules, in priority order

**Rule 1 — Declared retired spellings are rejected, by exact match.** The vocabulary declares, for each governed name, the spellings it retires:

| Canonical | Retired spellings |
|---|---|
| *(intrinsic span duration — no log field)* | `duration_ms`, `latency_ms` |
| `input_tokens` | `prompt_tokens` |
| `output_tokens` | `completion_tokens` |
| `@timestamp` | `ts`, `timestamp`, `started_at`, `probed_at`, `rated_at` |
| `event_type` | `event`, `event.name` — **as stored document keys** |

A record carrying a retired spelling fails. This is the rule that carries the decision: it is exact, so it produces no false positives, and it catches **all five** divergences this project has actually paid for — every one of which a similarity threshold misses.

**The event-key row is a decision, not an inheritance, and it reverses ADR-0128.** ADR-0128 D3 chose `event.name`; ADR-0129 D8 dropped that guarantee as unreachable ("semconv names spans, not every log record's event key"). Left there, the fifth divergence would have no canonical side and Rule 1 could not retire anything. This ADR therefore settles it the other way: **`event_type` is the canonical stored key**, because it is present on 100% of documents today and nothing is proposing to move it. structlog's in-process `event` key is untouched — it is structlog's message field, and `es_handler.py:121` performs the single translation, exactly as ADR-0128 D3 described. What Rule 1 forbids is a *stored document* carrying `event` or `event.name` as a field.

**Rule 2 — Near-miss of a governed name is rejected, with a declared exception list.** A key that is not itself governed, is not family-private-and-declared, and scores **≥ 0.85** `difflib.SequenceMatcher` similarity against a governed name, fails. This catches the typo class Rule 1 cannot see: `tarce_id` (0.875), `trace_ids` (0.941), `sesion_id` (0.947).

**The threshold is 0.85 and is decided here, not left to implementation** — it is the parameter that determines whether the rule works, and the Context table shows the cost curve is steep. At 0.85 the live 716-name mapping yields exactly **one** flagged name, `component` at 0.857, which is therefore the exception list's single opening entry. `max_latency_ms` scores 0.833 and falls below the threshold, needing no exception at all.

The exception list is closed and each entry states why. An exception without a stated reason is a defect, not a configuration.

**Rule 3 — Governed names carry their declared type.** A governed name whose value does not match its declared type fails. This is the rule that would have caught FRE-1107, where the capture template mapped `threshold_violations` as `integer` while the producer wrote a list of strings and Elasticsearch rejected every affected document whole.

**What is deliberately not enforced.** There is no presence obligation — a family that has no `component_id` writes none, exactly as ADR-0128 D3 decided. There is no rejection of unrecognised keys: a genuinely-new key such as `queue_depth` passes, and the 178 family-private names keep the freedom D3 granted them. ADR-0128 D5's word "exclusivity" is therefore **not** delivered as written, and this ADR says so plainly rather than reusing the word for something weaker.

### D4 — It fails at development time, and never drops telemetry in production

The processor's behaviour is split by environment, and the split is the whole point:

- **Under test and in CI, a violation raises.** This is where the guarantee lives — ADR-0128 D5's real promise was *"it fails at development time, where a mistake is cheapest,"* and that is the property being restored.
- **In production, a violation never drops, rejects or mutates the record.** The record is indexed as emitted, and the violation increments a counter published through the existing joinability monitor (`observability/joinability/`), which already runs on a schedule and already writes a health family.

Telemetry that is deleted for being malformed cannot tell you why it was malformed — ADR-0128 D4's reasoning, unchanged and re-affirmed. A validating processor that raised in production would take the service down on a telemetry defect, which is a strictly worse failure than the one it prevents.

**The counter publishes a denominator as well as a numerator: violations *and* records validated.** This is decided here because without it the production half of this ADR is unverifiable in the specific way that matters. A validator that is registered but never reached — a mis-ordered processor, an exception swallowed upstream, a second write path — publishes a permanent zero violations, which is indistinguishable from a clean corpus. The validated count is the **invocation witness**: compared against the documents actually indexed into `agent-logs` for the same window, it separates "nothing was wrong" from "nothing was checked." AC-2 rests entirely on it, and AC-5's zero-is-success clause is only honest because AC-2 supplies the denominator.

### D5 — Tier 2, the substrate-boundary ingest pipeline, is dropped

It is not deferred, parked or left for later evidence. The corpus it was designed to normalise was deleted by owner ruling on 2026-08-04, and the ticket that justified it (FRE-1109) closed the same day by a different remedy. FRE-1045 (ADR-0128 A3) is closed rather than left in `Backlog` implying a plan.

If a future collision makes the case again, an ingest pipeline remains available and costs nothing to hold in reserve. What is rejected is building one **now**, against no measured need, on a surface where D1–D4 already prevent the class at emit.

### D6 — The field registry is declined, and ADR-0090's deferred question is answered *no*

No registry is built. No per-field declaration generates Elasticsearch templates. No CI job diffs generated output against committed templates. Templates stay hand-written, and can drift, exactly as ADR-0129 D8 recorded.

ADR-0090 listed *"a declared field registry — a typed catalog the emit sites and templates both derive from"* under its open decisions in June. ADR-0128 D6 committed to it; ADR-0129 D8 abandoned it; FRE-1048 has sat unbuilt throughout. **This ADR answers it in the negative, which closes it by decision rather than by a fourth deferral.** FRE-1048 is closed.

**On FRE-1067's AC-12 — its intent is upheld and its wording is overridden, and this ADR states that plainly rather than reading its way out of it.**

AC-12 reads: *"no generated field registry, no per-field type declaration file and no template-generation step."* D3's vocabulary declares a type per governed name. **Under the literal words, it is a per-field type declaration file, and no reading of "registry versus vocabulary" changes that** — the honest position is that this ADR conflicts with AC-12 as worded and overrides it, not that AC-12 secretly permits it.

What AC-12 was written to prevent, and what this ADR upholds permanently, is the **registry as a generating source**: no artifact generates template `properties`, no CI job diff-gates generated output, no second derivation of the envelope's field sets. What it forbids by accident is a sixty-line leaf file that generates nothing and is read by one processor — which is not the debt AC-12 exists to prevent, and which the owner ruled on 2026-08-06 should be built.

**FRE-1067's AC-12 must therefore be re-worded before that ticket is built** — to name the generator and the diff-gate rather than any per-field type declaration. That is a control-plane edit on an Approved ticket, so it is master's, and it is recorded in this ADR's handoff comment as a required action rather than a note. Until it is re-worded, a build session picking up FRE-1067 will read a criterion this ADR contradicts.

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
- Leaves the `agent-logs` write path — ~98.8% of telemetry documents, 99.64% of them still log records after the chain — with no naming mechanism of any kind.
- Semconv is silent on Elasticsearch log-document field names by construction — ADR-0128 D2 established this for the timestamp and it holds generally.
- The divergence class recurs at emit and is discovered months later in the substrate, which is the entire measured history.

**Why Rejected:** It is the status quo, and the status quo's cost is documented across five ADRs. The distinguishing property of this decision is that its mechanism is one processor in a pipeline that another funded ticket is already registering a processor into — days of work, not a programme.

---

## Consequences

### Positive Consequences

- **The `agent-logs` write path acquires a naming mechanism for the first time** — the family holding ~98.8% of all telemetry documents, of which 99.64% remain log records after the ADR-0129 chain, currently governed by nothing.
- **The measured divergence class can no longer be introduced through CI.** All five recorded instances are exact-match retired spellings under Rule 1, and a sixth of the same shape fails the build. **This is deliberately weaker than "impossible at emit":** D4 lets a violation through in production rather than dropping the record, so the guarantee is that such a key cannot be *written into the codebase*, not that one can never reach storage. A key assembled dynamically at runtime, or emitted by a path CI never exercised, still lands — and is counted rather than blocked.
- **Coverage is a property of the pipeline, not of authors.** A new emit site is governed the moment it is written, with nothing to remember — the same argument ADR-0129 makes for context propagation, applied to names.
- **A three-times-deferred question is answered.** ADR-0090's registry open decision closes *no*, by decision, and stops consuming review attention.
- **FRE-1067's AC-12 acquires a ruling** before a build session has to guess at it.
- **The parity test's guarantee transfers rather than evaporates** when FRE-1067 deletes `CANONICAL_MODEL_CALL_*_FIELDS`.
- **Nothing in ADR-0129 is amended.** The two mechanisms govern disjoint surfaces, so this is additive rather than a partial supersession of an Accepted ADR.

### Negative Consequences

- **"Exclusivity" is not delivered.** An unrecognised key still passes. A genuinely novel misspelling that is neither a declared retired spelling nor similar enough to trip Rule 2 is stored silently, exactly as today. ADR-0128 D5 promised more and this ADR does not.
- **The vocabulary is a maintained artifact.** Roughly sixty names, their retired spellings and their types, kept current by hand. A name added to a template and not to the vocabulary is ungoverned and nothing says so — which is the registry's diff-gate, declined in D6.
- **Rule 2 will produce false positives**, and its exception list will grow. One is known today (`component`, 0.857); more will surface, and each is a small tax paid at CI time.
- **The other governed families are not reached.** Captures, reflections, insights, joinability and slm-health write outside the structlog seam and keep the naming freedom ADR-0129 left them. D1 states this rather than claiming corpus-wide governance, but it is a genuine hole: the divergence class remains fully available in those families.
- **Templates stay hand-written and can drift.** ADR-0090's mapping corner is unimproved, and FRE-1107's failure mode — producer and template disagreeing on type — is caught by Rule 3 at emit but not reconciled against the mapping.
- **Production violations are counted, not prevented.** D4's split means a wrong name still reaches Elasticsearch in production; only the development-time path fails. A violation counter nobody reads is a real risk, mitigated only by routing it through a monitor that already has a reader.
- **This is the seventh telemetry ADR**, in a lineage of six that changed nothing observable. Its defence is that its mechanism is small and lands inside a chain already funded — not that its diagnosis is better than its predecessors', which were correct.
- **Dropping tier two forecloses out-of-repo governance.** Should a second `slm_server`-shaped producer appear, nothing in this ADR reaches it, and D5's reasoning would need revisiting rather than extending.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| This becomes the seventh ADR that changes no bytes | **High** | The chain is three tickets landing inside a funded chain, and D7 sequences them against a head that is already labelled; AC-2 and AC-3 are outcome checks over production documents, not merge checks |
| **The validator is registered but never reached, and publishes a clean zero** | **High** | D4 commits the counter to publishing a denominator; AC-2 compares it against the independently-sourced `agent-logs` indexed count, so "nothing was checked" cannot present as "nothing was wrong" — every other production criterion is explicitly conditioned on it |
| The vocabulary rots — names added to templates, never declared | **High** | AC-7 measures declared-versus-live coverage from the corpus rather than trusting the artifact; the registry's diff-gate that would have prevented this structurally is declined in D6 and the gap is stated |
| Rule 2's exception list becomes a dumping ground that disables the rule | Medium | AC-1(ii) plants a typo at the advertised 0.875 boundary, not only an easy one; AC-6 caps and justifies the list and fails if an excepted field stops being emitted |
| The processor raises in production and takes the service down | Medium | D4 makes the environment split a decision rather than a configuration; AC-4 asserts validated count equals indexed count, so a raising validator shows as a shortfall |
| B1's falsification gate fails and this work continues regardless | Medium | D7 binds this chain to the same gate — it stops when the chain stops |
| Violation counter is published and never read | Low | It rides the existing joinability monitor, which already has a reader and an escalation path; AC-5 measures the rate's trajectory against a proven denominator, not the counter's existence |

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

- **AC-1 — Every declared rule rejects every violation it claims to, and rejects nothing else.** · **Check:** on a scratch branch, parameterised over the committed vocabulary rather than over one example: (i) for **each** declared retired spelling, a record carrying it fails CI; (ii) `tarce_id` (0.875) **and** `sesion_id` (0.947) each fail CI with the committed exception list in force; (iii) a governed name carrying a wrong-typed value fails CI; (iv) `queue_depth` — a name in no vocabulary and no near-miss — passes CI. · *Fails if* any spelling in (i) passes, **or** either name in (ii) passes, **or** (iii) passes, **or** (iv) fails. Each clause kills a specific cheat: (i) defeats a validator hard-coded to one spelling; (ii) pins Rule 2's threshold at both ends, since a threshold set above 0.875 still catches `sesion_id` and would pass a single-example check while missing the case D3 advertises; (iii) proves Rule 3 exists at all; (iv) proves the 178 family-private names kept the freedom D3 grants them, which a reject-everything validator would fail.

- **AC-2 — The validator actually ran over the production corpus.** · **Check:** over the window, the validated-record count published per D4 against the number of documents indexed into `agent-logs` for the same window, taken from Elasticsearch rather than from the counter. · *Fails if* the validated count is absent, **or** is below 99% of documents indexed. **This is the criterion the rest depend on, and it is stated first among the production checks for that reason.** A registered-but-unreached validator — mis-ordered processor, swallowed exception, a second write path — publishes zero violations over zero records and would otherwise satisfy AC-3 and AC-5 perfectly. The 1% allowance covers records emitted before processor registration at startup, not a coverage gap; a systematically-missed path shows up far below the threshold.

- **AC-3 — No declared retired spelling survives in stored production documents.** · **Check:** for each retired spelling in the vocabulary, an `exists` query over `agent-logs` documents for the post-cutover window — over documents, not mapping declarations. · *Fails if* any returns non-zero. Mapping-absence is insufficient: a retired field persists in `_source` under a dynamic type with no declaration at all. This carries force **only in combination with AC-2**, which is what distinguishes "Rule 1 held" from "no producer happened to emit one this week."

- **AC-4 — The validator neither dropped nor altered a record.** · **Check:** (a) the validated count from AC-2 equals the `agent-logs` indexed count for the window within the same 1% — an equality against an independently-sourced number, not a volume trend; (b) a production record deliberately carrying a violation is present in Elasticsearch, byte-identical to what was emitted, with the offending key intact and its violation counted. · *Fails if* (a) diverges, **or** the planted record is absent, altered, or stripped of the offending key. (b) is what discriminates D4's decision from a validator that quietly sanitises: sanitising would satisfy AC-3 while destroying the evidence AC-5 counts, and comparing against a pre-change volume baseline instead — as an earlier draft of this criterion did — would let a rising workload conceal dropped records.

- **AC-5 — The violation rate is published, bounded, and shrinking while it matters.** · **Check:** violation count divided by **AC-2's validated count** (never by an assumed total), published by the joinability monitor, over three consecutive weeks. · *Fails if* the rate is unpublished for any week, **or** exceeds 1%, **or** is at or above 0.1% and has not declined across the three weeks. A genuinely-zero rate is success and is not required to decline — but only because AC-2 has independently proven the denominator is the real corpus; a zero over an unproven denominator is the vanity number this criterion would otherwise be.

- **AC-6 — The exception list did not grow until it disabled Rule 2.** · **Check:** every entry on the committed exception list states the governed name it excepts, its measured similarity, and a reason; and `component` — the single entry justified at authoring — is still present in production documents over the window. · *Fails if* any entry lacks a stated reason or similarity, **or** the list exceeds five entries without each addition naming the emit site that forced it, **or** `component` stopped being emitted. The last clause matters: suppressing a legitimate field is the cheapest way to make a false positive disappear, and it would satisfy AC-1 while quietly deleting a field the corpus uses.

- **AC-7 — The vocabulary describes the corpus it claims to govern.** · **Check:** enumerate distinct field names actually present in `agent-logs` documents over the window; every name also present in one or more other governed families is either declared in the vocabulary or listed as a stated exclusion with a reason. · *Fails if* any such name is neither declared nor excluded. This is the vocabulary-rot check, and it is deliberately measured **from the live corpus rather than from the templates** — D6 declines the registry's template diff-gate, so the corpus is the only remaining witness that the artifact still matches reality.

- **AC-8 — No registry was built.** · **Check:** the repository contains no artifact generating Elasticsearch template `properties`, and no CI job diffing generated template output against committed templates. · *Fails if* either appears. A **guard**, not a proof of success — it asserts only that D6's ruling held and was not defeated by a later ticket. AC-1 and AC-2 carry the substance.

**Seam ticket:** **FRE-1178** — *ADR-0133 SEAM — adjudicate the emit-envelope criteria*. Filed parked (`Backlog`, no `stream:` label). **Due date: 2026-10-15.** That is the earliest date all eight criteria become adjudicable: AC-5 alone needs three consecutive published weeks after the last child deploys, and the chain sequences behind FRE-1064 and FRE-1067, neither of which has started. Master activates it at the first advance-dispatch on or after that date; an `adr` session adjudicates it and records one verdict per criterion in this ADR's Status Updates. This ADR reaches `Implemented` only if every verdict is green.

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
