# ADR-0147: Memory Belongs to the Planner — a Bounded Digest Shapes the Specs, and No Memory Section Reaches the Worker

**Status:** Proposed
**Date:** 2026-09-08
**Deciders:** Owner (design direction, 2026-09-08), adr seat (author)
**Tags:** orchestrator, expansion, memory, sub-agents, context-budget

---

## Context

### The measured defect

Master measured the following on FRE-1469 and FRE-1470. The figures are that investigation's, not
this ADR's, and the traces are cited in References so a reader can reproduce them.

`memory_in_context` is `False` on all 186 sub-agent captures ever written, across every index
month. The detector was checked before the number was trusted. `sub_agent.py:57` scans for
`"## Your Memory Graph"`, which is the exact prefix `executor.py:3758` emits, and five
primary-turn captures do carry the marker. The absence is real.

On trace `d5e83932d1afbdf3839c05ebfca6417b` a sub-agent's whole context was one message of 246
characters, with `context_message_count: 1`. The contract permits more than that: the dispatch
site sets `context=messages[-4:]` (`expansion_controller.py:622`), so up to four messages are
possible. The measured turn had one. What no turn has ever carried is memory.

The planner is memory-blind by construction. `_run_planner` (`expansion_controller.py:401`)
builds exactly two messages. The system prompt comes from `_build_planner_system_prompt`, which
takes only the live sub-agent tool surface. The user message reads
`f"Strategy: {strategy}\nQuery: {query}\n\nProduce the JSON plan."`. The token counts agree with
the code: the gateway assembled 516 memory tokens on that turn, and the planner call ran at 289
input tokens.

`spec.context` is a blind tail slice. No component chooses it.

### The finding that changes the cost argument

**The memory items are already in hand when the planner runs.** `step_init` assigns
`ctx.memory_context` from the gateway at `executor.py:4893`. The expansion call sits at
`executor.py:5036`, in the same function, 143 lines later.

Two consequences follow.

First, no design in this ADR needs a new recall query. The retrieval cost is already paid, and
the same list is offered to the primary's own renderer at `executor.py:5986`.

Second, **FRE-960 is a quality ceiling, not a blocker.** `multi_query_recall_arm`
(`memory/service.py:5678`) is one arm of the gateway's recall. Its silent failure lowers the
quality of what the gateway assembles. It does not block a mechanism that issues no query.

### What `ctx.memory_context` is, and what the primary actually sees

These are not the same set, and the difference is load-bearing for this design.

`_render_memory_section_with_ids` (`executor.py:3672`) drops session items outright, filters
every item whose text is blank, and then caps what remains at 15 entities, 5 episodes, 15
stances and 12 behavioural stances (`executor.py:3520-3549`). Stage 7 can also discard
`memory_context` wholesale to fit the budget (FRE-1004), in which case the primary sees no
memory at all.

So `ctx.memory_context` is a superset of what the primary receives. A digest built over the
superset can put a fact in front of a worker that the answering model never sees.

### The two mechanisms that were bundled

FRE-1470 describes two different designs in two places, and they are not the same thing.

**Inform.** The planner sees what memory holds and writes better task goals.

**Route.** The planner selects memory items per task, and the items ride in `spec.context` to
the worker as structured context.

The owner settled this on 2026-09-08: inform only. The reason is that the primary holds memory
at synthesis, so a worker does not need to *hold* a body of personal facts. It needs to be
*asked the right question*.

### What the owner decided before this ADR

The owner drove the design conversation recorded on FRE-1470 and made two challenges that each
moved it.

The first: *"if it happens in a subagent, and a subagent is given a specific task, will its
recall request be more precise?"* — yes, and that precision argument stands.

The second: *"but subagents have low reasoning ... are they capable of 'deciding' they need a
memory recall search?"* — no. This retracted master's own recommendation. The codebase already
agrees: FRE-1390 runs the planner at `ModelRole.PRIMARY` because *"decomposition is a reasoning
judgement about work"*, while `SUB_AGENT` resolves to a worker mode with thinking hard-disabled
(ADR-0145 D1).

The owner also endorsed the cheap first step: a topic-level list rather than the full memory
section.

---

## Decision

### D1 — The planner receives a bounded memory digest, built over the primary's own eligible set

A **memory digest** is a compact list, one line per item. This ADR uses that term for the rest
of the document. Each line carries the item kind, the item name or target, and one short clause
of the fact.

**The digest is defined over exactly the items the primary's renderer would render.** The
filter-then-cap selection inside `_render_memory_section_with_ids` moves into a shared function.
The renderer and the digest builder both call it. Two properties follow structurally rather than
by test:

- No fact can reach a worker through a goal unless the answering model also received it.
- A Stage 7 memory drop produces an empty digest, because it produces an empty render.

The digest omits what the primary's section carries for a different purpose: citation
identifiers (ADR-0138 D1 binds those to the answering model, not to a planner), the guidance
paragraphs, the episode bodies, and the `key_entities` lists.

**One clause per line, not a bare name.** A names-only list lets the planner write a goal that
names a fact the worker cannot obtain — for example *"consider the user's transmission
preference"*. The worker holds no memory and cannot resolve that reference, so a names-only
digest produces goals that are worse than silence.

The digest is built in `executor.py` beside the renderer, and passed into
`ExpansionController.execute` as a value object. The expansion controller never sees a raw
memory item, so it gains no knowledge of memory-item shape.

### D2 — No memory section reaches a worker. A single fact may, and only through a goal

The invariant this ADR guarantees, stated exactly:

> **No memory item is ever attached to `SubAgentSpec.context`, and no rendered memory section
> ever reaches a worker.** `spec.context` stays the `messages[-4:]` tail slice.

The invariant this ADR does **not** guarantee: that no memory-derived fact reaches a worker. D1
asks the planner to inline facts into goals. A goal becomes `SubAgentSpec.task` and is sent to
the worker verbatim. So a fact does travel, by design.

This is a deliberate trade and the difference from the rejected alternatives is one of volume
and authorship, not of category:

1. The volume is what one thinking-capable component judged a task needs, rather than 20 items
   times every arm.
2. Every fact that travels was chosen by a component that can reason about the choice.
3. Every fact that travels was already rendered to the answering model (D1), so nothing reaches
   a worker that the primary did not also receive.

The disclosure path stays open and is recorded as such: a worker holding `web_search` reaches
the self-hosted SearXNG instance and then upstream engines, so an inlined fact can enter a
search query. **Nothing in this ADR mechanically bounds that.** The mitigation is the planner's
deliberateness, which is weaker than a bound. This is stated rather than papered over, and it is
the strongest argument for reopening the routing question under a real egress control rather
than under a context-shape rule.

Structured routing stays rejected for the reasons in Alternative 3, and its risk grows: FRE-1467
threads identity into the sub-agent's `TraceContext`. That change is in flight and unmerged at
this date. When it lands, it removes the missing-identity guard that currently makes
`search_memory` return little to a worker.

### D3 — The plan carries an absence signal, not a validation arm

`ExpansionPlan` gains one field, `memory_relevance`. It has five values, and **exactly one
writer each**, so no state is ambiguous about who decided it:

| Value | Written by | Meaning |
|-------|-----------|---------|
| `absent` | the code, before the planner call | The digest was empty. The planner was never asked. |
| `used` | the planner | At least one digest line shaped at least one task goal. |
| `none_relevant` | the planner | A digest existed and nothing **in it** bears on this query. |
| `unstated` | the plan validator | A digest existed, the plan parsed, and the field was missing or invalid. |
| `fallback` | the fallback planner | No planner judgment exists. The LLM planner was asked and failed. |

`unstated` and `fallback` exist because both were previously collapsed into `absent`, which made
"the model judged" indistinguishable from "no model answered". `fallback` is not `absent`: the
fallback planner runs only after the LLM planner was asked and failed, so a digest may well have
existed.

`none_relevant` is scoped to the digest, never to all recalled memory. The planner sees at most
20 lines with no episode bodies. It can honestly report that nothing in that view bears on the
query. It cannot report on what the view omitted.

Within that scope, `none_relevant` is the first honest absence signal in the system: a
thinking-capable component states that recalled memory does not bear on the question. FRE-1118's
own defect — the relevance score never reaching the model, and the prompt forbidding the honest
answer — stays FRE-1118's work. **This ADR produces the signal. It does not change what the
primary says.**

No validation sub-agent is added. Such an arm is an extra LLM call per turn inside the loop
FRE-1138 shows is unbounded, for a weaker version of a signal the planner emits at no extra
call.

### D4 — The digest is bounded independently of the render bounds

The render bounds admit up to 47 items. At roughly 15 tokens per line, an unbounded
one-line-per-item digest reaches about 700 tokens. **That is more than the 516 the full section
cost.** The digest is cheap only if it is bounded on its own terms.

The bounds are:

- at most **20 items**, taken in the relevance order the memory context already carries;
- at most **120 characters** per line;
- a ceiling of **300 estimated tokens** for the whole block, measured with
  `request_gateway/budget.py:estimate_tokens`.

That estimator delegates to `cl100k_base` and is an approximation for the model families
actually served. The ceiling therefore binds the estimate, not the served token count. It is
named as an estimate everywhere in this ADR for that reason.

The ceiling binds by dropping whole lines from the tail until the block fits. A line is never
truncated mid-clause. A digest may therefore hold fewer than 20 items on a memory-rich turn.

Against FRE-1138's ungoverned within-turn growth, **the digest block is paid once per turn, not
once per arm**, because the planner runs once. This applies to the block only. Facts the planner
copies into goals are paid again in each worker prompt that carries them, including the FRE-1389
replacement dispatch, which reuses the same goal. That copied volume is small by construction —
a clause, not a section — but it is not zero and it is not bounded here.

### D5 — A fallback plan carries no digest, and every planner attempt reports

`generate_fallback_plan` (`fallback_planner.py:49`) is a deterministic keyword split and makes
no LLM call. It receives no digest and sets `memory_relevance` to `fallback`.

Today `planner_completed` is emitted only on the success path. A fallback returns after
`fallback_planner_used` with no terminal event carrying the same fields, so any rate computed
over `planner_completed` silently excludes every failed planner call. **One terminal planner
event must fire on every path** — success, validation failure, timeout, exception and fallback —
carrying `memory_digest_items`, `memory_digest_item_ids`, `memory_digest_tokens` and
`memory_relevance`. Without that, every criterion below measures a biased population.

### D6 — Enrichment stays out of scope

No write path is added. A worker's output is agent-derived text at exactly the tier FRE-1022
(ADR-0098 D6, the corroboration gate), FRE-1302 and FRE-1303 show the system already mis-trusts.
Adding a write path before those hold converts one live defect into a persistent one.

### D7 — No further memory tool is granted to a worker

This ADR grants nothing to the sub-agent tool principal. It also does not revoke the
`search_memory` grant FRE-1463 made on 2026-09-08, because a governance revocation is the
owner's decision and not this ADR's ask. The observation is recorded for the owner: once D1 and
D2 ship, that grant serves no purpose this design depends on, and master measured it inert.

---

## Alternatives Considered

### Option 1: Inject the full memory section into every sub-agent

**Description:** Render the same `## Your Memory Graph` section the primary receives, and put it
in each `SubAgentSpec.context`.

**Pros:**
- No planner change at all.
- Every worker holds the same facts the primary holds.

**Cons:**
- 516 tokens multiplied by a six-to-eight arm fan-out.
- Every worker becomes a disclosure path for the whole set, not for one clause.

**Why Rejected:** FRE-1470 names this as the shape that fails. It lands roughly 3,600 tokens in
the loop FRE-1138 shows is unbounded, and that loop exhausted the owner's 900-second budget on
2026-09-08.

### Option 2: Grant the worker a memory tool and let it decide

**Description:** The FRE-1463 approach. `search_memory` is granted to sub-agents, and a worker
calls it when it judges that it needs user context.

**Pros:**
- No planner change, no schema change.
- The worker's query is task-scoped, which is the precision argument.

**Cons:**
- The judgment sits in the component least able to make it.
- Measured inert.

**Why Rejected:** The owner's second challenge rejected it, and the measurement agrees. Sixteen
local sub-agent calls carried `reasoning_content_chars: 0`. Those same arms issued 25 competent
`web_search` calls, so a worker can use a tool — but `web_search` matched the verb in its own
instruction. *"Should I check what this user told me before?"* requires noticing that unstated
context might exist, which is meta-cognition about one's own ignorance, and worker mode removes
exactly that.

### Option 3: The planner selects memory items per task, attached to `spec.context`

**Description:** The route variant. The plan schema carries per-task memory identifiers, and the
selected items ride to the worker as structured context.

**Pros:**
- Selection is per task rather than per question, which is the strongest form of the precision
  argument.
- A worker with a long task can consult a fact the goal text has no room for.
- It would make AC-2 far stronger: a per-task reference is a checkable claim, where lexical
  overlap is not.

**Cons:**
- The planner must emit identifiers, and a model-authored identifier can be invented.
- The selected item reaches the worker as asserted context that nothing re-checks.
- It multiplies the disclosed volume per arm, which is the property D2 keeps small.

**Why Rejected:** The primary already renders the same facts at synthesis, so the worker does not
need to hold a body of them. **Reopening condition, stated in advance:** measurement under AC-2
shows planners routinely naming facts they had no room to inline, or an egress control lands
that bounds what a worker can disclose. Absent either, this stays rejected and no ticket is
filed for it.

### Option 4: A validation sub-agent arm

**Description:** One extra sub-agent per turn, asked whether the recalled memories bear on the
question.

**Pros:**
- It reads the memories in full, so its verdict covers what the 20-line digest omits. D3's signal
  is genuinely narrower.

**Cons:**
- One more LLM call per turn, inside FRE-1138's unbounded loop.
- The verdict arrives after the plan is written, so it cannot shape the specs.

**Why Rejected:** D3 obtains a narrower version of the same signal at no extra call, and obtains
it early enough to matter. The wider signal is not worth an unbounded-loop call while FRE-1138
is open.

### Option 5: Give the planner its own retrieval capability

**Description:** The planner issues a recall query of its own before writing the plan.

**Pros:**
- The planner asks for exactly what the decomposition needs.

**Cons:**
- It re-queries what the gateway already retrieved for this same question.
- It adds a serial round trip on the critical path, before any dispatch begins.
- It depends on the multi-query arm FRE-960 shows returns `[]` on every call.

**Why Rejected:** The items are already in hand at `executor.py:4893`. A second query buys
nothing and costs latency inside a turn budget that already fails.

---

## Consequences

### Positive Consequences

- The one component that decides the work stops being the only one denied user context.
- The retrieval cost is zero. The digest re-renders items the turn already paid for.
- The mechanism does not depend on FRE-960, which is dead today with no signal.
- Nothing can reach a worker that the answering model did not also receive (D1's shared
  selection).
- The system gains its first honest absence signal, at plan altitude, for no extra call.
- Every planner attempt reports for the first time (D5), which fixes a measurement bias that
  predates this ADR.

### Negative Consequences

- The planner's job gets harder. It must read a digest, judge relevance, and inline facts into
  goals, at a call that today runs at 289 input tokens.
- The planner's input roughly doubles in the worst case, from 289 estimated tokens to about 589.
- A fallback plan gains nothing (D5), so the benefit is absent exactly when the planner already
  failed.
- A memory-derived fact now reaches workers through goal text, and no mechanism bounds that
  (D2). This is new exposure, smaller than every rejected alternative's, but not zero.
- Two new failure surfaces exist: a digest built and never used, and a `used` claim no goal
  supports. AC-2 and AC-3 exist to catch both, and AC-2's limits are stated with it.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| The digest is built and the planner ignores it, repeating FRE-1463's inert grant | High | AC-3 fixes a floor in advance and measures it over a stratified population |
| The planner claims `used` without any goal reflecting the digest | Medium | AC-2 is a consistency invariant checked at 100%. Its blind spot is stated with it |
| The digest grows past its ceiling on a memory-rich turn and feeds FRE-1138 | Medium | D4's three bounds, plus AC-4's seeded negative over 47 oversized items |
| A future change attaches memory to `spec.context` and reopens structured routing | Medium | AC-5 asserts no rendered memory section appears in any sub-agent capture's context |
| An inlined fact enters a `web_search` query and reaches upstream engines | Medium | Not mechanically bounded. D2 states this. The volume is one clause chosen by a reasoning component, and never a fact the primary did not also receive |
| A digest fact was never shown to the answering model | Low | Structurally impossible: D1 shares one selection function with the renderer |

---

## Implementation Notes

**Files affected:**

- `src/personal_agent/orchestrator/executor.py` — extract the filter-then-cap selection out of
  `_render_memory_section_with_ids` into a shared function; add the digest builder beside it;
  the `controller.execute(...)` call site at `:5036` passes the digest.
- `src/personal_agent/orchestrator/expansion_types.py` — a frozen `PlannerMemoryDigest`
  (`text`, `item_ids`, `item_count`, `estimated_tokens`); `ExpansionPlan.memory_relevance`.
- `src/personal_agent/orchestrator/expansion_controller.py` — `execute` and `_run_planner`
  signatures; `_build_planner_system_prompt` gains the digest block and the schema field;
  `_validate_plan_json` classifies a missing or invalid field as `unstated`; the terminal
  planner event fires on every path (D5).
- `src/personal_agent/orchestrator/fallback_planner.py` — sets `memory_relevance` to `fallback`.

**Item identities.** `memory_item_identity` already yields a stable `(kind, id)` per item, and
`_render_memory_section_with_ids` already returns the rendered ids. The digest logs
`memory_digest_item_ids` from the same helper. **No digest text is logged** — the identities and
the counts carry every criterion below, so no new free-text personal-data surface is created in
Elasticsearch.

**Telemetry.** The terminal planner event carries `memory_digest_items`,
`memory_digest_item_ids`, `memory_digest_tokens` and `memory_relevance`. Every criterion reads
these fields plus existing sub-agent captures, so no new instrument is built.

**Testing strategy.** Unit tests cover all three bounds with a seeded negative: 47 items with
oversized descriptions must yield at most 20 items, no line over 120 characters, and at most 300
estimated tokens. A second test asserts the digest's item ids are a subset of the renderer's
rendered ids for the same input, including an input containing session items and blank items. A
regression test asserts `SubAgentSpec.context` still equals the `messages[-4:]` slice.

**Dependencies.** None. FRE-1467 is in flight and is not a blocker. FRE-960 is a quality
ceiling, not a dependency.

---

## Verification / Acceptance Criteria

**Two stated limits first, because both are design smells this ADR surfaces rather than hides.**

The first: these criteria prove the mechanism, not the answer quality. Whether a memory-informed
spec produces a better sub-agent result has no turn-scoped instrument here. The eval set is not
HYBRID-scoped, and a sub-agent digest is free text. The owner's own live turn stays the only
instrument for quality.

The second: no criterion bounds what a goal discloses to a worker (D2). Lexical checks can prove
that a fact travelled. Nothing here proves that it needed to. Closing that requires an egress
control on the worker, which is out of scope and is Alternative 3's second reopening condition.

- **AC-1** — Every item in the digest is one the primary's own renderer selected for the same
  turn, and the digest never holds more than 20. · **Check:** `memory_digest_item_ids` on the
  terminal planner event is a subset of that turn's `_rendered_memory_ids`, and
  `memory_digest_items <= min(rendered_items, 20)`, with equality whenever
  `memory_digest_tokens` is below the ceiling. · *Fails if* any digest id is absent from the
  rendered ids, which means a worker can be told something the answering model never saw.
  Equality is not asserted unconditionally because D4's token ceiling legitimately drops tail
  lines.

- **AC-2** — On **every** turn returning `memory_relevance: used`, at least one task goal names
  an entity, target or episode subject that is in the digest and absent from the user's own
  question. · **Check:** resolve `memory_digest_item_ids` to their names, then match against the
  plan's task goals and `user_message` for the same `trace_id`. · *Fails if* a single `used`
  turn has no such name, which means the planner claimed a use the plan does not show. The
  pre-change rate of this measure is structurally zero, because the planner had no access to
  those names. **Stated blind spot:** this is necessary, not sufficient. A planner that copies
  one name into every goal passes it without using the fact well. AC-2 catches a false claim; no
  criterion here certifies a good one.

- **AC-3** — Over at least 20 live turns whose digest held at least one **query-retrieved** item,
  `memory_relevance: used` is returned on **at least 25%**. · **Check:** count over the terminal
  planner events, excluding turns whose digest held only standing behavioural stances. · *Fails
  if* the rate falls below the floor, which reproduces FRE-1463's inert grant in a new place.
  **Why the population is stratified:** `_enrich_with_behavioural_stances`
  (`request_gateway/context.py:271`) injects standing preferences every turn, independently of
  what recall selected. Those items are not evidence that the gateway retrieved anything for
  this query, so a digest made only of them cannot test D1's premise. **Why 25%, fixed here
  before the first measured run:** the floor separates working from inert rather than grading
  quality, and it sits deliberately low because the input is a known-degraded upstream. FRE-960
  has left recall as one blunt query since 2026-07-20, and this ADR must not be adjudicated on
  that defect.

- **AC-4** — No turn ever produces a digest above 20 items, above 120 characters on any line, or
  above 300 estimated tokens. · **Check:** the maxima of `memory_digest_items` and
  `memory_digest_tokens` across all recorded turns, plus the seeded unit test over 47 oversized
  items asserting all three bounds. · *Fails if* any maximum is exceeded, or the seeded test
  passes only because its input was already small. The per-line bound is asserted explicitly:
  without it an implementation that never clips a line still passes the other two by dropping
  more lines.

- **AC-5** — No rendered memory section reaches any worker's context. `memory_in_context` remains
  `False` on **every** sub-agent capture written after this chain deploys, and
  `context_message_count` stays at the tail-slice size. · **Check:** the same Elasticsearch query
  master used for the 186-capture baseline. · *Fails if* any capture reports `True`. **Scope,
  stated because the instrument is narrower than the words:** the detector scans `spec.context`
  for a literal marker. It does not and cannot detect a fact inlined in `spec.task`, which D2
  permits by design. This criterion asserts the structured-routing invariant only.

- **AC-6** — Every plan carries a `memory_relevance` value written by that value's own writer,
  and no state is misattributed. Specifically: an empty digest yields `absent` on 100% of those
  turns and is never `used` or `none_relevant`; a fallback plan yields `fallback` and never
  `absent`; a parsed plan omitting the field yields `unstated` and never `none_relevant`. ·
  **Check:** the terminal planner event's `memory_relevance` against its own
  `memory_digest_items` and the plan's `is_fallback`, plus unit tests seeding each of the five
  cases. · *Fails if* any turn shows a value its designated writer could not have written — for
  example a model-authored value on a turn where the digest was empty, which would mean the
  deterministic writers in D3's table were not enforced.

**Where these are adjudicated.** On FRE-1470, this ADR's umbrella ticket, once both
implementation tickets have landed and deployed. Not at merge of this ADR, and not by either
implementation ticket, each of which carries criteria for its own work only.

---

## References

- [ADR-0145](ADR-0145-two-nouns-and-the-dialect-between-them.md) — D1: `SUB_AGENT` resolves to a
  worker mode with thinking hard-disabled
- [ADR-0138](ADR-0138-the-model-may-generate-but-may-not-assert.md) — D1/D2: the citation
  identifiers the digest deliberately omits
- [ADR-0098](ADR-0098-memory-substrate-and-lifecycle-architecture.md) — D6: the corroboration
  gate that sits in front of any enrichment path
- [ADR-0036](ADR-0036-expansion-controller.md) — the expansion controller and plan schema
- [ADR-0125](ADR-0125-two-quality-dimensions-and-turn-evidence-contract.md) — the input side of
  the evidence record
- FRE-1470 — this ADR's umbrella ticket, carrying the owner's own words on both challenges, and
  the source of every measured figure quoted in Context
- FRE-1469 — the sub-agent context barriers, measured
- FRE-1467 — sub-agent identity, in flight and unmerged at this date
- FRE-1463 — the sub-agent tool grant, and the `search_memory` grant measured inert
- FRE-1390 — the planner runs at `ModelRole.PRIMARY`, and why
- FRE-1138 — within-turn growth is the ungoverned axis
- FRE-1118 — the system cannot express "nothing relevant exists"
- FRE-1004 — Stage 7 can drop the memory context to fit the budget
- FRE-960 — multi-query paraphrase generation, silently dead since 2026-07-20
- Traces `d5e83932d1afbdf3839c05ebfca6417b`, `896c7b1d774d6a31dabe030f28e5633d`,
  `e169da700b8e99213dfdddca351a2af7`

---

## Status Updates

### 2026-09-08 - Proposed
**Changed By:** adr seat (FRE-1470)
**Reason:** Authored from the owner's design conversation of 2026-09-08. The owner settled the
inform-versus-route question in favour of inform only. Codex round 1 corrected the central
framing: an inlined fact does reach the worker, so D2 now states the invariant it actually
guarantees and names the exposure it does not close.
