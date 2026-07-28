# FRE-1025 — session-summary sweep: silent early-return paths

## Scope (from ticket)

The session-summary sweep (`BrainstemScheduler.run_session_summary_sweep`,
`src/personal_agent/brainstem/scheduler.py`) has four paths that return before doing
any work, and at production log level (`info`) all four are silent:

1. Feature disabled by settings, or `memory_service is None` — no log at all.
2. A sweep is already in progress — `log.debug`.
3. The sweep is stood down under the FRE-987 global pause — `log.debug`.
4. A consolidation pass is in flight — `log.debug`.

A fifth silent case, not named in the ticket but inside the same invariant ("every
tick produces exactly one info-level record... carrying either the work done or the
reason no work was done"): when the sweep runs to completion but finds zero
dirty-and-idle sessions, `result["considered"]` is `0`/falsy and the existing
`session_summary_sweep_completed` log is skipped entirely (line 563-564,
`if result["considered"]: log.info(...)`). This is folded into this ticket per Step 5
of the build skill (a supporting fix needed to actually meet the stated objective),
not filed separately.

## Design decision: log-every-tick vs transition-only

**Revised after codex plan-review (see below).** Original draft proposed
transition-only logging for the `disabled` reason. Codex correctly flagged that this
directly violates the AC as literally stated ("every tick... produces exactly one
info-level record"): a second consecutive disabled tick would emit nothing under
transition-only logging. Decision reversed: **all four reasons log every tick.**
`_summary_sweep_disabled_last_logged` tracking is dropped entirely — not needed.

The noise concern the ticket raised (a permanently-disabled feature logging every
300s forever) is real but secondary to the AC's explicit per-tick guarantee; the AC
wins. `session_summary_sweep_interval_seconds` defaults to 300s (5 min) — 288
lines/day for a disabled deployment is acceptable log volume, and matches how the
other three reasons already behave once elevated to info.

## Codex plan-review — two additional gaps found

Codex (adversarial second opinion, `codex:rescue`) found two cases where the
original plan did not actually guarantee "exactly one info-level record per tick,"
which is the AC's core invariant:

**1. Expired-pause ticks double-log.** The existing pause/resume block already logs
`session_summary_sweep_resumed` at info when a stand-down window has passed
(line 522-526). Combined with the unconditional `session_summary_sweep_completed`
(or a subsequent early-return reason), a tick that clears an expired pause and then
either completes or hits guard 4 would emit **two** info records, not one.

Fix: remove the standalone `session_summary_sweep_resumed` log line. Track the
cleared pause in a local `resumed_from_pause: str | None` (the ISO timestamp of the
pause that just cleared) and attach it as a field on whatever terminal record this
tick actually emits (`deferred_to_consolidation`, the exception record, or
`completed`). The transition is still fully visible and queryable — just carried as
a field on the tick's one record instead of as a second, separate record.

**2. An exception during session discovery or `_sweep_one_session` currently
produces zero info-level records for that tick.** The `try/finally` around the sweep
body only resets `_summary_sweep_in_progress`; the exception propagates to
`_session_summary_sweep_loop`, which logs at ERROR — but with a **fresh trace_id**,
disconnected from the tick that actually failed, and only when the sweep runs via
the loop (not when `run_session_summary_sweep` is called directly, as the tests and
any future caller might). From this function's own perspective the tick is silent,
which is the exact failure mode the ticket is about.

Fix: wrap the guarded body in `try/except Exception/finally` (not `except
BaseException` — `asyncio.CancelledError` must keep propagating untouched, same as
today). On exception, log
`session_summary_sweep_returned_early` with `reason="exception"` and the error
string, at the tick's own `trace_id`, then re-raise. The loop's existing ERROR log
is unaffected (defense in depth) and `test_single_flight_flag_is_released_on_error`
continues to pass unchanged (it only asserts `RuntimeError` propagates and the flag
resets).

## Implementation

File: `src/personal_agent/brainstem/scheduler.py`

No new `__init__` state — the transition-tracking flag from the original draft is
dropped.

**1. Guard 1 (disabled / no memory service, ~line 500-501)** — was a bare `return
result` with no log; now logs every tick:

```python
if not settings.session_summary_enabled or self.memory_service is None:
    log.info(
        "session_summary_sweep_returned_early",
        reason="disabled",
        trace_id=trace_id,
    )
    return result
```

**2. Guard 2 (already in progress, ~line 503-505)** — `log.debug` → `log.info`,
shared event name, `reason` field:

```python
if self._summary_sweep_in_progress:
    log.info(
        "session_summary_sweep_returned_early",
        reason="already_in_progress",
        trace_id=trace_id,
    )
    return result
```

**3. Pause block (~line 513-527)** — the early-return branch elevates to info with a
`reason`; the expired-pause branch **stops logging `session_summary_sweep_resumed`**
and instead threads the cleared timestamp forward as a local so it can be attached
to whichever record actually terminates this tick:

```python
resumed_from_pause: str | None = None
now = datetime.now(timezone.utc)
if self._summary_sweep_paused_until is not None:
    if now < self._summary_sweep_paused_until:
        log.info(
            "session_summary_sweep_returned_early",
            reason="paused",
            trace_id=trace_id,
            resumes_at=self._summary_sweep_paused_until.isoformat(),
        )
        return result
    resumed_from_pause = self._summary_sweep_paused_until.isoformat()
    self._summary_sweep_paused_until = None
```

**4. Guard 4 (deferred to consolidation, ~line 531-533)** — same treatment, plus the
`resumed_from_pause` field so a tick that both cleared a pause and deferred to
consolidation still emits exactly one record:

```python
if self._consolidation_in_progress:
    log.info(
        "session_summary_sweep_returned_early",
        reason="deferred_to_consolidation",
        trace_id=trace_id,
        resumed_from_pause=resumed_from_pause,
    )
    return result
```

**5. Sweep body — exception path (~line 535-561):** wrap in `except Exception` (not
`BaseException`, so `asyncio.CancelledError` keeps propagating untouched) that logs
once and re-raises, so a discovery or per-session exception still produces exactly
one record for this tick instead of zero:

```python
self._summary_sweep_in_progress = True
try:
    sessions = await self.memory_service.find_dirty_idle_sessions(...)
    result["considered"] = len(sessions)

    for row in sessions:
        pause_until = await self._sweep_one_session(row, result=result, trace_id=trace_id)
        if pause_until is not None:
            self._summary_sweep_paused_until = pause_until
            log.warning("session_summary_sweep_stood_down", ...)
            break
except Exception as e:
    log.info(
        "session_summary_sweep_returned_early",
        reason="exception",
        error=str(e),
        trace_id=trace_id,
        resumed_from_pause=resumed_from_pause,
    )
    raise
finally:
    self._summary_sweep_in_progress = False

log.info("session_summary_sweep_completed", **result, trace_id=trace_id, resumed_from_pause=resumed_from_pause)
```

Note the `session_summary_sweep_stood_down` WARNING (mid-loop denial stand-down,
unchanged) does not compete with the "exactly one info record" invariant — it is a
different log level, and it already coexists with the unconditional `completed` INFO
that follows it today.

One event name (`session_summary_sweep_returned_early`) carries a structured
`reason` field with five possible values now (`disabled`, `already_in_progress`,
`paused`, `deferred_to_consolidation`, `exception`) — queryable/filterable/countable
by `reason`, per the AC's "structured field, not prose" requirement. `exception` is
outside the ticket's named four but closes a real silent-tick gap Codex found using
the same mechanism, so it is folded in rather than special-cased.

No changes to `_session_summary_sweep_loop` — it already calls
`run_session_summary_sweep` unconditionally every tick; the outcome-logging lives
entirely inside the function, exercised directly by the existing test suite.

## Tests (TDD — failing first)

File: `tests/personal_agent/brainstem/test_session_summary_sweep.py`

All via `structlog.testing.capture_logs()` (established pattern, see
`tests/test_brainstem/test_scheduler.py:611`), asserting on `event["event"]`,
`event["log_level"]`, and `event["reason"]`. Per codex review, every scenario also
asserts the **count** of info-level records is exactly one (not just that the
expected one is present), since the double-log gaps codex found would otherwise
slip past field-only assertions.

1. `test_disabled_sweep_does_nothing` — extend: assert exactly one info event,
   `session_summary_sweep_returned_early` / `reason=="disabled"`. Add a second
   `run_session_summary_sweep` call (still disabled) and assert it **also** logs
   exactly one such event (every-tick, not transition-only — reversed from the
   original draft).
2. `test_sweep_without_a_memory_service_is_a_no_op` — extend: same assertion,
   `reason=="disabled"` (this guard is the same branch as #1).
3. `test_sweep_is_single_flight` — keep as the hand-set-flag unit test, extend:
   assert exactly one info event, `reason=="already_in_progress"`.
4. New: `test_a_second_sweep_started_while_the_first_is_running_returns_early` — a
   genuine concurrency test: patch `find_dirty_idle_sessions` to block on an
   `asyncio.Event` after signalling it has started, launch the first sweep as a task,
   await the signal, run a second sweep concurrently, assert the second returns the
   all-zero result with exactly one info event (`reason=="already_in_progress"`),
   release the block, await the first task and confirm it completed normally.
5. `test_sweep_defers_to_an_in_flight_consolidation` — extend: assert exactly one
   info event, `reason=="deferred_to_consolidation"`.
6. New: `test_paused_sweep_logs_the_reason_at_info` — set
   `_summary_sweep_paused_until` to a future instant (not yet expired, so this is the
   *early-return* branch, distinct from the existing resume-transition test at
   line ~1094), call the sweep, assert exactly one info event,
   `reason=="paused"`, with a `resumes_at` field present.
7. New: `test_an_expired_pause_does_not_double_log` — set `_summary_sweep_paused_until`
   to a past instant, sessions empty (so the tick completes cleanly after clearing
   the pause), call the sweep, assert **exactly one** info-level event total for the
   tick (`session_summary_sweep_completed`, `considered=0`) carrying
   `resumed_from_pause` equal to the cleared timestamp — proving the old
   `session_summary_sweep_resumed` line no longer fires alongside it.
8. New: `test_an_exception_during_discovery_logs_exactly_one_record_before_raising`
   — reuse the `test_single_flight_flag_is_released_on_error` failure setup
   (`find_dirty_idle_sessions` raising `RuntimeError`), assert the `RuntimeError`
   still propagates and the flag still resets (unchanged assertions), plus assert
   exactly one info event fired before the raise:
   `session_summary_sweep_returned_early` / `reason=="exception"` / `error` field
   containing the message.
9. New: `test_a_sweep_with_no_dirty_sessions_still_logs_completion` — empty
   `_FakeMemory` (no sessions), call the sweep, assert a
   `session_summary_sweep_completed` / info event fires with `considered=0` (proves
   the fifth silent path from the ticket's own stated invariant is closed).

Each of the four ticket-named reasons gets its own assertion in a distinct test,
satisfying the AC's "driving the loop through each of the four early-return
conditions and asserting a distinct reason is emitted for each" — asserting on the
`reason` field (not the message string) satisfies "queryable field, not prose" — and
the exact-count-of-one assertions in every test close the double-log/zero-log edge
cases codex identified, giving the AC's "exactly one info-level record" clause real
coverage rather than just the four named reasons.

## Quality gates

`make test-file FILE=tests/personal_agent/brainstem/test_session_summary_sweep.py`,
then `make test` (full), `make mypy`, `make ruff-check`, `make ruff-format`,
`pre-commit run --all-files`.

## Risk tier

Standard — touches `src/` behavioral logic (log semantics change, new instance
state). Codex plan-review required before implementation.
