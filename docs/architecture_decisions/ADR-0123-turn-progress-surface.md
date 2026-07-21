# ADR-0123: Turn progress surface — make the wait legible, so the user stays attached

**Status:** Proposed
**Date:** 2026-07-21
**Deciders:** Owner (architect), cc-adrs (Opus)
**Tags:** pwa, transport, observability, human-in-the-loop, reliability

---

## Context

**What is the issue we're addressing?**

A Seshat turn can run for minutes and say nothing while it does. Two turns measured on
2026-07-21 make the problem concrete:

| Turn | Shape | Outcome |
|---|---|---|
| 07:01:47 → 07:03:44 | `perplexity_query` 55 s, then a planning LLM call 43 s **in silence** | Socket dropped 32 s into the silent step (phone put down). The ADR-0122 decision card fired into the gap; the model choice was spent silently on the default. |
| 10:26:29 → 10:32:11 | artifact build, ~6 minutes, same shape | The owner asked master whether the system was broken. It was not — it was working and saying nothing. |

**Silence is not merely poor UX here; it is the head of a causal chain that ends in lost data.**
A long silence causes the user to disengage. A disengaged user backgrounds the phone. A
backgrounded phone drops the socket. A dropped socket means a decision that required the user is
resolved without them. That chain is exactly what FRE-928 catches at its *tail* — the constraint
waiter treating a momentary absence as permanent. **This ADR addresses the same failure at its
head.** The two are sequential, not parallel: a user who can see work happening stays attached, and
a pause that lands on an attached user never needs the recovery path at all.

**The substrate is largely present, and the gap is specific.** Before designing anything new it is
worth stating precisely what exists, because the missing piece is narrower — and sharper — than
"we have no progress reporting."

*Already on the AG-UI transport, streamed and persisted with a sequence number:*
- `ToolStartEvent` / `ToolEndEvent` (`transport/events.py:42-68`) — tool name, args, result
  summary. The PWA already renders these: `ToolIndicator` (`seshat-pwa/src/components/ToolIndicator.tsx`)
  shows a spinner for running tools and a checkmark for completed ones.
- `turn_status` `StateUpdateEvent` (`transport/agui/transport.py:248-258`), carrying **fifteen**
  live per-turn fields (`observability/topology/projector.py:427-447`): `context_tokens`,
  `context_max`, `tool_iteration`, `tool_iteration_max`, `turn_cost_usd`, `trace_id`, `topology`,
  `degraded`, `degradations`, `session_cost_usd`, `session_context_tokens`, `compaction_count`,
  `cache_reset_count`, `quality_alert_count`, `quality_alert`. **Note `tool_iteration_max` and
  `context_max` are already served** — the client has no need to seed a ceiling, which makes the
  fabricated warning of §5 a pure client-side defect rather than a missing-data problem.

*Only in structlog / Elasticsearch, never reaching the client:*
- `step_planning_started` / completed (`telemetry/events.py:46`)
- sub-agent and artifact-draft start events
- every other marker of an **inference** step

**That asymmetry is the whole finding.** A grep for `planning`, `artifact_draft`, or `sub_agent`
across `src/personal_agent/transport/` returns **nothing**. The transport models **tool execution**
but does not model **inference**. And in both measured turns, *every long silence was an inference
step* — the 43-second planning call and the artifact-build sub-agent call. The 55-second tool call,
by contrast, was the one part of turn one the user could actually see.

So the system is not silent because it lacks a progress channel. It is silent because the longest
things it does are the only things it never announces.

**A second, narrower defect must not be inherited.** The PWA currently renders fallback constants in
the same visual language as live data: `StreamingChat.tsx:177` seeds
`{ tool_iteration: 0, tool_iteration_max: 6 }` before any `turn_status` arrives, and the gate colours
amber near the ceiling. During a turn whose real resolved ceiling was 25, the owner was shown an
amber "4 of 6" near-limit warning derived entirely from a constant. **A warning computed from a
placeholder is worse than no warning**, because it spends the user's trust on a fiction. FRE-928
criterion 4 fixes that specific instance; this ADR must make the principle structural rather than
let the new surface inherit the habit.

**What needs to be decided:** what the user is shown while a turn runs; at what granularity;
how inference steps become visible at all; how the surface behaves across the frequent reconnects a
mobile client actually experiences; and what happens to it when the turn ends.

---

## Decision

Ship a **turn progress surface**: an ephemeral, live view of what the turn is doing, driven by a
**phase model** that finally includes inference, and built so that **unknown always looks unknown**.
Seven parts.

*(Amended after codex review — see Status Updates: the phase model carries concurrent children rather
than claiming a single active activity, and human waits are an explicit phase excluded from the
silence metric.)*

### 1. Model the turn as phases, not as a log of everything

The organizing question is granularity: every tool call, or coarse phases? Neither alone.

**A phase is the unit of the surface.** At any instant the turn has exactly one *active phase*,
named in language a waiting human can read. A phase may contain **concurrent children** — see
"concurrency" below, which is not an edge case but the normal shape of expansion turns:

| Phase | Derived from | Example line |
|---|---|---|
| Understanding your request | turn start → first LLM call | *Understanding your request* |
| Searching / running a tool | `ToolStartEvent` / `ToolEndEvent` | *Searching the web — perplexity_query* |
| Thinking | LLM inference (**new**, §2) | *Thinking* |
| Planning the artifact | planning inference (**new**, §2) | *Planning the artifact* |
| Building the artifact | artifact-draft sub-agent (**new**, §2) | *Building the artifact* |
| Writing the response | final synthesis | *Writing the response* |

**Tool calls appear individually within their phase, because they are individually meaningful and
already individually streamed** — `ToolIndicator` proves the shape works. But the phase is what
carries the *narrative*; a bare list of tool names does not tell a waiting person whether the system
is a third of the way through or stuck.

This is the honest middle: phases stop the surface degenerating into a debug log, and per-tool
detail inside a phase stops it degenerating into a meaningless spinner. Anything finer than a tool
call (individual LLM tokens of internal reasoning, per-chunk retrieval) is **noise** and is
deliberately excluded (§4).

**Concurrency: one active phase, N concurrent children.** "Exactly one active phase" would be false
as a claim about the underlying work — `expansion_controller.py:404-419` dispatches sub-agents
through `asyncio.gather`, genuinely concurrently, and `tools_dispatched_parallel`
(`executor.py:4515`) does the same for tools. The model accommodates this without abandoning the
narrative unit: **the phase is the parent; concurrent activities are its children**, each with its
own running/completed state and elapsed time.

*Researching — 3 running*
  · *sub-agent: pricing history — 12 s*
  · *sub-agent: competitor set — 8 s* ✓
  · *sub-agent: regulatory context — 12 s*

The parent phase ends when its last child does. This keeps one readable headline while reporting the
concurrency honestly — and it means a fan-out turn does not present as a single opaque "Thinking"
for its whole duration. AC-8 asserts it.

**Human waits are a phase too, and they are not silence.** When the turn pauses for a decision — the
ADR-0122 turn-start builder card, a tool approval, the attachment-cost gate — the surface shows an
explicit *Waiting for your choice* phase. This matters for two reasons. It is honest: the system is
not working, it is blocked on the user, and presenting that as "Thinking" would be a lie. And it
bounds the silence metric correctly: **time spent waiting on a human is excluded from AC-2's gap
clock**, because a gap the user is themselves responsible for closing is not a silence the system
should be charged for.

### 2. Put inference on the transport — the actual gap

Add a **phase event** to the AG-UI transport, alongside the existing tool events, emitted at the
boundaries of the steps that are currently invisible:

```
PhaseStartEvent(phase: <enum>, detail: str | None, session_id: str)
PhaseEndEvent(phase: <enum>, session_id: str)
```

Emitted for: the planning inference step (`step_planning_started`/completed, today telemetry-only),
the artifact-draft sub-agent build, and the final synthesis inference. Tool phases continue to be
derived from the existing `ToolStartEvent`/`ToolEndEvent` — **those are not re-implemented**, and the
new events must not duplicate them.

This is the minimum change that makes the measured silences visible, and it is the part of this ADR
without which nothing else matters: a beautifully designed surface fed by a transport that does not
report inference would still show a blank panel for 43 seconds.

**Emission is best-effort and must never affect turn correctness** — the same posture as
`turn_status` (`projector.py:14`, "every `emit_turn_status` call is best-effort"). Precisely: the
best-effort behaviour lives in the shared `_push_event` path (`transport/agui/transport.py:95-118`),
not in `emit_turn_status` itself, so phase events inherit it by using the same path rather than by
re-implementing it. A failed progress emit is a cosmetic loss, never a failed turn. AC-6 asserts this
directly.

### 3. Elapsed time is the honest signal for a long silent step

For a 43-second planning call, the design choice is: stream partial output, or name the step and
show it running?

**Name it, and show elapsed time.** The surface displays the active phase with a live elapsed
counter (*Planning the artifact — 38 s*). No partial inference output is streamed for internal
steps.

Rationale, in order of weight:
1. **Elapsed time is the information the user actually wants.** "Is this stuck?" is answered by a
   number that keeps moving, not by text. A counter that advances is proof of life; a wall of
   half-formed planning text is not obviously distinguishable from a loop.
2. **Internal reasoning is not addressed to the user.** Planning output is a working artefact in a
   prompt-shaped format. Streaming it invites the user to read something written for a model, and on
   a phone it would dominate the screen.
3. **It costs nothing.** Elapsed time is computed client-side from the phase-start event; no
   additional server traffic, no per-token streaming path for internal calls.

*Final response text continues to stream as it does today* — that is output addressed to the user,
and it is a different thing entirely from internal steps.

**Escalating candour on long phases.** A phase that exceeds a threshold adds context rather than
just counting: *Building the artifact — 2 m 10 s. Large artifacts can take several minutes.* This
is a static, honest statement, **not** an estimate — see §5 on why no completion estimate is offered.

### 4. What is deliberately excluded

- **Per-token streaming of internal inference** (§3).
- **A progress bar or percentage.** The turn's shape is not known in advance — the tool-iteration
  count is a ceiling, not a plan — so any percentage would be invented. §5.
- **A completion estimate.** Same reason, and a wrong estimate is worse than none: it converts a
  bounded annoyance into a broken promise.
- **Retrieval internals, token-by-token context assembly, sub-chunk detail.** Below the honest
  granularity floor.
- **Making the turn faster.** This ADR makes the wait *legible*, not shorter. A six-minute artifact
  build remains six minutes. Latency work is real, separate, and out of scope; conflating them would
  let a progress surface be mistaken for a performance fix.

### 5. Unknown must look unknown — a structural rule, not a style note

**No element of this surface may be rendered from a fallback constant in the same visual language as
live data.** Concretely:

- Before the first `turn_status` arrives, numeric gauges (tool iteration, context, cost) render
  **unknown** — a dash, a skeleton, or an explicitly "waiting" treatment — **never** a seeded value.
  `StreamingChat.tsx:177`'s `tool_iteration_max: 6` seed is precisely the pattern being outlawed.
- **A derived warning state may only be computed from received data.** No amber, no near-limit
  colour, no "4 of 6" unless both numbers arrived from the server for this turn.
- The distinction must be **representable in the type**, not merely respected by convention: an
  absent value is a distinct state from a zero value, and the component cannot silently coerce one
  into the other.

This generalizes FRE-928's criterion 4 from one component into a rule the new surface is built on.
The reasoning is that a fabricated warning is not a small cosmetic bug — it actively teaches the user
that the system's signals are unreliable, which is corrosive to the exact trust this ADR exists to
build.

### 6. Reconnect: rebuild from state, not from replay

The client reconnects constantly — the measured session dropped and reattached every 30–140 seconds.
An ephemeral surface must survive that without either vanishing or replaying a stale narrative.

**The rule: the surface is a projection of current phase state, not an accumulation of the event
log.** On reconnect the client rebuilds the active phase from the latest known state and resumes,
rather than replaying every phase transition since the turn began.

This works because the precedent already exists: `turn_status` is a **full-state replacement keyed by
session** (`observability/topology/projector.py:7`), deliberately designed so a client can converge
from the newest value alone. The phase surface follows the identical pattern — the current phase is
carried as replaceable state, so the newest one wins and history is not re-narrated.

**Elapsed time survives** because phase-start carries a server timestamp; a client reattaching
mid-phase computes elapsed from that, rather than from when it happened to reconnect. Without this,
a reconnect would silently reset a 3-minute counter to zero — visible proof of life turned into
visible evidence of a restart that did not happen.

**Honest limitation:** if a phase begins *and* ends entirely inside a disconnect window, the client
never renders it. That is acceptable — the surface's job is to answer "what is happening now",
and a phase that already completed is answered by the transcript (§7). It is **not** acceptable for
the *current* phase to be missed, which is what state-replacement guarantees.

### 7. On completion, collapse into the transcript — do not vanish

When the turn completes the live surface collapses into a **compact, persistent summary** attached to
the turn in the transcript: the phases that ran, their durations, and the tools used — collapsed by
default, expandable.

Vanishing was considered and rejected (Option 3). The summary is what answers "what took six
minutes?" *after* the fact — the question the owner actually asked master on turn two. It also feeds
the pedagogic goal: seeing that an artifact build spent four minutes in the builder and forty seconds
planning is exactly the kind of self-knowledge this project exists to accumulate.

The summary is **derived from events already persisted** with their sequence numbers; it introduces
no new **server-side** storage and no new durable schema.

**One honest caveat:** the client does need somewhere to hold it. The PWA's `ChatMessage` type and
its history hydration (`seshat-pwa/src/lib/types.ts`, `components/StreamingChat.tsx:138-154`) carry
no phase-summary field today, so the collapsed summary needs either a client-side field populated
from the replayed event stream or a derivation at render time. That is a real implementation cost —
small, but it is not "free", and the seam ticket owns it.

---

## Alternatives Considered

### Option 1: A generic "working…" indicator with no phase detail
**Description:** A single spinner plus elapsed time for the whole turn; no phase names, no tool
detail.
**Pros:**
- Trivial to build; nothing new on the transport at all.
- Zero risk of exposing internals or misreporting a phase.
- Immune to the fallback-constant problem, since it renders almost nothing.
**Cons:**
- **Does not answer the question that was actually asked.** The owner's question on turn two was "is
  this broken?", and a spinner that has been spinning for six minutes is precisely what prompts it.
- Cannot distinguish a slow build from a stuck loop — the two look identical.
- Wastes an existing substrate: tool events are already streamed and already rendered.
**Why Rejected:** it is the status quo plus a timer. The measured failures were not caused by the
absence of a spinner but by the absence of *information*.

### Option 2: Stream everything — full event log to the client
**Description:** Forward the structlog event stream (planning start/stop, retrieval internals,
compaction, per-iteration state) to the PWA and render it as a live log.
**Pros:**
- Maximum transparency; nothing hidden.
- No design judgement required about what matters — ship it all.
- Genuinely useful for debugging a turn.
**Cons:**
- **Noise defeats the purpose.** A waiting human needs to know "is this working and roughly where is
  it"; a scrolling log answers that worse than one sentence does, especially on a phone.
- Exposes internal event names and prompt-shaped material as though they were product surface.
- Couples the client to the internal telemetry vocabulary, so every new log line becomes a UI change
  and every rename becomes a breaking change.
**Why Rejected:** wrong altitude for the consumer. The debug view is a real want, but it is a
*separate* surface for a different question; the developer-facing path already exists in
Elasticsearch and Kibana.

### Option 3: Ephemeral surface that vanishes on completion
**Description:** As decided, but the progress view disappears when the final result arrives, leaving
only the response.
**Pros:**
- Cleanest possible transcript — no accumulated machinery around old turns.
- Simplest lifecycle: nothing persists, nothing to design for the collapsed state.
**Cons:**
- **Destroys the answer to the after-the-fact question.** "What took six minutes?" is unanswerable
  once the surface is gone, which is the exact question turn two produced.
- Loses the pedagogic record — where time actually goes across many turns is a thing worth being able
  to see.
- The information is already persisted server-side, so discarding it client-side throws away
  something already paid for.
**Why Rejected:** the surface is ephemeral in *prominence*, not in *existence*. Collapsing costs one
disclosure control and preserves the answer.

### Option 4: Server-computed progress percentage
**Description:** The server estimates turn completion (phases done vs expected, tool iterations used
vs ceiling) and streams a percentage.
**Pros:**
- The most immediately legible possible signal; a bar that moves is universally understood.
- Would make long turns feel bounded rather than open-ended.
**Cons:**
- **The denominator does not exist.** The turn's shape is decided as it runs — the tool-iteration
  count is a ceiling, not a plan, and the model may call one tool or fifteen. Any percentage would be
  fabricated from a constant.
- That is precisely the failure mode §5 exists to outlaw: it is the "4 of 6" amber warning again,
  wearing a more convincing costume.
- A bar that reaches 90% and stalls is worse than no bar — it converts an honest wait into a broken
  promise.
**Why Rejected:** it requires inventing information the system does not have. Elapsed time is the
honest analogue and is offered instead (§3).

### Option 5: Fix FRE-928 and stop there
**Description:** Treat the lost-decision problem purely as the constraint-waiter defect: make the
pause survive a disconnect via the existing timeout-and-replay path, and accept the silence.
**Pros:**
- Much smaller; one defect, already specified and ticketed.
- Directly repairs the observed data loss with no new transport events and no new UI.
**Cons:**
- **Treats the symptom at the tail of the chain.** The user still waits in silence, still disengages,
  still backgrounds the phone — the recovery path simply gets exercised on every long turn instead of
  never.
- Does nothing for turn two, where nothing was lost and the failure was purely that the owner could
  not tell a working system from a broken one.
- Leaves a reliability mechanism carrying load that should not exist in the first place.
**Why Rejected:** not actually an alternative — **FRE-928 is complementary and still required**
(§Consequences). The rejection is of stopping there, not of doing it.

---

## Consequences

### Positive Consequences

- **The measured silences become visible.** The 43-second planning step and the multi-minute artifact
  build are exactly the phases §2 puts on the transport for the first time.
- **Disengagement pressure drops at its source**, which is upstream of the socket drop that cost a
  decision on turn one. This is a reliability improvement expressed as a UI change.
- **"Is it broken?" becomes answerable from the screen**, without the owner asking master to read
  telemetry — the literal event that motivated this ADR.
- **Fabricated signals are outlawed structurally** (§5) rather than fixed one component at a time.
- **The after-the-fact question is answerable** via the collapsed summary (§7), and it accumulates a
  record of where turn time actually goes.
- **Most of the substrate is reused**: tool events, `turn_status`, the persisted+sequenced event
  channel, and the existing `ToolIndicator` shape all stand.

### Negative Consequences

- **The transport gains a new event family.** Small and additive, but it is a real contract that must
  be versioned and kept in step with the client.
- **Emission sites are spread across the executor and the artifact path** — planning, sub-agent
  build, synthesis. Each is a place a future step could be added and forgotten, leaving a new silent
  gap. The phase model makes such a gap *visible* (an unnamed stretch) rather than silent, but it
  does not prevent it.
- **Phase names are user-facing copy**, so they become a thing to maintain and to keep honest as the
  pipeline changes. A stale phase name is a small lie.
- **Client-side elapsed timers depend on server timestamps**, introducing a clock-skew surface that
  did not exist before. Bounded — it affects a displayed duration, never correctness.
- **It does not make anything faster**, and there is a risk of it being read as though it did (§4).
- **The phase model must track the pipeline's real concurrency**, so expansion fan-out and parallel
  tool dispatch each need a child representation rather than a single line. That is more client state
  than a flat list, and it is the part most likely to drift as the pipeline changes.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| A new silent gap: an inference step ships with no phase event | Medium | The surface renders the *active* phase; an unnamed stretch shows as a phase that never ends rather than as silence. AC-2 asserts no gap exceeding a threshold on a representative artifact-build turn |
| A fabricated value is rendered as live data (the "4 of 6" class) | **High** | §5 makes absent-vs-zero a type-level distinction and forbids warning states derived from unreceived data; AC-4 asserts it directly against a no-`turn_status` mount |
| Reconnect resets a long-running elapsed counter to zero | Medium | Phase-start carries a server timestamp; elapsed is computed from it, not from reconnect time; AC-3 |
| Reconnect re-narrates the whole turn | Medium | Phase state is a full-state replacement keyed by session, mirroring `turn_status` (`projector.py:7`); newest wins, no replay accumulation; AC-3 |
| Progress emission failure breaks a turn | **High** | Best-effort emission, identical posture to `turn_status` (`projector.py:14`); AC-6 asserts a turn completes with emission forced to fail |
| The surface is read as a performance fix and latency work is deprioritised | Low | Stated explicitly as out of scope (§4); the collapsed summary (§7) in fact makes latency *more* visible, not less |
| Phase detail leaks prompt-shaped internals to the user | Low | Only phase names and tool names are surfaced; no inference output for internal steps (§3) |
| A fan-out turn presents as one opaque phase for minutes | Medium | Parent/child model (§1); AC-8 asserts three concurrent sub-agents render as three children with independent lifecycles |
| A phase spins forever after a cancel or error | Medium | Terminal states resolve the active phase (`CANCELLED`/`RUN_ERROR`/`DONE` already modelled client-side); AC-9 |
| The AC-2 gap clock penalises time spent waiting on the user | Low | `Waiting for your choice` intervals are excluded from the gap computation (§1) |

---

## Implementation Notes

**Files affected:**

- `src/personal_agent/transport/events.py` — add `PhaseStartEvent` / `PhaseEndEvent` alongside
  `ToolStartEvent`/`ToolEndEvent` (`:42-68`), with a closed phase enum.
- `src/personal_agent/transport/agui/adapter.py` (`:56` / `:62` dispatch arms) and
  `src/personal_agent/transport/agui/transport.py` (`:306-337`) — serialize and enqueue the new
  events on the existing persisted+sequenced path, inheriting best-effort from `_push_event`
  (`transport.py:95-118`) exactly as `emit_turn_status` (`:248-258`) does.
- `src/personal_agent/orchestrator/executor.py` — emit phase boundaries around the planning
  inference step (today `step_planning_started`, `telemetry/events.py:46`, telemetry-only) and the
  final synthesis step.
- `src/personal_agent/tools/artifact_tools.py` — emit phase boundaries around the artifact-draft
  sub-agent build (the multi-minute silence of turn two).
- `seshat-pwa/src/components/ToolIndicator.tsx` — generalize into the phase surface, retaining the
  running/completed treatment that already works.
- `seshat-pwa/src/components/StreamingChat.tsx:177` — **remove the
  `{ tool_iteration: 0, tool_iteration_max: 6 }` seed**; represent "not yet received" as a distinct
  state (§5). This is the same defect FRE-928 criterion 4 covers; coordinate so it is fixed once.
- PWA — collapsed per-turn summary in the transcript (§7), derived from already-persisted events.

**Dependencies:** ADR-0046 (AG-UI protocol stack — the transport this extends), ADR-0075 (WebSocket
transport, durable channel, one active socket per session, replay-from-seq), ADR-0076 (`turn_status`
STATE_DELTA and the DecisionCard whose reliability this improves upstream), ADR-0092 (context-
compaction surfacing — an existing consumer of `turn_status`), FRE-928 (constraint-pause
timeout/replay — complementary, ships independently), FRE-478/FRE-471 (why artifact builds are long).

**Testing strategy:** unit tests for phase-event emission at each boundary and for best-effort
failure; a client test mounting the surface with no `turn_status` received (the fabricated-warning
guard); a reconnect test asserting state-replacement rather than replay accumulation and elapsed
continuity; a live check on the deployed stack driving a real artifact build and observing the phase
sequence end to end.

**Sequencing (one PR each):**
1. **Phase events on the transport** — event types (parent phases + concurrent children + the
   `Waiting for your choice` phase), adapter dispatch, best-effort emission, and the emitters at the
   planning / sub-agent / synthesis boundaries and around the ADR-0122 pause. No UI yet; AC-1, AC-2,
   AC-6, AC-8 provable from the event stream alone.
2. **Unknown-is-unknown in the client** — remove the `tool_iteration_max: 6` seed, make absent a
   distinct state, forbid warning states derived from unreceived data. Independently valuable and
   independently testable; AC-4. Coordinate with FRE-928 criterion 4.
3. **The live phase surface** — generalize `ToolIndicator` into the phase view with elapsed time,
   escalating candour, and concurrent children; reconnect via state replacement; terminal
   cancel/error handling. AC-3, AC-5, AC-9.
4. **Collapsed per-turn summary** in the transcript. **(Seam ticket, AC-7.)**

---

## Verification / Acceptance Criteria

- **AC-1 — Inference steps are announced on the transport at all.** *Check:* drive a turn containing
  a planning step and an artifact build; the session event stream contains `PhaseStart`/`PhaseEnd`
  pairs for **planning**, **artifact build**, and **synthesis**, each carrying a server timestamp.
  *Fails if* any of the three is absent — which is the state today, where a grep for `planning`,
  `artifact_draft`, or `sub_agent` across `src/personal_agent/transport/` returns nothing, so this
  fails against current code.
- **AC-2 — No silent gap longer than the threshold, closed by *semantic* events only.** *Check:* on
  a live artifact-build turn, compute wall-clock gaps between consecutive **semantic progress
  events** — defined as a phase start, a phase end, a child start, a child end, a tool start, or a
  tool end. **No gap exceeds 10 seconds**, measured from turn start to final response, **excluding
  any interval in a `Waiting for your choice` phase** (§1: a wait the user must close is not a
  silence the system is charged for).
  **Filler does not count.** Periodic heartbeats, timer ticks, keepalives, and repeated
  `turn_status` emissions carrying no phase transition are explicitly **not** semantic progress
  events and must be excluded from the gap computation. *(Without this clause the criterion is
  trivially gameable by emitting an empty event every 5 seconds — which would satisfy the letter
  while leaving the user exactly as uninformed as the 43-second silence did.)*
  **Additionally**, the phase sequence observed must match the work actually performed: a turn whose
  trace shows a planning inference, a sub-agent build, and a synthesis step must show all three as
  distinct phases. *Fails if* any gap exceeds the threshold, if the gap is only met by non-semantic
  filler, or if the phase sequence omits a step the trace proves ran.
  *Why 10 seconds:* it is well below the shortest harmful silence observed (43 s, the one that lost a
  decision) while remaining long enough that no phase boundary must be invented to meet it — every
  boundary in the table above corresponds to real work starting or finishing. It is a threshold the
  phase model satisfies naturally and a filler-based implementation cannot satisfy honestly.
- **AC-3 — A reconnect mid-phase resumes rather than restarts or re-narrates.** *Check, with
  deliberately distinct durations so no two hypotheses produce the same number:* let a phase run
  **60 s**, then drop the client socket for **30 s**, then reattach. On reattach assert:
  **(a)** the surface shows the **currently active** phase, not the turn's first phase;
  **(b)** the displayed elapsed is **≈90 s** (server phase-start to now), within a 2 s tolerance —
  **not ≈0 s** (restarted at reconnect), **not ≈30 s** (measured from the disconnect), and **not
  ≈60 s** (frozen at drop time). Assert additionally that the phase-start timestamp the client holds
  is **byte-equal to the server-emitted timestamp** on the persisted event;
  **(c)** phases that started *and* ended before the drop are **not** re-narrated as active.
  *Fails if* elapsed matches any of the three wrong hypotheses, if the client's phase-start timestamp
  differs from the server's, or if completed phases replay as live. *(The three distinct expected
  values are load-bearing: an earlier form of this criterion asserted only "elapsed ≥ disconnect
  duration", which an implementation starting from reconnect-plus-a-fixed-offset satisfies without
  ever reading the server timestamp.)*
- **AC-4 — Absent is a distinct state from zero, not merely a hidden one.** *Check, three cases,
  and the third is what makes it discriminating:*
  **(a) No data.** Mount with **no `turn_status` received**: every numeric gauge renders an explicit
  unknown treatment, and **no warning colour, near-limit treatment, or "N of M" is displayed**.
  **(b) Real data.** Deliver a `turn_status` with `tool_iteration_max: 25` — the gauge reflects
  **25**, proving the ceiling comes from the server (which already sends it,
  `projector.py:427-447`).
  **(c) A legitimate zero.** Deliver a `turn_status` with `tool_iteration: 0` — the gauge renders
  **"0"**, visibly *different* from case (a). *(This is the discriminator: an implementation that
  merely hides everything until some value is truthy passes (a) and (b) and **fails** (c), because a
  real zero would be hidden as though it were missing.)*
  Assert the distinction is **representable in the type** — the component's value prop admits an
  explicit absent variant rather than relying on a sentinel number — so a future contributor cannot
  reintroduce a seed without changing the type.
  *Fails if* any value or warning appears before data arrives, if a received zero is indistinguishable
  from absent, or if absence is encoded as a magic number. *(The live defect today:
  `StreamingChat.tsx:177` seeds a ceiling of 6, producing an amber "4 of 6" during a turn whose real
  ceiling was 25.)*
- **AC-5 — A long phase reports elapsed time that advances.** *Check:* during a phase exceeding 60 s,
  the displayed elapsed value increases monotonically and is within a small tolerance of true
  wall-clock elapsed at two sampled instants at least 30 s apart. *Fails if* the value is static,
  resets, or drifts beyond tolerance — a frozen counter is indistinguishable from a hung turn, which
  is the condition this surface exists to rule out.
- **AC-6 — Progress emission never affects turn correctness.** *Check:* force the phase-event emit
  path to raise on every call; the turn still completes and returns its normal response, and the
  failure is logged. *Fails if* a turn errors, hangs, or returns degraded output because a cosmetic
  emission failed — the best-effort posture `turn_status` already holds (`projector.py:14`).
- **AC-8 — Concurrent work is reported as concurrent, not collapsed into one opaque phase.**
  *Check:* drive an expansion turn that fans out to **three** sub-agents via
  `expansion_controller.py:404-419`. The surface shows one active parent phase with **three**
  children, each with its own running/completed state; as each finishes, its child resolves
  independently; the parent ends only when the **last** child does. *Fails if* the three appear as a
  single undifferentiated phase, if the parent ends when the first child does, or if only one child
  is represented — any of which would present a multi-minute fan-out as one opaque "Thinking".
- **AC-9 — Cancel and error terminate the surface honestly.** *Check:* (a) cancel a turn mid-phase —
  the active phase resolves to a **cancelled** state, no phase is left spinning, and the collapsed
  summary records the turn as cancelled with the phases that had run; (b) force a turn to fail
  mid-phase — the active phase resolves to an **error** state and the summary records it. The client
  already models `CANCELLED` / `RUN_ERROR` / `DONE` terminal states
  (`seshat-pwa/src/lib/types.ts`, `hooks/useSSEStream.ts`), so all three must be handled. *Fails if*
  a phase spins forever after a terminal event, or if the summary presents a cancelled or failed turn
  as though it completed — a spinner that never stops is precisely the "is it broken?" signal this
  ADR exists to remove.
- **AC-7 (assembled seam) — the whole loop, live.** *Check:* on the deployed stack, run a real
  artifact-build turn end to end. Throughout, the surface names the active phase with advancing
  elapsed time and no gap beyond AC-2's threshold; a mid-turn reconnect resumes per AC-3; on
  completion the surface collapses to a summary listing the phases that ran with their durations and
  the tools used; and the summary's phase durations reconcile with the persisted event stream's
  timestamps. **Then repeat the run and cancel it mid-build**, asserting AC-9(a) end to end on the
  deployed stack. *Fails if* any leg breaks — announced → visible → survives reconnect → terminates
  honestly → collapses to an accurate record.

**Seam owner:** AC-7 is owned by the **collapsed-summary ticket (step 4)** — the child where the
assembled intent first holds. This ADR does **not** close when the live surface (step 3) merges; it
closes only when AC-7 is proven on the deployed stack. Master asserts AC-7 at the acceptance gate.

---

## References

- ADR-0046 — Agent-to-UI protocol stack (the transport this extends)
- ADR-0075 — WebSocket transport + durable channel (one active socket, persisted events, replay-from-seq)
- ADR-0076 — Adaptive constraint governance (`turn_status` STATE_DELTA; the DecisionCard whose reliability this improves at its source)
- ADR-0092 — Context-compaction observability & surfacing (existing `turn_status` consumer)
- ADR-0122 — Per-build artifact builder selection (the decision lost to the turn-one silence)
- FRE-928 — constraint pause bypassing its own timeout with no socket; **complementary, ships independently**, and its criterion 4 overlaps this ADR's §5
- FRE-471 / FRE-478 — artifact-build truncation and output-cap incidents (why builds run long)
- `src/personal_agent/transport/events.py:42-68` — `ToolStartEvent` / `ToolEndEvent`, the existing streamed tool lifecycle
- `src/personal_agent/transport/agui/transport.py:248-258` — `emit_turn_status`, the best-effort STATE_DELTA pattern the phase events mirror
- `src/personal_agent/observability/topology/projector.py:7` — `turn_status` as a full-state replacement keyed by session (the reconnect precedent)
- `src/personal_agent/observability/topology/projector.py:14` — "every `emit_turn_status` call is best-effort" (the emission posture)
- `src/personal_agent/observability/topology/projector.py:428-443` — the live `turn_status` payload fields
- `src/personal_agent/telemetry/events.py:46` — `step_planning_started`, telemetry-only today
- `seshat-pwa/src/components/ToolIndicator.tsx` — the existing running/completed tool treatment, generalized by this ADR
- `seshat-pwa/src/components/StreamingChat.tsx:177` — the `tool_iteration_max: 6` seed that produced a fabricated amber warning

---

## Status Updates

### 2026-07-21 - Proposed
**Changed By:** cc-adrs (Opus)
**Reason:** Owner-raised: an ephemeral surface showing the processes running during a turn until the
final result arrives, with Claude Code named as the UX reference. Grounded in two turns master
measured the same day — a 117-second turn whose 43-second planning step ran in silence, during which
the socket dropped and an ADR-0122 decision card was consequently resolved without the user; and a
six-minute artifact build after which the owner asked master whether the system was broken.

The framing that shaped the design: **silence is the head of a causal chain that ends in lost data**
(silence → disengagement → backgrounded phone → dropped socket → decision resolved without the user),
so this is a reliability intervention expressed as a UI change, and it sits *upstream* of FRE-928
rather than parallel to it.

Investigation found the gap is narrower and sharper than "no progress reporting." Tool execution is
**already** on the AG-UI transport (`ToolStartEvent`/`ToolEndEvent`) and already rendered
(`ToolIndicator`); `turn_status` already streams live per-turn metrics. But a grep for `planning`,
`artifact_draft`, or `sub_agent` across `src/personal_agent/transport/` returns **nothing** — the
transport models tool execution and does not model inference. In both measured turns *every long
silence was an inference step*. Hence §2: the load-bearing change is putting inference phases on the
transport at all.

Design decisions worked through: **granularity** — phases as the narrative unit with per-tool detail
inside them, rejecting both a bare spinner and a full event log; **long silent steps** — named with a
live elapsed counter rather than streaming internal reasoning, because elapsed time is what answers
"is this stuck?" and internal output is not addressed to the user; **reconnect** — phase state as a
full-state replacement mirroring `turn_status`'s existing session-keyed pattern, with elapsed derived
from a server timestamp so a reattach does not reset a long counter; **completion** — collapse to a
persistent summary rather than vanish, because "what took six minutes?" is the question turn two
actually produced. No progress percentage and no completion estimate: the denominator does not exist,
and inventing one is the same failure as the fabricated warning below.

Master's caution is adopted as a structural rule (§5) rather than a style note: the PWA today seeds
`tool_iteration_max: 6` (`StreamingChat.tsx:177`) and showed an amber "4 of 6" near-limit warning
during a turn whose real ceiling was 25. Absent must be a distinct state from zero at the type level,
and no warning may be derived from unreceived data. AC-4 asserts it; FRE-928 criterion 4 covers the
existing instance, so the two are coordinated to fix it once.

**Revised after codex review.** The central architectural claim was verified against source and
holds. Eight blocking findings, all accepted.

*Two were design gaps that changed the model.* **Concurrency:** "exactly one active phase" was false
as a claim about the work — `expansion_controller.py:404-419` fans sub-agents out through
`asyncio.gather`, and `tools_dispatched_parallel` does the same for tools. The model now carries one
parent phase with N concurrent children (§1, AC-8); without this a multi-minute fan-out would have
presented as a single opaque "Thinking", which is the exact failure the ADR exists to remove.
**Human waits:** the ADR-0122 turn-start card now pauses the turn at its very start, and the ADR had
not said whether that counts as a phase or whether the silence clock runs during it. It is an
explicit `Waiting for your choice` phase, and its interval is **excluded** from AC-2 — a gap the user
must close is not a silence the system is charged for.

*Four were gameable acceptance criteria.* **AC-2** could be satisfied by emitting empty heartbeats
every few seconds, so "progress-bearing" is now defined as a semantic phase/child/tool transition
with filler explicitly excluded, and the 10-second threshold is justified against the 43-second
harmful silence rather than asserted. **AC-3** asserted only "elapsed ≥ disconnect duration", which a
reconnect-plus-offset implementation satisfies without ever reading the server timestamp; it now uses
three deliberately distinct durations (60 s phase, 30 s drop, ≈90 s expected) so each wrong hypothesis
produces a distinguishable number, plus timestamp equality against the persisted event. **AC-4** was
passable by hiding everything; it now requires a received **zero** to render visibly differently from
absent, and the distinction to be representable in the type. **AC-7** ignored terminal states; cancel
and error are now AC-9, with the cancel leg repeated on the deployed stack — a phase spinning forever
after a cancel is precisely the "is it broken?" signal being removed.

*One was factual.* The `turn_status` payload list was a subset: it carries **fifteen** fields, not
seven, and critically already includes `tool_iteration_max` and `context_max` — so the fabricated
ceiling warning is a pure client-side defect, not a missing-data problem. Non-blocking: best-effort
behaviour lives in the shared `_push_event` path rather than in `emit_turn_status` itself; several
line citations refreshed; and the "no new storage" claim now carries its honest client-side caveat,
since `ChatMessage` has no phase-summary field today.
