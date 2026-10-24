# FRE-1410 — ADR-0143 D3: the close is a deadline

Backing ADR: `docs/architecture_decisions/ADR-0143-the-request-task-is-the-unit-that-must-end.md` (D3 and its
AC-1, AC-2, AC-3, AC-7, AC-8). Blocker FRE-1408 is merged (PR #1349): the registry, `close_won`, and
`stage_two_deadline` exist and nothing reads them yet.

Class: Standard/Complex. It touches `src/` logic in the transport write path (DONE row) and the registry.
Diff class: escalated (production write path in the call chain).

## Design decisions

1. **Arbiter.** `RequestTaskRegistry.close_once(trace_id, how) -> bool` sets `close_won` and `close_how` on
   the first call and returns `True`. Later calls return `False`. A trace with no record returns `True`
   (a caller outside the registry keeps today's behaviour). The `task_ended` log gains `closed_by`.
2. **Degraded path** lives in `transport.py` as `close_stream_degraded(session_id, trace_id, *, trigger)`.
   The caller must already hold the `close_once` win. The registry reaches it through a function-level
   import (no import cycle). Steps:
   - One synchronous section: newer live record on the session -> log `reason=superseded`, stop. No active
     connection -> log `reason=no_connection`, stop. Else capture the connection and its `generation`.
   - `await queue.put(sentinel)` under `asyncio.timeout(close/2)`. Success ends the path.
   - On timeout: log `reason=queue_timeout`, then `close_connection_if_current(session_id, generation)`
     under `asyncio.timeout(close/2)`. A failure logs a second line (`close_timeout` or `close_failed`).
   - `trigger` is the failure that started the path (`lock_timeout`, `persist_timeout`, `persist_failed`,
     `deadline`, `shutdown`) and is logged first as `transport.done_degraded`. `trigger=None` logs no
     first line (the queue-full fallback of the normal path).
3. **`emit_done`.** Lock and DONE append under one `asyncio.timeout(close)`. The code tracks lock
   acquisition. Inside the lock after the append: `close_once` (False -> log `transport.done_after_close`,
   push nothing), then `put_nowait` as today. `QueueFull` leaves the lock and runs the degraded path with
   `trigger=None`. A timeout (our own timer, checked with `expired()`) or an exception runs the degraded
   path after `close_once` with the matching trigger.
4. **ws_endpoint.** `_ConnectionState.generation` (default 0), a module counter, set on each connect.
   `close_connection_if_current(session_id, generation)` closes only when the active connection still has
   that generation. Else it logs `ws.close_skipped_generation_mismatch` and returns `False`.
5. **Registry deadline.** `spawn` also arms `loop.call_at(stage_two_deadline, ...)`. The callback is
   cancelled in `_on_done`. If the task is alive at the deadline: mark `leaked`, log `chat_stream.task_leaked`
   (`reason=stage_two_deadline`), log the gauge, and, if `close_once("deadline")` wins, run the degraded
   path (`trigger="deadline"`) in a task that the registry keeps a strong reference to.
6. **Shutdown.** Before the cancel and the wait: for every live record, `close_once("shutdown")` then the
   degraded path (`trigger="shutdown"`), run together. Then cancel, wait, mark survivors leaked as today.
7. **Settings.** Update the description of `service_request_close_timeout_seconds`. Regenerate
   `docs/reference/CONFIG_INVENTORY.md`.

## Codex plan review (round 1) — disposition

| Finding | Disposition |
|---------|-------------|
| Two traces on one session can close the same stream in the normal path | Out of scope. ADR-0143 "Out of scope" names it. The normal path is unchanged. Noted in the handoff. |
| A degraded sentinel put after a reconnect closes the new connection | Accepted. The degraded sentinel carries the captured `generation`. `_sender` skips a sentinel whose generation differs from its connection. A sentinel without a generation is honoured as today. |
| Shutdown must close only when it wins `close_once` | Accepted. Shutdown calls the degraded path only inside `if close_once(trace_id, "shutdown")`. |
| A missing record returns `True` for every call | Declined. Only direct callers in tests and the harness have no record. A tombstone adds state for no production case. Noted in the handoff. |
| The shutdown test could pass without D3 | Already covered. The test asserts both terminal signals, `cancelled`, and the retained `leaked` record. |
| AC-1 lacks the nested `TimeoutError` case | Declined. That case is FRE-1408's (AC-3(b) there). |

## Steps (TDD)

1. Write `tests/personal_agent/service/test_request_close.py`. It uses the real `emit_done`, a fake
   `SessionEventBuffer`, and a fake connection with a real `_sender`. It imports no symbol that is new on
   this branch at module level. Run it on the branch before the change: confirm the failures.
   `make test-file FILE=tests/personal_agent/service/test_request_close.py`
2. `ws_endpoint.py`: generation field, counter, `close_connection_if_current`.
3. `request_tasks.py`: `close_once`, `close_how`, stage-two timer, deadline close, shutdown close,
   `closed_by` in `task_ended`.
4. `transport.py`: bounded `emit_done`, `close_stream_degraded`.
5. `settings.py` description and the inventory.
6. Run the new tests, then `tests/personal_agent/service/test_request_tasks.py`,
   `tests/personal_agent/transport/`, `tests/test_service/`.
7. AC-6: run the three seeded negatives on a clean checkout of `origin/main` (separate worktree). Record both runs.
8. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

## Tests (one per ticket criterion)

| AC | Test | Setup |
|----|------|-------|
| AC-1 | cooperative block, stage one wins | 1/2/1, block the reply push, assert DONE frame by 4 s, `closed_by=emit_done`, `outcome=timed_out`, no `task_leaked`, record gone by 5 s |
| AC-2a | lock held | 30/60/1, hold the emit lock in `_record_first_token`, `reason=lock_timeout`, DONE frame within 2 s |
| AC-2b | append blocked for DONE | `reason=persist_timeout` |
| AC-2c | full queue, sender held | no DONE frame, socket closed, `reason=queue_timeout` |
| AC-3 | suppressing block | 1/3/1, terminal signal by 5 s, `state=leaked` at 6 s, gauge, release -> `timed_out was_leaked=true` |
| AC-4a/b/c | stale deadline | superseded, generation mismatch, no_connection and next sender not closed |
| AC-5 | shutdown | 30/60/1, two sessions, `registry.shutdown()` returns within 2 s |
