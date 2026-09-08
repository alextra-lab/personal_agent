# ADR-0147: Memory Belongs to the Planner — a Bounded Digest Shapes the Specs, and No Memory Reaches the Worker

**Status:** Proposed
**Date:** 2026-09-08
**Deciders:** Owner (design direction, 2026-09-08), adr seat (author)
**Tags:** orchestrator, expansion, memory, sub-agents, context-budget

---

## Context

### The measured defect

A sub-agent's entire context is one message: the raw user question. Trace
`d5e83932d1afbdf3839c05ebfca6417b` recorded 246 characters and
`context_message_count: 1`. The field `memory_in_context` is `False` on all 186 sub-agent
captures ever written, across every index month. Master checked the detector before trusting
that number. `sub_agent.py:57` scans for `"## Your Memory Graph"`, which is the exact prefix
`executor.py:3758` emits, and five primary-turn captures do carry the marker. The absence is
real.

The planner is memory-blind by construction. `_run_planner` (`expansion_controller.py:401`)
builds exactly two messages. The system prompt comes from `_build_planner_system_prompt`, which
takes only the live sub-agent tool surface. The user message reads
`f"Strategy: {strategy}\nQuery: {query}\n\nProduce the JSON plan."`. The token counts agree with
the code: the gateway assembled 516 memory tokens on that turn, and the planner call ran at 289
input tokens.

`spec.context` is a blind tail slice. `expansion_controller.py:622` sets
`context=messages[-4:] if messages else []`. No component chooses it.

### The finding that changes the cost argument

**The memory items are already in hand when the planner runs.** `step_init` assigns
`ctx.memory_context` from the gateway at `executor.py:4893`. The expansion call sits at
`executor.py:5036`, in the same function, 143 lines later.

Two consequences follow.

First, no design in this ADR needs a new recall query. The retrieval cost is already paid, and
the same items render into the primary's own call at `executor.py:5986`.

Second, **FRE-960 is a quality ceiling, not a blocker.** `multi_query_recall_arm`
(`memory/service.py:5678`) is one arm of the gateway's recall. Its silent failure since
2026-07-20 lowers the quality of what the gateway assembles. It does not block a mechanism that
issues no query of its own.

### The two mechanisms that were bundled

FRE-1470 describes two different designs in two places, and they are not the same thing.

**Inform.** The planner sees what memory holds. It writes better task goals. Memory never
leaves the planner. The worker receives a sharper instruction, not a fact.

**Route.** The planner selects memory items per task. The items ride in `spec.context` to the
worker. The worker reads them as context.

The owner settled this on 2026-09-08: inform only. The reason is that the primary already holds
full memory at synthesis, so a worker never needs to *hold* a personal fact. It needs to be
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

### D1 — The planner receives a bounded memory digest

A **memory digest** is a compact list built from `ctx.memory_context`, one line per item. This
ADR uses that term for the rest of the document. Each line carries the item kind, the item name
or target, and one short clause of the fact.

The digest is built in `executor.py`, next to `_render_memory_section_with_ids`, and is passed
into `ExpansionController.execute` as a value object. The expansion controller never sees a raw
memory item, so it gains no knowledge of memory-item shape.

The digest omits everything the primary's own section carries for a different purpose: citation
identifiers (ADR-0138 D1 binds those to the answering model, not to a planner), the guidance
paragraphs, the episode bodies, and the `key_entities` lists.

**One clause per line, not a bare name.** A names-only list lets the planner write a goal that
names a fact the worker cannot obtain — for example *"consider the user's transmission
preference"*. The worker holds no memory and cannot resolve that reference, so a names-only
digest produces goals that are worse than silence. The clause is the part that makes the
mechanism work, because the planner inlines the fact itself into the goal text.

### D2 — Memory informs the specs and never reaches the worker

`spec.context` stays the `messages[-4:]` tail slice. No memory item is attached to any
sub-agent, by the planner or by anything else.

Three reasons, in order of weight:

1. The primary holds the full memory section at synthesis, so no fact needs to travel through a
   worker to reach the answer.
2. A worker holding `web_search` is a disclosure path. Egress reaches the self-hosted SearXNG
   instance and then upstream engines. A personal fact placed in a worker's context can enter a
   search query.
3. A planner-selected item arrives at the worker as asserted context, and no downstream
   component re-checks it. A wrong topic hint only produces a slightly off goal.

Point 2 grows stronger, not weaker, over time. FRE-1467 threads identity into the sub-agent's
`TraceContext`. That change is in flight and unmerged at the date of this ADR. When it lands, it
removes the missing-identity guard that currently makes `search_memory` return little to a
worker. The worker-holds-memory path becomes live at the moment it becomes risky.

### D3 — The plan carries an absence signal, not a validation arm

`ExpansionPlan` gains one field, `memory_relevance`, with three values and one writer each:

| Value | Written by | Meaning |
|-------|-----------|---------|
| `absent` | the code, deterministically | The digest was empty. The planner was never asked. |
| `used` | the planner | At least one digest line shaped at least one task goal. |
| `none_relevant` | the planner | A digest existed and nothing in it bears on this query. |

`none_relevant` is the first honest absence signal in the system: a thinking-capable component
states that recalled memory does not bear on the question. FRE-1118's own defect — the
relevance score never reaching the model, and the prompt forbidding the honest answer — stays
FRE-1118's work. **This ADR produces the signal. It does not change what the primary says.**

No validation sub-agent is added. Such an arm is an extra LLM call per turn inside the loop
FRE-1138 shows is unbounded, for a signal the planner emits at no extra call.

### D4 — The digest is bounded independently of the render bounds

The primary's render bounds admit up to 47 items: 15 entities, 5 episodes, 15 stances and 12
behavioural stances (`executor.py:3520-3549`). At roughly 15 tokens per line, an unbounded
one-line-per-item digest reaches about 700 tokens. **That is more than the 516 the full section
cost.** The digest is cheap only if it is bounded on its own terms.

The bounds are:

- at most **20 items**, taken in the upstream relevance order the memory context already
  carries;
- at most **120 characters** per line;
- a hard ceiling of **300 tokens** for the whole block, measured with
  `request_gateway/budget.py:estimate_tokens`, the gateway's own estimator.

The ceiling binds. When the assembled lines exceed it, lines are dropped from the tail until the
block fits. The digest is never truncated mid-line.

Against FRE-1138's ungoverned within-turn growth, the property that matters is **the digest is
paid once per turn, not once per arm.** The planner runs once. The rejected alternative of
injecting memory into every sub-agent pays 516 tokens against a six-to-eight arm fan-out.

### D5 — A fallback plan carries no digest

`generate_fallback_plan` (`fallback_planner.py:49`) is a deterministic keyword split and makes
no LLM call. It receives no digest and sets `memory_relevance` to `absent`. This is a stated
limitation, not an oversight. The fallback rate is already observable through the
`fallback_planner_used` log line and the FRE-1413 warning step.

### D6 — Enrichment stays out of scope

No write path is added. A worker's output is agent-derived text at exactly the tier FRE-1022
(ADR-0098 D6, the corroboration gate), FRE-1302 and FRE-1303 show the system already mis-trusts.
Adding a write path before those hold converts one live defect into a persistent one.

### D7 — No further memory tool is granted to a worker

This ADR grants nothing to the sub-agent tool principal. It also does not revoke the
`search_memory` grant that FRE-1463 made on 2026-09-08, because a governance revocation is the
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
- Every worker becomes a disclosure path.

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
selected items ride to the worker.

**Pros:**
- Selection is per task rather than per question, which is the strongest form of the precision
  argument.
- A worker with a long task can consult a fact the goal text has no room for.

**Cons:**
- The planner must emit identifiers it cannot be trusted to invent correctly.
- The selected item reaches the worker as asserted context that nothing re-checks.
- It reopens the disclosure path D2 closes, and FRE-1467 widens that path.

**Why Rejected:** The primary already holds full memory at synthesis, so the worker never needs
to hold the fact. **Reopening condition, stated in advance:** measurement under AC-2 shows
planners routinely naming facts they had no room to inline. Absent that evidence, this stays
rejected and no ticket is filed for it.

### Option 4: A validation sub-agent arm

**Description:** One extra sub-agent per turn, asked whether the recalled memories bear on the
question.

**Pros:**
- It produces an absence verdict from a component that reads the memories in full.

**Cons:**
- One more LLM call per turn, inside FRE-1138's unbounded loop.
- The verdict arrives after the plan is already written, so it cannot shape the specs.

**Why Rejected:** D3 obtains the same signal from the planner at no extra call, and obtains it
early enough to matter.

### Option 5: Give the planner its own retrieval capability

**Description:** The planner issues a recall query of its own before writing the plan.

**Pros:**
- The planner asks for exactly what the decomposition needs.

**Cons:**
- It re-queries what the gateway already retrieved for this same question.
- It adds a serial round trip on the critical path, before any dispatch begins.
- It depends on the multi-query arm that FRE-960 shows returns `[]` on every call.

**Why Rejected:** The items are already in hand at `executor.py:4893`. A second query buys
nothing and costs latency inside a turn budget that already fails.

---

## Consequences

### Positive Consequences

- The one component that decides the work stops being the only one denied user context.
- The retrieval cost is zero. The digest re-renders items the turn already paid for.
- The mechanism does not depend on FRE-960, which is dead today with no signal.
- The system gains its first honest absence signal, at plan altitude, for no extra call.
- The cost is paid once per turn rather than once per arm.

### Negative Consequences

- The planner's job gets harder. It must read a digest, judge relevance, and inline facts into
  goals, at a call that today runs at 289 input tokens.
- The planner's input roughly doubles in the worst case, from 289 tokens to at most 589.
- A fallback plan gains nothing (D5), so the benefit is absent exactly when the planner already
  failed.
- Two new failure surfaces exist: a digest that is built but never used, and a `used` claim that
  no goal supports. AC-2 and AC-3 exist to catch both.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| The digest is built and the planner ignores it, repeating FRE-1463's inert grant | High | AC-3 fixes a non-inertness floor in advance and measures it over live turns |
| The planner claims `used` without any goal reflecting the digest | Medium | AC-2 is a consistency invariant checked at 100%, not a threshold |
| The digest grows past its ceiling on a memory-rich turn and feeds FRE-1138 | Medium | D4's hard ceiling, plus AC-4's seeded negative over 47 oversized items |
| A future change attaches memory to `spec.context` and silently reopens the disclosure path | Medium | AC-5 asserts `memory_in_context` stays `False` on every sub-agent capture |
| The digest inlines a personal fact into a goal that a worker then puts in a search query | Low | The planner is thinking-capable and chooses the volume deliberately. The same risk exists today whenever the user's own question carries the fact |

---

## Implementation Notes

**Files affected:**

- `src/personal_agent/orchestrator/executor.py` — a new digest builder beside
  `_render_memory_section_with_ids`; the `controller.execute(...)` call site at `:5036` passes
  the digest.
- `src/personal_agent/orchestrator/expansion_types.py` — a frozen `PlannerMemoryDigest`
  (`text`, `item_count`, `estimated_tokens`); `ExpansionPlan.memory_relevance`.
- `src/personal_agent/orchestrator/expansion_controller.py` — `execute` and `_run_planner`
  signatures; `_build_planner_system_prompt` gains the digest rule and the schema field;
  `_validate_plan_json` parses and defaults `memory_relevance`.
- `src/personal_agent/orchestrator/fallback_planner.py` — sets `memory_relevance` to `absent`.

**Telemetry.** The `planner_started` and `planner_completed` log lines gain
`memory_digest_items`, `memory_digest_tokens` and `memory_relevance`. Every criterion below
reads these fields, so no new instrument is built.

**Testing strategy.** Unit tests cover the bound with a seeded negative — 47 items with
oversized descriptions must yield a digest at or below 20 items and 300 tokens. A regression
test asserts `SubAgentSpec.context` still equals the `messages[-4:]` slice after the change.

**Dependencies.** None. FRE-1467 is in flight and is not a blocker. FRE-960 is a quality
ceiling, not a dependency.

---

## Verification / Acceptance Criteria

**A stated limit first.** These criteria prove the mechanism, not the answer quality. Whether a
memory-informed spec produces a better sub-agent result has no turn-scoped instrument here. The
eval set is not HYBRID-scoped, and the sub-agent digest is a free-text summary. The owner's own
live turn stays the only instrument for quality. This gap is stated rather than papered over
with a proxy metric.

- **AC-1** — On a live HYBRID or DECOMPOSE turn where the gateway assembled memory, the digest
  the planner received contains only items from that turn's own `ctx.memory_context`, and its
  item count equals `min(assembled_items, 20)`. · **Check:** compare `memory_digest_items` on
  the `planner_started` line against `memory_enrichment_completed`'s `conversations_found` for
  the same `trace_id`, then read the digest lines against the turn's memory items. · *Fails if*
  the counts disagree, or a digest line names something the turn never recalled.

- **AC-2** — On **every** turn where the planner returned `memory_relevance: used`, at least one
  task goal contains a distinctive term that appears in the digest and does **not** appear in
  the user's own question. · **Check:** per-trace comparison of the plan's task goals against the
  digest text and `user_message`. · *Fails if* any single `used` turn has no such term, which
  means the planner claimed a use that the plan does not show. The pre-change rate of this
  measure is structurally zero, because the planner had no access to those terms.

- **AC-3** — Over at least 20 live turns with a non-empty digest, `memory_relevance: used` is
  returned on **at least 25%**. · **Check:** count over the `planner_completed` lines. · *Fails
  if* the rate falls below the floor, which reproduces FRE-1463's inert grant in a new place.
  The floor is fixed here, before the first measured run. Its basis: the digest is not arbitrary
  memory, it is memory the gateway retrieved *for this same query*, so a mechanism that uses it
  on fewer than one turn in four is not delivering D1. The floor is set deliberately low so it
  separates working from inert rather than grading quality.

- **AC-4** — No turn ever produces a digest above 20 items or 300 estimated tokens, including on
  a memory-rich turn. · **Check:** the maximum of `memory_digest_items` and
  `memory_digest_tokens` across all recorded turns, plus a unit test that seeds 47 items with
  oversized descriptions. · *Fails if* either maximum is exceeded, or the seeded test produces a
  digest that fits only because the input was already small.

- **AC-5** — `memory_in_context` remains `False` on **every** sub-agent capture written after
  this ADR's chain deploys, and `context_message_count` stays at the tail-slice size. ·
  **Check:** the same Elasticsearch query master used to establish the 186-capture baseline. ·
  *Fails if* any capture reports `True`, which means memory reached a worker and D2 was
  breached.

- **AC-6** — When the gateway assembled zero memory items, `memory_relevance` reads `absent` on
  100% of those turns, and no such turn is ever recorded as `used` or `none_relevant`. ·
  **Check:** join `planner_completed` against `memory_enrichment_completed` absence for the same
  `trace_id`. · *Fails if* the model wrote a value the code owns, which means the deterministic
  writer in D3's table was not enforced.

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
- FRE-1470 — this ADR's umbrella ticket, carrying the owner's own words on both challenges
- FRE-1469 — the sub-agent context barriers, measured
- FRE-1467 — sub-agent identity, in flight and unmerged at this date
- FRE-1463 — the sub-agent tool grant, and the `search_memory` grant measured inert
- FRE-1390 — the planner runs at `ModelRole.PRIMARY`, and why
- FRE-1138 — within-turn growth is the ungoverned axis
- FRE-1118 — the system cannot express "nothing relevant exists"
- FRE-960 — multi-query paraphrase generation, silently dead since 2026-07-20
- Traces `d5e83932d1afbdf3839c05ebfca6417b`, `896c7b1d774d6a31dabe030f28e5633d`,
  `e169da700b8e99213dfdddca351a2af7`

---

## Status Updates

### 2026-09-08 - Proposed
**Changed By:** adr seat (FRE-1470)
**Reason:** Authored from the owner's design conversation of 2026-09-08. The owner settled the
inform-versus-route question in favour of inform only.
