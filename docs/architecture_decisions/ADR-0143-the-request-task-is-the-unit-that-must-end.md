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
  observation `completed`, emits it, and pops the trace (`projector.py:400–434`). Reflection
  (detached by the executor) and consolidation (run by the `request.captured` event consumer)
  then publish `turn.model_call_completed` under the turn's trace id, because the cost boundary
  publishes under whatever trace its caller passes (`cost_tracker.py:60`, called at `:260`) and
  post-turn work passes the turn's. Each event recreates a blank observation — `tool_iteration 0`,
  `context_tokens 0`, `context_max None` (`projector.py:200`) — and emits it as `turn_status`.
  Seq 25–33 of the session are ten such rows, 4 to 22 seconds after DONE. Over the preceding 24
  hours, 7 of 23 turns did this, average 11 rows, longest tail 30 seconds. The owner sees `None`
  as "–". This is a bug, filed separately. It is not a decision this ADR makes.
- **The reply waits for span extraction.** Synthesis awaits the span-extraction model call
  (`executor.py:6950`). The live process ran in `observe` mode on this turn (read from the
  `grounding_verification_completed` line; the checked-in default is `off`), in which the call
  records a compliance observation and strips markers and cannot change the reply. In `enforce`
  mode it can re-route or replace the reply (`executor.py:6953`), so there the wait is by design.
  Whether the observe-mode wait belongs on the critical path is filed separately.

### The gap that is real

What the measurement did confirm is the structural claim in FRE-1403's title, moved one layer up.

The chat turn is an `asyncio.Task` created at `app.py:2561` with no reference kept and no done
callback. Nothing can enumerate it, bound it, or observe its cancellation. After the executor
returns, its body awaits the reply push (a Postgres append under the per-session emit lock), then
either a Redis publish or a direct Postgres append, and — in the `finally` — `emit_done`, which
takes the same lock and does another append. Inside the executor, `observe_topology`'s exit awaits
a cost fetch, a route-trace write, up to one segment write per sub-agent, and a Redis publish.

No call site in that list carries an `asyncio.timeout`. The service SQLAlchemy engine sets no
command timeout (`database.py:12`). The route-trace asyncpg pool sets `command_timeout=10` on
statements (`ledger.py:123`) but nothing on pool acquisition. The Redis publish has no local
bound. None of them logs on entry. A block in any one would produce exactly the signature the
ticket described: answer delivered, trace silent, socket open — because the sender closes only
when it dequeues the sentinel that `emit_done` pushes last (`ws_endpoint.py:832`).

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
itself never returns.

One fact of the runtime shapes everything below and this ADR states it rather than designs around
it: **asyncio cannot kill a task that suppresses cancellation.** So the guarantee this ADR makes
is exact and narrower than the title's verb. Stage one ends the task in every case where the body
cooperates with cancellation. Stage two closes the client stream on a deadline whether or not the
task cooperates. A task that outlives stage two is not ended; it is **named, counted, kept in the
registry as leaked, and alarmed.** What this ADR forbids is the state we had: a request whose
ending nobody can observe.

**D1 — Every chat request task is created through a service-level registry, keyed by trace.** A
module in `service/` owns a `trace_id → RequestTask` map. The record holds the task, the session
id, `started_at` (monotonic, stamped at registry insertion — the canonical start of every clock in
this ADR), the absolute deadlines derived from it, a `state ∈ {live, leaked}`, and a close-once
flag (D3). `chat_stream` creates the turn task through the registry and nowhere else, and the
registry enforces that: it sets a contextvar the task body asserts on entry, so a
`_process_chat_stream_background` started any other way raises before it does anything. Two turns
on one session are two records. The registry keeps a strong reference for the task's lifetime and
removes the record when the task ends. A task still alive at its stage-two deadline is marked
`leaked` and **kept** — a leaked record is the only evidence the task exists, and removing it would
make enumeration lie. The registry attaches one done callback that logs `chat_stream.task_ended`
with `trace_id`, `session_id`, elapsed seconds from `started_at`, and
`outcome ∈ {completed, timed_out, cancelled, failed}`. `timed_out` is stage one firing;
`cancelled` is any other cancellation, including shutdown; `failed` is an exception. The callback
handles `CancelledError` explicitly — `task.result()` inside `except Exception` does not. A leaked
task that later ends still fires the callback, so a leak that resolves is visible too.

The registry is process-local, like `_active_connections` and `_session_emit_locks`. Seshat runs
one worker; this ADR does not make it service-wide. On lifespan shutdown the registry cancels
every live task, runs the stage-two close (D3) for each one immediately rather than at its
deadline, awaits the tasks under `service_request_close_timeout_seconds`, marks the survivors
`leaked`, logs `chat_stream.task_leaked` for each, and returns. The registry also logs
`request_tasks_gauge` with `live` and `leaked` counts at every state change; a non-zero `leaked`
in production is an incident, not a statistic.

**D2 — Stage one: the request task carries a cooperative wall-clock bound.** A new setting,
`service_request_task_lifetime_seconds`, bounds the body of `_process_chat_stream_background`
with `asyncio.timeout_at(started_at + lifetime)` — the absolute deadline from the registry record,
so scheduling delay between `create_task` and first execution is charged to the turn, not
forgiven. A validator requires the setting to exceed `orchestrator_turn_lifetime_seconds`; the
proposed default is that value plus 300 seconds, so the orchestrator's cap fires first on a
long-but-live turn and this bound fires only when something below it did not return. The timeout
wraps the existing `try` body; a dedicated `except TimeoutError` sits before the existing
`except Exception`, records `outcome=timed_out` on the registry record, and pushes the same
error delta the existing handler does. The `finally` then runs the bounded close (D3a).

This stage is cooperative and this ADR says so. Cancellation runs every `finally` on the way out,
and a `finally` that awaits — `observe_topology`'s exit does, `emit_done` does — can itself block.
Stage one is expected to end the task in every case where a coroutine is merely slow. It is not
trusted to end the task in every case, which is what stage two is for.

**D3 — Stage two: the close is a deadline the task does not have to reach.** Four parts, and one
arbiter.

- *The arbiter.* Every close for a trace goes through `registry.close_once(trace_id, how)`. It
  returns `True` exactly once per trace and `False` after. The three actors — the task's normal
  `emit_done`, its degraded handler, and the registry's deadline — all call it, and only the caller
  that gets `True` pushes a sentinel or closes a socket. This is what makes duplicate sentinels
  and double closes impossible by construction rather than by timing. `emit_done` calls it after
  the DONE append returns and before it pushes the sentinel; if it gets `False` there, the
  registry's deadline already closed the stream, and `emit_done` logs `transport.done_after_close`
  and pushes nothing.
- *(a) `emit_done` is bounded, lock included.* Lock acquisition and the DONE append run under one
  `asyncio.timeout(service_request_close_timeout_seconds)`, proposed default 10 seconds. The
  code tracks whether the lock was acquired, so a timeout reports `reason=lock_timeout` or
  `reason=persist_timeout` truthfully. A timeout that fires after the append committed but before
  it returned leaves a DONE row **and** a degraded record; that is accepted and AC-6 is written to
  allow it. On exception or timeout the degraded path runs.
- *(b) The degraded path closes outside the lock, and says why.* It has its own budget of one
  `service_request_close_timeout_seconds`, shared by everything it does. It first checks the
  registry for a **newer** live record on the same session; if one exists it does not touch the
  connection, because the transport is session-scoped (one connection, one queue, one sender per
  session — `ws_endpoint.py:91`, `:577`) and closing it would strand the newer turn. It logs
  `transport.done_degraded` with `reason=superseded` and stops. Otherwise it pushes the close
  sentinel — which carries the trace id, as today — with `await queue.put(...)` under the budget,
  never `put_nowait` (a full queue drops the sentinel today, `transport.py:312`), but **only if the
  session has an active connection**: a sentinel queued with no sender would sit in the session
  queue and close the next connection on sight. With no connection it logs
  `reason=no_connection` and stops; the reconnecting client replays a series with no DONE for
  that trace and shows the turn as open until its next message — the known degraded state, named
  so it can be counted. If the put times out it closes the socket directly through
  `get_active_connection` under what remains of the budget and logs `reason=queue_timeout`. A
  raw close does not carry the DONE frame; the client sees a closed socket and reconnects, which
  is the same degraded state. The policy is explicit: **in the degraded path, closure beats
  ordering.** The sentinel carries no `seq`, so the persisted series is untouched; a sequenced
  event still queued behind it reaches the client on the next replay, not live. The reason
  logged is the first failure; a later step's failure is logged as a second
  `transport.done_degraded` line with its own reason, so the sequence is readable.
- *(c) The registry arms the deadline independently of the task.* At insertion, the registry
  schedules a close for `started_at + lifetime + close`. If `close_once` has not been won by then,
  the registry runs the degraded path of (b) for that trace itself, marks the record `leaked`,
  logs `chat_stream.task_leaked`, and leaves the task to stage one's cancellation or to process
  exit. The bound on stream closure is therefore `started_at + lifetime + 2 × close`: one close
  budget for the task's own attempt, one for the registry's. That number is the one AC-1 through
  AC-3 test against. This is the one benefit of a watchdog, taken narrowly: not a scanner, armed
  per task at a known deadline, and only in the degraded path where ordering is already given up.
- *(d) The deadline is independent of the task, not of the event loop.* A loop blocked in
  synchronous code fires no timer. That case is outside this ADR; `py-spy` is its instrument.

**D4 — The orchestrator's cap binds where it says it binds, and one more place.** The description
of `orchestrator_turn_lifetime_seconds` is corrected to name its bind points: the in-flight primary
call, a constraint pause, the tool-iteration-limit gate, and — new — a pre-step check in the
state-driver loop. The loop checks the lifetime once at its common dispatch point, before every
`step_func` await, for every state **except `SYNTHESIS` and the terminal states**. When the
lifetime is exhausted it calls `_stop_turn_for_lifetime_cap` and sets the state to `SYNTHESIS`,
which then runs and reaches `COMPLETED` through the existing salvage path. The exemption is what
stops an exhausted turn looping on the check it just failed. The orchestrator cap does **not**
cancel a tool already in flight: a tool's side effects are not the orchestrator's to unwind. The
per-step check bounds the *next* step. The service bound (D2) bounds the whole task, and it will
cancel a tool in flight, because tool calls are gathered directly (`executor.py:6677`) and parent
cancellation propagates. That is accepted: D2 fires only after the orchestrator's own cap failed
to end the turn. A tool with external side effects that cannot survive cancellation needs
idempotency or a shielded commit of its own; classifying tools that way is outside this ADR and is
recorded as a consequence, with the list of commit-style tools as the first deliverable of that
future work.

**D5 — Post-turn work stays detached, and the request task never awaits it.** Reflection runs in
`run_in_background`'s tracked set; consolidation runs in the `request.captured` event consumer.
Both stay where they are. The invariant this ADR states is the other direction: **nothing after
the executor returns may await detached work, and the request task does nothing after its own
terminal close except the synchronous dedup release and contextvar clearing.** When the registry
closes the stream around a task that is still stuck, that task's lifetime has not ended — it is
`leaked`, per D1, and its dedup entry expires by TTL (FRE-392) rather than by release. Post-turn
work continues to publish cost under the turn's trace id, because that is where the spend
belongs. What the projector does with a post-completion event is the bug ticket's decision, not
this ADR's.

**Out of scope, named so it is not mistaken for forgotten.** Two concurrent turns on one session
already break the session-scoped transport today: the first turn's sentinel exits the only sender
(`ws_endpoint.py:838`). The trace-keyed registry records both turns exactly and D3(b) refuses to
close a connection a newer turn is using, but this ADR does not multiplex the transport per trace.

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
policy, behind a close-once arbiter so it can never race the task's own close. Codex round 1
established that a single cooperative timeout cannot honestly claim "cannot outlive the bound"
while `finally` blocks await; round 2 established that the two actors need the arbiter.

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

- A client stream closes by `started_at + lifetime + 2 × close` whenever the event loop is
  running and a connection exists, whether or not the task cooperates. The claim is checkable and
  AC-1 through AC-3 check it from three directions.
- A request task's ending is always observable: `task_ended` with an outcome, or `task_leaked`
  with a record that stays. The next investigation starts from the task's own record, not from
  the last event some other task emitted.
- Two turns on one session are two records; the registry is exact, and a bypass fails at runtime.
- `orchestrator_turn_lifetime_seconds` says what it binds, and binds one more place.
- Shutdown has a contract with a deadline and closes streams before it waits.

### Negative Consequences

- Three timing settings — orchestrator lifetime, service lifetime, close timeout — with an
  ordering constraint. The validator makes a misorder fail at startup, not at 1800 seconds.
- A body cancelled at the service bound loses the executor's partial-reply salvage, which runs in
  an `except Exception` that `CancelledError` bypasses. The client receives the degraded terminal
  event, not a salvaged answer. Accepted: the service bound fires only after the orchestrator's own
  cap — which does salvage — failed to end the turn.
- A body cancelled inside `observe_topology`'s exit can leave the route-trace row or
  `turn.completed` unwritten for that turn. The `task_ended outcome=timed_out` record is the
  durable marker that this happened; the projector's `_MAX_TRACKED_TRACES` eviction eventually
  drops the orphaned observation.
- A tool with external side effects can be cancelled mid-execution by D2. Tools that commit
  externally need idempotency or a shielded commit. That classification is future work and is not
  blocked by this ADR; its first deliverable is the list of such tools.
- A degraded close with `reason=no_connection` or a raw socket close leaves the client showing an
  open turn until its next message. Both are named reasons, so the rate is measurable, and both
  occur only after two earlier failures.
- A leaked task holds whatever it holds — a pool connection, memory, a tool subprocess — until
  process exit. The gauge makes the count visible; it does not free anything.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Stage-one cancellation lands inside the route-ledger's asyncpg write and the pool's acquire or release blocks | Medium | That is the case stage two exists for. AC-1 seeds a cooperative block there and proves stage one; AC-3 seeds a cancellation-suppressing block and proves stage two |
| Stage-one cancellation lands inside `AsyncSession.__aexit__`'s rollback on the service engine, which has no command timeout | Medium | AC-2(b) seeds the block inside the SQLAlchemy append and proves the close does not wait on it |
| A task bypasses the registry | Medium | D1's entry assertion makes a bypass raise at runtime. AC-5 proves it |
| D3's degraded close fires on a transient Postgres stall and a healthy turn gains a degraded record beside its DONE row | Low | 10 seconds is an order of magnitude above `SessionEventBuffer.append`'s measured cost. AC-6 counts degraded closes and their reasons in production; a non-zero rate on healthy substrate is a signal to tune, not a silent loss |
| The degraded sentinel closes the sender ahead of a sequenced event still in the queue | Low | Stated policy: closure beats ordering in the degraded path. The event is persisted and replays |
| The registry's deadline closes a connection a newer turn is using | Low | D3(b) checks for a newer live record on the session first and yields with `reason=superseded` |
| A sentinel enqueued with no sender poisons the next connection | Low | D3(b) enqueues only when a connection is active; otherwise `reason=no_connection` |
| The pre-step check (D4) stops a turn between a tool round and the synthesis that would have used its results | Low | `_stop_turn_for_lifetime_cap` already routes through synthesis with `tool_results` salvage (FRE-973 shape) |
| Leaked tasks accumulate silently | Low | `request_tasks_gauge.leaked` is logged at every state change; the population criterion reports it, and any non-zero value is an incident |
| The event loop itself is blocked | Out of scope | No timer fires. `py-spy dump` is the instrument, as it was for this incident |

---

## Implementation Notes

**Files affected:**
- `src/personal_agent/service/request_tasks.py` (new) — the registry, the entry contextvar and
  assertion, the done callback, `close_once`, the armed deadline, the gauge, the shutdown contract
  (D1, D3c).
- `src/personal_agent/service/app.py` — `chat_stream` creates through the registry; the body
  bounded with `timeout_at` and the `except TimeoutError` branch (D2); the lifespan shutdown calls
  the registry's shutdown.
- `src/personal_agent/transport/agui/transport.py` — `emit_done` gains the close timeout over
  lock and append with the acquired-lock state, the `close_once` call, and the degraded path with
  its reasons (D3a, D3b).
- `src/personal_agent/transport/agui/ws_endpoint.py` — the bounded direct socket close used by
  `reason=queue_timeout`.
- `src/personal_agent/config/settings.py` — two new settings with the ordering validator (D2, D3);
  the corrected description (D4).
- `src/personal_agent/orchestrator/executor.py` — the pre-step lifetime check at the driver
  loop's dispatch point with the `SYNTHESIS` exemption (D4).

**Testing strategy:** every criterion below is a seeded fault. A seeded fault that the bound does
not catch is the test failing. No criterion is satisfied by the absence of a fault, and none is
satisfied by prose. Two kinds of seeded block are used and the difference is the point:
a *cooperative* block is `await asyncio.Event().wait()`, which raises `CancelledError` when
cancelled; a *suppressing* block is `while True: try: await asyncio.sleep(3600) except
asyncio.CancelledError: continue`, which never ends. Stage one must beat the first. Only stage two
can beat the second.

**Dependencies:** none on other in-flight work. The two adjacent defects (projector re-open;
span extraction on the observe-mode critical path) are independent tickets and land in any order.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

Adjudicated on FRE-1403 once the implementation chain has landed and deployed. Every test below
sets all three timing settings explicitly — `orchestrator_turn_lifetime_seconds` /
`service_request_task_lifetime_seconds` / `service_request_close_timeout_seconds` — so the
validator's ordering holds and the windows are small. "Terminal signal" below means the sender
dequeued a close sentinel for the trace, or the socket was closed by the degraded path; which one
is asserted separately where it matters.

- **AC-1 — Stage one ends a cooperatively-blocked task and closes the stream from inside the
  task.** · **Check:** integration test, settings 1 / 2 / 1. Seed `_write_durable_row` (the
  topology exit, inside the executor's `finally`) with a *cooperative* block. Run a short turn.
  Assert: `chat_stream.task_ended` is logged with `outcome=timed_out` and the trace id; the close
  sentinel was dequeued by the sender and the registry's `close_once` was won by the task's own
  `emit_done` (not the deadline); the registry holds no record for the trace by 4 s after
  `started_at`; no `task_leaked` line exists. · *Fails if* the outcome is anything but
  `timed_out`, the close was won by the deadline, the record remains, or a leak is logged — a
  registry deadline alone cannot pass this.
- **AC-2 — A close path whose lock or persistence is unavailable still closes within one close
  budget, and the task ends.** · **Check:** settings 30 / 60 / 1. (a) Patch the point between
  the reply push and `emit_done` to acquire the session's emit lock and never release it, so
  earlier phase emits are unaffected; run a normal turn. (b) Seed `SessionEventBuffer.append`
  with a cooperative block only when `event_type == "DONE"`; run a normal turn. In both: assert
  the sentinel is dequeued within 2 s of the executor returning; `transport.done_degraded` is
  logged with `reason=lock_timeout` (a) or `reason=persist_timeout` (b); `chat_stream.task_ended`
  is logged with `outcome=completed`; the registry holds no record. · *Fails if* the close waits
  on the lock or the append, the reason is wrong, or the task does not end — an implementation
  that bounds only the append passes (b) and fails (a).
- **AC-3 — Stage two closes the stream around a task that suppresses cancellation, and keeps the
  leak.** · **Check:** settings 1 / 3 / 1. Register a tool whose executor is a *suppressing*
  block; send a turn that calls it. Assert: a terminal signal reaches the client by 5 s after
  `started_at` (`lifetime + 2 × close`); `chat_stream.task_leaked` is logged for the trace;
  the registry record exists with `state=leaked` at 6 s and the task is still alive;
  `request_tasks_gauge.leaked` is 1. Then release the block and assert `task_ended` fires and
  the record is removed. · *Fails if* no terminal signal by 5 s, the record is missing at 6 s,
  the gauge is 0, or the late `task_ended` does not fire — the exact gap FRE-1403 named, with a
  tool that ignores cancellation, and the leak accounted for on both ends.
- **AC-4 — The driver loop stops at the cap before any step, and the turn ends with a reply.**
  · **Check:** unit test parametrized over every non-terminal `TaskState` except `SYNTHESIS`.
  Advance a fake monotonic clock past `orchestrator_turn_lifetime_seconds` before the loop
  dispatches that state. Assert the turn reaches `COMPLETED` with `turn_stopped_early=True` and a
  non-empty `final_reply`, the loop iterated at most once more after the clock advanced, and the
  mocked LLM client records zero calls after it. A separate case starts in `SYNTHESIS` with the
  cap exhausted and asserts synthesis runs and the turn completes. · *Fails if* any state
  dispatches its step after the cap, any LLM call is made after it, the turn ends without a
  reply, or the `SYNTHESIS` case loops.
- **AC-5 — The registry is exact under concurrency, and it is the only door.** · **Check:**
  POST two chat messages to one session concurrently. Assert the registry holds two records with
  distinct trace ids and the same session id. Complete one turn; cancel the other. Assert the
  registry is empty and `chat_stream.task_ended` was logged twice with `outcome=completed` and
  `outcome=cancelled`. Then create `_process_chat_stream_background(...)` with a bare
  `asyncio.create_task` and await it: assert it raises the registry's entry error before any
  side effect, and the registry is still empty. · *Fails if* the second turn overwrote the first,
  a finished task remains, an outcome is wrong, or the bypass runs — a wrapper coroutine cannot
  evade an assertion made inside the body.
- **AC-6 — In production, every launched turn has an observable ending, on time, and no ending
  is silent.** · **Check:** daily, for the 7 days after deploy, over the trailing 24 hours (inside
  the session-event retention window). Join `chat_stream.launched` (ES) to
  `chat_stream.task_ended` and `chat_stream.task_leaked` (ES) on `trace_id`. Require: every
  launched trace has a `task_ended` or a `task_leaked`; every `task_ended` has elapsed ≤
  `lifetime + 2 × close`; every launched trace has at least one of {a DONE row in
  `session_events` with that trace id, a `transport.done_degraded` line with that trace id}; and
  `request_tasks_gauge.leaked` is 0 at the end of the window. Report the degraded count by
  reason. · *Fails if* any launched trace lacks an ending, any elapsed exceeds the bound, any
  trace has neither terminal marker, or a leak is present. This criterion reads telemetry, and
  says so: the seeded tests above are the behavioural proof; this one proves the telemetry the
  next investigation will depend on is complete.
- **AC-7 — Shutdown closes live streams first, then ends what it can, within one close budget.**
  · **Check:** settings 30 / 60 / 1. Start two turns: one whose tool is a cooperative block, one
  whose tool is a suppressing block. Trigger lifespan shutdown. Assert shutdown returns within
  2 s; both clients received a terminal signal; `task_ended outcome=cancelled` was logged for
  the first; `task_leaked` was logged for the second and its record has `state=leaked`. · *Fails
  if* shutdown blocks past 2 s, either socket is left open, the cooperative task is reported
  leaked, or the suppressing task is reported ended.

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
- FRE-392 — the dedup entry and its TTL, which is how a leaked task's entry expires
- FRE-1398 — the four earlier settings that claimed more than they bound
- `src/personal_agent/service/app.py:212` — `_process_chat_stream_background`;
  `app.py:2561` — the untracked `create_task`
- `src/personal_agent/transport/agui/transport.py:283` — `emit_done`; `transport.py:312` — the
  `put_nowait` that drops the sentinel on a full queue
- `src/personal_agent/transport/agui/ws_endpoint.py:91`, `:577`, `:838` — one connection, one
  queue, one sender per session; the sender exits on the first sentinel
- `src/personal_agent/observability/topology/seam.py:193` — `observe_topology`
- `src/personal_agent/observability/topology/projector.py:400–434` — the completed-then-pop path
- `src/personal_agent/llm_client/cost_tracker.py:60` — the cost boundary's publish, under the
  caller's trace id; `:260` — the call

---

## Status Updates

### 2026-09-05 - Proposed
**Changed By:** adr seat
**Reason:** Written after the FRE-1403 incident was measured and found not to be a hang. The
design gap the ticket named is real one layer up; the incident's two actual defects are filed as
separate tickets and are not decisions of this ADR. Codex round 1 turned a single cooperative
bound into the two-stage design and rewrote every criterion so a half-finished implementation
cannot pass it. Round 2 found the contradiction between "must end" and a task that suppresses
cancellation, and the races between three close actors; the Decision now states the runtime
fact, keeps leaked records, and routes every close through one arbiter.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
