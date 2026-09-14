# ADR-0151: A Turn That Cannot Be Grounded Says So — Enforcement Narrowed to the Turns a Retry Can Repair

**Status:** Proposed
**Date:** 2026-09-14
**Deciders:** Owner (the direction "states it could not ground", 2026-09-13; enforcement on shape B, the two retry limits, the unsettled-statement rule and the withdrawal of pre-generation forcing, 2026-09-14), `adr` seat at Opus 5 (author, the 91-turn measurement), master (the FRE-1325 note rate and the two-shape proposal this ADR revises)
**Tags:** grounding, citations, enforcement, verification, disclosure

**Amends:** ADR-0138 D4 (the remedy) and ADR-0138 D5 (pre-generation forcing). **Consumes:** the FRE-1325 note.
**Umbrella:** FRE-1328.

---

## Context

### The defect FRE-1328 filed

ADR-0138 D1 requires a citation on every non-exempt statement. ADR-0138 D2 refuses any tool whose
arguments the model writes, for example `bash` and `run_python`. So a turn that learns its facts by
running a command has no source that it can cite. D4 then blocks that turn, forces retrieval and
retries. Retrieval cannot supply what the command produced. After the last attempt, D4 replaces the
answer with a refusal. So `enforce` makes this turn shape unanswerable.

ADR-0140 T4 (Proposed) keeps arbitrary-code tools inadmissible. ADR-0139 D2 and D3 are withdrawn.
So the refusal of these tools stands, and the remedy is the part to change.

### The owner's decision, 2026-09-13

Asked which way `enforce` goes, the owner answered: **"states it could not ground"**. The turn ships
and states what it could not ground. It is not blocked and not refused. Master relayed the decision
on FRE-1328 with a proposed discriminator: a turn where a tool was refused declares mildly, and a
turn where nothing was offered is the alarming shape, where enforcement forces retrieval.

### FRE-1325 shipped the surface

FRE-1325 (Done, 2026-09-13, PR #1155) appends a system-written line to a delivered reply in
`observe` mode:

> Note: N of M factual statements in this answer are not backed by a source Seshat verified this
> turn. Check them before you rely on them.

Master measured it before merge: the line fires on 11 of the 12 most recent turns with a grounding
record. A line that fires on almost every turn teaches the reader to ignore it.

### The measurement this ADR is built on

**Population.** Every capture from 2026-08-31 to 2026-09-13 with verification available and one or
more non-exempt statements: 91 turns. ES `grounding_verification_completed` holds the same 91
turns, so ES lost no events in this window.

**Method.** The refusal fields (`refused_origin_admissibility`) exist only since FRE-1359
(2026-09-12), on 3 of the 91 events. So the shape was reconstructed from each capture's
`tools_used`, against `ARBITRARY_CODE_TOOLS` in `grounding/source_registry.py`. This is a proxy:
a tool that ran and failed offered no result to the registry. D1 below uses the registry counts,
not this proxy.

| Shape | Turns | Of which eval | Statements | Passed | Main outcomes |
|---|---|---|---|---|---|
| **A** — only arbitrary-code tools ran | 14 | 4 | 269 | 0 | 242 uncited |
| **A+B** — arbitrary-code tools and typed tools ran | 9 | 1 | 183 | 5 | 124 uncited · 36 not contained · 18 unverifiable by containment |
| **B** — only typed tools ran (`web_search` 34 turns, `fetch_url` 15, `search_memory` 8) | 38 | 1 | 775 | 21 | 273 uncited · 246 unverifiable by containment · 175 not contained · 55 source not entitled |
| **C** — no tool ran | 30 | 16 | 507 | 0 | 483 uncited |

**Not one of the 91 turns passed completely.** Five findings follow from the table.

1. **The FRE-1328 population is the smallest group.** Turns where an arbitrary-code tool ran are 23
   of 91.
2. **Shape C is not the alarming shape.** 16 of its 30 turns were eval turns. The 14 owner turns are
   general-knowledge questions and conversation, for example "What is today's date?" and a
   multi-turn exchange about a cable in a drilled conduit. Enforcement on shape C forces retrieval on
   those turns.
3. **Shape B is the largest group, and the master proposal did not name it.** The turn retrieved
   sources, and the answer did not cite them or did not match them. A retry can repair this shape
   without new retrieval, because the sources are already in the registry.
4. **The verifier did not settle 265 statements**, all in shapes B and A+B: 264 unverifiable by
   containment and 1 entailment unavailable. In the current code these count as failures in the
   note and they block in `enforce`.
5. **Excluding unsettled statements does not make the note rare.** Recomputed on the same 91 turns,
   the note still fires on 91. No turn failed only on unsettled statements.

### Three facts in the code

- `decide()` (`grounding/enforcement.py`) retries on every member of `verification.failures`, and
  `failures` is every span that did not pass (`grounding/verification.py:224`). That includes the
  `_MACHINE_UNDECIDED` outcomes, which the same module defines as "outcomes our own machinery could
  not settle".
- Heavy enforcement (ADR-0138 D5, FRE-1285) forces retrieval before generation with a directive and a
  `tool_choice` pin. The turn shape is not known before generation. Every model measures 0%
  compliant, so under `enforce` every turn is heavy, shape C included.
- `first_generation_compliant` is `verification.compliant and attempts == 1`
  (`grounding/verification.py:678`). A turn that needed a retry does not count as compliant.

---

## Decision

### D1 — Three turn shapes, classified from the source registry after generation

A turn has a shape only when verification is available and it has one or more non-exempt statements.
The shape is read from the turn record when verification runs:

| Shape | Rule | Meaning |
|---|---|---|
| **B** | `tool_results_admitted ≥ 1` | The registry holds one or more tool sources from this turn. |
| **C** | The model emitted no tool call, dispatched or not, **and** no sub-agent ran | Nothing was done to gather evidence this turn. |
| **A** | Every other turn | Tool calls were made or sub-agents ran, and the registry admitted none of their output. |

- The registry counts alone cannot define shape C. `tool_results_offered` counts only results that
  reach `register_tool_result`. A malformed call goes to `_record_undispatched_invocation`
  instead, and sub-agent reports live in `ctx.sub_agent_results`, outside `ctx.tool_results`. Both
  paths leave `tool_results_offered` at 0. Shape C must therefore read every tool-call path and
  the expansion record.
- A HYBRID turn with no admitted source is shape A. Worker findings are uncitable under ADR-0138 D2,
  and ADR-0149 records that by design.
- A turn where an arbitrary-code tool and a typed tool both ran is shape B. The typed source is
  citable, and the retry in D3 has something to cite.
- Memory recalled into the context is not a tool result. A shape C turn can hold memory sources.
  Vision attachments are message blocks, not tool results, so a vision turn with no tool call is
  shape C.

### D2 — Settled and unsettled statements

A statement that did not pass is either **settled** or **unsettled**.

- **Unsettled:** the outcomes in `_MACHINE_UNDECIDED` — `unverifiable_by_containment`,
  `entailment_required`, `entailment_unavailable`. The verifier could not decide.
- **Settled:** every other outcome that is not `passed` — `uncited`, `unresolved`,
  `source_not_entitled`, `unreachable`, `not_contained`, `not_entailed`,
  `contradicted_by_source`. A gate decided that the statement failed.

The owner named `uncited`, `not_contained` and `not_entailed`. The other four outcomes are in the
settled set by the same test: a gate reached a decision. `contradicted_by_source` is a stronger
failure than `not_contained`, so leaving it out would make no sense.

Two rules follow:

1. **An unsettled statement never triggers a retry.** A limit of the verifier is not evidence about
   the model's statement.
2. **An unsettled statement never counts as "not backed by a source" in the note.** The note counts
   settled failures only. A turn whose only failures are unsettled carries no note. When a note is
   shown and unsettled statements also exist, the note states their count separately as statements
   that Seshat could not check.

`entailment_required` is transient: inline verification replaces it with a judge verdict or
`entailment_unavailable` before a verification is delivered. It is listed for completeness.

The fix to the verifier is a separate ticket. **The compliance metric does not change.** A turn with
an unsettled statement is still not compliant under FRE-1284. The FRE-1325 scope fence on
`compliance.py` stays in force, and the verifier ticket reduces the unsettled count at its source.

### D3 — `enforce` retries shape B only, and the retry cites, it does not retrieve

This amends ADR-0138 D4.

- In `enforce`, a shape B turn with one or more **settled** failures on its first generation gets
  **one** retry.
- The retry directive tells the model to cite each statement from the sources already registered
  this turn, and to leave out a statement that no registered source supports. It does not tell the
  model to retrieve.
- The retry adds no retrieval grant: `GROUNDING_RETRY_TOOL_GRANT` is not added.
- **The retry request pins `tool_choice="none"`.** Without the pin, every non-synthesis call
  receives the normal tool set with `tool_choice` unrestricted, and the model can retrieve new
  sources on the retry. The tool list stays in the request, because removing it discards the prompt
  cache (ADR-0149 D6).
- There is at most one retry. `grounding_max_generation_attempts` narrows to the range 1 to 2:
  1 means no retry, and 2 means one retry. A configured value above 2 fails settings validation.
- **Shapes A and C never retry.** Retrieval cannot supply a command's output, and forcing retrieval on
  a general-knowledge answer is the intervention the owner rejected.
- A turn that needed the retry is **not** first-generation compliant. The current code already holds
  this. This ADR makes it an invariant.

### D4 — Every delivered turn carries the declaration, and the refusal is withdrawn

This amends ADR-0138 D4.

- **`TurnDecision.TERMINAL_NO_SOURCE` is withdrawn.** After the retry, or with no retry, the turn
  delivers its last generation. `build_no_source_statement` never replaces a reply.
- A delivered turn with one or more settled failures carries the declaration, in `observe` and in
  `enforce`. After a retry, the declaration describes the verification of the delivered generation.
- The declaration is built from the turn record and never generated. It is appended after the
  capture is written, as FRE-1325 does, so it never enters `TaskCapture.assistant_response`.
- Each sentence is a system-record statement about this turn. It contains:
  - the count of settled failures and the count of non-exempt statements;
  - one sentence for the shape: shape A — tools or sub-agents ran, and Seshat cannot cite their
    output; shape B — the statements are not backed by the sources this turn retrieved; shape C — no
    tool and no sub-agent ran this turn;
  - the count of unsettled statements, when that count is above zero;
  - the instruction to check the statements before relying on them.
- The exact wording is the implementation ticket's decision, within these contents.

### D5 — Pre-generation forcing is withdrawn

This amends ADR-0138 D5.

- Nothing forces retrieval before generation, in any mode.
- The heavy directive (`_append_heavy_directive`) and the heavy `tool_choice` pin become inert.
  Whether to delete them is the implementation ticket's decision.
- The enforcement selector (FRE-1285) selects nothing that changes a request. Whether to keep its
  standing and probation state is the implementation ticket's decision.
- **`retrieval_forced` reads the attempt count only.** Today it is true when `attempts > 1` **or**
  the applied selection level is heavy (`orchestrator/executor.py:2247-2251`,
  `grounding/enforcement_selection.py`). A retained selector would keep marking heavy turns as
  forced, and `compliance.py` would exclude them from the FRE-1284 metric although nothing forced
  them. So the selection level must never set `retrieval_forced`. After this change,
  `retrieval_forced` is true exactly when `attempts ≥ 2`.
- **No live behaviour changes from this section.** Production runs `observe`
  (`AGENT_GROUNDING_VERIFICATION_MODE=observe` in the gateway container, verified 2026-09-14; the
  committed default is `off`). `_select_enforcement` returns early outside `enforce`
  (`orchestrator/executor.py:2044`), so no production turn is heavy today.
- The rest of ADR-0138 D5 stands. The contract and the verification do not vary by model.

### D6 — What this ADR does not do

- **It does not detect fabrication.** The FRE-1327 turn is shape A. It declares, truthfully, that it
  worked from commands that Seshat cannot cite, and its figures were still invented. An honest report
  and a fabricated one produce the same declaration. ADR-0140 Option 6, held on FRE-1361, is still
  the only design in this chain that separates them. FRE-1361 stays open.
- **It does not make the note rare.** On the measured 91 turns the note fires on 91, with or without
  unsettled statements. The declaration becomes informative, because it tells the reader why the
  statements are unsourced. It stops firing only when compliance rises. The shape B retry is the one
  mechanism in this ADR that can raise it.
- **It does not change D1, D2 or the D3 checks of ADR-0138**, the span extractor, admissibility, or
  the compliance metric.
- **It does not switch `enforce` on.** Changing `grounding_verification_mode` stays a separate
  decision for the owner.

---

## Alternatives Considered

### Option 1: The 2026-09-13 split — enforce where nothing was offered

**Description:** Declare on turns where a tool was refused. Force retrieval on turns where nothing
was offered, as the alarming shape.

**Why rejected:** The measurement contradicts the premise. Shape C is 30 turns: 16 eval turns,
general-knowledge answers and conversation. Forcing retrieval on them is the heavy intervention the
owner wanted to avoid. The split also leaves shape B, the largest group, without a remedy. The owner
accepted the revision on 2026-09-14.

### Option 2: Keep ADR-0138 D4 as written, on every shape

**Description:** Block every failure, force retrieval, retry, and refuse at the bound.

**Why rejected:** This is the FRE-1328 defect. On shape A the retry cannot succeed, and the turn
ends in a refusal of an answer that was correct in substance. It also blocks on unsettled
statements, which punishes the model for a limit of the verifier.

### Option 3: Exempt shapes A and C from the note and the metric

**Description:** Remove uncitable turns from the denominator and show nothing on them.

**Why rejected:** Silence. FRE-1328 names this failure: "trading a measurable, honest 0% for an
unmeasurable 100%". A reader of a shape C answer gets no signal that no source stands behind it.

### Option 4: Keep heavy pre-generation forcing, and narrow only the retry

**Description:** Low-compliance models keep forced retrieval before generation. Only D4's retry is
narrowed to shape B.

**Why rejected:** The shape is unknown before generation, and every model measures 0% compliant. So
every turn is heavy, and shape C is forced on every turn. That contradicts "no forced retrieval on
C".

### Option 5: Retry shape B, then refuse at the bound

**Description:** Keep `TERMINAL_NO_SOURCE` for a shape B turn that fails its retry.

**Why rejected:** The owner's decision is that the turn ships and states its limits. The refusal
discards a whole answer for the failures of some of its statements. It also names "what was
searched", which on shape B is exactly the set of sources the answer used.

---

## Consequences

### Positive Consequences

- `enforce` no longer makes any turn shape unanswerable. The FRE-1328 blocker on `enforce` is
  removed.
- The reader learns why the statements are unsourced: a refused tool, sources not cited, or no
  source at all.
- A limit of the verifier no longer reads, to the reader, as a failure of the model.
- The retry is cheap. It needs no new tool call, and the tool list keeps the prompt cache.
- FRE-1316 no longer gates `enforce`. Vision turns register no tool result, so they are shape C or A
  and declare.

### Negative Consequences

- A shape B retry costs one more primary call and one more extraction call on each retried turn.
- A turn delivered with settled failures still reaches the reader. A reader can ignore the
  declaration. This is surfacing, not blocking.
- The note still fires on almost every turn until compliance rises.
- The compliance metric still counts an unsettled statement as non-compliant, so the metric and the
  note count different things. This is recorded, not repaired here.

### Risks and Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| The retry does not reduce settled failures, so it is cost without value | Medium | AC-6 measures this and falsifies D3 if the retry does not reduce them. |
| The model imitates the declaration in a later turn, because the persisted reply carries it | Low | Accepted in FRE-1325 on the attachment-disclosure precedent. A copy that the model writes is model output, so verification checks it like any other statement. |
| The retry directive makes the model delete correct statements rather than cite them | Medium | AC-6 counts statements on both attempts, so a retry that only shrinks the answer is visible. |
| A reader takes the declaration as fabrication detection | Medium | D6 states the limit. FRE-1361 stays open. |

---

## Implementation Notes

- Shape classification reads the same registry counts that `classify_turn_evidence` reads. Put it
  beside that function so both classifications change together.
- `decide()` gains the settled/unsettled partition and the shape. `RETRY_WITH_FORCED_RETRIEVAL`
  becomes a cite-only retry, and its name must change with its meaning.
- `_unsourced_assertion_disclosure` gains the shape and the unsettled count, and runs in `enforce`
  as well as `observe`.
- The ES event already carries `tool_results_offered`, `tool_results_admitted`, `outcomes` and
  `attempts`, and `_record_grounding` runs on every attempt. No new field is required for the
  criteria below. `blocking_outcomes` on `grounding_enforcement_decision` must list settled
  outcomes only.
- Acceptance runs in `enforce` go on the eval stack, never on production. The eval gateway must run
  with the production grounding settings passed through.

---

## Verification / Acceptance Criteria

**How will we know this decision delivered, not only merged?**

Production runs `observe`. AC-1 to AC-4, AC-6 and AC-7 are checked on an eval-stack run in `enforce`
over a held-out set of one or more turns of each shape, and 20 or more shape B turns. AC-5 is
checked on that run and on production `observe` turns after deploy. Every bar that is not stated
here is fixed in the ticket that builds the check, before results are seen.

- **AC-1 — An unsettled statement never triggers a retry and never counts in the note.**
  **Check:** every `grounding_enforcement_decision` event with a retry decision has
  `blocking_outcomes` that contains one or more settled outcomes and no unsettled outcome. A seeded
  shape B turn whose only failures are `unverifiable_by_containment` delivers with one attempt and
  no note. *Fails if* any retry event lists an unsettled outcome, or the seeded turn retries or
  carries a note.

- **AC-2 — Shapes A and C never retry, the retry retrieves nothing, and nothing forces retrieval.**
  **Check:** group `grounding_verification_completed` events by `trace_id`.
  - Every turn whose last event has `tool_results_admitted = 0` has `attempts = 1`.
  - No event has `attempts ≥ 3`.
  - On every capture grounding record, `retrieval_forced` equals `attempts ≥ 2`. That field is
    true today on a first attempt when heavy forcing applied.
  - For every retried turn, the attempt 2 event has the same `tool_results_offered` as the attempt 1
    event. The counts are cumulative per turn, so a retry that called a tool raises the count.

  *Fails if* any shape A or C turn has `attempts ≥ 2`, any event has `attempts ≥ 3`, any record has
  `retrieval_forced` different from `attempts ≥ 2`, or any retry raises `tool_results_offered`.

- **AC-3 — No turn is refused.**
  **Check:** count `grounding_enforcement_decision` events with `decision = terminal_no_source`
  over the run. Seeded negative: a shape B turn that fails its retry delivers its second generation
  with the declaration. *Fails if* the count is above zero, or any delivered reply contains the
  `build_no_source_statement` opening sentence "I could not find a source for".

- **AC-4 — A retried turn is not first-generation compliant.**
  **Check:** every `grounding_verification_completed` event with `attempts ≥ 2` reads
  `first_generation_compliant = false` and `compliance_observation = "confounded"`. `compliance.py`
  rejects a record with `retrieval_forced = true`, so a retried turn writes no observation to the
  metric. *Fails if* any retried turn reads `first_generation_compliant = true` or
  `compliance_observation = "recorded"`.

- **AC-5 — The declaration matches the record of the delivered generation.**
  **Check:** for each delivered turn, compare the reply as returned by `/chat` with the last
  `grounding_verification_completed` event for its `trace_id`. The note's failure count equals the
  count of settled outcomes. The shape sentence matches D1: shape B from the event's
  `tool_results_admitted`, and shape C only where the turn's capture has empty `tools_used` and no
  `hybrid_expansion_start` event exists for the `trace_id`. The unsettled count appears exactly when
  that count is above zero. Seeded negatives: a fully passed turn carries no note, and a turn whose
  only tool call was malformed declares shape A, not shape C. *Fails if* one delivered turn
  disagrees with its record on any of the four checks, or either seeded negative fails.

- **AC-7 — The declaration never enters the capture.**
  **Check:** for every delivered turn that carries a note in AC-5, the capture's
  `assistant_response` does not contain the note's closing instruction "Check them before you rely
  on them". *Fails if* any capture contains it.

- **AC-6 — The shape B retry repairs statements, not only removes them.**
  **Check:** over 20 or more retried shape B turns, compare the first-attempt and second-attempt
  events for each `trace_id`. Count settled failures and passed statements on both. The ticket
  records a bar before the run for the share of turns where settled failures fall **and** passed
  statements rise. *Fails if* that share is below the bar. A failure falsifies D3, and the retry is
  withdrawn rather than tuned.

---

## References

- ADR-0138 — the grounding contract. D4 and D5 are amended here. Accepted.
- ADR-0139 — D1 gives the metric its denominator and the `TurnEvidenceClass` this ADR splits.
  Proposed, partly withdrawn.
- ADR-0140 — T4 keeps arbitrary-code tools inadmissible. Option 6 is the fabrication-detection
  design. Proposed.
- ADR-0146 — the probe harness that ADR-0140 Option 6 waits on. Proposed.
- ADR-0149 — D6, keep the tool list and pin `tool_choice`. Accepted.
- FRE-1328 — the umbrella, and the 2026-09-13 owner decision in its comments.
- FRE-1325 — the note this ADR shapes. Done, PR #1155.
- FRE-1327 — the confabulation case. Shape A, and not detected by a declaration.
- FRE-1361 — the obligation ADR. Stays open.
- FRE-1284, FRE-1285 — the compliance metric and the enforcement selector.
- FRE-1316 — vision uncitability, no longer a gate on `enforce`.
- `src/personal_agent/grounding/enforcement.py` — `decide()`, `build_retry_directive`,
  `build_no_source_statement`.
- `src/personal_agent/grounding/verification.py` — `CheckOutcome`, `_MACHINE_UNDECIDED`,
  `classify_turn_evidence`, `first_generation_compliant`.
- `src/personal_agent/orchestrator/executor.py` — `step_synthesis`, `_select_enforcement`,
  `_append_heavy_directive`, `_unsourced_assertion_disclosure`.

---

## Status Updates

### 2026-09-14 - Proposed
**Changed By:** `adr` seat, from the owner's decisions of 2026-09-13 and 2026-09-14.
**Reason:** The 91-turn measurement revised the discriminator relayed on 2026-09-13. The owner moved
enforcement to shape B, set the two retry limits, set the unsettled-statement rule, and withdrew
pre-generation forcing.

**Codex review round 1** found nine blocking defects. Each was checked against the code before it
was accepted:
- The retry was not cite-only, because the tool set stays unrestricted. D3 now pins
  `tool_choice="none"`, and AC-2 checks that the retry raises no tool count.
- Shape C by registry count misclassified malformed tool calls and HYBRID turns. D1 now defines shape
  C on every tool-call path and the expansion record. AC-5 has a seeded malformed-call negative.
- AC-1 contradicted D2 on unsettled-only turns. D2 now states that such a turn carries no note.
- AC-4 was impossible, because a retried turn records `confounded`, not a non-compliant observation.
  AC-4 now checks `confounded`.
- A retained selector would still mark heavy turns as forced and starve the metric. D5 now makes
  `retrieval_forced` read the attempt count only, and AC-2 checks it on every record.
- "One retry whatever the setting says" was unresolved. D3 narrows the setting to 1 to 2, and AC-2
  checks that no event has three attempts.
- The capture exclusion had no check. AC-7 adds it.
- The production mode claim was unverified. It is now verified from the gateway container.
