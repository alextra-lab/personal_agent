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
| The relevance signal never reaches the model | Open | `relevance_score` appears nowhere in `executor.py`. The rendered section carries no rank, score or band. |

ADR-0147 §D3 reserved the remainder: *"FRE-1118's own defect — the relevance score never reaching
the model, and the prompt forbidding the honest answer — stays FRE-1118's work."* Half of that
sentence is already stale. This ADR settles the other half, and it reframes it.

### The remainder is not a missing score. It is a missing representation.

Removing the floors gave the system the ability to admit nothing. Watch what the system does when
it exercises that ability.

`executor.py:5985-5999`: when `ctx.memory_context` is empty, or the render produces no text,
`memory_section` stays `None`. No section is appended. The model receives silence.

Silence is the input in four different situations:

1. Recall ran to completion and honestly found nothing above the bar.
2. An arm failed. `dense_recall_arm` (`memory/service.py:5054-5069`) catches the embedder
   exception, logs `dense_recall_arm_embed_failed`, and returns `[]`.
3. Stage 7 discarded the context to fit the token budget. The drop itself is
   `request_gateway/budget.py:276`, which sets `memory_context = None` inside the Phase 2 block
   at `:269`. `pipeline.py:148-156` only maps the recorded action onto telemetry phases
   afterwards.
4. The memory subsystem was not wired for this turn.

The type states the collapse directly. `request_gateway/types.py:133`:

```
memory_context: list[dict[str, Any]] | None
```

`None` carries every non-populated state at once. The docstring of `dense_recall_arm`
(`memory/service.py:5051`) says the same thing about its own return value: *"Empty when
disconnected, the query is empty, embedding fails, or nothing clears the floor."* Four causes,
one value, no discriminator. The turn-evidence record inherits it —
`captains_log/turn_evidence.py:574` reads *"Empty when there is no memory context."*

A per-item relevance score cannot repair this. In the case that matters there are no items to
attach a score to.

### The renderer is a second filter, and it is not the same set

`_render_memory_section_with_ids` (`executor.py:3672-3794`) does not emit everything it is handed.
It partitions items by kind, drops session items and unsupported kinds, filters every item whose
text is blank, and caps what remains at 15 entities, 5 episodes, 15 stances and 12 behavioural
stances. ADR-0147 documented this: `ctx.memory_context` is a **superset** of what the primary
receives.

So a section can be empty because recall found nothing, or because the renderer dropped
everything it was given. Those are different facts and the current code reports neither.

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

### Absence is not reachable today, for two independent reasons

A vocabulary for absence is worth nothing if the absent state never occurs. Two separate
mechanisms prevent it. The second does not depend on any embedder measurement.

#### Reason one: a standing layer populates the context on most turns

`_inject_behavioural_stances` (`context.py:271-318`) pushes the curated standing behavioural
stance set into the context **every turn**, and its own docstring states that it *"never reads
`memory_context` for its targets and runs even when `memory_context` is `None` (nothing else was
recalled this turn)"*. When the context is `None` it builds a new list and returns it populated.

So on any authenticated turn where that fetch returns at least one stance, `memory_context` is
non-empty regardless of what recall found. A successful but empty fetch leaves the context
unchanged (`context.py:305-317`), so this is a frequent case rather than a universal one. That is
enough: a status derived from "is `memory_context` non-empty" reads `POPULATED` whenever the
curated set resolves, which is the normal condition. This is the ADR-0147 §D3 defect in a new
place, and it is decisive for the design: the status must describe the **recall layer**, not the
container.

#### Reason two: recency still substitutes for relevance, on the candidate most likely to be admitted

FRE-694 (PR #283, 2026-06-29) benchmarked embedder separation in Neo4j score space, under an
owner-mandated parity gate against the `calibrate` medians. Its metric is stated precisely in
`docs/research/2026-06-29-fre-694-embedder-separation.md:35`: *"per-expected-entity positives vs
each query's strongest non-match negative"*. On the 0.6B@1024 arm the medians were **positive
0.776, negative 0.706, negative maximum 0.792**.

**The negative statistic is the strongest non-match per query, not a typical irrelevant item.**
That distinction matters, and it makes the statistic the right one rather than the wrong one for
this question. A vector search returns its nearest neighbours. On a query whose answer is not in
the corpus, the top-ranked candidate **is** the strongest non-match. So 0.706 describes precisely
the item that admission is decided on. It does not describe the corpus at large, and this ADR does
not claim that it does.

Read the positive median and the negative maximum together. **The negative maximum, 0.792, exceeds
the positive median, 0.776.** The strongest non-match can out-score a typical true match on
embedding alone. That is the separation ceiling in two numbers, and FRE-694's verdict was that no
embedder tested opens a clean floor.

FRE-1287 removed the floors and left the weights. `config/settings.py:2033-2067` still reads 0.45
embedding, 0.25 entity overlap, 0.20 recency, 0.10 topic, a 30-day half-life, and a 0.30 admission
bar. Apply FRE-1287's own rescale, `2x - 1` clamped at zero, to the measured medians:

| Top-ranked non-match | Neo4j score | Raw cosine | Embedding term | Admitted with zero overlap and zero topic |
|---|---|---|---|---|
| Median | 0.706 | 0.412 | 0.185 | while younger than **24 days** |
| Worst case | 0.792 | 0.584 | 0.263 | while younger than **73 days** |

Recency alone no longer buys admission. Recency plus the top-ranked non-match still does.

**The bounded claim.** This establishes that a recent irrelevant candidate is admitted whenever the
top-ranked non-match scores near the measured medians, on the proactive path, on the 0.6B arm
measured in June. It does not establish a rate over live traffic, and no such rate has been
measured. It is sufficient to show `NOTHING_RELEVANT` is not reliably reachable on that path. It is
not a claim that absence never occurs.

The owner confirmed on 2026-09-10 that `proactive_memory_enabled` is `true` in production. The
default in `config/settings.py:2029` is `False`, so this path is live by configuration.

### Why the test suite reported success

`tests/personal_agent/memory/test_proactive_scoring_floors.py:141` constructs FRE-1287's AC-1
candidate as:

```
vector_score=0.5,  # orthogonal
```

Orthogonal maps to 0.0 under the new rescale. The test therefore asserts non-admission at an
embedding term of zero. A top-ranked non-match does not score orthogonal. Its **median** across
FRE-694's probe set was 0.706, and the distribution around that median is what the calibration
must re-measure — no single non-match is claimed to score 0.706.

The companion assertion `test_recency_weight_is_below_the_admission_bar` states
`0.20 × 1.0 < 0.30`. That is true, and it proves recency does not admit *alone*. Recency never acts
alone.

The repository demonstrates the gap in its own fixture. `test_proactive_selection_rules.py:233`
records `0.45 * max(0, 2*0.65-1) + 0.20 = 0.335`, and notes that both rows clear `min_score`. A
Neo4j score of 0.65 is **below** the measured negative median.

FRE-1287 is correct as far as it goes. Its verification does not reach the case it was written for.
This ADR treats that as a verification defect to repair, not as a reason to redo the change.

### Three admitting paths, not one

`context.py` holds three, and they do not share a scoring model.

- **Broad recall**, for `MEMORY_RECALL` intent (`context.py:409`).
- **Proactive** (`context.py:441`). The four-subscore combination and the 0.30 bar.
- **Entity match** (`context.py:486`), reached when proactive is disabled or returns nothing. It
  builds a `MemoryRecallQuery` with `entity_names[:5]`, `recency_days=30` and `limit=5`, and reads
  `result.entities`. **It computes no embedding, overlap, recency or topic subscore at all.** Its
  admission gate is name resolution plus a 30-day recency window.

A relevance gate written only against the proactive combination leaves two paths untouched, and one
of them is reached exactly when proactive admits nothing.

**And the recall core below them does not gate either.** `recall_similarity_floor` is not a
turn-level relevance bar. It is the dense arm's own noise guard (ADR-0103 §4), applied inside
`_dense_vector_search_ranked` at `memory/service.py:5019-5020` — and applied with `>=`, so an item
exactly at the floor is admitted. The multipath core states its own rule at
`memory/service.py:5111-5120`: it *"never applies a score threshold to the fused or reranked set —
the reranker orders, it does not gate (AC-5)"*. The lexical, multi-query and structural arms carry
no floor of their own, and the structural arm is described there as *"a plain closed-axis
(recency-ordered) scan with no caller-supplied predicate"*.

That last clause is recency-only admission, stated as a design property. It is not a defect in the
core — ADR-0103 and ADR-0104 decided that ordering and gating are different jobs. It does mean a
relevance obligation cannot be discharged by calibrating one arm's noise guard, and it fixes where
the obligation must sit.

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
fallback signal) are the same disease in a neighbouring component. This ADR names them and does not
decide them.

---

## Decision

### D1 — Every stage reports its own outcome, and the turn composes them

`memory_context` stops being a bare list. It becomes a result that pairs the items with a status
and the cause that produced it.

No single stage can compute the status alone. The cause of a failed retrieval is known at the arm,
for one stack frame, and is then discarded. The cause of a budget drop is known at
`budget.py:269`. The cause of a render drop is known only at the renderer, which owns its own
filters and caps.

So each stage reports what it did, and the turn composes those reports:

- **Producing paths** report ran-to-completion, or a failure with its cause.
- **The budget stage** reports whether it discarded a non-empty context.
- **The renderer** reports whether it dropped every item it was handed.

This resolves the apparent contradiction between "the renderer cannot compute the status" and
"render caps produce `WITHHELD`". The renderer cannot compute the *whole* status. It is the only
component that can report its own drops, and it already returns the identities it emitted
(`executor.py:3794`), so the reporting seam exists.

### D2 — The status describes the recall layer, on one axis, with a stated precedence

The axis is **what the model may honestly conclude about recalled memory**. It is not what went
wrong, and it is not whether the container holds items.

**The status is scoped to the recall layer.** Items injected by a standing layer independent of
recall — the curated behavioural stances of `context.py:271-318` — never make the status
`POPULATED`. They render in their own section (`executor.py:3750-3754`) and they say nothing about
whether the system holds a record bearing on the question. Without this scoping the status reads
`POPULATED` on every authenticated turn and the whole design is inert.

| Rendered state | Arises from | Licenses |
|---|---|---|
| `POPULATED` | Recall admitted items and the renderer emitted at least one. | Reason from them. |
| `NOTHING_RELEVANT` | Every arm ran to completion. Nothing cleared the bar. | *"I have no record of that."* |
| `WITHHELD` | Recall admitted items. A **capacity** limit removed all of them. | *"I hold records on this and could not fit them into this turn."* |
| `UNAVAILABLE` | An arm raised, a store was unreachable, or memory was not wired. | *"I could not reach my records."* |

The memory section is **always present**. In the three non-populated states it carries one line
naming the state. Silence stops being a state.

**Precedence, because the states are not naturally disjoint.** A turn can satisfy more than one
description at once — one arm raises while another returns items, or the budget drops a context
that was itself partial. The order is fixed and total:

1. `UNAVAILABLE` outranks everything. If any arm failed to run to completion, the turn did not
   establish what exists, whatever else happened.
2. `WITHHELD` outranks `POPULATED` only when **every** admitted item was removed by a capacity
   limit. If any item survived to the render, the state is `POPULATED`.
3. `NOTHING_RELEVANT` is reachable only when every arm ran to completion and admitted nothing
   renderable. It is the one state that requires positive evidence of a complete run.

A partially degraded recall is therefore `UNAVAILABLE`, never `NOTHING_RELEVANT`. That is the whole
point of the ordering: absence is the strongest claim the layer can make, so it is the hardest to
reach.

`WITHHELD` stays separate from `UNAVAILABLE` because a capacity drop is the one non-populated case
where the system **knows** usable items existed. That is strictly more information than a failure
supplies, and it is actionable for the reader, who can narrow the question and ask again.

**Capacity is not the renderer's only reason to drop an item, and the other reason is not
`WITHHELD`.** The renderer removes items whose text is blank and kinds it does not render
(`executor.py:3709-3740`), before any cap applies. An item with no renderable content carries no
knowledge, so *"I could not fit them into this turn"* would be false — nothing failed to fit.

Such items are therefore **not admitted items** for the purpose of this status. A turn where recall
returned only blank or unrenderable items is `NOTHING_RELEVANT`, and the evidence record carries
the cause: recall matched, and what it matched held nothing to read. That is the honest reading —
the model has nothing to reason from — and it points at the real defect, which is FRE-1115's
finding that 18.7% of entities carry an empty description. FRE-1114 is the same seam: the item cap
runs before the emptiness filter, so a blank item can take a slot that nothing backfills.

The full cause stays in the turn-evidence record, where more than four values are useful and
harmless. The rendered vocabulary and the recorded vocabulary are deliberately different sizes.

### D3 — The status defaults to the weaker claim

A producing path that reports no status renders `UNAVAILABLE`. It never renders
`NOTHING_RELEVANT`, and it is never inferred from the item count.

This follows a pattern the codebase already sets. `RecallDiscardReport`
(`request_gateway/context.py:52-70`) pairs discards with a `population` field that defaults to
`POST_SELECTION`, *"so a path that says nothing cannot accidentally claim completeness"*. An
earlier revision stamped the stronger claim unconditionally and asserted it for paths that truncate
silently. The same trap applies here, and the same default closes it.

### D4 — One relevance predicate, at the admission boundary

The obligation is stated once, and it binds at one place:

> **The admission boundary is where an item enters `memory_context`, in `context.py`.** Every item
> admitted there carries a relevance value. An item whose relevance value is below a calibrated
> bound is not admitted, whatever its recency, its name match, or any other non-relevance signal.

**The boundary is the consumer, not the recall core.** This is deliberate, and it is what keeps the
decision compatible with the layer beneath. `memory/service.py:5111-5120` states that the core
*"never applies a score threshold to the fused or reranked set — the reranker orders, it does not
gate"*, per ADR-0103 §4 and ADR-0104 AC-5. That contract is untouched. Ordering stays the core's
job. Gating becomes the admitting path's job, one layer up, where the turn-level decision actually
belongs.

An earlier revision of this ADR placed the obligation on each path's own scoring model and named
`recall_similarity_floor` as the broad-recall half. Both were wrong. `recall_similarity_floor` is
the dense arm's noise guard, not a turn-level bar, and it reaches neither the lexical, multi-query
nor structural arms — so calibrating it would have left the recency-ordered structural arm feeding
items in unchecked.

What each path must supply at the boundary:

- **Proactive** — the relevance value exists. The predicate is a hard gate ahead of
  `_combine_scores` (`memory/proactive.py:113-127`): a candidate with no entity overlap, no topic
  hit, and an embedding term below the bound is not admitted. The gate precedes the combination
  rather than adjusting weights inside it, so recency cannot compensate for it.
- **Broad recall** — the reranker's score at the fused output is the relevance value. The core
  keeps ordering without gating. The path applies the bound when it admits.
- **Entity match** — this path computes no relevance value today, and **it must acquire one.**
  There is no exemption: an item admitted on name resolution and a 30-day window alone is exactly
  what the rule forbids, and `POPULATED` licenses unqualified reasoning that such an item does not
  support. The implementation ticket decides **which** value — routing the path through the
  multipath core for a reranker score, or computing a similarity over the `query_text` it already
  carries — against that path's measured population. It does not decide **whether**.

`recall_similarity_floor` remains worth calibrating on ADR-0100's own terms. That is a separate
concern from this ADR's obligation, and this ADR does not discharge one with the other.

**The bound comes from a calibration, not from this document.** Note the existing dense-arm
comparison is `>=` (`memory/service.py:5020`), so an item exactly at a floor is kept. The predicate
here is stated as **below the bound is rejected**, to match that convention rather than to fight
it, and the calibration therefore chooses a bound **strictly above** the median top-ranked
non-match.

**No FRE-694 number describes the deployed arm.** The medians were measured in June on the
0.6B@1024 arm. FRE-694 also benchmarked an 8B model, at Youden J 0.53 against 0.6B's 0.42, but that
arm was served locally at `slm:8505`. Production runs an OVH-managed 8B endpoint, which FRE-694
never measured. **This ADR therefore attaches no separation figure to the deployed arm.** What
transfers is FRE-694's verdict across every arm and dimension it did test — that no embedder opens
a clean floor, and the clouds overlap — which is a statement about the method, not about a
deployment. The day counts in the Context section are illustrative of the shape. The bound is set
by measuring the arm that is actually serving.

**What the calibration must produce**, so that "calibrated" is not a word standing in for a
judgment call:

- It runs against the deployed embedder arm, over a labelled probe set, and reports the positive
  and negative distributions in Neo4j score space, as FRE-694's harness does. It passes the same
  hard parity gate FRE-694 used against the Neo4j `calibrate` path before its numbers are trusted.
- The bound is chosen strictly above the **median top-ranked non-match**, so that median is
  rejected.
- It reports the share of the **whole labelled positive set** that the chosen bound admits. AC-5
  binds that share, and names the denominator so it cannot be re-cut after the fact.
- **If no bound satisfies both constraints**, the calibration reports that fact and stops. It does
  not pick one. An incompatibility means the deployed arm cannot separate the two populations at
  the required rate, which is a finding for the owner — the same finding FRE-694 reached about the
  arms it tested — and the response is a reranker-side bound or a different arm, not a quietly
  chosen number.
- Its output is committed as the source of the configured value. A missing or stale calibration
  leaves the previous bound in force and raises a `config_guard` finding. It never silently
  defaults to zero, and a hand-written constant with no committed calibration behind it is the
  condition AC-10 exists to fail on.

A candidate rejected by the gate carries its own drop reason, distinct from
`DropReason.RECALL_SCORE_THRESHOLD`, so a relevance rejection stays separable from a threshold
rejection in the discard report.

### D5 — Enforcement is the existing citation contract. This ADR adds none.

In `NOTHING_RELEVANT`, `WITHHELD` and `UNAVAILABLE` the per-turn source registry holds no memory
sources. A span asserting a fact about the user's history therefore has no admissible referent, and
ADR-0138 D1's default-deny already marks it uncited.

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

The reason is D4, and only D4. The band's purpose would have been to let the model discount a thin
set of barely-admitted items. The predicate narrows that set at the admission boundary, so the band
would describe a population the design has already reduced. It does not empty it, and the paragraph
below says what is left.

**A weaker claim than the first draft made.** An earlier revision argued that containment already
catches the weak-citation case structurally. It does not, fully. `check_containment`
(`grounding/containment.py:774`) can return `CONTAINED` on normalized token presence without
semantic support, and it can return `UNVERIFIABLE` or `ENTAILMENT_REQUIRED` rather than a
rejection. Containment is a partial catch. It is not a guarantee, and this ADR does not rest on one.

The residual is named rather than hidden: a weakly relevant admitted memory, cited for a claim
whose tokens it happens to contain, is not caught by anything in this design. D4 narrows that
population. It does not empty it.

---

## Alternatives Considered

### Option 1: Render a relevance score or band per item

**Description:** Carry each admitted item's `relevance_score` into the rendered section, either as
a number or as a high/medium/low band, so the model can discount a weak set.

**Pros:**
- Addresses FRE-1118's literal wording.
- Cheap to implement — the score exists at admission on the proactive path.

**Cons:**
- A band is a threshold, and setting one without a distribution of admitted scores repeats the
  error FRE-1287 warned against: *"moves a number without fixing the shape."*
- The model must invent an interpretation policy, because none is stated.
- Two of the three admitting paths compute no score to render.
- It does nothing for the case that motivated the ticket, where there are no items.

**Why Rejected:** It treats the symptom at the wrong layer. D4 stops the thin set from being
assembled, which removes most of the population a band would have described.

### Option 2: Collapse `WITHHELD` into `UNAVAILABLE`

**Description:** Render three states rather than four, on the argument that the model may conclude
the same thing from both — do not claim absence.

**Pros:**
- A smaller vocabulary is easier to render and easier to test.
- It keeps the agent from describing its own plumbing to the reader.

**Cons:**
- It discards information the system holds. On a budget or render drop the system knows items
  existed.
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
- It reaches only one of the three admitting paths.

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
  repository. The two together predict that a recent top-ranked non-match is admitted.
- The standing-stance layer makes the absent state unreachable regardless of any score, and that
  needs no measurement at all.
- It would ship a well-verified change to a path that does not execute.

**Why Rejected:** Considered and rejected during the 2026-09-09 discussion, after the owner
challenged it. The deferral described the question as unmeasured when the measurement was in hand.

---

## Consequences

### Positive Consequences

- Absence becomes reachable. A question with no answer in the corpus can produce a genuinely empty
  recall result rather than a recent nearest neighbour, and the standing layer no longer masks it.
- Absence becomes sayable, and the sentence is accurate about its cause.
- A failed recall and an empty recall become distinguishable, at the model and in the evidence
  record. That is FRE-1120's third question, answered once.
- The FRE-1287 verification defect is repaired at its root: a fixture pinned to the measured
  top-ranked non-match replaces one pinned to orthogonal.
- ADR-0100's `recall_similarity_floor` stops shipping at its legacy-equivalent zero.

### Negative Consequences

- `memory_context` changes type. The gateway, the executor, ADR-0147's digest and the turn-evidence
  record all read it.
- The memory section is always present, so every turn pays a small token cost even when recall
  holds nothing. The cost is one line.
- The gate suppresses some true positives. Any relevance gate does. AC-5 and AC-6 exist to bound
  that rather than to deny it.
- The entity-match path must acquire a relevance value it does not have today. This ADR fixes that
  it must. Which value it acquires is left to its implementation ticket, so the cost of that path is
  known in kind and not in size.
- The contract is inert until FRE-1325 lands. The system will state its own state honestly and still
  be able to ignore that statement.
- A weakly relevant memory cited for a token-matching claim remains uncaught. D6 names this residual.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| The gate is calibrated on a stale arm and suppresses relevant recall | High | D4 fixes what the calibration must produce. AC-5 binds the admitted share of labelled positives. AC-6 requires no regression on the FRE-1122 present half, per probe. |
| The four states are rendered but the model ignores them | High | Declared, not mitigated. D5 states the FRE-1325 dependency. AC-7 asserts the structural half that holds today. |
| The status defaults to the stronger claim on some path | High | D3 fixes the default at `UNAVAILABLE`. AC-3 constructs a status-omitting producer and asserts the exact value. |
| The standing-stance layer re-enters the status by a later change | High | D2 scopes the status to the recall layer. AC-8 asserts it directly on a turn where stances are injected and recall found nothing. |
| Two states apply at once and the implementation picks either | High | D2 states a total precedence order. AC-2 exercises the partial-failure case that ranks `UNAVAILABLE` over items. |
| The gate lands on one path and the defect moves to another | High | D4 binds at the admission boundary rather than per path, and AC-4 requires one test per admitting path, failing a path that has no relevance value to test. |
| The obligation collides with the recall core's ordering contract | High | D4 sits at the consumer, one layer above `memory/service.py:5111-5120`. ADR-0103 §4 and ADR-0104 AC-5 are untouched. |
| The calibration has no owner and never happens | High | D4 makes a missing calibration a `config_guard` finding that holds the previous bound rather than defaulting to zero. AC-10 fails a configured value with no committed artifact behind it. |
| No bound separates the two populations on the serving arm | Medium | D4 requires the calibration to report the incompatibility and stop rather than pick a number. AC-5 fails a bound configured in that case. The response is the owner's. |
| The type change breaks ADR-0147's digest, which reads the same field | Medium | The digest reads the items, and the status is additive. AC-9 asserts the digest still builds. |

---

## Implementation Notes

**Files affected:**

- `src/personal_agent/request_gateway/types.py` — `memory_context` gains its status.
- `src/personal_agent/request_gateway/context.py` — per-path outcomes, the composition rule, and
  the recall-layer scoping that excludes `_inject_behavioural_stances`.
- `src/personal_agent/request_gateway/budget.py` — the drop at `:269` reports `WITHHELD`.
- `src/personal_agent/memory/service.py` — `dense_recall_arm` and its siblings report a cause
  rather than returning a bare `[]`.
- `src/personal_agent/memory/proactive.py` — the D4 gate ahead of `_combine_scores`, plus its own
  drop reason.
- `src/personal_agent/config/settings.py` — the admission bound, sourced from the committed
  calibration artifact.
- `scripts/eval/` — the calibration harness and its committed artifact, following FRE-694's
  harness and its parity gate.
- `src/personal_agent/orchestrator/executor.py` — the renderer reports its own drops, renders the
  state line, and emits the section unconditionally.
- `src/personal_agent/captains_log/turn_evidence.py` — the record carries the full cause.

**Dependencies:**

- FRE-1325 (nothing consumes the compliance signal) gates whether the contract binds. It does not
  gate this ADR's delivery.
- The calibration that sets D4's bounds must run against the deployed embedder arm.

**Testing strategy:**

Unit tests construct candidates at the **calibrated top-ranked-non-match median in Neo4j score
space**, never at 0.5. Every gate test carries a companion assertion that the pre-gate arithmetic
for the same fixture crosses the bar, so a test that cannot fail is visible as one.

The embedder fault-injection seam is `generate_embedding`, called at `memory/service.py:5059` inside
the `try` that logs `dense_recall_arm_embed_failed`. Tests and the fixture induce failure there.

The FRE-1122 fixture supplies the end-to-end evidence. Its report gains one column: the state each
probe's memory section carried.

---

## Verification / Acceptance Criteria

- **AC-1** — On a probe whose subject is verified absent from the graph and the message history, the
  assembled memory context carries exactly `NOTHING_RELEVANT`. · **Check:** the FRE-1122 fixture's
  ten absent probes, each with its recorded zero-row evidence; read the state from the turn-evidence
  record. · *Fails if* any absent probe yields any other value, `POPULATED`, `WITHHELD`,
  `UNAVAILABLE` or absent, since each of those means the run did not demonstrate reachable absence.
  A probe whose subject genuinely shares entities with the corpus is replaced under FRE-1122's own
  AC-1 replacement rule, with its replacement's zero-row evidence recorded — it is not excused in
  place.

- **AC-2** — A failed recall and an empty recall are distinguishable in the context the model
  receives, and a partial failure ranks as failure. · **Check:** an integration test over the
  assembly path, not the live fixture, since FRE-1122's runner dispatches real HTTP turns and holds
  no fault-injection control. Assemble one query whose subject is verified **present**, three
  times: unmodified; with `generate_embedding` (`memory/service.py:5059`) raising, which disables
  every embedding-dependent arm; and with one arm raising while a non-embedding arm — lexical or
  structural, per the arm set at `memory/service.py:5144-5150` — still returns items. · *Fails if*
  the unmodified run is not `POPULATED`, or either failure run is not exactly `UNAVAILABLE`, or the
  partial-failure run reports `POPULATED` on the strength of the surviving arm. The third case is
  the one that tests D2's precedence rather than the plumbing.

- **AC-3** — An unreported status renders the weaker claim. · **Check:** a unit test constructing a
  producing path that returns items and omits the status. · *Fails if* the composed status is
  anything other than exactly `UNAVAILABLE`, including a status inferred from the item count.

- **AC-4** — The relevance predicate binds at the admission boundary, on the measured non-match
  rather than on orthogonal, for **every** path that admits. · **Check:** one test per admitting
  path — proactive (`context.py:441`), broad recall (`context.py:409`) and entity match
  (`context.py:486`) — each admitting a candidate whose relevance value sits at the calibrated
  median top-ranked non-match, with zero entity overlap, zero topic hits and a same-instant
  timestamp, against the deployed weights and bound. · *Fails if* any path admits the candidate, or
  if any path has no relevance value to test, which is the entity-match case as it stands today.
  Each test carries a companion assertion that the same fixture is admitted with the predicate
  disabled — a test that passes both before and after the change proves nothing.

- **AC-5** — The bound is calibrated, and it does not suppress genuine relevance. · **Check:** apply
  the configured bound to the calibration's labelled positive set, taken **whole** — every labelled
  positive, not a score-selected subset. · *Fails if* the bound does not reject the median
  top-ranked non-match, or if it admits fewer than **90%** of that whole positive set. Both halves
  must hold: the first alone permits a bound that rejects everything, the second alone permits a
  bound of zero. *Also fails if* a bound is configured at all in the case D4 reserves — where no
  value satisfies both halves — instead of the calibration reporting the incompatibility.

- **AC-6** — Relevant recall does not regress end to end, per probe. · **Check:** the FRE-1122
  present half, ten probes with named stored rows and quoted expected text; compare outcome per probe
  identifier against the same ten before this chain landed. · *Fails if* any probe that previously
  produced a correct recall now does not. An aggregate count that holds while individual probes swap
  outcomes is a failure, not a pass.

- **AC-7** — In each of the three non-populated states, the source registry holds no recall-derived
  memory source. · **Check:** construct one turn in each of `NOTHING_RELEVANT`, `WITHHELD` and
  `UNAVAILABLE`; read the `source_registry_snapshot` for each directly. · *Fails if* the snapshot
  holds any recall-derived memory source in any of the three. The registry is asserted directly and
  not through a generated citation, because a model that simply declines to cite would otherwise
  mask a wrongly populated registry.

- **AC-8** — The standing behavioural-stance layer does not defeat the absence state. · **Check:** an
  authenticated turn where `_inject_behavioural_stances` returns at least one item and recall admits
  none; read the status and the rendered sections. · *Fails if* the status is anything other than
  exactly `NOTHING_RELEVANT`, or if the behavioural section's presence suppresses the
  `NOTHING_RELEVANT` line, or if the behavioural section is itself suppressed by that line. This
  criterion fails against today's code, which is why it is here.

- **AC-9** — The type change does not silently break the ADR-0147 digest, and the digest respects
  the recall-layer scoping. · **Check:** build a planner digest on a turn in each of the four states,
  including the AC-8 turn where recall is empty and behavioural stances are present. · *Fails if*
  the digest raises, or if a non-populated state produces a digest containing any **recall-derived**
  item. Standing behavioural items may appear in any state — ADR-0147 AC-3 stratifies on exactly
  that case — so a digest holding only behavioural items is a pass, not a failure.

- **AC-10** — The configured bound traces to a committed calibration against the serving arm. ·
  **Check:** the calibration artifact exists in the repository, names the embedder arm and date it
  measured, and its reported bound equals the configured value; re-run the parity gate assertion the
  artifact records. · *Fails if* the configured value has no artifact behind it, if the artifact
  names an arm other than the one currently serving, or if the artifact is present but its bound and
  the configured value disagree. A hand-written constant that happens to satisfy AC-4 and AC-5 fails
  here, which is the point.

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
- FRE-694 — the embedder separation ceiling; `docs/research/2026-06-29-fre-694-embedder-separation.md`
- FRE-1170 and FRE-240 — the same shape in the reranker; named, not decided
- ADR-0138 — the citation contract; D1 default-deny, D2 admissibility, D6 the prompt deletion
- ADR-0147 — memory belongs to the planner; §D3 reserved this work and states the superset/subset fact
- ADR-0126 — the curated standing behavioural stance layer that D2 scopes out of the status
- ADR-0100 — `recall_similarity_floor`, the dense arm's noise guard, which D4 explicitly does not
  use to discharge its obligation
- ADR-0103 §4 and ADR-0104 AC-5 — the recall core orders without gating; D4 sits above that boundary
- FRE-1114 and FRE-1115 — blank and unusable entity content, the defect behind D2's unrenderable-item rule
- ADR-0039 — proactive memory scoring, the origin of the weights
- `src/personal_agent/memory/proactive.py`, `src/personal_agent/memory/service.py`,
  `src/personal_agent/request_gateway/context.py`, `src/personal_agent/request_gateway/budget.py`,
  `src/personal_agent/orchestrator/executor.py`

---

## Status Updates

### 2026-09-10 - Proposed
**Changed By:** adr seat, with the owner
**Reason:** Authored after a two-day exploration on FRE-1118. The session established that two of
the ticket's three mechanisms had already shipped under ADR-0138, that the remainder is a missing
representation rather than a missing score, and — after the owner challenged a proposed deferral —
that absence is not reachable today. Codex round 1 corrected the reachability argument's
statistics, found the standing-stance layer as an independent and stronger cause, and found two of
the three admitting paths ungoverned by the first draft's gate. Codex round 2 then found that the
revised gate still collided with the recall core's ordering contract, which moved the obligation to
the admission boundary; that `WITHHELD` was claiming capacity for items that were simply
unrenderable; that the entity-match escape hatch contradicted D4's own rule; and that four criteria
still permitted a wrong outcome to pass.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
