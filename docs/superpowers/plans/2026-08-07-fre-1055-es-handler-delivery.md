# FRE-1055 — Elasticsearch log handler: make delivery survive threads and shutdown

**Ticket:** [FRE-1055](https://linear.app/frenchforest/issue/FRE-1055) (Approved, High, Tier-1:Opus, stream:build1)
**Predecessor (measurement + probe):** FRE-1051 · **Successor (call sites):** FRE-1056
**Backing ADR:** none. This is defect hardening derived from the FRE-1051 measurement; ADR-0090's
delivery corner is being written separately on FRE-1058. Design intent is stated on the ticket itself.

## Boundary against FRE-1056

FRE-1055 builds the **primitive** on the handler (queue, owner-loop consumer, `drain()`, counters).
FRE-1056 wires the **call sites** (attach in the standalone gateway; call `drain()` before disconnect in
the service lifespan). `ElasticsearchHandler.disconnect()` draining itself is in scope here because
disconnect is the handler's own teardown; changing `service/app.py` / `gateway/app.py` is not.

## The four confirmed defects (from the ticket)

1. **Off-loop emission silently dropped** — `emit()` only ships when the calling thread has a running
   loop, so anything under `asyncio.to_thread` vanishes. Documented defensively in four places
   (`reflection_dspy.py:57,228,269`, `reflection.py:483`) and worked around for exactly one event
   (`emit_missing_skill_warnings`).
2. **No drain on shutdown** — `disconnect()` closes the client without awaiting in-flight writes.
3. **Task references discarded** — `asyncio.create_task(...)` with the reference dropped; CPython may
   collect the task mid-execution.
4. **Circuit breaker over-broad** — counts any failure, and `_log_async` re-checks the breaker *after*
   the task was created, so one burst discards everything queued for 30s.

## Design (as mandated by the ticket, revised by its codex review)

Replace fire-and-forget task creation with a **bounded queue drained by a single consumer task bound to
one explicitly captured owner loop**.

| Requirement (ticket) | How |
|---|---|
| Remove the semaphore | Serial consumer makes it redundant; it is itself a cross-loop hazard (an `asyncio.Semaphore` created in `__init__` binds to whatever loop first awaits it). |
| Only the owner loop touches queue/client/consumer/drain | `connect()` captures `asyncio.get_running_loop()` → `_owner_loop` and starts `_consumer`. `drain()` / `disconnect()` raise `ESHandlerLoopError` when called from any other loop. `connect()` on an already-connected handler drains and cancels the previous consumer first, so re-connect (ownership moving to a new loop) is a defined transition rather than an undefined one. |
| Enqueue from any thread | `emit()` enqueues **synchronously when it is already on the owner loop**, and via `_owner_loop.call_soon_threadsafe(self._enqueue, item)` from any other thread or loop. Two paths, because one is not enough — see "The drain barrier" below. |
| Capture destination index at emission time | `emit()` resolves the index name once and carries it on the queued item; the consumer passes it to `log_event(..., index=...)`. Prevents a backlog crossing a **month** boundary (indices are monthly since FRE-1036 — the ticket says "midnight", written when they were daily) from landing in the next month's index. |
| Exclude the handler's own internal diagnostics | Add `personal_agent.telemetry.es_handler` and `personal_agent.telemetry.es_logger` to the existing logger-name exclusion tuple. `es_logger` logs its own indexing failures through this same pipeline — a failure loop that ends at queue overflow. |
| Declare the overflow policy explicitly | **drop-oldest**, as a documented module constant. On `QueueFull` the oldest queued item is discarded (`get_nowait()` + `task_done()`) and the incoming one takes its place. Safe without locking because `_enqueue` runs only on the owner loop and never awaits. |
| Count drops rather than losing them silently | One counter per drop reason (see `ESDeliveryStats` below). |
| Export those counters | A public typed `stats()` snapshot (the in-process read, and what FRE-1056 / `/health` can surface later), **plus** a periodic `es_delivery_counters` document written by the consumer **directly through `es_logger.log_event`, bypassing `emit()` and the queue entirely**. Bypassing is what makes the export loop-free — see below. |

### The drain barrier (codex Critical — the design's real hole)

`call_soon_threadsafe` only *schedules*. If `emit()` used it unconditionally, then on the owner loop
`emit(); await drain()` would see an empty queue and return **before the record was ever enqueued** —
and `disconnect()` would then cancel the consumer and close the client. `Queue.join()` covers queued
work, not submissions still sitting in the loop's callback queue. Two mechanisms close this:

1. **Same-loop emits enqueue synchronously.** No scheduling gap at all for the common path.
2. **A submission barrier for cross-thread emits.** `emit()` increments `_pending_submissions` under a
   `threading.Lock` *before* scheduling; `_enqueue` decrements it on the owner loop. `drain()` first
   yields until `_pending_submissions == 0`, and only then awaits `_queue.join()`.

Residual, stated honestly and documented: a foreign thread can always schedule one more submission
*after* the barrier reads zero. That is inherent to cross-thread emission and is the same class of
boundary the ticket already accepts for abrupt process death.

### Drain timeout and the mid-write item (codex Major)

An ES request can take 30s with two retries, far beyond the drain timeout. So:

- The consumer calls `task_done()` in a `finally` for **every** `get()`, including when cancelled
  mid-write — accounting can never desynchronise.
- `drain()` is `asyncio.wait_for(...)`-bounded and returns `bool` (drained / timed out); it does not
  raise on timeout.
- `disconnect()` drains first, then cancels the consumer, then **explicitly empties the queue calling
  `task_done()` per item**, counting them as `dropped_shutdown`, then closes the client. So a timed-out
  drain leaves exact accounting and a subsequent `drain()` cannot hang.

### Counter export is loop-free by construction (codex Major)

The first draft emitted counters through a dedicated non-excluded logger. Codex is right that this is
not loop-free: every handler sits on the **root** logger, so that event re-enters `emit()` and mutates
the counters it just exported. The fix is topological, not rate-limiting: the consumer takes a snapshot
and writes it with `es_logger.log_event(...)` **directly**, never through `emit()`. Triggers: at most
once per `_STATS_INTERVAL_S` (60s) and only when a counter changed, plus one final write in `drain()`.
A failure of that write is logged by `es_logger`'s own logger, which is excluded — so no loop.

### Overflow: drop-oldest, and why the first draft was wrong (codex Major)

The first draft chose drop-newest and justified it with a pop-then-push race. Codex is right that the
race cannot occur — `_enqueue` is single-loop and synchronous — so that argument was invalid and the
conclusion had to be re-derived. Drop-newest has a worse failure mode for *this* system: once the queue
is full it stops accepting anything new, so during an incident the ES-backed dashboards flatline and
read as healthy-but-idle. That is the exact silent-empty shape the program keeps getting caught by.
**Drop-oldest** keeps fresh events flowing and loses the oldest of a backlog that is, by construction,
already stale. Logs still reach the file sink either way — the ES copy is for aggregation, which cares
about recency.

### Circuit breaker, narrowed (codex Major)

The breaker counts only outcomes of the actual ES write, inside the consumer. Enqueue-side problems
(foreign/closed loop, `QueueFull`, serialization) get their own counters and **never** call
`_record_failure`. The consumer checks the breaker once immediately before each write; the
create-task-then-recheck window is gone. Codex correctly notes `log_event` also returns `None` when the
client is absent, so the consumer **pre-checks `es_logger.client`** and counts that as
`dropped_not_connected` without touching the breaker. Residual, documented in the docstring: an
exception raised inside `_index_agent_log` (e.g. a redaction failure) is swallowed by `log_event` and is
indistinguishable from a transport failure — it is counted as a write failure, which is defensible
because it is equally fatal to delivery and equally worth pausing on.

### Honest boundary (stated on the ticket, carried into the docstring)

An in-memory queue cannot survive abrupt process death, and `call_soon_threadsafe` only *schedules* —
an event never enters the queue if the owner loop stops first. This ticket claims **no loss on graceful
shutdown only**. `drain()` is bounded by a timeout (default 5s) and counts `drain_timeouts`, because an
unbounded drain against a dead ES would hang shutdown for `request_timeout` × queue depth.

### `ESDeliveryStats` (frozen dataclass, returned by `stats()`)

`enqueued` · `delivered` · `write_failures` · `dropped_queue_full` · `dropped_circuit_open` ·
`dropped_not_connected` · `dropped_shutdown` · `enqueue_errors` · `drain_timeouts` · `queue_depth`

### Fold-in: `close()` removes the handler from the root logger

`add_elasticsearch_handler` only ever adds, and shutdown never removes (`app.py:1427-1436`), so repeated
in-process lifespans accumulate dead handlers bound to closed loops (codex Major). Each one then costs an
`enqueue_errors` increment per log record forever. Two lines in the handler's own `close()` fix it, and
`close()` is the handler's own teardown, so this does not collide with FRE-1056's ownership of the
call sites. Folded in per build § 5 rather than ticketed.

## Files

| File | Change |
|---|---|
| `src/personal_agent/telemetry/es_handler.py` | Rewrite: queue + owner-loop consumer + drain + counters + narrowed breaker. |
| `src/personal_agent/telemetry/es_logger.py` | `_get_index_name` → `current_index_name` (public, needed at emit time); thread optional `index` through `_index_agent_log` and `log_event`. |
| `src/personal_agent/exceptions.py` | Add `ESHandlerLoopError(RuntimeError)`. |
| `tests/test_telemetry/test_es_handler.py` | New tests (below); update the two existing `emit()` tests to await `drain()` instead of `sleep(0)`. |
| `tests/test_telemetry/test_es_logger_redaction.py` | Structural bypass guard matches the renamed `current_index_name`. |
| `src/personal_agent/captains_log/reflection_dspy.py`, `reflection.py` | Update the four stale defensive comments — off-loop emission now works. **Not** removing `emit_missing_skill_warnings`: that is a behaviour change beyond this ticket's objective, and its main-loop call site is still correct. |

## Steps

1. **RED** — `test_emit_from_worker_thread_reaches_es` (emits under `asyncio.to_thread`, asserts arrival).
   → verify: fails on current `main` code. `make test-file FILE=tests/test_telemetry/test_es_handler.py`
2. `ESHandlerLoopError` + `current_index_name` rename + `index=` threading in `es_logger.py`.
   → verify: `make test-file FILE=tests/test_telemetry/test_es_logger_redaction.py` green.
3. Rewrite `es_handler.py`: `_owner_loop`, bounded `asyncio.Queue`, `_consumer`, `_enqueue`, `drain()`,
   counters, `stats()`, narrowed breaker, exclusion tuple, index-at-emit.
   → verify: step-1 test goes green.
4. Remaining AC tests (burst, graceful drain, breaker isolation, overflow counter) **plus the lifecycle
   cases codex flagged as uncovered**: the cross-thread drain barrier, drain-timeout-then-second-drain,
   emit before `connect()`, emit after the owner loop closed, foreign-loop `drain()` rejection, reconnect
   onto a new loop, index captured at emit time, and self-diagnostics exclusion.
   → verify: whole file green.
5. Update the stale defensive comments in `captains_log/`.
6. Quality gates + self-review (Step 8).

## Acceptance criteria → proof

The ticket's `PROOF REQUIRED` section, one row per required proof.

| # | Criterion (ticket wording) | Test / evidence |
|---|---|---|
| AC1 | A test emitting from `asyncio.to_thread` that asserts the event arrives, **failing before the change** | `test_emit_from_worker_thread_reaches_es` + recorded pre-change failure output |
| AC2 | A concurrency burst asserting every event arrives | `test_burst_of_emits_all_arrive` (200 records, asserts count + no drops) |
| AC3 | A graceful-shutdown test with events in flight asserting the drain completes | `test_disconnect_drains_events_in_flight` (slow writer, events queued, `disconnect()` → all delivered) |
| AC4 | A test that a foreign-loop or enqueue error cannot open the circuit breaker | `test_enqueue_failure_does_not_open_circuit_breaker` |
| AC5 | A test that overflow increments an exported counter | `test_queue_overflow_increments_exported_counter` (maxsize 2, asserts `stats().dropped_queue_full` **and** that drop-oldest kept the newest) |
| — | ~~The FRE-1051 probe re-run **live**, showing the delivery ratio for every family that has an oracle~~ | **Not this ticket's criterion.** See below. |

### The live-probe line is a mis-written criterion, not a gap I carry

The ticket's `PROOF REQUIRED` ends with "then the FRE-1051 probe re-run live, showing the delivery ratio
for every family that has an oracle." That is not decidable from this ticket's own deliverable: it needs
a deploy, a traffic window, and an oracle-backed measurement of a *running* process. ADR-0130 D6 says a
`stream:` label asserts a ticket's criteria are decidable — so this line should not have been written on
a build ticket at all.

It is not dropped, it is **relocated**: a live delivery-ratio measurement is precisely ADR-0090's
delivery-corner objective, and an ADR's own criteria are asserted once, by that ADR's seam ticket
(ADR-0130 D1/D2). FRE-1058 is authoring that corner in the `adr` stream and owns filing the seam ticket
that carries it. Recorded in the Step-9 handoff for master so it lands there rather than evaporating.

What this ticket proves is AC1–AC5: the handler no longer loses events across threads or shutdown. What
the probe would prove is that the deployed system's delivery ratio recovered — a different altitude,
belonging to a different ticket.

## Diff class (Step 8)

**Escalated** — trigger 1, production write path: this code issues the `agent-logs-*` writes in the
running service, and sits on the root logger of every process. Self-serve review still runs and its
findings still get fixed on-branch; the escalation is flagged in the PR body and the ticket handoff for
the owner's `/code-review ultra` before merge.
