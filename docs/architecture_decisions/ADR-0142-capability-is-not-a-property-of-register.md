# ADR-0142: Capability Is Not a Property of Register — a Turn Earns Its Budget by Demonstrating Need, and the User Arbitrates

**Status:** Proposed
**Date:** 2026-09-05
**Deciders:** Owner (architect); adr seat (Opus)
**Tags:** routing, request-gateway, orchestrator, governance, cost-control, transport

---

## Context

**What is the issue we are addressing?**

Stage 4 classifies every user message into a `TaskType`. That label then decides two things the
user never asked about: how many tool iterations the turn may spend, and whether the turn may
expand into sub-agents at all. A turn labelled `conversational` is capped at 6 iterations, against
8 for `memory_recall` and 25 for analysis, planning, tool_use, delegation and self_improve.
`decomposition.py:103` returns `SINGLE` for it unconditionally, with the reason string
`conversational_always_single`. Complexity is computed and then never consulted.

FRE-1288 filed this as a claim about tone. The measurements say something narrower and worse.

### The label is an absence of evidence, not a classification

The FRE-1377 router review replayed the deployed classifier over every capture carrying a
`user_message` (n=2125, 2026-05 to 2026-09). The ladder's *ordering* decides 3.8% of messages. Its
*no-match fallback* decides 78.3%. In the recent real window (non-eval, from 2026-07, n=417) the
fallback decides 81.3%.

`intent.py:343` names the signal literally: `no_special_patterns`. There is no `RESEARCH` member in
the `TaskType` enum. So the system reads the absence of a keyword as positive evidence of a
low-capability request, and that reading governs four turns in five.

### The bucket is not homogeneous

Of 339 recent `conversational` turns, 204 used at least one tool, 128 called `web_search`, and 55
ran for a minute or more. At the same time 81 of them are five words or fewer, and the label fits
those perfectly. One label, assigned by a missing keyword, covers both `"Yes"` and a multi-source
regulatory comparison. It grants both the same allowance.

### The operator has already built a workaround

The same question, asked eight times over six days, classified `conversational` four times and
`analysis` four times. The only difference is the word "Research" at the front. Across the recent
window, 20 of 21 turns opening with a steering verb escape the `conversational` lane, while 338 of
396 turns without one (85.4%) do not.

A manual routing control already exists. It is undocumented, unlabelled, and spelled as an
incantation at the start of a sentence. To exercise its own research path, the owner has to phrase
the question the way a regex expects.

### The two effects are different sizes

The 6-iteration cap binds on 13 of 504 `conversational` turns (2.6%), average 1.42 iterations. The
expansion denial applies to 504 of 504, unconditionally. FRE-1288 is right that the second effect is
the larger one.

### Removing the cap removes an unintended brake

Four mechanisms bound a runaway turn today:

1. **The loop gate** (ADR-0063, `loop_gate.py`). Per-turn, per-tool finite state machines over three
   signals: a repeated `(tool, args)` signature, an identical output hash seen twice, and N
   consecutive calls of one tool. Defaults are `loop_max_per_signature=1` and
   `loop_max_consecutive=2`, with terminal escalation at signature identity plus 2.
2. **The iteration ceiling.** At `max - 2` it injects a wrap-up warning. At the limit it raises an
   ADR-0076 constraint pause offering "Continue (10 more)" or "Finish now". Only when the user
   declines, the pause times out, or no client is attached does `force_synthesis_from_limit` fire.
3. **The turn deadline.** 900 seconds of wall clock, enforced at the model-call seam.
4. **The tool result digest.** Bounds what one result contributes to context.

The first three catch **repetition**, **elapsed time**, and **single-result size**. None catches
**drift** — a turn making twenty distinct, non-repeating, individually reasonable tool calls that
collectively go nowhere. The loop gate sees twenty different signatures and allows every one.

For 78% of traffic the 6-iteration cap is the drift control. Not by design. It is a capability
allowance that happens to terminate early. Trace 515625b3 shows the far end when the ceiling is
distant: input grew from 29,527 to 76,706 tokens across three rounds, the turn hit the 900-second
deadline, and it produced a 160-character reply. The deadline is a wall, not a brake.

So widening the allowance without replacing that brake does not merely cost more. It removes the de
facto bound from the majority of traffic and substitutes nothing.

### Why a better classifier is not the answer

FRE-1337's probe arm was run for the first time on 2026-09-04. On its seven committed fixtures three
models agreed unanimously. Pointed at 60 randomly sampled real turns, the same probe agrees with the
deterministic cascade 70% of the time and the two models agree with **each other** only 84.2%. A
second opinion that disagrees with a third opinion one time in six is not ground truth.

Latency rules out the hot path independently: 7.93s median on the local primary against a 13.5s
median `conversational` turn, and 3 of 60 local calls returned `503` from the shared single GPU, one
after 324.8 seconds of retries.

### What needs to be decided

Whether the taxonomy may allocate capability at all; what bounds cost once it stops; what observable
establishes that a turn needs more than the baseline; and who resolves the ambiguity when that
observable cannot distinguish productive work from circular work.

---

## Decision

**Capability is not a property of conversational register. A turn receives a uniform baseline, and it
earns more by demonstrating need. Where the demonstration is ambiguous, the user arbitrates through
the mechanism ADR-0076 already built.**

Four decisions.

### D1 — The task type stops allocating capability

`orchestrator_max_tool_iterations_by_task_type` is deleted. Every turn resolves to
`orchestrator_max_tool_iterations` (25), plus any grant the user has explicitly made.

`conversational_always_single` is deleted from `decomposition.py`. `CONVERSATIONAL` is assessed
through the same complexity matrix as every other non-special type. The complexity estimate the
pipeline already computes, and currently discards for this branch, becomes load-bearing.

`MEMORY_RECALL`, `SELF_IMPROVE` and `TOOL_USE` keep their `SINGLE` forcings. Those rest on the shape
of the work, not on the register of the request.

The classifier keeps its stated job. `intent.py:6` already says classification drives context
assembly, not capability. This decision makes the code match the docstring.

**This ADR does not re-cut the taxonomy.** Adding a `RESEARCH` rung would improve the 3.8% the ladder
decides and leave the 78% untouched. The fallback is the subject; the rungs are not.

### D2 — Two ceilings replace the label

**Fan-out is bounded by the brainstem budget.** `governance.expansion_budget` is computed from live
CPU and memory on every turn, emitted, and today read exactly once as a zero-check. Two turns on
2026-09-04 carried `budget=1` and spawned four sub-agents. The expansion controller takes the minimum
of its per-strategy constant and the live budget. This is FRE-1382, and it becomes a precondition of
D1 rather than a later improvement, because D1 removes the only thing currently bounding fan-out for
this population.

**Per-turn tool cost is bounded by a spend threshold that raises a question.** When a turn's tool
iterations cross a configured threshold below the ceiling, the turn pauses and asks (D3).

**The trigger is spend, not drift.** This is deliberate, and an earlier draft of this ADR had it the
wrong way round. Drift has two forms. One is **redundancy** — the same information gathered again
under different queries. The other is **irrelevance** — twenty distinct, non-repeating, individually
reasonable results that do not answer the question. That second form is the failure this ADR's
Context describes, and **no telemetry signal detects it**, because irrelevant results are perfectly
novel. A design that triggered on a novelty signal would let exactly its own motivating failure run
free, and worse, would read that failure's high novelty as evidence of productive work.

So spend is the trigger, because spend is the only thing that is true of both forms.

**Novelty is evidence carried by the question, not the trigger for it.** The loop gate already hashes
tool output for its identity check, and that hash is exact-match, so near-duplicates pass. Widening
it to detect near-duplicates gives the pause something concrete to show — for example, that the last
six calls returned two new sources. The user reads that alongside the question. It informs the
answer; it does not decide it.

**Near-duplicate rates on real traffic are unmeasured.** No claim is made here about how often the
novelty measure fires or what its threshold is. Because it only decorates a question, being wrong
about it degrades the card's usefulness rather than the turn's bound. The spend threshold is what
must be right, and it is a count, not an inference.

### D3 — Crossing the spend threshold raises a constraint pause; it does not kill and it does not merely log

A drifting turn and a genuinely hard research turn read identically on every available observable —
iterations consumed, context growth, elapsed time, distinct searches. No threshold separates them,
because the difference is not in the telemetry. It is in what the person asked for.

That person is reachable. `_maybe_pause_for_constraint` (ADR-0076, `executor.py:640`) pushes a typed
`CONSTRAINT_PAUSE` event over the WebSocket transport, blocks the executor, survives a client
reconnect (FRE-928), surfaces a `WAITING_FOR_CHOICE` phase so the wait is honest, and falls back to a
safe default when no client answers. The PWA renders it as a `DecisionCard`. It is already wired to
the iteration ceiling.

So the threshold's output is a question, not a verdict. The mechanism's job drops from *decide
whether this turn deserves more* to *know when to ask*. That is a materially weaker requirement, and
a spend count meets it where no inference could meet the stronger one.

**The pause fires at most twice per turn**, and this is a decision, not a hope: once at the spend
threshold, once at the ceiling (the existing `tool_iteration_limit` pause). A turn granted a
continuation does not re-ask at the same threshold. Without this bound D4a's deadline credit is
unbounded, so the cardinality is load-bearing and is stated here rather than left to a risk note.

**This is also the escalation trigger for demonstrated-need routing.** A turn that has spent real
work and is still returning novel results has demonstrated need in a way no pre-turn classifier can
predict from a ten-word message. The spend threshold and the escalation trigger are the same
crossing, read for opposite purposes. They are decided together here so that they cannot disagree
later.

Demonstrated-need escalation is not built by this ADR. Today `ctx.expansion_strategy` comes from the
gateway and from nowhere else, and it is consumed before the tool loop starts, through two dispatch
paths rather than one — `ExpansionController.execute` in enforced mode (`executor.py:4786`) and
`execute_hybrid` in autonomous mode (`executor.py:6134`). No mid-turn upgrade path exists on either.
D3 fixes the *trigger* and its *output surface* so that adding one later is a wiring change rather
than a second design. That both dispatch paths would need the wiring is recorded here so the later
estimate is not taken against a single seam.

### D4 — Pause economics

**D4a — Human wait time does not consume the turn, and a second bound stops that becoming
unbounded.** `turn_started_monotonic` is stamped once at context creation and never adjusted, so
`_turn_deadline_remaining` charges a pause to the turn's 900-second budget. At the 180-second default
pause timeout, one unanswered pause spends 20% of the turn. Two spend 40%.

The deadline is extended by the duration of each `WAITING_FOR_CHOICE` interval, so
`orchestrator_task_timeout_seconds` becomes a **work** budget rather than a wall-clock one.

That change alone is unsafe, and this ADR is the reason it does not ship alone. Pauses already recur
in production — attachment cost (`executor.py:3273`), artifact-builder selection (`4413`), context
compression (`5181`), and the iteration limit (`6413`), which grants 10 more iterations on every
"Continue". Crediting each one back turns turn lifetime into `900s of work + N × 180s of waiting`
with no bound on N. A turn at 800s elapsed that takes one unanswered 180-second pause would otherwise
live to roughly 1,080 seconds, and nothing stops the next one.

Two bounds therefore accompany the credit:

- **An absolute lifetime cap**, `orchestrator_turn_lifetime_seconds`, proposed at 1800. It is
  wall-clock from `turn_started_monotonic`, it is never extended by anything, and it terminates the
  turn through the existing synthesis path. The work budget bounds work; this bounds the clock.
- **A creditable-pause limit**, proposed at 3 per turn. Pauses beyond it still function, and are
  still offered, but their wait is charged to the work budget as it is today.

Both numbers are proposals open to tuning during implementation. The structure — a work budget, an
unextendable lifetime cap, and a cap on how many waits may be credited — is the decision.

**D4b — An expansion or drift grant may not be remembered.** The pause helper accepts
`allow_preference=False` precisely so that a remembered "always proceed" can never silently spend
money (ADR-0101 §8b, FRE-691). An unbounded capability grant is the same class of decision. The drift
constraint carries `allow_preference=False`. A stored preference that reads "always continue" would
otherwise restore, silently and permanently, the exact unbounded case D2 exists to prevent.

**D4c — No client means the safe default, and evals therefore measure a different system.** CLI,
headless and evaluation turns have no socket. The safe default applies on timeout, so those turns
terminate at the baseline rather than escalating. This is correct behaviour and it is stated here so
that a future eval disagreeing with live behaviour is not read as a regression.

---

## Alternatives Considered

### Option 1: Invert the fallback polarity

**Description:** Keep the taxonomy allocating capability, but flip the default. A message matching no
rung receives the capable lane. Restrict only on positive evidence of a cheap turn — a short message,
a follow-up, a greeting.

**Pros:**
- Attacks the 78% directly, which is the measured problem.
- Very small change: one branch in `intent.py` and one in `decomposition.py`.
- Preserves the existing structure, so it is easy to revert.

**Cons:**
- Keeps a lexical signal deciding capability. The shape of the defect is unchanged; only the sign
  moves.
- Names no ceiling, so it fails FRE-1288's own "It fails if" clause.
- The new restrict-list becomes the next thing that mis-sorts, and it mis-sorts in the expensive
  direction rather than the cheap one.

**Why Rejected:** The ticket forbids re-cutting on another surface-form signal, and this is that,
inverted. It also converts a cheap failure mode into an expensive one while removing the accidental
drift brake described in the Context, with nothing put in its place.

### Option 2: Add a `RESEARCH` task type

**Description:** Extend the `TaskType` enum and add a rung to the ladder that recognises
research-shaped questions, routing them to `HYBRID` at moderate complexity.

**Pros:**
- Directly expresses a category the taxonomy genuinely lacks.
- Fits the existing architecture with no new mechanism.
- FRE-1337's fixture arm supports it: three models unanimously called the research fixtures
  `analysis` where the cascade said `conversational`.

**Why Rejected:** It improves the 3.8% of messages the ladder's ordering decides and leaves the 78%
that the fallback decides exactly where they are. The fixture unanimity that appears to support it is
a property of the fixtures: on 60 real messages the same models agree with the cascade only 70% of
the time and with each other 84.2%. A new rung would also have to be reached by a regex, which is the
mechanism that produced the incantation the owner is already typing.

### Option 3: A model classifier, in the hot path or as a gated arbiter

**Description:** Call a model to classify, either on every turn or only when no rung matches and the
message exceeds a length threshold.

**Pros:**
- Reads meaning rather than surface form, which is the axis the ticket says matters.
- The instrument already exists and is committed (FRE-1337's harness).

**Why Rejected:** Measured latency is 7.93s median and 14.41s p90 on the local primary, against an
18.0s median turn and a 13.5s median `conversational` turn. Worse, 3 of 60 local calls returned
`503` from the shared GPU, one after 324.8 seconds of retries — a hard-failure rate on a call that
must complete before the turn can start. Cloud arbitration at 2.26s is affordable but is not an
oracle: the two models agree with each other on 84.2% of real messages. Adopting one swaps a legible
failure (a regex matched the wrong substring) for an illegible one. The length-gated variant was
drafted, measured and dropped in the FRE-1377 study: every substantive disagreement in the sample sat
in messages under 15 words, so the gate selects the wrong population.

### Option 4: Trigger on a novelty signal rather than on spend

**Description:** Detect drift directly. Widen the loop gate's exact-match output hash to catch
near-duplicates, and raise the pause when a turn's recent results stop returning new information.

**Pros:**
- Targets drift itself rather than a proxy for it, so healthy expensive turns are never interrupted.
- Reuses an instrument that already exists, and reads it at no inference cost.

**Why Rejected — and this ADR shipped it in an earlier draft before catching the inversion.** Drift
has two forms. Redundancy is detectable this way. Irrelevance is not: twenty distinct, non-repeating,
useless results are perfectly novel. Irrelevance is the failure this ADR's Context describes, so a
novelty trigger would let the motivating case run free while stopping the milder one. Worse, D3 reads
sustained work as evidence of genuine need, so high novelty on a useless turn would read as a reason
to *grant* more. The signal survives as evidence carried by the card, where being wrong costs
usefulness rather than the bound.

### Option 5: Make the threshold a terminator instead of a question

**Description:** When the spend threshold is crossed, cut the turn and force synthesis. No pause, no
user involvement.

**Pros:**
- Works headless, where no user is reachable.
- Bounds cost with no transport dependency and no human latency.
- It is what the 6-iteration cap effectively does today for 78% of traffic.

**Why Rejected:** It is the current behaviour, and the current behaviour is the defect. A hard
research question and a drifting one cross the same threshold, and terminating both is precisely the
allocation error this ADR exists to correct — merely relocated from the classifier to a counter. The
pause achieves the same bound while routing the ambiguity to the only party who can resolve it. D4c
keeps the terminator where no one can be asked.

### Option 6: Ship the measurement only and decide later

**Description:** Emit the spend and novelty measurements as telemetry, change no behaviour, and
revisit once the data exists.

**Pros:**
- Zero behavioural risk.
- Produces the threshold measurement D2 admits is missing.

**Why Rejected:** Not rejected as a step — the novelty measure ships this way. Rejected as the *end
state*, because D1 removes the current brake on the same schedule. A pure observer would leave 78% of
traffic with a 25-iteration ceiling and no control other than the 900-second wall, which is the
condition trace 515625b3 documents.

---

## Consequences

### Positive Consequences

- A research-shaped question receives research capability without the owner having to prefix a verb.
  The undocumented incantation stops being load-bearing.
- The complexity estimate the pipeline already computes becomes real for 78% of traffic instead of
  being computed and discarded.
- `governance.expansion_budget` becomes a working load-shed signal rather than a boolean, so
  brainstem pressure actually reduces fan-out.
- A turn that spends heavily acquires a control for the first time. Today nothing intervenes between
  the iteration ceiling and the 900-second wall.
- The turn deadline starts measuring work rather than work plus human latency, which also improves
  every existing ADR-0076 pause, and it gains an explicit lifetime bound it never had.
- The classifier's job narrows to what its own docstring claims, which makes the next router change
  smaller.

### Negative Consequences

- Cost rises. 78% of traffic moves from a 6-iteration ceiling to 25. The average
  `conversational` turn uses 1.42 iterations, so the typical turn is unaffected, but the tail widens
  and the tail is where spend lives.
- More turns interrupt the user. The pause is the mechanism, and a mechanism that asks is a mechanism
  that intrudes. D4b forbids the preference that would silence it, so the intrusion cannot be
  configured away for this constraint.
- Live and headless behaviour diverge by design (D4c). Evaluation results and PWA behaviour are no
  longer directly comparable on any turn that would have escalated.
- Deleting `orchestrator_max_tool_iterations_by_task_type` removes a tuning surface that currently
  works, in exchange for one that is not yet measured.
- The change is sequenced behind three tickets, so the defect the owner hits today persists until
  they land.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| The spend threshold is set too low and the pause fires on healthy turns | High | The threshold is a count, not an inference, and it is tunable without redesign. The recent-window distribution (average 1.42 iterations, 13 of 504 turns at or above 6) sizes it before it arms. AC-2 seeds a known high-spend turn rather than waiting for one. |
| Cost rises materially once 78% of traffic gets a 25-iteration ceiling | High | D2's brainstem budget binds fan-out; the spend-threshold pause bounds iterations. AC-6 asserts the budget is respected rather than assumed, on a window that must contain real expansions. |
| The novelty measure is wrong, so the card shows misleading evidence | Low | It decorates a question rather than deciding it. A wrong measure degrades the card and never the bound. This is why D2 moved it off the trigger. |
| Ask-fatigue drives the owner to disable the pause | Medium | D4b prevents a stored preference for this constraint, so the pressure surfaces as a complaint rather than as a silent unbounded grant. If it becomes intolerable, the threshold is wrong and AC-2's measurement is the place to fix it. |
| Expansion is opened before sub-agents can use it | High | FRE-1389 (sub-agents hold no tools) is a precondition. A research arm dispatched to a toolless sub-agent cannot search, so opening the lane first would measure a mechanism whose benefit is unbuilt. |
| Extending the deadline across pauses lets a turn live far beyond 900 seconds of clock | High | D4a pairs the credit with two bounds stated as decisions rather than intentions: an unextendable `orchestrator_turn_lifetime_seconds` cap, and a creditable-pause limit. D3 additionally caps this ADR's own pause at two per turn. AC-7 tests the composition by taking three pauses. |
| The spend threshold and a later escalation trigger disagree | Medium | D3 decides them as one crossing. This ADR is the record that they share a definition. |

---

## Implementation Notes

**Files affected:**

- `src/personal_agent/config/settings.py` — delete `orchestrator_max_tool_iterations_by_task_type`;
  add the spend threshold, `orchestrator_turn_lifetime_seconds`, and the creditable-pause limit.
- `src/personal_agent/orchestrator/executor.py` — `_resolve_max_iterations` (D1);
  `_turn_deadline_remaining`, the lifetime cap and the `WAITING_FOR_CHOICE` span (D4a); the spend
  check and its pause call site (D3).
- `src/personal_agent/request_gateway/decomposition.py` — delete the `CONVERSATIONAL` branch (D1).
- `src/personal_agent/orchestrator/expansion_controller.py` — take the minimum of `_MAX_TASKS` and
  `governance.expansion_budget` (D2). The budget reaches only the gateway log today
  (`request_gateway/pipeline.py`), so plumbing it to the controller is part of the work.
- `src/personal_agent/orchestrator/loop_gate.py` — the near-duplicate novelty measure, widening the
  existing exact-match output hash. Evidence for the card only (D2).
- `src/personal_agent/orchestrator/constraint_options.py` — a new constraint entry with
  `allow_preference=False` (D3, D4b).
- `src/personal_agent/orchestrator/types.py` — credited pause duration and pause count on
  `ExecutionContext` (D3, D4a).
- `src/personal_agent/observability/route_trace/types.py` — the effective ceiling and the constraint
  resolution (AC-1, AC-5).
- `src/personal_agent/transport/events.py`, `transport/agui/transport.py` — timestamp the
  `WAITING_FOR_CHOICE` span end (AC-3).

**Sequence.** FRE-1389 and FRE-1382 land before D1. The spend-threshold pause arms before D1 removes
the ceiling. Removing the ceiling first leaves the 900-second deadline as the sole bound on 78% of
traffic.

**Instrumentation this ADR requires, because its criteria are not checkable without it.** Three
fields do not exist today and several criteria below name them:

- `RouteTraceRow` carries no effective tool ceiling and no constraint resolution. Both are added, so
  AC-1 and AC-5 are answerable from the ledger rather than by inference.
- The `WAITING_FOR_CHOICE` span emits a timestamped start and an untimestamped paired end
  (`transport/agui/transport.py`, `transport/events.py`). The end is timestamped, so a pause has a
  measurable duration.
- `ExecutionContext` accumulates credited pause duration and a pause count, which AC-3 and AC-7 read.

Adding instrumentation to make a criterion checkable is part of the work, not a precondition of it.
Recorded here so that no implementation ticket discovers the gap at adjudication time.

**Testing strategy.** Unit tests for `_resolve_max_iterations` returning a task-type-independent
ceiling, for the expansion-budget minimum, and for the deadline arithmetic across a simulated pause.
A seeded high-spend fixture is required — a clean corpus cannot demonstrate that a threshold fires.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

Adjudicated on FRE-1288 once the implementation chain has landed and deployed.

- **AC-1** — The effective tool ceiling is independent of the task type, across the whole deployed
  population and not merely on a chosen pair. · **Check:** over a deployed window of at least 200
  turns spanning at least three distinct `task_type` values, group `route_traces` by `task_type` and
  compare the recorded effective ceiling; then submit both phrasings of the F19 General Product
  Safety question and compare their ceiling and `strategy`. · *Fails if* any two task types show
  different ceilings, or if the steering-verb phrasing receives a different ceiling or strategy. A
  ceiling that still branches on type fails the population check even when a single pair happens to
  agree.

- **AC-2** — A turn crossing the spend threshold actually blocks on the user, and resumes only on a
  decision. · **Check:** run a seeded probe that issues distinct queries past the threshold; assert a
  `WAITING_FOR_CHOICE` span with non-zero duration, a recorded resolution naming the chosen
  `action_id`, and that the first tool call after the threshold is timestamped after that resolution.
  · *Fails if* the pause event is emitted while execution continues, if the span duration is zero, or
  if the turn reaches `orchestrator_task_timeout_seconds` with no pause offered. An emit-and-continue
  implementation fails on the timestamp ordering.

- **AC-3** — Human wait time does not consume the turn's working budget. · **Check:** two-level. In
  process, hold the `ExecutionContext` and assert `_turn_deadline_remaining(ctx)` is unchanged, within
  tolerance, either side of a simulated pause of duration *d*. On a deployed turn, assert the recorded
  credited-pause total equals the sum of that turn's `WAITING_FOR_CHOICE` span durations. · *Fails if*
  the in-process remaining budget decreased by approximately *d*, or if the deployed credited total is
  zero on a turn that demonstrably paused.

- **AC-4** — A stored preference cannot silently grant unbounded capability, even when one is present
  in storage. · **Check:** write a preference row for the spend-threshold constraint **directly to the
  preference store**, bypassing whatever API-level guard rejects it, then run the AC-2 probe; assert
  the pause is still raised and no `constraint_preference_applied` event names this constraint. ·
  *Fails if* the probe proceeds past the threshold without asking. Seeding at the storage layer is the
  point: an implementation that merely declines to *write* the preference, while still *honouring* a
  row that exists, passes an API-level test and fails this one.

- **AC-5** — A headless high-spend turn terminates at the baseline, not at the deadline. · **Check:**
  run the AC-2 probe over the CLI with no WebSocket attached; assert it resolves through the safe
  default and its `route_traces` row records that resolution as no-client. · *Fails if* it reaches the
  900-second deadline, or if it escalates without a client having answered.

- **AC-6** — No turn spawns more sub-agents than its live expansion budget permits, measured on a
  population that actually expanded. · **Check:** over a deployed window containing at least 20 turns
  whose strategy is not `SINGLE` and at least 5 carrying `expansion_budget < 3`, join each turn's
  `governance.expansion_budget` to its dispatched sub-agent count. · *Fails if* any turn's sub-agent
  count exceeds its budget — the condition measured on 2026-09-04, where two turns carrying `budget=1`
  each spawned four — **or if the window cannot be populated**, which means expansion is not running
  and the criterion has proved nothing.

- **AC-7** — A turn's total wall-clock lifetime is bounded even when it pauses repeatedly. · **Check:**
  run a probe that takes three or more pauses, letting each reach the 180-second timeout; assert the
  turn terminates at or before `orchestrator_turn_lifetime_seconds`, and that its credited-pause total
  stops increasing after the creditable-pause limit. · *Fails if* total lifetime exceeds the cap, or if
  every pause is credited without limit — the unbounded composition of D4a and D3 that this criterion
  exists to catch.

**Where these are adjudicated.** On FRE-1288, once the implementation chain has landed and deployed —
not at merge of this ADR, and not by any single implementation ticket.

---

## References

- [ADR-0076](ADR-0076-adaptive-constraint-governance.md) — adaptive constraint governance; supplies
  the pause mechanism D3 and D4 build on
- [ADR-0063](ADR-0063-primitive-tools-action-boundary-governance.md) §D5 — the tool loop gate whose
  exact-match output hash D2's novelty measure widens
- [ADR-0138](ADR-0138-the-model-may-generate-but-may-not-assert.md) — the grounding contract FRE-1288
  was broken out of; explicitly does not cover capability allocation
- [ADR-0101](ADR-0101-agent-vision-ingestion.md) §8b — the `allow_preference=False` precedent D4b
  extends from spend to capability
- `docs/research/2026-09-04-fre-1377-router-design-review.md` — F2, F6, F13, F14, F16, F17, F19, F20;
  every measurement cited above
- FRE-1288 — this ADR's umbrella ticket
- FRE-1382 — the brainstem expansion budget; a precondition of D1
- FRE-1389 — sub-agents hold no tools; a precondition of opening the expansion lane
- FRE-1377 — the router design review commission
- FRE-1337 — the intent probe harness whose arm 2 supplies the model-agreement numbers
- `telemetry/evaluation/fre1337-intent-probe/2026-09-04-fre1377.md` — the probe artifact

---

## Status Updates

### 2026-09-05 - Proposed
**Changed By:** adr seat (Opus), on owner direction
**Reason:** Authored from FRE-1288 after the FRE-1377 router review supplied the population sizes the
decision needed. The owner set the target as demonstrated-need routing, reached through the
capability-decoupling step, and raised runaway-loop control as the gap the first draft had not
covered. D3 and D4 exist because of that challenge.

Revised the same day after Codex review, round 1. Three changes of substance. **D2's trigger was
inverted**: it fired on a novelty signal, which cannot see the irrelevance form of drift that the
Context describes, and which D3 would have read as evidence of productive work. Spend is now the
trigger and novelty is evidence carried by the card. **D4a was unbounded**: crediting every pause
back to the deadline made turn lifetime `900s + N × 180s` with no bound on N, and the one-shot claim
that would have contained it existed only in a risk table. An unextendable lifetime cap and a
creditable-pause limit are now decisions, and D3 states its own pause cardinality. **Four of six
criteria admitted a broken implementation** and AC-3 had no measurement seam; all six were rewritten,
AC-7 was added for the lifetime bound, and the instrumentation the criteria depend on is now named in
the Implementation Notes. Two factual corrections: `memory_recall` is 8, not 25, and expansion has two
pre-tool-loop dispatch paths rather than one.
