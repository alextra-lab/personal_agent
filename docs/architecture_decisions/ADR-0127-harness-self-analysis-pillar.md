# ADR-0127: The Harness Self-Analysis Pillar — Collectors Emit Facts, One Analyzer Judges, Findings Are Keyed by Evidence

**Status:** Proposed — 2026-07-28
**Date:** 2026-07-28
**Deciders:** project owner (FRE-991)
**Tags:** self-improvement, observability, evidence, analysis, captains-log, insights

---

## Context

**What is the issue we're addressing?**

Four subsystems perform harness self-analysis — Insights, Captain's Log reflection, Context
Quality, and the consolidation quality monitor. They overlap, most are switched off, and the
pipeline delivers almost nothing. FRE-991 asked whether they should be consolidated into one
pillar centred on a reasoning Analyzer, shaped after the way the master session gates a pull
request.

This ADR answers that question. It does **not** build the Analyzer. It fixes the contract the
Analyzer must satisfy, the layer it sits in, what it is allowed to treat as truth, and how its
output acquires identity — because every recorded failure of the current pipeline is a failure
of one of those four things rather than of the reasoner.

### What was measured

Everything below was measured on 2026-07-28 against the live cloud-sim stack or read from
source. Figures attributed to the 2026-07-26 explore note are marked as such and were not
re-measured here.

**The record is smaller than the plan assumed.** The capture corpus holds **1,941 turns**
(April 475 · May 1,064 · June 195 · July 138), read via `_count`. The figure of 8,880 carried
in `docs/research/2026-07-26-harness-self-analysis-deep-dive-queue.md` comes from
`_cat/indices` `docs.count`, which counts Lucene *nested sub-documents*; `tool_results`,
`context_messages`, `metrics_structured` and `telemetry_refs` are all mapped `nested`, inflating
the count roughly 4.5×. July is 138 turns, not 165.

**There is no labelled corpus.** `user-turn-ratings-*` holds 1,943 documents, of which
**1,916 carry the single value `2`**. `scripts/migrate_fre757_backfill_default_rating.py`
states in its own docstring that `rating = 2` is a **default "ok"** written on send by the PWA's
DONE hook and backfilled onto every historical turn. It is the absence of a judgment, persisted.
Ratings that express an actual user judgment number **27** — twelve `0`, nine `1`, six `3` — and
six of those arrived inside a 73-second window on 2026-06-26 that is consistent with interface
testing. The claim in `docs/research/2026-07-26-session-analyzer-pillar.md` that "1,933 rated
turns" supply the labelled signal a DSPy optimisation loop would need does not hold.

**Two of the three questions the Analyzer is meant to ask have no evidence to reason over.**

| Question | Evidence required | Live coverage |
|---|---|---|
| How did it run? | steps, tools, errors, timing | **complete** — `steps[]` 100%, `tool_results[]` on every tool-using turn |
| Did it work? | user message, response, judgment | text complete; **judgment: 27 labels** |
| What did it cost, for what return? | tokens joined to dollars | **`cost_usd` mapped, 0% populated, all four months** |

Conversation and tool prose are genuinely well captured: `user_message` and
`assistant_response` are present and full on 100% of turns in all four months, and
`tool_results[]` carries `tool_name`, `success` and **untruncated `output` prose** on every
tool-using turn (63–79% of turns by month). Assembled context arrived only with FRE-1004,
deployed 2026-07-27 20:49Z: `assembled_context` is present on 9 July turns (7%),
`context_messages` on none, and `evidence_presence.reasoning_trace` reads `not_recorded` on all
9 — the reasoning trace is structurally unavailable because the bound models do not return raw
chain-of-thought.

**Storage is not the same as reachability.** `tool_results.output` and `tool_results.arguments`
are mapped `"index": false`. The prose is present in `_source` and retrievable per document, and
it is excluded from both search and aggregation. At 1,941 documents a client-side scan is
trivial; the constraint binds later, and it has never been taken as a decision.

**The interesting joins are cross-substrate by construction.** Turn text and tool prose are in
Elasticsearch, dollars are in PostgreSQL `api_costs`, and entities, stances and claims are in
Neo4j. No single-substrate reader can answer *"what did this turn cost, what was recalled for
it, and was the answer any good."*

**Three of the four subsystems are switched off, and the one still running is the one whose
model is known to be wrong.** Read from the live gateway environment:

| Subsystem | Live setting | State |
|---|---|---|
| Insights | `AGENT_INSIGHTS_ENABLED=false`, `AGENT_INSIGHTS_WIRING_ENABLED=false` | off |
| Captain's Log reflection | `AGENT_CAPTAINS_LOG_REFLECTION_MIN_INTERVAL_SECONDS=86400000` (1,000 days) | off by throttle |
| Context Quality Phase 2 | `context_quality_governance_enabled` default `False`, never flipped since April | off |
| KG quality monitor | `AGENT_GRAPH_QUALITY_GOVERNANCE_ENABLED=true` | **running** |

Delivery is unchanged: **1,957 reflections carry 7 `linear_issue_id` values — 0.36%.**

### Why nobody noticed: dedup was measuring the wrong object

The deep-dive queue diagnosed the quality monitor's category error correctly — a *condition* is
a level, a *proposal* is a discrete idea, and forcing both through one promotion mechanism makes
duplicates the correct output of the wrong model. Comparing the four fingerprint functions in
source shows the same error in a sharper and more general form:

| Module | Identity computed over | Origin of every component |
|---|---|---|
| `telemetry/context_quality.py` · `fingerprint_incident` | `noun_phrase`, `dropped_entity`, `component` | facts read off the turn |
| `insights/fingerprints.py` · `cost_fingerprint` | `anomaly_type`, `observation_date` | deterministic |
| `insights/fingerprints.py` · `pattern_fingerprint` | `insight_type`, `pattern_kind`, normalised `title` | code-assigned |
| `captains_log/dedup.py` · `compute_proposal_fingerprint` | `category`, `scope`, `normalized_what` | **all three are model output** |

The three that hash **facts** deduplicate. The one that hashes **judgments** produced 832
distinct fingerprints across 942 proposals (measured 2026-07-26, explore note) — 88% textually
distinct while topically concentrated.

This matters because it changes the remedy. The explore note attributes the failure to a
`ChangeScope` enumeration that cannot name `memory`, `request_gateway`, `cost_gate`, `sysgraph`,
`observability`, `events`, `mcp`, `storage`, `gateway`, `transport` or `delegation`, and implies
that enlarging the enumeration is the fix. Enlarging it improves the accuracy of one axis and
leaves the mechanism intact, because `normalized_what` remains free text the model chose. Two
differently-worded descriptions of the same defect cannot be made to collide by improving the
vocabulary they are written in. **A conclusion cannot be deduplicated; only the evidence it was
drawn from can be.**

### What the four subsystems actually emit

The premise that the four are four instances of one thing does not survive reading them.
`second_brain/quality_monitor.py` emits `QualityReport` and `GraphHealthReport` — ratios, rates
and counts, which are levels. `telemetry/context_quality.py` emits `CompactionQualityIncident` —
a discrete, fact-fingerprinted event. `insights/engine.py` emits `Insight` and `CostAnomaly`,
which are facts with confidence, **and** `Improvement`, which is a proposal. Only
`captains_log/reflection.py` is purely a judgment producer.

Insights therefore already *is* the two-layer arrangement, built and running-capable, and it
already reads cost: it holds a `cost_tracker` dependency and a `detect_cost_anomalies()` method.
The explore note's "it never sees cost" is true of the reflector specifically and false as a
statement about the pillar.

### Retention is currently an accident

`agent-captains-captures-*` indices have **no ILM policy attached** — the index `_settings`
lifecycle block is empty. The corpus survives because nothing deletes it, not because anything
decided to keep it. `session_retention_days` is 180 and `purged` is 0. This is the substrate
every future analysis depends on, preserved by omission.

---

## Decision

Build **one pillar in two layers**, and fix its contract before its reasoner. Nine decisions.

### D1 — Two layers, not one merged subsystem

Deterministic collectors emit **facts**. One reasoning **Analyzer** emits **judgments**. The
four existing subsystems do not merge into the Analyzer:

- `insights/`, `telemetry/context_quality.py` and `second_brain/quality_monitor.py` are
  **collectors** and are retained as inputs. They already emit fact-shaped, fact-keyed output.
  Their detectors — the insight analyzers, the KG gauges, the compaction incidents — are a
  library of known-good questions, and they are load-bearing dependencies of the Analyzer, not a
  starting repertoire it may absorb or discard.
- `captains_log/reflection.py` is the judgment producer, and it is the subsystem this pillar
  replaces.

This is the master pattern the explore note set out to copy, applied honestly: `pr_gate.py`
surfaces each check's raw state one-to-one with its source and never synthesises "CI passed";
master's judgment is the thin layer no collector covers. Merging collectors into the reasoner
would re-commit, one altitude higher, exactly the level-versus-proposal category error that
produced the duplicate-ticket incident.

### D2 — Finding identity is computed over evidence, never over conclusion

Every Analyzer output carries the **evidence keys** it was derived from, and identity is
computed over those keys alone. A model-chosen value — a category, a scope, a free-text summary,
a title the model wrote — **may not appear in any identity key**.

An evidence key is a value that exists independently of the reasoner: a `trace_id`, a
`session_id`, an ADR identifier and criterion number, a tool name, an entity identifier, a
component path, a date bucket, a configuration key. The reasoner's prose is payload, never
identity.

This is the generalisation of what already works in three of four modules, and it is the
correction to the dedup remedy the explore note proposed.

### D3 — The normative reference is the ADR acceptance-criteria corpus, re-checked continuously

A gate without a spec is an opinion generator. The explore note names this as the central design
problem and observes that a session has no spec. It does — it is just not being used as one.

**79 of 129 ADRs carry a Verification / Acceptance-Criteria section.** Master proves those
criteria **once**, at the merge gate, and they are never evaluated again. The Analyzer's spine is
to re-check them against the running system, continuously, and report each as green, red, or
INCONCLUSIVE with the evidence that decided it.

This supplies the three things opinion generation lacks: a normative reference that exists in
writing, independent ground of the *source* kind, and — with D2 — a natural identity, since a
finding keys on `(ADR, criterion, subject)`.

**Turn ratings are explicitly rejected as the normative reference**, on the measurement in
Context: 27 expressed judgments, not 1,933. ADR-0126's D7 — *a producer needs a criterion that
fails when nothing reads it* — is the seed of this decision and is the model for how criteria
should be written so they remain checkable after the ticket closes.

### D4 — Owner dispositions are the teacher signal, and must be joinable

Every Analyzer output that reaches the owner carries a disposition, written when the owner acts
on it, and joinable to the finding **and to the finding's evidence keys**.

The disposition vocabulary already exists in the reflection status enumeration —
`awaiting_approval`, `approved`, `rejected`, `implemented` — and nothing reads it as a signal.
It is the only source that will ever say whether the Analyzer's *judgment* is good, as opposed
to whether its facts are right, and it costs nothing to begin recording.

The join is the load-bearing half. FRE-1023 is the live example of the failure this prevents: a
borderline-attribution signal logs its scores but no claim key, so a decision cannot be joined
to the thing it decided, and the evidence is unusable as built.

### D5 — The Analyzer consumes a cross-substrate evidence package under a fixed contract; the contract is decided before the host

The Analyzer's input is an **evidence package** assembled by a collector across Elasticsearch,
PostgreSQL and Neo4j, and its output is a **structured verdict**. That contract is fixed first;
the reasoner behind it is replaceable.

This follows from measurement rather than taste. Cost lives in `api_costs` in PostgreSQL, turn
text and tool prose in Elasticsearch, and the knowledge substrate in Neo4j, so a reader native to
any one store cannot answer the pillar's central question. An Elasticsearch-native job would meet
the PostgreSQL wall on its first cost question.

Where the reasoner runs — an external Claude Code session, an in-harness sub-agent, or a
swappable host — is **not decided here** (see D9). If a reasoner outside the harness is chosen,
its spend sits outside the harness ledger and must be given a declared separate budget rather
than being allowed to become an unmeasured stream; that is the defect FRE-989 documents for
`insights`, `promotion` and `freshness`, which pass the cost gate unbounded and produce no
counter.

### D6 — Absent evidence is declared, and a verdict resting on it is INCONCLUSIVE

The evidence package declares, per dimension, whether the evidence is `present`, `empty` or
`not_recorded`, extending the `evidence_presence` object ADR-0125 shipped. A verdict that depends
on a dimension recorded as `not_recorded` is returned as **INCONCLUSIVE** — neither a pass nor a
finding — naming the missing dimension.

This is the anti-confabulation rule, and it is the same shape as ADR-0126's entity-selection
precondition, where a missing precondition is inconclusive rather than a stance defect or a
vacuous pass. Without it, the reasoner's most likely failure is exactly the one already
observed in the reflector: asked for a file path and symbol with no repository access, it returns
something coherent instead of nothing.

It also makes the cost gap safe to carry. `cost_usd` is unpopulated today, so any verdict about
cost must be INCONCLUSIVE until the dimension is real, rather than silently reasoned around.

### D7 — Events trigger; bounds are terminal

The Analyzer runs on events, not on a cadence. The dispatch stack has already made this
migration — ADR-0110 (poll) to ADR-0116 (event-driven, Accepted). An event-driven analyzer with
nothing to do costs nothing; a sweep with nothing to do still runs.

Bounds are **terminal conditions, not ceilings**. The idle sweep had a ceiling of 2 and reached
311 attempts because its exclusion predicate required a terminal reason and classed
`budget_denied` as transient.

Three bounds are declared, and the first one reached ends the run:

1. **No forward progress — the primary bound.** An investigation step that adds **no new evidence
   key** to the package terminates the analysis **immediately**, at that step. Not after a
   retry allowance, and regardless of remaining budget or attempt count.
2. **A step ceiling** of 8 investigation steps per analysis, as a backstop for a run that keeps
   producing new keys without converging.
3. **A declared per-analysis cost budget**, set in configuration.

Bound 1 is the one that matters and is stated first because it is the one the sweep lacked. A
denial, an error, or an empty result that yields no new evidence key **is** no forward progress,
whatever its transient-or-terminal classification says — that classification is exactly what let
`budget_denied` retry 311 times against a ceiling of 2, and this rule makes the classification
irrelevant to termination.

A run ending on any bound emits *"undetermined within budget"* with the steps taken and the bound
that fired.

### D8 — The capture corpus is a named substrate with explicit retention

The reconstructable turn record is promoted from a by-product of a subsystem to a **named
substrate with a stated retention policy**, and the policy is written down rather than inherited
from the absence of a lifecycle rule.

The initial policy is **retain indefinitely**, revisable on measured storage cost. The corpus is
1,941 turns and a few megabytes; the cost of keeping it is negligible and the cost of losing it
is every future analysis, including back-testing anything this pillar builds against real
history.

**The policy must be attached, not merely absent.** Today the capture indices carry *no*
lifecycle policy at all, so "nothing deletes them" and "we decided to keep them" are
indistinguishable, and the next person to attach a default policy destroys the corpus without
overriding any stated decision. This decision is satisfied only by a **named lifecycle policy
explicitly attached to `agent-captains-captures-*` whose phase set contains no delete phase**. An
unattached index fails this decision — that is the current state, and it is the thing being
fixed.

Capture is LLM-free and upstream of every consumer, so the entire analysis layer can be switched
off — as three quarters of it currently is — without losing the raw material.

### D9 — Scope boundary: what this ADR does not decide

Named so they are not silently assumed decided:

- **Where the reasoner runs.** D5 fixes the contract deliberately so this stays open.
- **Delta versus clock for digest rebuild.** ADR-0124 triggers wholesale regeneration on an idle
  clock; the explore note and the session-summarizer brainstorm brief independently concluded
  rebuild should fire on accumulated delta as a hybrid of cheap increments plus periodic full
  rebuild. **This belongs to ADR-0124's trigger, not here**, and is recorded so it stops being
  rediscovered. The correction of record stands: the cost incident was a bug, not an indictment
  of wholesale regeneration, and at a measured maximum of 17 turns per session unbounded input is
  not yet live.
- **The verification oracle.** Its own build, after its own research, per ADR-0124 Amendment B.
- **Promotion mechanics to Linear**, including the 200-open-issue throttle that produced the
  0.36% delivery rate. ADR-0105 D6 makes it a queryable funnel state.
- **Whether the KG quality monitor's conditions and proposals become one pipeline or two.** D1
  places it as a collector; how its levels are surfaced remains the deep dive the queue note
  reserved.
- **The ADR-0126 implementation chain is not gated by this ADR.** FRE-1015, FRE-1016, FRE-1017
  and FRE-1018 all read the ADR-0098 Claim and Stance layer in Neo4j; none reads a session digest
  or a capture, and none is affected by the unit of analysis. The hold recorded in MASTER_PLAN
  section 0 should be lifted.

  The genuine dependency is **FRE-1021**, through the entity-selection precondition, and it binds
  the **topic-scoped** surface only — ADR-0126 D2 states that the always-present behavioural
  surface does not inherit that failure mode. Per MASTER_PLAN section 0 the precondition has been
  written into **FRE-1017's AC-3 and FRE-1015's AC-1**, while **FRE-1016, FRE-1018 and FRE-1019
  were never checked for the same relay gap**; that check is owed regardless of this ADR. FRE-1021
  is a measurement ticket and can run in parallel rather than in front.

---

## Alternatives Considered

### Option 1: Repair the reflector in place

**Description:** Keep the existing `GenerateReflection` signature and pipeline; fix the
`ChangeScope` enumeration to name the missing subsystems, and give the reflector repository
access so its proposals cite real symbols.

**Pros:**
- Smallest possible change; no new layer, no new contract.
- Directly addresses the two defects the explore note leads with.
- Repository access genuinely does convert coherence into correctness for code proposals.

**Cons:**
- Enlarging the enumeration improves one axis of a three-axis identity key whose third component,
  `normalized_what`, is free text the model chose. Duplicates survive by construction.
- Leaves the reflector as the only self-analysis consumer, so cost and outcome remain unasked.
- Retains one overloaded call doing three grafted-on jobs, most of whose output fields are
  documented as "empty string if…".

**Why Rejected:** the measurement says the dedup failure is structural, not lexical. A better
taxonomy produces a better number and the same mechanism. Repository access is worth having and
is preserved — it is subsumed by D5's evidence package, which can include source.

### Option 2: One merged Analyzer replacing all four subsystems

**Description:** The shape FRE-991 proposed — delete Insights, reflection, Context Quality and
the quality monitor, and replace them with a single reasoning agent that investigates freely.

**Pros:**
- Conceptually clean; one thing to own, one budget, one trigger.
- Removes two subsystems that are inert and two that overlap.
- Matches the master-gate analogy most directly at first reading.

**Cons:**
- Re-commits the conditions-versus-proposals category error one altitude higher. Three of the
  four emit facts with working fact-keyed identity; one emits judgments with broken identity.
- Discards `detect_cost_anomalies()`, the KG gauges and the compaction detectors — the only
  deterministic collectors the pillar has.
- Makes the reasoner responsible for measurement as well as judgment, which is precisely what
  the master pattern separates.

**Why Rejected:** the master gate works *because* collectors and judgment are separate. Copying
the pattern by merging them copies the label and not the mechanism.

### Option 3: Turn ratings as the normative reference

**Description:** Use the rating corpus as ground truth — as the Analyzer's spec, and as the
metric and training set for a DSPy-optimised signature.

**Pros:**
- Would require no new capture work; the index exists and is joinable to captures by `trace_id`.
- An end-to-end outcome label is the strongest kind of signal when it is real.
- Completes DSPy's stated requirement of objective plus metric plus examples.

**Cons:**
- 1,916 of 1,943 documents are a backfilled default written on send, not a judgment.
- 27 expressed ratings, of which six look like interface testing.
- Even at volume, a rating is unattributed: it says the turn was bad, not which mechanism made
  it bad.

**Why Rejected:** measurement. The corpus this option depends on does not exist. Recorded at
length because the claim appears in the research note the investigation started from and would
otherwise be inherited.

### Option 4: Build the Analyzer now on the evidence that exists

**Description:** Accept the current substrate, build the reasoner, and let the evidence gaps
close underneath it as FRE-989 and the ADR-0125 chain land.

**Pros:**
- Fastest route to something running, and the pedagogical objective is served by building.
- "How did it run" is fully evidenced today and could be analysed immediately.
- Avoids a long dependency chain before any capability exists.

**Cons:**
- "How did it run" is the one question the existing reflector already asks and which delivers at
  0.36%. Building a more expensive machine to ask it again is the expensive order.
- Cost is unpopulated and sits behind FRE-989 (Needs Approval) and ADR-0120's ask-first gate.
- Without D6, a reasoner facing absent evidence produces confident output about dimensions it
  cannot see.

**Why Rejected:** not rejected outright — deferred by sequencing. D6 is what makes building on a
partial substrate safe, which is why it is a decision here rather than an implementation note.

### Option 5: Retire self-analysis entirely

**Description:** Three of four subsystems are already off. Delete all four, keep the capture
substrate, and let the owner read a periodic digest.

**Pros:**
- Removes an entire class of cost and duplicate-ticket incidents at a stroke.
- At 138 turns in July with a single user who is also the actuator, a human reading a good
  digest is competitive with any automation.
- The capture substrate is LLM-free, so nothing durable is lost.

**Cons:**
- Discards working deterministic collectors that cost nothing to run.
- Forecloses the harness learning thread, which is this work's stated justification.
- Leaves the ADR acceptance-criteria corpus permanently unchecked after merge, which is a
  measured standing debt.

**Why Rejected:** the owner's justification for this pillar is as a learning thread expected to
evolve, not as a throughput-justified feature. Recorded because it is the honest null option and
should be visible to the next reader, who will otherwise score the pillar as over-engineering
against 138 turns a month.

---

## Consequences

### Positive Consequences

- Findings acquire durable identity. Two reasoners describing the same defect in different prose
  produce one finding, because identity is the evidence and not the wording.
- The Analyzer gains a normative reference that exists in writing, is discriminating, and can
  fail — and 79 ADRs' criteria stop being single-use artifacts retired at the merge gate.
- Absent evidence becomes visible instead of confabulated, so the pillar can be built and run on
  a partial substrate without producing confident nonsense about cost.
- The deterministic collectors are preserved rather than absorbed, so the cheap always-on layer
  survives independently of whether the expensive reasoning layer is switched on.
- The capture corpus stops depending on the absence of a lifecycle rule.
- The ADR-0126 build wave unblocks: five Approved tickets and two idle build streams are released
  by D9 rather than waiting on this investigation.

### Negative Consequences

- The pillar is larger than "replace four things with one," and its first deliverable is a
  contract rather than a capability. Nothing user-visible ships from this ADR alone.
- Requiring evidence keys on every finding constrains the reasoner's output shape and makes some
  genuinely diffuse observations — "this session felt unfocused" — unrepresentable. That is
  intended, and it is a real loss.
- The cost dimension stays INCONCLUSIVE until FRE-989 and its governance gate resolve, so the
  question the audit most wants answered is the last one available.
- Retaining three collectors means retaining three subsystems' maintenance surface, including one
  (`quality_monitor`) whose own model is still unresolved.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Evidence-keyed identity is implemented as "the model also returns some keys," reintroducing judgment into the key | High | AC-1 asserts both collision on shared evidence and non-collision on shared prose; a model-chosen field in the key fails the second half |
| INCONCLUSIVE becomes a silent default that swallows real findings | High | AC-3 requires a verdict to be INCONCLUSIVE *only* when a depended-on dimension is `not_recorded`, and green/red otherwise on the same input |
| Only about half the ADR corpus carries criteria, and many are prose rather than executable | Medium | D3 scopes the spine to criteria that are checkable; ADR-0126 D7 is the authoring rule going forward; unconvertible criteria are reported, not silently skipped |
| A reasoner hosted outside the harness spends unmeasured, repeating the FRE-989 pattern | Medium | D5 requires a declared separate budget before any external host is chosen |
| The pillar accretes questions the way the reflector accreted output fields | Medium | D1 keeps new questions as new collectors or new bounded checks, never as more fields on one call |
| Retention decided as "indefinite" now becomes unaffordable later | Low | D8 states it as revisable on measured storage cost; the corpus is currently a few megabytes |

---

## Implementation Notes

**Files and surfaces this touches**

- `src/personal_agent/captains_log/dedup.py` — `compute_proposal_fingerprint` is the D2
  violation; its replacement takes evidence keys.
- `src/personal_agent/captains_log/reflection.py`, `reflection_dspy.py` — the judgment producer
  D1 replaces.
- `src/personal_agent/insights/`, `src/personal_agent/telemetry/context_quality.py`,
  `src/personal_agent/second_brain/quality_monitor.py` — retained as collectors; their
  fingerprint functions are the D2 pattern to follow.
- `src/personal_agent/captains_log/capture.py` and `turn_evidence.py` — where `evidence_presence`
  is written; D6 extends this object rather than introducing a parallel one.
- The evidence-package collector is new and spans three stores: Elasticsearch
  `agent-captains-captures-*`, PostgreSQL `api_costs`, Neo4j.

**Dependencies and sequencing**

- FRE-1004 (Done, deployed 2026-07-27 20:49Z) already supplies `assembled_context` and
  `recall_admission`; the D6 dimension vocabulary builds directly on it.
- FRE-989 (Needs Approval) and ADR-0120 (Proposed) gate the cost dimension. D6 is what allows the
  pillar to proceed without them.
- The reasoning-trace dimension is `not_recorded` and may be unobtainable; FRE-756 covers the
  token split, and the raw trace needs a feasibility ticket before an implementation one.
- No dependency on the ADR-0126 chain in either direction (D9).

**Testing strategy**

Every acceptance criterion below is checked against production data or a seeded fixture in the
test substrate (Neo4j :7688, Elasticsearch :9201, PostgreSQL :5433 per FRE-375), never against
prod substrate without the documented opt-in.

---

## Verification / Acceptance Criteria

- **AC-1** — Two findings derived from the same evidence collapse to one identity, and two
  findings sharing prose but not evidence do not. · **Check:** seed two findings with identical
  evidence keys and deliberately different summaries, category and scope values, and assert one
  identity results; then seed two findings with byte-identical summaries but different
  `trace_id`/criterion keys and assert two identities result. · *Fails if* either pair behaves
  the other way — in particular, the second half fails whenever any model-chosen value has been
  retained in the key, which is the exact defect that produced 832 fingerprints from 942
  proposals.

- **AC-2** — The Analyzer reproduces independently-established verdicts on ADR criteria it was not
  told the answers to, and never returns a false green. · **Check:** **pre-registration is part of
  the criterion.** Draw a sample of at least five criteria at random from the Accepted-and-
  Implemented ADR corpus, including at least one independently established as **unsatisfied** in
  production; record the sample and every expected outcome in the implementation ticket **before
  the Analyzer is run against them**. Then run blind and compare. · *Fails if* the Analyzer
  returns green on any criterion established as unsatisfied — a false green is the vacuous pass
  this pillar exists to make impossible — or if it cannot cite the query or record that decided a
  verdict, or if the sample or its expected outcomes were recorded after any run. A false *red* is
  noise and does not fail this criterion; the asymmetry is deliberate.

- **AC-3** — For **every** declared evidence dimension, a verdict depending on that dimension is
  INCONCLUSIVE when the dimension is `not_recorded` and green-or-red when it is present. ·
  **Check:** iterate the full dimension vocabulary — `user_message`, `assistant_response`,
  `tool_calls`, `assembled_context`, `recalled_memory`, `model_and_params`, `identifiers`,
  `reasoning_trace`, and the cost dimension — and for each, evaluate a criterion depending on it
  twice: once on a fixture where it is `not_recorded`, asserting INCONCLUSIVE **naming that
  dimension**, and once where it is populated, asserting green or red. · *Fails if* any dimension
  yields a confident verdict while absent, if any yields INCONCLUSIVE while present, or if the
  INCONCLUSIVE result names a different dimension than the missing one. Passing on `cost_usd`
  alone does not satisfy this criterion — dimension-specific branching that handles cost and
  mishandles the rest is the failure being excluded.

- **AC-4** — An owner disposition is joinable back to the finding and to the evidence that
  produced it. · **Check:** promote one finding, record a `rejected` disposition against it, then
  query from the disposition alone and assert it resolves to the finding, its evidence keys, and
  the specific criterion. · *Fails if* the join requires reconstruction from timestamps or free
  text — the FRE-1023 failure mode, where a decision cannot be joined to the thing it decided.

- **AC-5** — A verdict asserting anything about cost carries a joined PostgreSQL cost row. ·
  **Check:** emit a cost-bearing verdict and assert the cited `api_costs` row exists and its
  amount matches the verdict's figure; then construct a verdict asserting a cost figure with no
  joinable row and assert the contract rejects it. · *Fails if* a cost claim can be emitted
  without a joined row, which would let the pillar reason about dollars from the unpopulated
  `cost_usd` capture field.

- **AC-6** — An analysis that stops making progress halts **at the step that made none**. ·
  **Check:** construct a run whose step *N* adds a new evidence key and whose step *N+1* adds
  none — the `budget_denied` shape, a denial that returns no new key while classed transient —
  and assert the run terminates at step *N+1*, emitting *"undetermined within budget"* naming
  bound 1 and reporting *N+1* steps taken. · *Fails if* step *N+2* is attempted for any reason,
  including a retry allowance, a transient classification, or remaining budget. Eventual
  termination does not pass: a run that retries twice and then emits the phrase fails, because
  the defect being excluded is the 311-attempt loop that also terminated eventually.

- **AC-7** — The capture indices carry an **explicitly attached** retention policy with no delete
  phase. · **Check:** resolve the effective lifecycle policy for `agent-captains-captures-*` and
  assert (a) a **named policy is attached**, and (b) that policy's phase set contains no delete
  action. Then assert the check **fails** in both directions: against an index with no policy
  attached, and against an index whose attached policy carries a delete phase. · *Fails if* the
  check passes on an index with no policy attached — that is the **current** state, where the
  corpus survives by omission, and a criterion that green-lights zero change verifies nothing.

- **AC-8** — Removing any retained collector turns a *pre-named* assertion red from a green
  baseline. · **Check:** establish a green baseline across AC-1 to AC-7 **and** the three
  assertions below, then disable each collector in turn per this matrix and assert the named
  assertion — and only that one — turns red:

  | Collector disabled | Assertion that must turn red | The assertion |
  |---|---|---|
  | `insights/engine.py` | **A-INS** | a verdict about cost or a cross-substrate trend cites at least one `Insight` or `CostAnomaly` evidence key |
  | `telemetry/context_quality.py` | **A-CQ** | a verdict about context assembly cites at least one `CompactionQualityIncident` fingerprint |
  | `second_brain/quality_monitor.py` | **A-KG** | a verdict about graph health cites at least one `GraphHealthReport` or `QualityReport` metric |

  · *Fails if* any collector can be disabled with all three assertions still green — that
  collector is then an input to nothing and D1's claim that the collectors are load-bearing is
  false — or if the assertion that turns red is chosen after observing the mutation rather than
  taken from this matrix. This is ADR-0126's D7 rule applied to this ADR's own producers.

**Seam owner:** AC-8 owns the assembled-ADR seam. This ADR does **not** close when its last child
ticket merges — it closes when the mutation run in AC-8 completes from a green baseline, because
every other criterion can be satisfied by a component that nothing consumes. If a precondition
fails while establishing the baseline, the baseline is not green and the mutation run must not
proceed on it.

---

## References

- [ADR-0125](ADR-0125-two-quality-dimensions-and-turn-evidence-contract.md) — the two quality dimensions and the turn evidence contract; supplies `evidence_presence`, which D6 extends (Accepted)
- [ADR-0126](ADR-0126-reading-the-living-knowledge-substrate.md) — D7's producer-criterion rule is the authoring model for D3; its INCONCLUSIVE precondition is the model for D6 (Accepted)
- [ADR-0124](ADR-0124-session-summary-producer-and-phased-consumption.md) — owns the delta-versus-clock rebuild trigger that D9 explicitly leaves out of scope (Accepted, amended)
- [ADR-0105](ADR-0105-convergent-self-improvement-pipeline-and-system-graph.md) — convergence, dedup and loop-closure for the promotion pipeline; D6 there makes the throttle a queryable funnel state (Accepted)
- [ADR-0116](ADR-0116-event-driven-dispatch-actuation.md) — the poll-to-event migration D7 follows (Accepted)
- [ADR-0120](ADR-0120-cost-governance-visibility-consent.md) — gates the cost dimension referenced in D5 and AC-5 (Proposed)
- [ADR-0098](ADR-0098-memory-substrate-and-lifecycle-architecture.md) — the Claim and Stance layer the ADR-0126 chain reads, cited in D9's independence argument (Accepted)
- `docs/research/2026-07-26-session-analyzer-pillar.md` — the owner's originating proposal; its dedup remedy and its ratings claim are corrected here
- `docs/research/2026-07-26-harness-self-analysis-deep-dive-queue.md` — the four-subsystem survey; its corpus figure is corrected here
- `docs/research/2026-07-22-fact-verifier-guardian.md` — coherence versus correctness, and the four kinds of independent ground D3 and D6 rest on
- FRE-991 — the investigation this ADR answers
- FRE-989 — cost attribution audit; the dependency behind D5's budget requirement and AC-5
- FRE-1023 — the join failure D4 and AC-4 are written to prevent
- FRE-1021 — the entity-selection displacement that genuinely gates FRE-1015, per D9

---

## Status Updates

### 2026-07-28 - Proposed

**Changed By:** adr session (FRE-991)
**Reason:** Authored from the FRE-991 investigation after measuring the substrate, the rating
corpus, the four subsystems' emitted types and their fingerprint functions against the live
cloud-sim stack. Three claims in the originating research notes were corrected by measurement:
the corpus size, the existence of a labelled rating corpus, and the proposed dedup remedy.
