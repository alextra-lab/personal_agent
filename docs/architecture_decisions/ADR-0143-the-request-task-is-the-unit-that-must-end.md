# ADR-0143: The Request Task Is the Unit That Must End — Bound the Service Task, Not the Turn

**Status:** Proposed
**Date:** 2026-09-05
**Deciders:** Owner (architect); adr seat (Fable 5.1, on the owner's instruction)
**Tags:** service, transport, orchestrator, reliability, observability

---

## Context

**What is the issue we are addressing?**

FRE-1403 was filed as a follow-up to ADR-0142 D4a: the orchestrator's lifetime cap,
`orchestrator_turn_lifetime_seconds`, binds only an in-flight LLM call and a constraint pause. Tool
dispatch and the state-driver loop are not bounded by it. The same day, the ticket was raised to
Urgent on a live report: a turn streamed its answer and then hung. Master traced it and concluded
that no DONE row was persisted and that the request task was stuck before its own cleanup, with
the sysgraph write as the first suspect.

This ADR session measured that incident before designing anything. The measurement changed the
problem.

### The incident did not happen as described

Session `ddcdeb1a…`, trace `81f33956…`, 2026-09-05. Every fact below was read from the live
substrate, not inferred from the log tail.

| Ticket claim | Measured | Instrument |
|---|---|---|
| "No DONE row was ever persisted" | `session_events` seq 23 is `DONE`, trace `81f33956…`, at 14:47:05.220 | Postgres |
| "The `finally` never ran" | `request.completed` — the last statement of the `try` — landed in Redis at 14:47:05.214. DONE followed 6 ms later | Redis stream ids |
| "Stuck before its own cleanup" | Process healthy 30 minutes later. All five threads idle. Seven Postgres sockets matched seven idle backends. Redis answered | `py-spy dump`, `ss`, `pg_stat_activity` |
| "sysgraph's Postgres write is the first suspect" | Reflection runs in a detached task (`executor.py:4157`, `run_in_background`). It cannot block the request task | code |
| "The answer is finished at 14:46:55" | A third model call, `role=span_extraction` on Sonnet, ran 14:46:55.5 to 14:47:04.9. `reply_ready` at 14:47:05.117 | gateway log |

The turn finished 10.3 seconds after the primary model produced the answer. The last visible
events in the trace — `dspy_reflection_succeeded`, `sysgraph_read_before_emit_decided` — came from
a different task that ran concurrently and finished later. Reading them as the request task's
last position was the error.

### What the owner saw was two other defects

The owner reported, on request: the context counter's denominator went to "–", the token count
"comes and goes", and the response arrived in one dump.

- **Post-turn work re-opens a completed turn.** At `turn.completed` the projector marks the
  observation `completed`, emits it, and pops the trace (`projector.py:434`). Entity extraction,
  consolidation and reflection then each publish `turn.model_call_completed` under the turn's
  trace id, because the cost boundary attributes their spend to the turn. Each one recreates a
  blank observation — `tool_iteration 0`, `context_tokens 0`, `context_max null` — and emits it as
  `turn_status`. Seq 25–33 of the session are ten such rows, 4 to 22 seconds after DONE. Over the
  preceding 24 hours, 7 of 23 turns did this, average 11 rows, longest tail 30 seconds. `null`
  renders as "–". This is a bug, filed separately. It is not a decision this ADR makes.
- **The reply waits for span extraction.** `grounding_verification_completed` records a
  compliance observation and strips citation markers. It does not change or gate the reply. It
  sits on the critical path anyway. Filed separately.

### The gap that is real

What the measurement did confirm is the structural claim in FRE-1403's title, moved one layer up.

The chat turn is an `asyncio.Task` created at `app.py:2562` with no reference kept and no done
callback. Nothing can enumerate it, bound it, or observe its cancellation. Its body awaits, after
the executor returns, a Postgres append, a Redis publish, and — in the `finally` — `emit_done`,
which takes a per-session lock and does a second Postgres append. Inside the executor, the
`observe_topology` exit awaits a Postgres write and a Redis publish. None of these six awaits has a
timeout. None logs on entry. A block in any one of them would produce exactly the signature the
ticket described: answer delivered, trace silent, socket open.

`orchestrator_turn_lifetime_seconds` describes itself as an "absolute wall-clock cap on a turn".
It binds two call sites inside `execute_task`. Two of the six awaits above are outside
`execute_task`, so the setting cannot bind them even in principle. This is the fifth setting this
week found to claim more than it binds (FRE-1398 lists the first four).

### What needs to be decided

Where the ceiling on a request lives, what it covers, and what happens at the ceiling when the
cleanup path shares the resource that failed.

---

## Decision

**The request task, not the turn, is the unit that must end.** The service layer owns it, tracks
it, bounds it, and closes the client stream at the bound even when its own persistence is
unavailable. The orchestrator's cap stays what it is and says so.

**D1 — Every chat request task is created through a service-level registry.** A module in
`service/` owns a `session_id → asyncio.Task` map. `chat_stream` creates the turn task through it
and nowhere else. The registry keeps a strong reference for the task's lifetime, removes it on
completion, and attaches one done callback that logs `chat_stream.task_ended` with `trace_id`,
`outcome ∈ {completed, cancelled, failed}` and elapsed seconds. A cancelled task is no longer
silent. The registry is enumerable, so a health probe or a shutdown hook can see live turns.

**D2 — The request task carries an outermost wall-clock bound.** A new setting,
`service_request_task_lifetime_seconds`, wraps the whole body of `_process_chat_stream_background`
in `asyncio.timeout`. A validator requires it to exceed `orchestrator_turn_lifetime_seconds`; the
proposed default is that value plus 300 seconds, so the orchestrator's cap fires first on a
long-but-live turn and this bound fires only when something below it did not return. At expiry the
body is cancelled. Cancellation runs every `finally` on the way out, including the executor's
topology exit and the request task's own.

**D3 — The close path is bounded on its own, and degrades rather than waits.** `emit_done` runs
under `asyncio.timeout(service_request_close_timeout_seconds)`, proposed default 10 seconds. If the
DONE row cannot be persisted inside that window — exception or timeout — the close sentinel is
pushed to the session queue anyway, and `transport.done_degraded` is logged at warning with the
reason. The live socket always closes. Replay after a degraded close lacks a DONE row; that is a
known, logged, recoverable state, and it is strictly better than a socket that never closes.

**D4 — The orchestrator's cap binds where it says it binds, and one more place.** The description
of `orchestrator_turn_lifetime_seconds` is corrected to name its bind points: an in-flight
primary-model call, a constraint pause, and — new — a pre-step check in the state-driver loop.
The loop checks the lifetime once per transition and routes to `_stop_turn_for_lifetime_cap` when
it is exhausted. A tool call already in flight is **not** cancelled mid-execution: a tool's side
effects are not the orchestrator's to unwind. The per-step check bounds the *next* step; D2 bounds
the whole task. Tool dispatch is bounded by the service task, at the boundary where cancellation
is safe because every `finally` runs.

**D5 — Post-turn work stays detached, and the request task never awaits it.** Entity extraction,
consolidation and reflection run in `run_in_background`'s tracked set today. That stays. The
invariant this ADR states is the other direction: the request task's lifetime ends at DONE, and
nothing after the executor returns may await detached work. Post-turn work continues to publish
cost under the turn's trace id, because that is where the spend belongs. What the projector does
with a post-completion event is the bug ticket's decision, not this ADR's.

**Why this and not the alternatives.** The block, when it comes, will come from a call site nobody
listed. Bounding call sites one by one is a list that is wrong the day after it is written. A
bound at the outermost boundary of the unit of work covers what exists and what is added later.
The cleanup path is bounded separately because it shares Postgres with the most likely cause, and
a bound that depends on the failed resource is not a bound.

---

## Alternatives Considered

### Option 1: Bound each I/O call site in the request tail

**Description:** Wrap the Postgres append, the Redis publishes, the topology row write and
`emit_done`'s append each in their own `asyncio.timeout`.

**Pros:**
- Precise. Each timeout is sized to its call.
- No cancellation of the orchestrator body.

**Cons:**
- Six sites today. The list grows with every feature that adds a tail await.
- Misses the case this ADR exists for: the call nobody listed.
- FRE-1403 itself flagged this shape as undesirable for the step gate.

**Why Rejected:** It solves the known sites and leaves the class open.

### Option 2: An external watchdog that closes the stream

**Description:** A separate task scans the registry and, for any turn past the bound, pushes the
close sentinel to that session's queue directly, leaving the stuck task alone.

**Pros:**
- Survives a cleanup path that is itself stuck.
- No cancellation semantics to reason about.

**Cons:**
- A second writer to the session stream. FRE-518 serialises every emit under the per-session lock
  precisely so `seq` order equals enqueue order; a writer outside that lock reintroduces the race
  ADR-0075's replay guard cannot recover from.
- The stuck task keeps its Postgres connection and its memory. Nothing ends.
- Two mechanisms — a bound and a watchdog — where one boundary suffices.

**Why Rejected:** D3 gets the watchdog's one benefit — a socket that closes when persistence is
down — without the second writer, by making the close path degrade inside the lock.

### Option 3: Extend `orchestrator_turn_lifetime_seconds` to cover the request tail

**Description:** Move the executor's cap up so it also wraps the service-layer awaits.

**Pros:**
- One setting.

**Cons:**
- The orchestrator does not own the service task. Making an orchestrator setting bind
  service-layer code is the same "claims more than it binds" shape, inverted.
- ADR-0142 D4a's cap is a turn-semantics decision: it preempts a pause and routes to synthesis.
  The service bound has no synthesis to route to; it ends a task.

**Why Rejected:** Wrong layer. Two bounds, each owned by the layer it binds, and a validator that
orders them.

### Option 4: Do nothing — the incident did not happen

**Description:** Close FRE-1403 on the finding that the turn completed normally.

**Pros:**
- No code.

**Cons:**
- The untracked task and the six unbounded awaits are measured facts, not the incident's
  inference. A cancelled turn task is silent today. The setting's description is wrong today.
- The next real hang will present with the same signature, and the same wrong diagnosis will be
  reached from the same log tail, because the request task leaves no trace of its own ending.

**Why Rejected:** The design gap is real. Its priority is not Urgent, and this ADR says so.

---

## Consequences

### Positive Consequences

- A chat turn cannot outlive `service_request_task_lifetime_seconds`. The claim is checkable.
- A client stream always closes, including when Postgres is unavailable at close time.
- A cancelled or failed request task logs its own ending with `trace_id`. The next investigation
  starts from the task's record, not from the last event some other task emitted.
- `orchestrator_turn_lifetime_seconds` says what it binds.
- Live turns are enumerable. Shutdown and health can see them.

### Negative Consequences

- Two lifetime settings with an ordering constraint. The validator makes a misorder fail at
  startup, not at 1800 seconds.
- A body cancelled at the service bound loses the executor's partial-reply salvage, which runs in
  an `except Exception` that `CancelledError` bypasses. The client receives the degraded terminal
  event, not a salvaged answer. Accepted: the service bound fires only after the orchestrator's own
  cap — which does salvage — failed to end the turn.
- A degraded close leaves replay without a DONE row for that turn. Logged; the next turn's DONE
  closes the series.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| `asyncio.timeout` cancellation lands inside a Postgres transaction and leaves it open | Medium | SQLAlchemy's async session rolls back on `__aexit__` with any exception, `CancelledError` included. AC-1 seeds the block inside `AsyncSessionLocal` to prove it |
| The registry holds a strong reference and a task leaks on a code path that bypasses it | Medium | D1 makes the registry the only creation site for the turn task. A test asserts `app.py` has no bare `create_task` of `_process_chat_stream_background` |
| D3's degraded close fires on a transient Postgres stall and a healthy turn loses its DONE row | Low | 10 seconds is an order of magnitude above `SessionEventBuffer.append`'s measured cost. AC-6 counts degraded closes in production; a non-zero rate on healthy substrate is a signal to tune, not a silent loss |
| The pre-step check (D4) stops a turn between a tool round and the synthesis that would have used its results | Low | `_stop_turn_for_lifetime_cap` already routes through synthesis with `tool_results` salvage (FRE-973 shape) |

---

## Implementation Notes

**Files affected:**
- `src/personal_agent/service/request_tasks.py` (new) — the registry, D1.
- `src/personal_agent/service/app.py` — `chat_stream` creates through the registry; the task body
  wrapped per D2; `finally` calls the bounded close per D3.
- `src/personal_agent/transport/agui/transport.py` — `emit_done` gains the close timeout and the
  degraded path, D3.
- `src/personal_agent/config/settings.py` — two new settings with the ordering validator (D2, D3);
  the corrected description (D4).
- `src/personal_agent/orchestrator/executor.py` — the pre-step lifetime check in the driver loop
  (D4).

**Testing strategy:** every criterion below is a seeded fault. A seeded fault that the bound does
not catch is the test failing. No criterion is satisfied by the absence of a fault.

**Dependencies:** none on other in-flight work. The two adjacent defects (projector re-open;
span extraction on the critical path) are independent tickets and land in any order.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

Adjudicated on FRE-1403 once the implementation chain has landed and deployed.

- **AC-1 — A request task that blocks after the executor returns still closes the stream.**
  · **Check:** integration test seeds an indefinite `await` inside the request task after
  `execute_task` returns and before `emit_done`, with `service_request_task_lifetime_seconds`
  set to 2. Assert: a DONE row exists for the session within 2 + close-timeout seconds, the close
  sentinel is dequeued by a sender, and `chat_stream.task_ended` is logged with
  `outcome=cancelled` and the trace id. · *Fails if* the sentinel is not dequeued, or the task is
  still in the registry after the window.
- **AC-2 — A close path whose persistence is unavailable still closes the stream.**
  · **Check:** test seeds `SessionEventBuffer.append` to block indefinitely, runs a normal short
  turn, and asserts the close sentinel is dequeued within `service_request_close_timeout_seconds`
  + 1 and `transport.done_degraded` is logged with `reason=timeout`. · *Fails if* the sentinel
  waits on the append, or the log line is absent.
- **AC-3 — A tool call that never returns cannot outlive the service bound.**
  · **Check:** test registers a tool whose executor awaits forever, sends a turn that calls it,
  with `orchestrator_turn_lifetime_seconds=1` and `service_request_task_lifetime_seconds=3`.
  Assert the request task ends within 4 seconds and the client receives a terminal event.
  · *Fails if* the task is alive at 5 seconds — the exact gap FRE-1403 named.
- **AC-4 — The driver loop stops at the cap between steps.**
  · **Check:** unit test advances a fake monotonic clock past `orchestrator_turn_lifetime_seconds`
  after a `TOOL_EXECUTION` step returns, and asserts the next transition is to synthesis via
  `_stop_turn_for_lifetime_cap`, not to `LLM_CALL`. · *Fails if* another LLM call is made after
  the cap.
- **AC-5 — The registry is exact.**
  · **Check:** unit test starts three turn tasks, completes one, cancels one; asserts the
  registry holds exactly one, and that the done callback fired for the other two with the right
  `outcome`. A second assertion scans `app.py` for `create_task(_process_chat_stream_background`
  and requires zero matches outside the registry. · *Fails if* a finished task remains, a live
  task is absent, or a bypass creation site exists.
- **AC-6 — In production, every launched turn ends, and ends on time.**
  · **Check:** over the first 7 days after deploy, join `chat_stream.launched` (ES, `trace_id`)
  to `session_events` DONE rows (Postgres, `payload->>'trace_id'`). Require: zero launched turns
  without a DONE row older than `service_request_task_lifetime_seconds` +
  `service_request_close_timeout_seconds`; and `chat_stream.task_ended` present for every
  launched trace. Report the count of `transport.done_degraded` separately. · *Fails if* any
  launched trace has no ending, or any ending is later than the bound allows.
- **AC-7 — The setting says what it binds.**
  · **Check:** unit test parses the `orchestrator_turn_lifetime_seconds` field description and
  asserts it names the three bind points of D4 and does not contain the phrase "cap on a turn"
  unqualified. · *Fails if* the description still claims the whole turn.

**Where these are adjudicated.** On FRE-1403, after the implementation chain deploys. AC-6 is
population-level and needs the 7-day window; the umbrella stays open until it is read.

---

## References

- ADR-0142 — Capability Is Not a Property of Register (D4a: the orchestrator lifetime cap this
  ADR corrects and layers under)
- ADR-0075 — WebSocket Transport + Durable Channel (replay from `seq`; the reason D3 stays
  inside the emit lock)
- ADR-0076 — Adaptive Constraint Governance Protocol (`turn_status` STATE_DELTA; the surface the
  adjacent projector bug corrupts)
- ADR-0088 — Execution Topology Observability Contract (`observe_topology`, the two tail awaits
  inside the executor)
- ADR-0138 — The Model May Generate, But It May Not Assert (span extraction; the adjacent
  critical-path ticket)
- FRE-1403 — this ADR's umbrella ticket
- FRE-1392 — the predecessor that shipped the orchestrator cap
- FRE-973 — the work-budget deadline and the `tool_results` salvage shape D4 reuses
- FRE-518 — per-session emit serialisation; why Option 2 was rejected
- FRE-1398 — the four earlier settings that claimed more than they bound
- `src/personal_agent/service/app.py:212` — `_process_chat_stream_background`;
  `app.py:2562` — the untracked `create_task`
- `src/personal_agent/transport/agui/transport.py:283` — `emit_done`
- `src/personal_agent/observability/topology/seam.py:193` — `observe_topology`
- `src/personal_agent/observability/topology/projector.py:400` — the completed-then-pop path

---

## Status Updates

### 2026-09-05 - Proposed
**Changed By:** adr seat
**Reason:** Written after the FRE-1403 incident was measured and found not to be a hang. The
design gap the ticket named is real one layer up; the incident's two actual defects are filed as
separate tickets and are not decisions of this ADR.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
