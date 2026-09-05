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
substrate, not inferred from the log tail. These are observations of one process on one day; the
code references beside them are what makes each observation possible, not proof of it.

| Ticket claim | Measured | Instrument |
|---|---|---|
| "No DONE row was ever persisted" | `session_events` seq 23 is `DONE`, trace `81f33956…`, at 14:47:05.220 | Postgres |
| "The `finally` never ran" | `request.completed` — the last statement of the `try`'s Redis branch — landed in Redis at 14:47:05.214. DONE followed 6 ms later | Redis stream ids |
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
  observation `completed`, emits it, and pops the trace (`projector.py:434`). Reflection (detached
  by the executor) and consolidation (run by the `request.captured` event consumer) then publish
  `turn.model_call_completed` under the turn's trace id, because the cost boundary attributes
  their spend to the turn (`cost_tracker.py:260`). Each one recreates a blank observation —
  `tool_iteration 0`, `context_tokens 0`, `context_max None` (`projector.py:200`) — and emits it as
  `turn_status`. Seq 25–33 of the session are ten such rows, 4 to 22 seconds after DONE. Over the
  preceding 24 hours, 7 of 23 turns did this, average 11 rows, longest tail 30 seconds. The owner
  sees `None` as "–". This is a bug, filed separately. It is not a decision this ADR makes.
- **The reply waits for span extraction.** Synthesis awaits the span-extraction model call
  (`executor.py:6950`). The deployed mode is `observe`, in which the call records a compliance
  observation and strips markers; it cannot change the reply. In `enforce` mode it can re-route or
  replace the reply (`executor.py:6953`), so there the wait is by design. Whether the observe-mode
  wait belongs on the critical path is filed separately.

### The gap that is real

What the measurement did confirm is the structural claim in FRE-1403's title, moved one layer up.

The chat turn is an `asyncio.Task` created at `app.py:2561` with no reference kept and no done
callback. Nothing can enumerate it, bound it, or observe its cancellation. After the executor
returns, its body awaits the reply push (a Postgres append under the per-session emit lock), then
either a Redis publish or a direct Postgres append, and — in the `finally` — `emit_done`, which
takes the same lock and does another append. Inside the executor, `observe_topology`'s exit awaits
a cost fetch, a route-trace write, up to one segment write per sub-agent, and a Redis publish.

No call site in that list carries an `asyncio.timeout`. The service SQLAlchemy engine sets no
command timeout (`database.py:12`). The route-trace pool sets `command_timeout=10` on statements
(`ledger.py:123`) but nothing on pool acquisition. The Redis publish has no local bound. None of
them logs on entry. A block in any one would produce exactly the signature the ticket described:
answer delivered, trace silent, socket open — because the sender closes only when it dequeues the
sentinel that `emit_done` pushes last (`ws_endpoint.py:832`).

`orchestrator_turn_lifetime_seconds` describes itself as an "absolute wall-clock cap on a turn". It
binds three call sites inside `execute_task`: the in-flight primary call (`executor.py:6077`), a
constraint pause (`executor.py:774`), and the tool-iteration-limit gate (`executor.py:6481`). The
service-tail awaits are outside `execute_task`, so the setting cannot bind them even in principle.
This is the fifth setting this week found to claim more than it binds (FRE-1398 lists the first
four).

### What needs to be decided

Where the ceiling on a request lives, what it covers, and what happens at the ceiling when the
cleanup path shares the resource that failed — or is itself the thing that is stuck.

---

## Decision

**The request task, not the turn, is the unit that must end.** The service layer owns it, tracks
it, bounds it in two stages, and closes the client stream at the second stage even when the task
itself never returns. The orchestrator's cap stays what it is and says so.

**D1 — Every chat request task is created through a service-level registry, keyed by trace.** A
module in `service/` owns a `trace_id → RequestTask` map, where the record holds the task, the
session id, and `started_at` (monotonic, stamped at registry insertion — the canonical start of
every clock in this ADR). `chat_stream` creates the turn task through the registry and nowhere
else. Two turns on one session are two records. The registry keeps a strong reference for the
task's lifetime, removes the record on completion, and attaches one done callback that logs
`chat_stream.task_ended` with `trace_id`, `session_id`, elapsed seconds, and
`outcome ∈ {completed, timed_out, cancelled, failed}`. `timed_out` is the service bound (D2);
`cancelled` is any other cancellation, including shutdown; `failed` is an exception. The callback
handles `CancelledError` explicitly — `task.result()` inside `except Exception` does not.

The registry is process-local, like `_active_connections` and `_session_emit_locks`. Seshat runs
one worker; this ADR does not make it service-wide. On lifespan shutdown the registry cancels
every live request task and awaits them under `service_request_close_timeout_seconds`; a task
still alive after that is logged `chat_stream.task_leaked` and abandoned to process exit.

**D2 — Stage one: the request task carries a cooperative wall-clock bound.** A new setting,
`service_request_task_lifetime_seconds`, wraps the whole body of `_process_chat_stream_background`
in `asyncio.timeout`. A validator requires it to exceed `orchestrator_turn_lifetime_seconds`; the
proposed default is that value plus 300 seconds, so the orchestrator's cap fires first on a
long-but-live turn and this bound fires only when something below it did not return. At expiry
the body is cancelled and the outcome is `timed_out`.

This stage is cooperative and this ADR says so. Cancellation runs every `finally` on the way out,
and a `finally` that awaits — `observe_topology`'s exit does, `emit_done` does — can itself block.
Stage one is expected to end the task in every case where a coroutine is merely slow. It is not
trusted to end the task in every case, which is what stage two is for.

**D3 — Stage two: the close is a deadline the task does not have to reach.** Three parts.

- *(a) `emit_done` is bounded, lock included.* Lock acquisition and the DONE append run under one
  `asyncio.timeout(service_request_close_timeout_seconds)`, proposed default 10 seconds. On
  exception or timeout the degraded path runs.
- *(b) The degraded path closes outside the lock, and says why.* It pushes the close sentinel to
  the session queue without holding the emit lock and logs `transport.done_degraded` at warning
  with `reason ∈ {lock_timeout, persist_timeout, persist_error, queue_timeout}`. The policy is
  explicit: **in the degraded path, closure beats ordering.** The sentinel carries no `seq`, so the
  persisted series is untouched; a sequenced event still queued behind it reaches the client on the
  next replay, not live. The sentinel push is `await queue.put(...)` under the same close timeout,
  never `put_nowait` — a full queue drops the sentinel today (`transport.py:312`). If even that
  times out, the degraded path closes the socket directly through `get_active_connection` and logs
  `reason=queue_timeout`. A live socket always closes.
- *(c) The registry arms the deadline independently of the task.* At insertion, the registry
  schedules a close for `started_at + service_request_task_lifetime_seconds +
  service_request_close_timeout_seconds`. If the record is still present when it fires, the
  registry runs the degraded path of (b) for that trace itself, logs `chat_stream.task_leaked`,
  and leaves the task to stage one's cancellation or to process exit. The task's own `emit_done`
  cancels the scheduled close when it runs. This is the one benefit of a watchdog, taken
  narrowly: not a scanner, and only in the degraded path where ordering is already given up.

**D4 — The orchestrator's cap binds where it says it binds, and one more place.** The description
of `orchestrator_turn_lifetime_seconds` is corrected to name its bind points: the in-flight primary
call, a constraint pause, the tool-iteration-limit gate, and — new — a pre-step check in the
state-driver loop. The loop checks the lifetime once at its common dispatch point, before every
`step_func` await, and routes to `_stop_turn_for_lifetime_cap` when the lifetime is exhausted. The
orchestrator cap does **not** cancel a tool already in flight: a tool's side effects are not the
orchestrator's to unwind. The per-step check bounds the *next* step. The service bound (D2)
bounds the whole task, and it will cancel a tool in flight, because tool calls are gathered
directly (`executor.py:6677`) and parent cancellation propagates. That is accepted: D2 fires only
after the orchestrator's own cap failed to end the turn, and a tool with external side effects that
cannot survive cancellation needs idempotency or a shielded commit of its own. Classifying tools
that way is outside this ADR and is recorded as a consequence.

**D5 — Post-turn work stays detached, and the request task never awaits it.** Reflection runs in
`run_in_background`'s tracked set; consolidation runs in the `request.captured` event consumer.
Both stay where they are. The invariant this ADR states is the other direction: **the request
task's lifetime ends at its terminal close — a DONE row or a degraded close — after which only the
synchronous dedup release and contextvar clearing remain.** Nothing after the executor returns
may await detached work. Post-turn work continues to publish cost under the turn's trace id,
because that is where the spend belongs. What the projector does with a post-completion event is
the bug ticket's decision, not this ADR's.

**Why this and not the alternatives.** The block, when it comes, will come from a call site nobody
listed. Bounding call sites one by one is a list that is wrong the day after it is written. A
bound at the outermost boundary of the unit of work covers what exists and what is added later.
The close is a deadline rather than a wait because the cleanup path shares Postgres and the emit
lock with the most likely cause, and a bound that depends on the failed resource is not a bound.

---

## Alternatives Considered

### Option 1: Bound each I/O call site in the request tail

**Description:** Wrap the reply push, the assistant append, the Redis publishes, the topology
exit's writes and `emit_done`'s append each in their own `asyncio.timeout`.

**Pros:**
- Precise. Each timeout is sized to its call.
- No cancellation of the orchestrator body.

**Cons:**
- Seven sites today, once the topology exit is counted honestly. The list grows with every
  feature that adds a tail await.
- Misses the case this ADR exists for: the call nobody listed.
- FRE-1403 itself flagged this shape as undesirable for the step gate.

**Why Rejected:** It solves the known sites and leaves the class open.

### Option 2: A scanning watchdog as the primary mechanism

**Description:** A separate task periodically scans live turns and, for any past the bound,
pushes the close sentinel directly, leaving the task alone. No cancellation.

**Pros:**
- Survives a cleanup path that is itself stuck.
- No cancellation semantics to reason about.

**Cons:**
- As the *primary* mechanism it is a second writer to every session stream in the normal path.
  FRE-518 serialises emits under the per-session lock so `seq` order equals enqueue order; a
  writer outside that lock reintroduces the race ADR-0075's replay guard cannot recover from.
- The stuck task keeps its Postgres connection and its memory. Nothing ends.
- A scan interval is a second timing parameter with no principled value.

**Why Rejected as primary; adopted narrowly:** D3(c) takes the one property a watchdog has that a
cooperative timeout lacks — a close that does not depend on the task — and confines it to the
degraded path, armed per task at a known deadline, where ordering is already sacrificed by
policy. Codex round 1 established that a single cooperative timeout cannot honestly claim "cannot
outlive the bound" while `finally` blocks await; the two-stage shape is the answer.

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
- The untracked task and the unbounded tail awaits are measured facts, not the incident's
  inference. A cancelled turn task is silent today. The setting's description is wrong today.
- The next real hang will present with the same signature, and the same wrong diagnosis will be
  reached from the same log tail, because the request task leaves no trace of its own ending.

**Why Rejected:** The design gap is real. Its priority is not Urgent, and this ADR says so.

---

## Consequences

### Positive Consequences

- A client stream always closes by `started_at + lifetime + close`, whether or not the task
  cooperates. The claim is checkable and AC-1 through AC-3 check it from three directions.
- A cancelled, timed-out or failed request task logs its own ending with `trace_id`. The next
  investigation starts from the task's record, not from the last event some other task emitted.
- Two turns on one session are two records; the registry is exact.
- `orchestrator_turn_lifetime_seconds` says what it binds, and binds one more place.
- Live turns are enumerable per process. Shutdown has a contract with a deadline.

### Negative Consequences

- Three timing settings — orchestrator lifetime, service lifetime, close timeout — with an
  ordering constraint. The validator makes a misorder fail at startup, not at 1800 seconds.
- A body cancelled at the service bound loses the executor's partial-reply salvage, which runs in
  an `except Exception` that `CancelledError` bypasses. The client receives the degraded terminal
  event, not a salvaged answer. Accepted: the service bound fires only after the orchestrator's own
  cap — which does salvage — failed to end the turn.
- A body cancelled inside `observe_topology`'s exit can leave the route-trace row or
  `turn.completed` unwritten for that turn. The `chat_stream.task_ended` record with
  `outcome=timed_out` is the durable marker that this happened; the projector's
  `_MAX_TRACKED_TRACES` eviction eventually drops the orphaned observation.
- A tool with external side effects can be cancelled mid-execution by D2. Tools that commit
  externally need idempotency or a shielded commit. That classification is future work and is not
  blocked by this ADR.
- A degraded close leaves replay without a DONE row for that turn. The client's next turn starts
  a new series; a sequenced event that was still queued arrives on replay rather than live. The
  `transport.done_degraded` line names the trace, so the gap is attributable.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Stage-one cancellation lands inside a Postgres transaction and the rollback in `AsyncSession.__aexit__` itself blocks | Medium | That is exactly the case stage two exists for. AC-1 seeds the block in the topology exit's write so the rollback seam is the one under test |
| The registry holds a strong reference and a task leaks on a code path that bypasses it | Medium | D1 makes the registry the only creation site. AC-5 records every task creation during a request and requires that the turn task came through the registry |
| D3's degraded close fires on a transient Postgres stall and a healthy turn loses its DONE row | Low | 10 seconds is an order of magnitude above `SessionEventBuffer.append`'s measured cost. AC-6 counts degraded closes in production; a non-zero rate on healthy substrate is a signal to tune, not a silent loss |
| The degraded sentinel closes the sender ahead of a sequenced event still in the queue | Low | Stated policy: closure beats ordering in the degraded path. The event is persisted and replays |
| The pre-step check (D4) stops a turn between a tool round and the synthesis that would have used its results | Low | `_stop_turn_for_lifetime_cap` already routes through synthesis with `tool_results` salvage (FRE-973 shape) |
| Shutdown waits on a stuck task | Low | D1's shutdown contract bounds the wait by the close timeout and logs the leak |

---

## Implementation Notes

**Files affected:**
- `src/personal_agent/service/request_tasks.py` (new) — the registry, the done callback, the
  armed close deadline, the shutdown contract (D1, D3c).
- `src/personal_agent/service/app.py` — `chat_stream` creates through the registry; the task body
  wrapped per D2; the lifespan shutdown calls the registry's shutdown.
- `src/personal_agent/transport/agui/transport.py` — `emit_done` gains the close timeout over lock
  and append, and the degraded path with its four reasons (D3a, D3b).
- `src/personal_agent/transport/agui/ws_endpoint.py` — the direct socket close used by
  `reason=queue_timeout`.
- `src/personal_agent/config/settings.py` — two new settings with the ordering validator (D2, D3);
  the corrected description (D4).
- `src/personal_agent/orchestrator/executor.py` — the pre-step lifetime check at the driver
  loop's dispatch point (D4).

**Testing strategy:** every criterion below is a seeded fault. A seeded fault that the bound does
not catch is the test failing. No criterion is satisfied by the absence of a fault, and none is
satisfied by prose.

**Dependencies:** none on other in-flight work. The two adjacent defects (projector re-open;
span extraction on the observe-mode critical path) are independent tickets and land in any order.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

Adjudicated on FRE-1403 once the implementation chain has landed and deployed. Every test below
sets all three timing settings explicitly — `orchestrator_turn_lifetime_seconds`,
`service_request_task_lifetime_seconds`, `service_request_close_timeout_seconds` — so the
validator's ordering holds and the windows are small.

- **AC-1 — A request task that blocks in a `finally` still closes the stream, on the deadline.**
  · **Check:** integration test with settings 1 / 2 / 1. Seed `_write_durable_row` (the topology
  exit, inside the executor's `finally`) to await forever. Run a short turn. Assert: the close
  sentinel is dequeued by a sender no later than 4 s after `started_at`; `transport.done_degraded`
  is logged for the trace; `chat_stream.task_ended` is logged with `outcome=timed_out` and the
  trace id, or `chat_stream.task_leaked` is logged for the trace; the registry holds no record
  for the trace at 5 s. · *Fails if* the sentinel is not dequeued by 4 s, or the record remains
  at 5 s, or neither ending line is logged.
- **AC-2 — A close path whose lock or persistence is unavailable still closes, and the task
  ends.** · **Check:** two variants, settings 30 / 60 / 1. (a) Acquire the session's emit lock in
  the test and never release it, then run a normal turn. (b) Seed `SessionEventBuffer.append` to
  await forever only when `event_type == "DONE"`, then run a normal turn. In both: assert the
  sentinel is dequeued within 2 s of the executor returning; `transport.done_degraded` is logged
  with `reason=lock_timeout` (a) or `reason=persist_timeout` (b); `chat_stream.task_ended` is
  logged with `outcome=completed`; the registry holds no record. · *Fails if* the close waits on
  the lock or the append, the reason is wrong, or the task does not end.
- **AC-3 — A cancellation-resistant tool cannot outlive the deadline.** · **Check:** settings
  1 / 3 / 1. Register a tool whose executor does `await asyncio.shield(asyncio.Event().wait())`,
  send a turn that calls it. Assert the client receives a terminal event (DONE frame or degraded
  sentinel) by 5 s after `started_at` and the registry holds no record by 6 s. · *Fails if* the
  terminal event is absent at 5 s or the record is present at 6 s — the exact gap FRE-1403 named,
  with a tool that ignores cancellation.
- **AC-4 — The driver loop stops at the cap before any step, and the turn ends with a reply.**
  · **Check:** unit test parametrized over every non-terminal `TaskState`. Advance a fake
  monotonic clock past `orchestrator_turn_lifetime_seconds` before the loop dispatches that state.
  Assert the turn reaches `COMPLETED` with `turn_stopped_early=True` and a non-empty
  `final_reply`, and the mocked LLM client records zero calls after the clock advanced. · *Fails
  if* any state dispatches its step after the cap, or any LLM call is made after it, or the turn
  ends without a reply.
- **AC-5 — The registry is exact under concurrency, and it is the only door.** · **Check:**
  patch the running loop's `create_task` to record every task created during the test. POST two
  chat messages to one session concurrently. Assert the registry holds two records with distinct
  trace ids and the same session id. Complete one turn; cancel the other. Assert the registry is
  empty and `chat_stream.task_ended` was logged twice with `outcome=completed` and
  `outcome=cancelled`. Assert that every recorded task whose coroutine is
  `_process_chat_stream_background` was created by the registry. · *Fails if* the second turn
  overwrote the first, a finished task remains, an outcome is wrong, or a turn task was created
  outside the registry.
- **AC-6 — In production, every launched turn ends, ends on time, and ends exactly one way.**
  · **Check:** daily, for the 7 days after deploy, over the trailing 24 hours (inside the
  session-event retention window). Join `chat_stream.launched` (ES) to `chat_stream.task_ended`
  (ES) on `trace_id`. Require: every launched trace has a `task_ended`; every `task_ended` has
  elapsed ≤ `service_request_task_lifetime_seconds + service_request_close_timeout_seconds`; and
  every launched trace has exactly one of {a DONE row in `session_events` with that trace id, a
  `transport.done_degraded` line with that trace id}. Report the degraded count and reasons.
  · *Fails if* any launched trace lacks an ending, any elapsed exceeds the bound, or any trace has
  zero or two terminal markers.
- **AC-7 — Shutdown ends a live turn on the close deadline.** · **Check:** settings 30 / 60 / 1.
  Start a turn whose tool awaits forever, then trigger lifespan shutdown. Assert shutdown returns
  within 2 s, the client received a terminal event, and `chat_stream.task_ended` with
  `outcome=cancelled` or `chat_stream.task_leaked` was logged for the trace. · *Fails if* shutdown
  blocks past 2 s or the socket is left open.

The D4 description correction is an obligation of its implementation ticket, checked there. It is
not a criterion here: correct prose proves nothing about enforcement, and AC-4 proves the
enforcement.

**Where these are adjudicated.** On FRE-1403, after the implementation chain deploys. AC-6 is
population-level and needs the 7-day window; the umbrella stays open until it is read.

---

## References

- ADR-0142 — Capability Is Not a Property of Register (D4a: the orchestrator lifetime cap this
  ADR corrects and layers under)
- ADR-0075 — WebSocket Transport + Durable Channel (replay from `seq`; the reason D3's degraded
  path is a stated exception to ordering, not a silent one)
- ADR-0076 — Adaptive Constraint Governance Protocol (`turn_status` STATE_DELTA; the surface the
  adjacent projector bug corrupts)
- ADR-0088 — Execution Topology Observability Contract (`observe_topology`, the tail awaits
  inside the executor's `finally`)
- ADR-0138 — The Model May Generate, But It May Not Assert (span extraction; the adjacent
  critical-path ticket)
- FRE-1403 — this ADR's umbrella ticket
- FRE-1392 — the predecessor that shipped the orchestrator cap
- FRE-973 — the work-budget deadline and the `tool_results` salvage shape D4 reuses
- FRE-518 — per-session emit serialisation; why Option 2 was rejected as primary
- FRE-1398 — the four earlier settings that claimed more than they bound
- `src/personal_agent/service/app.py:212` — `_process_chat_stream_background`;
  `app.py:2561` — the untracked `create_task`
- `src/personal_agent/transport/agui/transport.py:283` — `emit_done`; `transport.py:312` — the
  `put_nowait` that drops the sentinel on a full queue
- `src/personal_agent/observability/topology/seam.py:193` — `observe_topology`
- `src/personal_agent/observability/topology/projector.py:400` — the completed-then-pop path
- `src/personal_agent/llm_client/cost_tracker.py:260` — the cost boundary's publish, under the
  caller's trace id

---

## Status Updates

### 2026-09-05 - Proposed
**Changed By:** adr seat
**Reason:** Written after the FRE-1403 incident was measured and found not to be a hang. The
design gap the ticket named is real one layer up; the incident's two actual defects are filed as
separate tickets and are not decisions of this ADR. Codex round 1 turned a single cooperative
bound into the two-stage design and rewrote every criterion so a half-finished implementation
cannot pass it.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
