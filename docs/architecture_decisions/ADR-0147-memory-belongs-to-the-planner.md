# ADR-0147: Memory Belongs to the Planner — a Bounded Digest Shapes the Specs, and No Memory Section Reaches the Worker

**Status:** Proposed
**Date:** 2026-09-08
**Deciders:** Owner (design direction, 2026-09-08), adr seat (author)
**Tags:** orchestrator, expansion, memory, sub-agents, context-budget

---

## Context

### The measured defect

Master measured the following on FRE-1469 and FRE-1470. The figures are that investigation's, not
this ADR's. They were verified there, against live Elasticsearch and the named traces. **They are
not reproducible from this repository**, which holds no saved query or result export for them.
Treat them as externally verified evidence, cited to their ticket.

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

**The digest is derived from the primary's own rendered text, not from raw memory items.** Two
extractions move into shared functions that the renderer and the digest builder both call: the
filter-then-cap selection inside `_render_memory_section_with_ids`, and the per-item text
helpers `_entity_line`, `_episode_text` and `_stance_line`. The digest builder never reads a raw
item dictionary, so it cannot surface a field or a wording the renderer omits.

Three properties follow structurally rather than by test:

- No fact can reach a worker through a goal unless the same fact was eligible for the primary's
  render, in the same words.
- A Stage 7 memory drop produces an empty digest, because it produces an empty render.
- A change to either shared function moves both outputs together.

**Eligible for the render is not the same as delivered to the model.** The rendered ids record
what the renderer emitted. Whether the volatile block reached the wire is checked separately
(`captains_log/turn_evidence.py:780`). This ADR guarantees render eligibility. It does not
guarantee wire admission, and no criterion below claims it does.

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
3. Every fact that travels was rendered for the answering model in the same words (D1), so
   nothing reaches a worker that the primary's own section did not also carry.

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

`ExpansionPlan` gains one field, `memory_relevance`. It records **the planner's judgment about
the digest, and nothing else.** It does not also record whether a digest existed, or whether a
plan came from the planner at all. Those are two separate axes, and folding them into one enum
produced a state that demanded two values at once: a turn with an empty digest whose planner
then failed is both "no digest" and "no plan".

The two other axes are already available and are logged beside it: `memory_digest_items` (zero
means no digest) and `ExpansionPlan.is_fallback`.

| Value | Written by | When |
|-------|-----------|------|
| `used` | the planner | At least one digest line shaped at least one task goal. |
| `none_relevant` | the planner | A digest existed and nothing **in it** bears on this query. |
| `unstated` | the plan validator | The planner returned a plan that parsed, and the field was missing or invalid. |
| `not_applicable` | the code | No judgment exists to record. Either the digest was empty, so the planner was never asked to judge, or no planner plan exists. |

`not_applicable` is written deterministically and **takes precedence over anything the model
wrote**. The two reasons behind it are never merged, because the reader separates them from the
two fields logged alongside: `memory_digest_items == 0` is the never-asked case, and
`is_fallback` is the no-plan case. Both together are one coherent state, not a conflict.

`unstated` covers a missing field and a malformed one alike. The difference is not
decision-relevant, and splitting it would add a field that changes no outcome.

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
no LLM call. Its plan carries `memory_relevance: not_applicable` and `is_fallback: True`.

Today `planner_completed` is emitted only on the success path. A fallback returns after
`fallback_planner_used` with no terminal event carrying the same fields, so any rate computed
over `planner_completed` silently excludes every failed planner call. **One terminal planner
event must fire on every path** — success, validation failure, timeout, exception and fallback.
Without it, every criterion below measures a biased population.

The event carries these fields. AC-1's arithmetic, AC-3, AC-4 and AC-6 read these and nothing
else. AC-5 reads existing sub-agent captures. AC-1's text-parity half and the whole of AC-2 are
tests, not telemetry queries, and are marked as such where they appear.

| Field | Meaning |
|-------|---------|
| `memory_relevance` | D3's four-value judgment |
| `is_fallback` | Whether the plan came from the fallback planner |
| `memory_digest_eligible_items` | How many items the shared selection returned |
| `memory_digest_items` | How many lines the digest actually carried |
| `memory_digest_items_dropped` | Lines dropped by the token ceiling |
| `memory_digest_kinds` | A count per item kind, so a criterion can stratify without parsing an identity |
| `memory_digest_max_line_chars` | The longest emitted line, so the per-line bound is checkable live |
| `memory_digest_tokens` | The estimated size of the emitted block |
| `memory_digest_item_keys` | One compound `(kind, identity, ordinal)` key per line, in order |
| `memory_rendered_item_keys` | The same compound keys for the shared selection's whole output, in order — AC-1's comparison set, which no existing capture holds |

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
- It would make per-task attribution checkable in production: a stated reference is a claim a
  query can test, where a goal's free text is not.

**Cons:**
- The planner must emit identifiers, and a model-authored identifier can be invented.
- The selected item reaches the worker as asserted context that nothing re-checks.
- It multiplies the disclosed volume per arm, which is the property D2 keeps small.

**Why Rejected:** The primary already renders the same facts at synthesis, so the worker does not
need to hold a body of them. **Reopening conditions, stated in advance:** AC-2's relevant-fact
run shows the planner naming a fact it had no room to inline, or an egress control lands that
bounds what a worker can disclose. Absent either, this stays rejected and no ticket is filed for
it.

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
- Nothing can reach a worker in words the primary's own section did not carry (D1's shared
  selection and shared text helpers).
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
- Two new failure surfaces exist. AC-2 and AC-3 catch the first — a digest built and never used.
  The second, a live `used` claim that no goal supports, **stays unmeasured**: AC-2 proves
  seeded capability on two controlled runs, and AC-3 only reads the field's distribution. Live
  per-turn attribution needs the structured references Alternative 3 was rejected for.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| The digest is built and the planner ignores it, repeating FRE-1463's inert grant | High | AC-2's paired seeded runs prove the planner responds to a digest at all; AC-3 detects the field going constant in production |
| The planner claims `used` without any goal reflecting the digest | Medium | Unmitigated on live turns, and stated as such in Consequences. AC-2 proves the capability on a seeded pair only |
| The digest grows past its ceiling on a memory-rich turn and feeds FRE-1138 | Medium | D4's three bounds, plus AC-4's seeded negative over 47 oversized items |
| A future change attaches memory to `spec.context` and reopens structured routing | Medium | AC-5 asserts no rendered memory section appears in any sub-agent capture's context |
| An inlined fact enters a `web_search` query and reaches upstream engines | Medium | Not mechanically bounded. D2 states this. The volume is one clause chosen by a reasoning component, and never a clause the primary's section did not also carry |
| A digest fact was never rendered for the answering model | Low | Structurally excluded: D1 shares both the selection and the per-item text helpers with the renderer. Wire admission stays out of scope and is stated in D1 |

---

## Implementation Notes

**Files affected:**

- `src/personal_agent/orchestrator/executor.py` — extract the filter-then-cap selection out of
  `_render_memory_section_with_ids` into a shared function; the digest builder calls it and the
  per-item helpers `_entity_line` / `_episode_text` / `_stance_line` (`:3603`, `:3635`, `:3650`)
  rather than raw items; the `controller.execute(...)` call site at `:5036` passes the digest.
- `src/personal_agent/orchestrator/expansion_types.py` — a frozen `PlannerMemoryDigest`
  (`text`, `item_keys`, `rendered_item_keys`, `item_count`, `eligible_count`, `dropped_count`,
  `kind_counts`, `max_line_chars`, `estimated_tokens`); `ExpansionPlan.memory_relevance`.
- `src/personal_agent/orchestrator/expansion_controller.py` — `execute` and `_run_planner`
  signatures; `_build_planner_system_prompt` gains the digest block and the schema field;
  `_validate_plan_json` classifies a missing or invalid field as `unstated`; the terminal
  planner event fires on every path (D5).
- `src/personal_agent/orchestrator/fallback_planner.py` — sets `memory_relevance` to
  `not_applicable`.

**Item keys.** `memory_item_identity` (`captains_log/turn_evidence.py:220`) yields
`(kind, identity)`, but the identity alone is not a key: an entity's identity is its name, an
episode's is a conversation or turn id, and an unrecognised or id-less item collapses to the
empty string. `_render_memory_section_with_ids` then discards the kind and returns identity
strings only. The digest therefore logs a **compound key** — kind, identity and the line's
ordinal — and comparisons are ordered multisets, never sets. A bare identity is never used as a
key anywhere in this design.

**What is logged, stated accurately.** No digest clause is logged. The keys are not free of
personal data: an entity key contains the entity's name and a stance key contains its target.
Those are the same values the existing evidence record already carries, so this adds no new
*class* of personal data to Elasticsearch, but it does add another field that holds names. That
is the honest statement, and it is why no clause text is added on top of it.

**Testing strategy.** Unit tests cover all three bounds with a seeded negative: 47 items with
oversized descriptions must yield at most 20 items, no line over 120 characters, and at most 300
estimated tokens. A second test feeds a set containing session items, blank items and over-cap
items, and asserts every digest line is a prefix of the renderer's own line for the same item. A
third seeds each of D3's four values and asserts AC-6's matrix. A regression test asserts
`SubAgentSpec.context` still equals the `messages[-4:]` slice. AC-2's paired runs are an
integration test, because they need the real planner model.

**Dependencies.** None. FRE-1467 is in flight and is not a blocker. FRE-960 is a quality
ceiling, not a dependency.

---

## Verification / Acceptance Criteria

**Three stated limits first, because each is a design smell this ADR surfaces rather than hides.**

The first: these criteria prove the mechanism, not the answer quality. Whether a memory-informed
spec produces a better sub-agent result has no turn-scoped instrument here. The eval set is not
HYBRID-scoped, and a sub-agent digest is free text. The owner's own live turn stays the only
instrument for quality.

The second: no criterion bounds what a goal discloses to a worker (D2). Lexical checks can prove
that a fact travelled. Nothing here proves that it needed to. Closing that requires an egress
control on the worker, which is out of scope and is Alternative 3's second reopening condition.

The third: **a live `used` claim is never verified against the plan it describes.** AC-2 proves
on a seeded pair that the planner can respond to a digest. In production the field stays a model
self-report. Verifying it per turn needs the per-task structured references Alternative 3 was
rejected for, so this is a consequence of that rejection, deliberately taken.

- **AC-1** — Every digest line carries the primary renderer's own text for an item that renderer
  selected, and the arithmetic of the bound closes. · **Check, two halves.** *Telemetry:* on the
  terminal planner event,
  `memory_digest_items == min(memory_digest_eligible_items, 20) - memory_digest_items_dropped`,
  and `memory_digest_item_keys` is an ordered sub-multiset of `memory_rendered_item_keys` on
  that same event. *Test:* a unit test feeds a set containing session items, blank items and
  more than 47 items, and asserts every digest line is a prefix of the renderer's own line for
  the same item. · *Fails if* the arithmetic does not close, a digest key is absent from the
  rendered keys, or any digest line carries text the renderer would not have emitted for that
  item — each of which means a worker can be told something the primary's section never carried.
  Both key lists are logged on the one event precisely so the comparison needs no join: no
  existing capture holds the renderer's compound keys.

- **AC-2** — **The digest changes the plan, and an irrelevant digest does not.** Two seeded
  planner runs on the same query: one whose digest carries a decisive relevant fact, one whose
  digest carries only unrelated items. · **Check:** an integration test
  (`PERSONAL_AGENT_INTEGRATION=1`) invoking the real planner. **The oracle is deterministic
  because the test owns the seed.** The relevant digest's decisive value is a token that cannot
  arise by chance and cannot be guessed from the query — a coined identifier, not a real-world
  word. The test declares that token plus a small closed set of accepted equivalents, and
  asserts: the relevant run carries one of them in at least one task goal, and the unrelated run
  carries none of them in any goal and returns `none_relevant`. · *Fails if* the relevant run
  produces no goal carrying the token, which means the digest changed nothing that matters, or
  if the unrelated run reports `used` or leaks the token. Plan text differing between runs is
  **not** accepted as a pass: a planner varies stylistically while ignoring memory entirely, so
  only the seeded token discriminates. **Why this replaced a live lexical count:** an earlier
  draft counted digest terms appearing in goals on live turns. That check is not even necessary
  — a clause such as *"prefers automatic"* can shape a goal without the goal repeating the
  item's name — so it can fail on a working planner, and it can pass on one that copies a name
  into every goal. **The seeded token is not that check under another name.** The test owns the
  seed, so the token is decisive by construction and its absence is a real failure. On a live
  turn nothing tells the reader which recalled term mattered, so the same match proves nothing
  either way.

- **AC-3** — In production the judgment **discriminates**: over at least 20 live turns whose
  digest was non-empty, both `used` and `none_relevant` are observed. · **Check:** count the four
  `memory_relevance` values over the terminal planner events, stratified by
  `memory_digest_kinds` so turns whose digest held only standing behavioural stances are
  reported separately. · *Fails if* `memory_relevance` is constant across the sample, which is
  FRE-1463's inert grant reproduced in a new place, or if `unstated` is the most frequent of the
  four values, which means the planner never learned the field. Both conditions are counts over
  one logged field, with no threshold left to judgment. **Why no rate is fixed here.** An earlier draft set a 25% floor. That
  number had no empirical basis, and the criterion below it is a model self-report that a
  planner can satisfy by emitting `used` mechanically. The honest position is that no defensible
  production rate exists in advance, because the digest's own relevance depends on recall that
  FRE-960 has left as one blunt query since 2026-07-20. AC-2's seeded pair is what proves the
  capability. AC-3 only detects a mechanism that has gone inert. **This weakness is stated, not
  designed around:** a genuine relevance rate needs a labelled turn set, which is a study, not
  an acceptance criterion.

- **AC-4** — No turn ever produces a digest above 20 items, above 120 characters on any line, or
  above 300 estimated tokens. · **Check:** the live maxima of `memory_digest_items`,
  `memory_digest_max_line_chars` and `memory_digest_tokens` across all recorded turns, plus the
  seeded unit test over 47 oversized items asserting all three bounds. · *Fails if* any maximum
  is exceeded, or the seeded test passes only because its input was already small.
  `memory_digest_max_line_chars` exists for this criterion: without it, no logged field can
  detect an overlong line, and an implementation that never clips a line still passes the other
  two bounds by dropping more lines.

- **AC-5** — No rendered memory section reaches any worker's context. `memory_in_context` remains
  `False` on **every** sub-agent capture written after this chain deploys, and
  `context_message_count` stays at the tail-slice size. · **Check:** the same Elasticsearch query
  master used for the 186-capture baseline. · *Fails if* any capture reports `True`. **Scope,
  stated because the instrument is narrower than the words:** the detector scans `spec.context`
  for a literal marker. It does not and cannot detect a fact inlined in `spec.task`, which D2
  permits by design. This criterion asserts the structured-routing invariant only.

- **AC-6** — The three axes never contradict each other. The allowed matrix, over the terminal
  planner event's own fields, is exactly:

  | `memory_digest_items` | `is_fallback` | Allowed `memory_relevance` |
  |---|---|---|
  | 0 | either | `not_applicable` only |
  | > 0 | `True` | `not_applicable` only |
  | > 0 | `False` | `used`, `none_relevant` or `unstated` only |

  · **Check:** the matrix over every recorded turn, plus unit tests seeding each of the four
  values. · *Fails if* any turn falls outside a row — for example a model-authored `used` on a
  turn whose digest was empty, which means the deterministic writer did not take precedence, or
  a `not_applicable` on a successful planner turn with a digest, which means the field was never
  wired. The matrix is decidable from three logged fields alone, so no separate "who wrote this"
  field is added: the writer is a function of the row.

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
guarantees and names the exposure it does not close. Codex round 2 showed the five-value
relevance enum demanded two values at once on an empty-digest fallback, and killed the original
AC-2 on the stronger ground that a lexical check is not even necessary. D3 now records one axis,
and AC-2 is a paired seeded planner run. Codex round 3 confirmed D3 free of ambiguous and
unreachable states, and closed the last gaps: AC-1's comparison set is now logged rather than
assumed, AC-2's oracle is a coined seed token rather than a semantic judgment, and AC-3's one
undefined threshold is a plurality test. The round budget is exhausted at three.
