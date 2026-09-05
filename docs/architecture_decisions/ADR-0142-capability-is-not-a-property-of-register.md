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
expand into sub-agents at all. A turn labelled `conversational` is capped at 6 iterations against
25 for every other type, and `decomposition.py:103` returns `SINGLE` for it unconditionally, with
the reason string `conversational_always_single`. Complexity is computed and then never consulted.

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

**Per-turn tool cost is bounded by a drift signal.** The signal is **novelty of retrieved content**.
The loop gate already hashes tool output for its identity check, but that hash is exact-match, so
near-duplicates pass. The drift signal widens the existing instrument: a turn whose recent results
repeat information already gathered is drifting, even when its queries differ.

**Near-duplicate rates on real traffic are unmeasured.** No claim is made here about the threshold or
about how often the signal fires. Establishing both is the first implementation ticket's work, and
the signal ships observable-only until that measurement exists.

### D3 — The drift signal raises a constraint pause; it does not kill and it does not merely log

A drifting turn and a genuinely hard research turn read identically on every available observable —
iterations consumed, context growth, elapsed time, distinct searches. No threshold separates them,
because the difference is not in the telemetry. It is in what the person asked for.

That person is reachable. `_maybe_pause_for_constraint` (ADR-0076, `executor.py:640`) pushes a typed
`CONSTRAINT_PAUSE` event over the WebSocket transport, blocks the executor, survives a client
reconnect (FRE-928), surfaces a `WAITING_FOR_CHOICE` phase so the wait is honest, and falls back to a
safe default when no client answers. The PWA renders it as a `DecisionCard`. It is already wired to
the iteration ceiling.

So the drift signal's output is a question, not a verdict. Its job drops from *decide whether this
turn deserves more* to *know when to ask*. That is a materially weaker requirement, and signals that
exist today can meet it where none could meet the stronger one.

**This is also the escalation trigger for demonstrated-need routing.** A turn that has spent real
work and is still returning novel results has demonstrated need in a way no pre-turn classifier can
predict from a ten-word message. The drift detector and the escalation trigger are one instrument
read with opposite sign. They are decided together here so that they cannot disagree later.

Demonstrated-need escalation is not built by this ADR. Expansion is entered once today, at
`executor.py:4780`, from the gateway's strategy, before the tool loop starts. There is no mid-turn
entry. D3 fixes the *signal* and its *output surface* so that building the entry later is a wiring
change rather than a second design.

### D4 — Pause economics

**D4a — Human wait time does not consume the turn.** `turn_started_monotonic` is stamped once at
context creation and never adjusted, so `_turn_deadline_remaining` charges a pause to the turn's
900-second budget. At the 180-second default pause timeout, one unanswered pause spends 20% of the
turn. Two spend 40%. The deadline is extended by the duration of each `WAITING_FOR_CHOICE` interval,
so the budget measures work and not human latency.

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

### Option 4: Make the drift signal a terminator

**Description:** Ship the novelty signal as an automatic kill. A turn whose recent results stop
returning new information is cut and forced to synthesise.

**Pros:**
- Works headless, where no user is reachable.
- Bounds cost with no human in the loop and no transport dependency.

**Why Rejected:** The threshold has never been measured against real traffic, and the signal cannot
distinguish a drifting turn from a hard one. Terminating on an unvalidated threshold kills good turns,
and it does so invisibly — the class of failure this project has repeatedly paid for. The pause
achieves the same bound while routing the ambiguity to the only party that can resolve it. D4c keeps
the terminator available where no one can be asked.

### Option 5: Ship the drift signal observable-only and stop there

**Description:** Emit the novelty measurement as telemetry, change no behaviour, and decide later.

**Pros:**
- Zero behavioural risk.
- Produces the threshold measurement D2 admits is missing.

**Why Rejected:** Not rejected as a step — D2 requires exactly this before the pause arms. Rejected as
the *end state*, because D1 removes the current brake on the same schedule. A pure observer would
leave 78% of traffic with a 25-iteration ceiling and no drift control other than the 900-second wall,
which is the condition trace 515625b3 documents.

---

## Consequences

### Positive Consequences

- A research-shaped question receives research capability without the owner having to prefix a verb.
  The undocumented incantation stops being load-bearing.
- The complexity estimate the pipeline already computes becomes real for 78% of traffic instead of
  being computed and discarded.
- `governance.expansion_budget` becomes a working load-shed signal rather than a boolean, so
  brainstem pressure actually reduces fan-out.
- Drift acquires a control for the first time. Today no mechanism detects a turn making twenty
  distinct useless calls.
- The turn deadline starts measuring work rather than work plus human latency, which also improves
  every existing ADR-0076 pause.
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
| The novelty threshold is wrong and the pause fires on healthy turns | High | D2 ships the signal observable-only first; the threshold is set from measured near-duplicate rates before the pause arms. AC-2 seeds a known-drifting turn rather than waiting for one. |
| Cost rises materially once 78% of traffic gets a 25-iteration ceiling | High | D2's brainstem budget binds fan-out; the drift pause bounds iterations. AC-6 asserts the budget is respected rather than assumed. |
| Ask-fatigue drives the owner to disable the pause | Medium | D4b prevents a stored preference for this constraint, so the pressure surfaces as a complaint rather than as a silent unbounded grant. If it becomes intolerable, the threshold is wrong and AC-2's measurement is the place to fix it. |
| Expansion is opened before sub-agents can use it | High | FRE-1389 (sub-agents hold no tools) is a precondition. A research arm dispatched to a toolless sub-agent cannot search, so opening the lane first would measure a mechanism whose benefit is unbuilt. |
| Extending the deadline across pauses lets a turn live far beyond 900 seconds of clock | Medium | The extension is bounded by the pause timeout (180s default) times the number of pauses, and the drift constraint may pause at most once per turn under D3. |
| The drift detector and a later escalation trigger disagree | Medium | D3 decides them as one instrument. This ADR is the record that they share a definition. |

---

## Implementation Notes

**Files affected:**

- `src/personal_agent/config/settings.py` — delete `orchestrator_max_tool_iterations_by_task_type`.
- `src/personal_agent/orchestrator/executor.py` — `_resolve_max_iterations` (D1);
  `_turn_deadline_remaining` and the `WAITING_FOR_CHOICE` span (D4a); the drift check and its pause
  call site (D3).
- `src/personal_agent/request_gateway/decomposition.py` — delete the `CONVERSATIONAL` branch (D1).
- `src/personal_agent/orchestrator/expansion_controller.py` — take the minimum of `_MAX_TASKS` and
  `governance.expansion_budget` (D2).
- `src/personal_agent/orchestrator/loop_gate.py` — the novelty signal, widening the existing output
  hash (D2, D3).
- `src/personal_agent/orchestrator/constraint_options.py` — a new constraint entry with
  `allow_preference=False` (D3, D4b).
- `src/personal_agent/orchestrator/types.py` — accumulated pause duration on `ExecutionContext`
  (D4a).

**Sequence.** FRE-1389 and FRE-1382 land before D1. The drift signal ships observable-only, its
threshold is measured, and only then does D1 remove the ceiling. Removing the ceiling before the
drift pause arms leaves the 900-second deadline as the sole bound on 78% of traffic.

**Testing strategy.** Unit tests for `_resolve_max_iterations` returning a task-type-independent
ceiling, for the expansion-budget minimum, and for the deadline arithmetic across a simulated pause.
A seeded drifting fixture is required — a clean corpus cannot demonstrate that a drift detector
detects anything.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

Adjudicated on FRE-1288 once the implementation chain has landed and deployed.

- **AC-1** — The same question, submitted with and without a leading steering verb, resolves to the
  same effective tool ceiling and the same decomposition strategy. · **Check:** submit both phrasings
  of the F19 General Product Safety question; compare `task_type`, the recorded effective ceiling, and
  `strategy` on the two `route_traces` rows. · *Fails if* the prefixed form receives a different
  ceiling or a different strategy, which means register still allocates capability.

- **AC-2** — A turn that is drifting is stopped by a question, before the wall-clock deadline. ·
  **Check:** run a seeded probe that issues distinct queries returning near-duplicate content; assert
  a `constraint_pause_emitted` event carrying the drift constraint appears, and that the turn ends
  without reaching `orchestrator_task_timeout_seconds`. · *Fails if* the probe reaches the 900-second
  deadline, or terminates through `force_synthesis_from_limit` with no pause offered.

- **AC-3** — Human wait time does not consume the turn's working budget. · **Check:** on a turn
  containing a `WAITING_FOR_CHOICE` span of duration *d*, compare the deadline remaining immediately
  before and immediately after the span. · *Fails if* the remaining budget decreased by approximately
  *d* rather than staying level, which means the pause is still charged to the turn.

- **AC-4** — A stored preference cannot silently grant unbounded capability. · **Check:** store a
  preference for every constraint the user is able to store, then run the AC-2 drift probe; assert the
  drift pause is still raised. · *Fails if* the probe proceeds past the drift point without asking.

- **AC-5** — A headless drifting turn terminates at the baseline, not at the deadline. · **Check:** run
  the AC-2 probe over the CLI with no WebSocket attached; assert it resolves through the safe default
  and its `route_traces` row records the no-client resolution. · *Fails if* it reaches the 900-second
  deadline, or if it escalates without a client having answered.

- **AC-6** — No turn spawns more sub-agents than its live expansion budget permits. · **Check:** over a
  deployed window, join each expansion turn's `governance.expansion_budget` to its dispatched
  sub-agent count. · *Fails if* any turn's sub-agent count exceeds its budget — the condition measured
  on 2026-09-04, where two turns carrying `budget=1` each spawned four.

**Where these are adjudicated.** On FRE-1288, once the implementation chain has landed and deployed —
not at merge of this ADR, and not by any single implementation ticket.

---

## References

- [ADR-0076](ADR-0076-adaptive-constraint-governance.md) — adaptive constraint governance; supplies
  the pause mechanism D3 and D4 build on
- [ADR-0063](ADR-0063-primitive-tools-action-boundary-governance.md) §D5 — the tool loop gate whose
  output hash D2's novelty signal widens
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
