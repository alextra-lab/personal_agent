# ADR-0148: Absence Must Be Reachable and Sayable

**Status:** Proposed
**Date:** 2026-09-10
**Deciders:** Owner (design direction, 2026-09-09 and 2026-09-10), adr seat (author)
**Tags:** memory, recall, grounding, context-assembly, orchestrator

---

## Context

### What FRE-1118 named, and what remains of it

FRE-1118 recorded three mechanisms, each independently sufficient to stop the system from
reporting that it holds nothing relevant. ADR-0138 absorbed the ticket on 2026-08-23 and shipped
two of the three. Nobody closed FRE-1118 afterwards.

| Mechanism | State on 2026-09-10 | Evidence |
|---|---|---|
| Subscore floors admit irrelevance by construction | Shipped | `memory/proactive.py:46-110` — the 0.5 recency fallback, the 0.3/0.5 topic fallbacks and the Neo4j cosine normalization each carry a `FRE-1287, ADR-0138` comment. PR #953. |
| The prompt forbids the honest answer | Shipped | `"Do NOT say you have no memory."` is absent from `src/`. `executor.py:3766-3770` retains only the FRE-1150 wording. PR #955. |
| The relevance signal never reaches the model | Open | `relevance_score` appears nowhere in `executor.py`. `_render_memory_section_with_ids` (`executor.py:3672-3794`) emits every admitted item flat. |

ADR-0147 §D3 reserved the remainder: *"FRE-1118's own defect — the relevance score never reaching
the model, and the prompt forbidding the honest answer — stays FRE-1118's work."* Half of that
sentence is already stale. This ADR settles the other half, and it reframes it.

### The remainder is not a missing score. It is a missing representation.

Removing the floors gave the system the ability to admit nothing. Watch what the system does when
it exercises that ability.

`executor.py:5986-5992`: when `ctx.memory_context` is empty, or every item renders blank,
`memory_section` stays `None`. No section is appended. The model receives silence.

Silence is the input in four different situations:

1. Recall ran to completion and honestly found nothing above the bar.
2. An arm failed. `dense_recall_arm` (`memory/service.py:5054-5069`) catches the embedder
   exception, logs `dense_recall_arm_embed_failed`, and returns `[]`.
3. Stage 7 discarded the section wholesale to fit the token budget
   (`request_gateway/pipeline.py:153`, `dropped_memory_context`).
4. The memory subsystem was not wired for this turn.

The type states the collapse directly. `request_gateway/types.py:133`:

```
memory_context: list[dict[str, Any]] | None
```

`None` carries every non-populated state at once. The docstring of `dense_recall_arm`
(`memory/service.py:5051`) says the same thing about its own return value: *"Empty when
disconnected, the query is empty, embedding fails, or nothing clears the floor."* Four causes,
one value, no discriminator. The turn-evidence record inherits it — `captains_log/turn_evidence.py:574`
reads *"Empty when there is no memory context."*

A per-item relevance score cannot repair this. In the case that matters there are no items to
attach a score to.

### A degradation signal exists and is routed away from the model

`request_gateway/pipeline.py:204-209` appends `context_assembly:memory_unavailable` to
`degraded_stages`. Two properties make it unusable for this purpose.

It is intent-gated. The flag fires only when the task type is `MEMORY_RECALL`. A question that
classifies `conversational` never sets it. Recall itself is **not** intent-gated: `context.py:409`
routes `MEMORY_RECALL` to `recall_broad`, and every other intent falls through to the proactive
and entity-match paths. So a conversational turn attempts recall, and its failures are never
flagged.

It never reaches the model. `degraded_stages` is consumed only by `observability/route_trace/`.
`request_gateway/types.py:157` labels it *"for telemetry"*.

### The second finding: absence is not currently reachable

A vocabulary for absence is worth nothing if the absent case never occurs. It does not occur
today, and the arithmetic that shows this was already measured.

FRE-694 (PR #283, 2026-06-29) benchmarked embedder separation in Neo4j score space, under an
owner-mandated parity gate against the `calibrate` medians. On the 0.6B@1024 arm the medians were
**positive 0.776, negative 0.706, negative maximum 0.792**. The verdict was that no embedder
opens a clean floor, on any arm or dimension tested.

Read the positive median and the negative maximum together. **The negative maximum exceeds the
positive median.** An irrelevant memory can out-score a typical relevant one on embedding alone.

FRE-1287 removed the floors and left the weights. `config/settings.py:2033-2067` still reads
0.45 embedding, 0.25 entity overlap, 0.20 recency, 0.10 topic, a 30-day half-life, and a 0.30
admission bar. Apply FRE-1287's own rescale, `2x - 1`, to the measured medians:

| Candidate | Neo4j score | Raw cosine | Embedding term | Admitted with zero overlap and zero topic |
|---|---|---|---|---|
| Irrelevant, median | 0.706 | 0.412 | 0.185 | while younger than **24 days** |
| Irrelevant, worst case | 0.792 | 0.584 | 0.263 | while younger than **73 days** |

Recency alone no longer buys admission. Recency plus an embedder whose negative cloud sits at
0.412 raw cosine still does.

The owner confirmed on 2026-09-10 that `proactive_memory_enabled` is `true` in production. The
default in `config/settings.py:2029` is `False`, so this path is live by configuration and not by
default.

### Why the test suite reported success

`tests/personal_agent/memory/test_proactive_scoring_floors.py:141` constructs FRE-1287's AC-1
candidate as:

```
vector_score=0.5,  # orthogonal
```

Orthogonal maps to 0.0 under the new rescale. The test therefore asserts non-admission at an
embedding term of zero. The production embedder does not emit orthogonal for an irrelevant item.
It emits about 0.706.

The companion assertion `test_recency_weight_is_below_the_admission_bar` states
`0.20 × 1.0 < 0.30`. That is true, and it proves recency does not admit *alone*. Recency never
acts alone.

The repository demonstrates the gap in its own fixture. `test_proactive_selection_rules.py:233`
records `0.45 * max(0, 2*0.65-1) + 0.20 = 0.335`, and notes that both rows clear `min_score`. A
Neo4j score of 0.65 is **below** the measured negative median.

FRE-1287 is correct as far as it goes. Its verification does not reach the case it was written
for. This ADR treats that as a verification defect to repair, not as a reason to redo the change.

### Scope, and the ticket this ADR merges

The owner settled the boundary on 2026-09-09: **merge the decision, split the delivery.**

FRE-1120 records that an embedder failure fails open into silent empty recall. Its own text said
telling the model that retrieval failed *"is the same signal the absence work needs and should
probably be designed alongside it rather than separately"*, and master expressed that by blocking
FRE-1120 behind FRE-1118. This ADR settles that shared signal.

FRE-1120's retry and its turn-evidence record stay a separate build ticket. Neither depends on the
vocabulary, and the retry needs no architecture — the FRE-1116 analysis retried the same question
thirty-seven seconds later and it worked.

FRE-1170 (the reranker degrades to passthrough silently) and FRE-240 (the reranker is down with no
fallback signal) are the same disease in a neighbouring component. This ADR names them and does
not decide them. A decision covering every retrieval component cannot be verified on one probe run.

---

## Decision

### D1 — The memory context carries its own status, computed where the cause is known

`memory_context` stops being a bare list. It becomes a result that pairs the items with a status
and the cause that produced it.

The renderer cannot compute the status. `executor.py:3672` receives a list and nothing else, so it
cannot separate an empty list caused by an embedder failure from one caused by genuine absence.
The cause is known at the arm, for one stack frame, and is then discarded.

Each producing path therefore states its own outcome. A path that raises, times out, or finds its
store unreachable reports a failure. A path that ran every arm to completion and admitted nothing
reports absence. The gateway composes the per-path outcomes into one turn-level status.

A partially degraded recall is a failure, not an absence. If one arm raised and another returned
nothing, the turn did not establish that nothing exists.

### D2 — Four rendered states, on one axis

The axis is **what the model may honestly conclude**. It is not what went wrong. Folding the two
axes into one enum produced the state that demanded two values at once, which ADR-0147 §D3 had to
repair.

| Rendered state | Arises from | Licenses |
|---|---|---|
| `POPULATED` | Items were rendered. | Reason from them. |
| `NOTHING_RELEVANT` | Every arm ran. Nothing cleared the bar. | *"I have no record of that."* |
| `WITHHELD` | Items existed. The budget drop or the render caps removed them. | *"I hold records on this and could not fit them into this turn."* |
| `UNAVAILABLE` | An arm raised, a store was unreachable, or memory was not wired. | *"I could not reach my records."* |

The memory section is **always present**. In the three non-populated states it carries one line
naming the state. Silence stops being a state.

`WITHHELD` stays separate from `UNAVAILABLE` because a budget drop is the one non-populated case
where the system **knows** items existed. That is strictly more information than a failure
supplies, and it is actionable for the reader, who can narrow the question and ask again.

The full cause stays in the turn-evidence record, where more than four values are useful and
harmless. The rendered vocabulary and the recorded vocabulary are deliberately different sizes.

### D3 — The status defaults to the weaker claim

A producing path that reports no status renders `UNAVAILABLE`. It never renders
`NOTHING_RELEVANT`. Only a path that positively ran to completion may claim absence.

This follows a pattern the codebase already sets. `RecallDiscardReport` (`request_gateway/context.py:52-70`)
pairs discards with a `population` field that defaults to `POST_SELECTION`, *"so a path that says
nothing cannot accidentally claim completeness"*. An earlier revision stamped the stronger claim
unconditionally and asserted it for paths that truncate silently. The same trap applies here, and
the same default closes it.

### D4 — A hard relevance gate runs ahead of the score combination

A candidate with **no entity overlap, no topic hit, and an embedding term below the calibrated
negative-cloud bound** is not admitted, whatever its age.

The gate is deterministic and it precedes `_combine_scores` (`memory/proactive.py:113-127`). It
removes recency's ability to substitute for relevance, rather than merely stopping recency from
acting alone. Without it, `NOTHING_RELEVANT` is unreachable for anything discussed in the last few
weeks, and D1 through D3 ship a vocabulary for a state that never occurs.

FRE-1287's ticket listed this option — *"a hard relevance gate ahead of the combination"* —
alongside floor removal and embedding rescaling. The build delivered the first two.

**The bound comes from a calibration, not from this document.** The FRE-694 medians were measured
in June on the 0.6B@1024 arm. Production now runs the OVH-managed 8B model, whose Youden J was
0.53 against 0.6B's 0.42 — better separation, and still overlapping clouds on every arm tested. The
direction holds. The numbers do not transfer, and the day counts in the Context section are
illustrative of the shape rather than of the current arm.

A discarded candidate carries its own drop reason, distinct from `DropReason.RECALL_SCORE_THRESHOLD`,
so a gate rejection stays separable from a threshold rejection in the discard report.

The gate applies to the entity-match path as well as the proactive path. Both admit items into the
same section, and a gate on one path only moves the defect rather than closing it.

### D5 — Enforcement is the existing citation contract. This ADR adds none.

In `NOTHING_RELEVANT`, `WITHHELD` and `UNAVAILABLE` the per-turn source registry holds no memory
sources. A span asserting a fact about the user's history therefore has no admissible referent, and
ADR-0138 D1's default-deny already marks it uncited. In `POPULATED`, a weakly matched memory cited
for a fact it does not contain fails `check_containment` (`grounding/containment.py:774`).

The state marker's single job is to make the refusal **accurate**. Without it a model refuses
arbitrarily, or says *"I have no record"* when the truth is *"I could not reach my records"*.

A prompt marker is a hint and the model may ignore it. FRE-1283's instruction to its builder was
explicit: *"Do not add prompt text that presumes the model will self-police — that is the failure
mode the ADR exists to avoid."* This ADR does not lean on the marker for enforcement, and it does
not build a second enforcement path for one signal.

**The dependency is declared rather than assumed.** Verification runs in `observe` mode. FRE-1325
records that a turn scored 0/9 uncited, the observation was written, and the answer was served
anyway, because nothing consumes the signal. FRE-1328 records why `enforce` cannot simply be
switched on: ADR-0138 D2 makes tool-driven turns structurally uncitable, so enforcing today would
make them unanswerable. **Until FRE-1325 closes the loop, this ADR's contract is honest and
inert.** That is stated here so no reader mistakes the vocabulary for a guarantee.

### D6 — No relevance band on the populated case

A rendered item carries no score and no band. Items keep the relevance order the memory context
already carries.

Two reasons. Containment already catches the weak-citation case structurally, so a band would be a
second mechanism aimed at a defect the first one covers. And a band is a threshold that the model
must interpret without a stated policy, which is a hint with no structural counterpart — the shape
D5 refuses.

D4 is what makes this safe. The band's purpose would have been to let the model discount a thin set
of barely-admitted items. The gate stops that set from being assembled.

---

## Alternatives Considered

### Option 1: Render a relevance score or band per item

**Description:** Carry each admitted item's `relevance_score` into the rendered section, either as
a number or as a high/medium/low band, so the model can discount a weak set.

**Pros:**
- Addresses FRE-1118's literal wording.
- Cheap to implement — the score exists at admission.

**Cons:**
- A band is a threshold, and setting one without a distribution of admitted scores repeats the
  error FRE-1287 warned against: *"moves a number without fixing the shape."*
- The model must invent an interpretation policy, because none is stated.
- No structural counterpart. A weakly matched memory is a valid source under D1, so the citation
  resolves and only containment can object.
- It does nothing for the case that motivated the ticket, where there are no items.

**Why Rejected:** It treats the symptom at the wrong layer. D4 stops the thin set from being
assembled, which removes the need for the model to discount it.

### Option 2: Collapse `WITHHELD` into `UNAVAILABLE`

**Description:** Render three states rather than four, on the argument that the model may conclude
the same thing from both — do not claim absence.

**Pros:**
- A smaller vocabulary is easier to render and easier to test.
- It keeps the agent from describing its own plumbing to the reader.

**Cons:**
- It discards information the system holds. On a budget drop the system knows items existed.
- The two states license different sentences. *"I hold records and could not fit them"* is
  actionable. *"I could not reach my records"* is an incident.

**Why Rejected:** The owner chose the separation on 2026-09-09, on the ground that the actionable
case is worth its own line.

### Option 3: Raise `proactive_memory_min_score`

**Description:** Lift the 0.30 admission bar until irrelevant candidates fall below it.

**Pros:**
- One configuration change, no new code.
- Immediately reduces the admitted population.

**Cons:**
- The bar sits above a weighted sum in which the one subscore measuring genuine relevance is
  outvoted. Raising the bar suppresses true positives at the same rate as false ones.
- It cannot separate a same-day irrelevant item from an older relevant one, because recency is
  inside the sum being compared.

**Why Rejected:** FRE-1287's own ticket ruled this out in advance: *"Do not simply raise
`min_score` — that moves a number without fixing the shape."* The finding is the shape.

### Option 4: A deterministic pre-check that blocks the turn

**Description:** When the state is `UNAVAILABLE` and the question is memory-dependent, block the
turn before generation and return a fixed message.

**Pros:**
- The model cannot talk its way past it.
- It does not depend on FRE-1325.

**Cons:**
- It needs a classifier for "memory-dependent", which is the axis FRE-1279 already found the intent
  taxonomy measures badly.
- It builds a second enforcement path for one signal, which is how a system acquires two
  vocabularies for one axis.
- It fails closed on a class of turns the model can often answer without memory at all.

**Why Rejected:** The structural mechanism already exists in the citation contract. Declaring a
dependency on FRE-1325 is honest. Duplicating enforcement is not.

### Option 5: Defer the populated case to the FRE-1122 baseline run

**Description:** Ship D1 through D3, and let the baseline run decide whether a gate or a band is
needed.

**Pros:**
- Nothing is decided without a measurement.
- The state vocabulary ships sooner.

**Cons:**
- The measurement already exists. FRE-694 measured the separation, and the weights are in the
  repository. The two together predict that absent probes return recent irrelevant items.
- It would ship a well-verified change to a path that does not execute.

**Why Rejected:** Considered and rejected during the 2026-09-09 discussion, after the owner
challenged it. The deferral described the question as unmeasured when the measurement was in hand.

---

## Consequences

### Positive Consequences

- Absence becomes reachable. A question with no answer in the corpus can produce an empty candidate
  set rather than a thin set of recent neighbours.
- Absence becomes sayable, and the sentence is accurate about its cause.
- A failed recall and an empty recall become distinguishable, at the model and in the evidence
  record. That is FRE-1120's third question, answered once.
- The FRE-1287 verification defect is repaired at its root: a fixture pinned to the measured
  negative cloud replaces one pinned to orthogonal.
- Silent degradation acquires a reader. `degraded_stages` remains telemetry, and the model gets a
  signal it can act on.

### Negative Consequences

- `memory_context` changes type. The gateway, the executor, ADR-0147's digest and the turn-evidence
  record all read it.
- The memory section is always present, so every turn pays a small token cost even when memory
  holds nothing. The cost is one line.
- The gate suppresses some true positives. Any relevance gate does. AC-5 and AC-6 exist to bound
  that rather than to deny it.
- The contract is inert until FRE-1325 lands. The system will state its own state honestly and
  still be able to ignore that statement.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| The gate is calibrated on a stale arm and suppresses relevant recall | High | AC-5 requires the bound to admit a stated share of the calibration's labelled positives. AC-6 requires no regression on the FRE-1122 present half. |
| The four states are rendered but the model ignores them | High | Declared, not mitigated. D5 states the FRE-1325 dependency. AC-7 asserts the structural half that does hold today. |
| The status defaults to the stronger claim on some path | High | D3 fixes the default at `UNAVAILABLE`. AC-3 constructs a status-omitting producer and asserts the weaker claim. |
| A second vocabulary appears when FRE-1120 ships its evidence record | Medium | FRE-1120's record consumes this ADR's cause values. It defines none of its own. |
| The type change breaks ADR-0147's digest, which reads the same field | Medium | ADR-0147 §D4 takes items in the order the memory context carries. The digest reads the items, not the status, and the status is additive. |
| The gate moves the defect to the entity-match path | Medium | D4 binds both paths. AC-4's fixture runs against the deployed configuration. |

---

## Implementation Notes

**Files affected:**

- `src/personal_agent/request_gateway/types.py` — `memory_context` gains its status.
- `src/personal_agent/request_gateway/context.py` — per-path outcomes; the composition rule;
  `RecallDiscardReport` is the pattern for the default.
- `src/personal_agent/request_gateway/pipeline.py` — the budget drop sets `WITHHELD`.
- `src/personal_agent/memory/service.py` — `dense_recall_arm` and its siblings report a cause
  rather than returning a bare `[]`.
- `src/personal_agent/memory/proactive.py` — the D4 gate ahead of `_combine_scores`, plus its own
  drop reason.
- `src/personal_agent/orchestrator/executor.py` — `_render_memory_section_with_ids` renders the
  state line, and the section is emitted unconditionally.
- `src/personal_agent/captains_log/turn_evidence.py` — the record carries the full cause.

**Dependencies:**

- FRE-1325 (nothing consumes the compliance signal) gates whether the contract binds. It does not
  gate this ADR's delivery.
- The calibration that sets D4's bound must run against the deployed embedder arm, not against the
  FRE-694 numbers.

**Testing strategy:**

Unit tests construct candidates at the **calibrated negative median in Neo4j score space**, never
at 0.5. Every gate test carries a companion assertion that the pre-gate arithmetic for the same
fixture crosses the bar, so a test that cannot fail is visible as one.

The FRE-1122 fixture supplies the end-to-end evidence. Its report gains one column: the state each
probe's memory section carried.

---

## Verification / Acceptance Criteria

- **AC-1** — On a probe whose subject is verified absent from the graph and the message history,
  the assembled memory context carries `NOTHING_RELEVANT`. · **Check:** the FRE-1122 fixture's ten
  absent probes, each with its recorded zero-row evidence; read the state from the turn-evidence
  record. · *Fails if* any absent probe yields `POPULATED`. A probe whose subject genuinely shares
  entities with the corpus is replaced under FRE-1122's own AC-1 replacement rule, with its
  replacement's zero-row evidence recorded — it is not excused in place.

- **AC-2** — A failed recall and an empty recall are distinguishable in the context the model
  receives. · **Check:** take one probe whose subject is verified **present**. Run it twice, once
  with an induced embedder failure and once without. Compare the assembled sections. · *Fails if*
  the two renders are identical, or if the induced-failure run yields `NOTHING_RELEVANT`.

- **AC-3** — An unreported status renders the weaker claim. · **Check:** a unit test constructing a
  producing path that returns items and omits the status. · *Fails if* the assembled section claims
  absence, or if the status is inferred from the item count.

- **AC-4** — The gate binds on the measured negative cloud, not on orthogonal. · **Check:** a unit
  test at the calibrated negative median in Neo4j score space, with zero entity overlap, zero topic
  hits and a same-instant timestamp, against the deployed weights. · *Fails if* the candidate is
  admitted. The test must carry a companion assertion that the same fixture crosses the bar without
  the gate — a test that passes both before and after the change proves nothing.

- **AC-5** — The gate's bound is calibrated, and it does not suppress genuine relevance. · **Check:**
  apply the configured bound to the calibration's labelled positive set. · *Fails if* the admitted
  share of true positives falls below the share recorded before the gate, by more than the margin
  the calibration states. This is what stops "tighten the gate until nothing passes".

- **AC-6** — Relevant recall does not regress end to end. · **Check:** the FRE-1122 present half,
  ten probes with named stored rows and quoted expected text. · *Fails if* the correct-recall count
  falls below the count the same ten probes produced before this chain landed.

- **AC-7** — In the three non-populated states, a memory-derived assertion has no admissible
  referent. · **Check:** on a turn whose state is `UNAVAILABLE`, where the model nonetheless asserts
  a fact about the user's history, query the grounding record for that span. · *Fails if* the span
  resolves to a memory source, which would mean the registry was populated in a state that claims
  it is not.

**Where these are adjudicated.** On FRE-1118, this ADR's umbrella ticket, once the implementation
chain has landed and deployed. Not at merge of this ADR, and not by any single implementation
ticket.

---

## References

- FRE-1118 — the originating ticket, and this ADR's umbrella
- FRE-1120 — an embedder failure fails open into silent empty recall (decision merged here, delivery split)
- FRE-1122 — the absence-probe fixture, and the instrument for AC-1, AC-2 and AC-6
- FRE-1287 — proactive scoring floors; shipped, with the verification defect this ADR repairs
- FRE-1283 — the prompt deletions; shipped
- FRE-1279 — the umbrella that absorbed FRE-1118 into ADR-0138
- FRE-1325 — nothing consumes the compliance signal; D5's declared dependency
- FRE-1328 — D2 makes tool-driven turns structurally uncitable; why `enforce` is not on
- FRE-694 — the embedder separation ceiling; the measurement behind D4
- FRE-1170 and FRE-240 — the same shape in the reranker; named, not decided
- ADR-0138 — the citation contract; D1 default-deny, D2 admissibility, D6 the prompt deletion
- ADR-0147 — memory belongs to the planner; §D3 reserved this work, §D4 bounds the digest
- ADR-0100 — `recall_similarity_floor`; the seam D4 must stay coherent with
- ADR-0039 — proactive memory scoring, the origin of the weights
- `src/personal_agent/memory/proactive.py`, `src/personal_agent/memory/service.py`,
  `src/personal_agent/request_gateway/context.py`, `src/personal_agent/orchestrator/executor.py`

---

## Status Updates

### 2026-09-10 - Proposed
**Changed By:** adr seat, with the owner
**Reason:** Authored after a two-day exploration on FRE-1118. The session established that two of
the ticket's three mechanisms had already shipped under ADR-0138, that the remainder is a missing
representation rather than a missing score, and — after the owner challenged a proposed deferral —
that absence is not currently reachable at all.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
