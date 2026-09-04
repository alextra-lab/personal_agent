# FRE-1375 — Stop button must abort an in-flight generation

## Problem

The cancel flag set by `USER_CANCEL` is read at exactly one checkpoint:
`step_tool_execution`, between tool rounds. A turn spends almost all of its
wall-clock inside the primary model call, so Stop is silently ignored there,
and when the checkpoint IS reached it still issues one more LLM call
(`force_synthesis_from_limit = True` → `TaskState.LLM_CALL`).

## Design

Reuse the FRE-973 deadline-cancellation mechanism (`orchestrator/executor.py`
`step_llm_call`, the `asyncio.wait_for(llm_client.respond(...), timeout=...)`
around line 5937) rather than inventing a new one. That mechanism is already
proven to:
- cancel the in-flight `litellm.acompletion()` call (`llm_client/litellm_client.py:1018`
  and `:1528` both catch, refund/cleanup, and re-raise `asyncio.CancelledError`
  without swallowing it)
- release the concurrency-controller slot (`llm_client/concurrency.py`
  `request_slot`'s `finally` at line 343 releases both semaphores on ANY exit,
  including cancellation)

Add a second cancellation source that races the same call: an `asyncio.Event`,
set the instant `USER_CANCEL` arrives (today the WS handler only flips a bool
the tool-round checkpoint polls). Wherever the call is awaited, race it
against the existing deadline AND this event.

**Revised after codex plan-review** (findings below folded in — see inline
notes marked `[codex]`):

### 1. `transport/agui/ws_endpoint.py`

- `[codex]` The event must be **session-scoped**, not tied to `_ConnectionState`.
  The ticket's own incident log shows a reconnect mid-cancel-storm
  (`ws.disconnected` at 05:05:49, cancels resumed after reconnect at
  05:06:02) — a connection-scoped event object would be replaced by a fresh,
  unset one on exactly that reconnect, orphaning any in-flight race that
  captured the old object. Mirrors `_session_constraint_waiters` (`:122`),
  which is session-scoped for the identical reason (FRE-928).
- New module-level `_session_cancel_events: dict[str, asyncio.Event] = {}`
  next to `_session_constraint_waiters`.
- New `_get_or_create_cancel_event(session_id: str) -> asyncio.Event` (private).
- New `get_cancel_event(session_id: str) -> asyncio.Event` (public, always
  returns — creates on first use) for the executor to await without polling.
- `case "USER_CANCEL":` — also call `_get_or_create_cancel_event(conn.session_id).set()`.
- `clear_cancel_flag()` — `[codex]` clears **both** the bool and the event in
  the same function, keyed off the same `session_id` parameter, so they can
  never diverge (finding: "stale event could fire a future turn's call").
  No `_ConnectionState` field changes needed — the bool mechanism is untouched.

### 2. `orchestrator/executor.py`

- `types.py`: `ExecutionContext` gets a new `turn_stopped_early: bool = False`.
- `[codex, high]` `step_synthesis`'s grounding-verification block (`enforce`
  mode) can clear `ctx.final_reply` and return `TaskState.LLM_CALL`
  (`grounding/enforcement.py` `decide()`, proven by
  `test_executor_grounding.py::test_enforce_blocks_and_returns_to_llm_call_with_retrieval_forced`).
  Since `_emit_turn_cancelled` already cleared the cancel state before
  reaching synthesis, an unlucky enforcement retry would issue exactly the
  extra model call AC-3 forbids — a latent gap in the FRE-973 deadline path
  too, not something specific to this ticket's new code, but it directly
  threatens the AC this ticket is adding, so it is folded in here. Fix: gate
  `step_synthesis`'s whole grounding block on `not ctx.turn_stopped_early`.
  Both `_stop_turn_for_deadline` and the new `_stop_turn_for_cancel` set
  `ctx.turn_stopped_early = True`.
- New `_get_cancel_event(session_id: str) -> asyncio.Event` next to
  `_is_turn_cancelled` (same lazy-import pattern, avoids an import cycle).
- New `_stop_turn_for_cancel(ctx: ExecutionContext) -> None`, sibling to
  `_stop_turn_for_deadline` (`:2778`) — salvages `ctx.tool_results` into
  `ctx.final_reply` with stop-specific wording, appends a `warning` step,
  sets `ctx.turn_stopped_early = True`. Deliberately separate from
  `_stop_turn_for_deadline`: AC-4 needs the user-visible message to say the
  turn was *stopped*, not that a time budget ran out.
- `step_llm_call` (`:5933-5974`) — replace the single `asyncio.wait_for` with
  a manual two-task race when a cancel event exists for this session.
  `[codex]` NOT nested `asyncio.wait_for` calls or a "watcher that cancels
  the inner future out-of-band" — that mixes two independent cancellation
  sources on one future in a way that is fragile across asyncio's
  cancel/uncancel bookkeeping (3.11+ `Task.uncancel()`). Plain
  `asyncio.wait()` over two real tasks, both explicitly owned and torn down:
  ```python
  _respond_coro = llm_client.respond(...)  # unchanged kwargs
  _cancel_event = _get_cancel_event(ctx.session_id) if ctx.session_id else None
  if _cancel_event is None:
      response = await asyncio.wait_for(_respond_coro, timeout=_deadline_remaining)
  else:
      _respond_task = asyncio.ensure_future(_respond_coro)
      _cancel_wait_task = asyncio.ensure_future(_cancel_event.wait())
      _race_tasks = (_respond_task, _cancel_wait_task)
      try:
          done, _pending = await asyncio.wait(
              _race_tasks, timeout=_deadline_remaining,
              return_when=asyncio.FIRST_COMPLETED,
          )
      finally:
          # [codex, high] unconditional — runs even if THIS await is itself
          # cancelled from outside (a turn-level cancellation), so
          # _respond_task is never orphaned still generating and holding
          # its concurrency slot.
          for _t in _race_tasks:
              if not _t.done():
                  _t.cancel()
          await asyncio.gather(*_race_tasks, return_exceptions=True)

      # [codex, high] cancel checked FIRST: if both complete in the same
      # asyncio.wait() call (e.g. an already-set event racing a fast/mocked
      # response), Stop must win — a response that arrives in the same
      # instant as a cancel must never be delivered (AC-3).
      if _cancel_wait_task in done:
          await _emit_turn_cancelled(session_id=ctx.session_id, trace_id=ctx.trace_id)
          _stop_turn_for_cancel(ctx)
          log.info(STEP_PLANNING_COMPLETED, ..., status="user_cancelled", next_state="synthesis")
          return TaskState.SYNTHESIS
      if _respond_task in done:
          response = _respond_task.result()
      else:
          raise TimeoutError
  ```
  The `raise TimeoutError` in the timeout branch re-enters the existing
  `except TimeoutError:` handler below unchanged (still `_stop_turn_for_deadline`
  → `TaskState.SYNTHESIS`) — no duplication of that path.
  The already-cancelled-before-the-call case needs no separate pre-check:
  if `cancel_event` is already set when the race starts, `_cancel_wait_task`
  resolves immediately and wins the tie-break above.

- `step_tool_execution`'s existing checkpoint (`:6281-6284`) — stop routing
  through another `LLM_CALL`:
  ```python
  if ctx.session_id and _is_turn_cancelled(ctx.session_id):
      await _emit_turn_cancelled(session_id=ctx.session_id, trace_id=ctx.trace_id)
      _stop_turn_for_cancel(ctx)
      return TaskState.SYNTHESIS
  ```
  This is what closes AC-3 for the one path that already worked, too — today
  it still burns one more model call via `force_synthesis_from_limit`.

## Acceptance criteria mapping

- **AC-1** (stop works during generation): the `step_llm_call` race — a slow
  `llm_client.respond()` mock, cancel event set mid-await, turn reaches
  `TaskState.SYNTHESIS` without the mock ever completing.
- **AC-2** (slot released, measured): `request_slot`'s `finally` releases
  unconditionally — covered by a concurrency-controller unit test that
  cancels a task holding the slot and asserts the semaphore is free
  afterward. The full claim (llama.cpp's OWN slot, not just our local
  semaphore) can only be measured live; documented as a post-deploy
  verification step in the handoff, per FRE-1154's own caution against
  arguing this from code.
- **AC-3** (repeated presses don't queue work): both new cancel exits return
  `TaskState.SYNTHESIS` directly — no `force_synthesis_from_limit` in either
  path — plus a test asserting `llm_client.respond` is called exactly once
  across several `USER_CANCEL`-equivalent event sets.
- **AC-4** (user sees it stopped): both paths call `_emit_turn_cancelled`
  (existing `CANCELLED` transport event, unchanged) before returning, and
  `_stop_turn_for_cancel` always populates `ctx.final_reply`.

## Files touched

- `src/personal_agent/transport/agui/ws_endpoint.py`
- `src/personal_agent/orchestrator/executor.py`
- `tests/personal_agent/orchestrator/test_user_cancel_mid_generation.py` (new)
- `tests/personal_agent/transport/test_ws_integration.py` or a sibling
  (extend for `get_cancel_event`/`cancel_event` set/clear)
- `tests/test_llm_client/test_concurrency.py` (extend for AC-2's
  cancel-releases-slot unit test)

## Test commands

```
make test-file FILE=tests/personal_agent/orchestrator/test_user_cancel_mid_generation.py
make test-file FILE=tests/personal_agent/transport/test_ws_integration.py
make test-file FILE=tests/test_llm_client/test_concurrency.py
make test
make mypy
make ruff-check
```

## Diff class

Escalate — production write path (the primary turn's LLM_CALL step is in
every turn's call chain).
