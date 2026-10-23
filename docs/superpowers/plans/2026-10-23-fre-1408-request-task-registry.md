# FRE-1408 — Request-task registry and stage-one deadline (ADR-0143 D1 + D2)

Backing design: `docs/architecture_decisions/ADR-0143-the-request-task-is-the-unit-that-must-end.md`
(D1, D2). The owner accepted the ADR with the ticket approval (Linear comment, 2026-10-23).
Scope: this ticket only. D3 (bounded `emit_done`, degraded close, armed close, `close_once`
arbiter) and D4 are separate tickets. This PR ships the `close_won` field and the stage-two
deadline field, but no code reads them.

Tier: Standard (touches `src/` logic, one `src/` module added, settings). Codex plan review required.

## Decisions (flagged for review)

1. **Clock.** `started_at` is `loop.time()` at registry insertion. `asyncio.timeout_at` needs the
   loop clock, and `loop.time()` is `time.monotonic()` on the default loop. One clock for the
   record, the timer and the timeout.
2. **Entry guard.** The registry sets a contextvar holding the trace id while it calls
   `asyncio.create_task`, then resets it. The task copies the context. The body calls
   `current_request_task(trace_id)` as its first statement. It raises `RequestTaskEntryError` when
   the contextvar is unset, differs from the body's `trace_id`, or no live record exists.
   A detached task that the turn spawns inherits the contextvar, which is harmless: it holds a string.
3. **Outcome rules.** The record holds `outcome: RequestOutcome | None`.
   - The stage-one timer (`loop.call_at(stage_one_deadline)`) stamps `timed_out` when the task is not
     done. It overwrites a `failed` stamp. A task that is past its deadline is `timed_out`.
   - The body stamps `failed` (first stamp wins) in the outer `except Exception`, in the outer
     `except TimeoutError` when `expired()` is false, and in the inner orchestrator handler.
     The inner handler swallows the exception and the task ends normally, so without the stamp
     the record says `completed` for a turn the user saw fail. AC-3(b) needs this stamp when the
     seeded error comes from the orchestrator.
   - `cancelled` and `completed` are derived by the done callback only when the record has no outcome.
4. **Re-indent.** The body of the existing outer `try` moves one level into `async with deadline:`.
   Review with `git diff -w`.
5. **Duplicate trace id.** `spawn` closes the coroutine and raises `ValueError` when a record with
   that trace id exists. Overwriting would drop a strong reference.
6. **Existing tests.** Seven test files await the body directly and would now raise. A helper
   `tests/_helpers/request_tasks.py::run_turn(**kwargs)` spawns through the registry singleton and
   awaits the task. `test_chat_trace_identity.py` patches `asyncio.create_task`. It patches
   `get_request_task_registry` instead.

## Acceptance criteria → proof

| AC | Proof (test in `tests/personal_agent/service/test_request_tasks.py`) |
|----|----------------------------------------------------------------------|
| AC-1 | `test_bare_create_task_raises_entry_error_before_any_side_effect`; `test_registry_spawn_runs_the_body` |
| AC-2 | `test_two_turns_on_one_session_are_two_records_and_both_endings_are_logged` (via `chat_stream_endpoint` twice, fake body gated on events) |
| AC-3a | `test_stage_one_times_out_a_cooperative_block_after_the_executor` (real body, `_push_event` blocks) |
| AC-3b | `test_an_inner_timeout_error_before_the_deadline_is_failed` (orchestrator raises `TimeoutError` at 0.2 s) and `test_an_escaping_timeout_error_before_the_deadline_is_failed` |
| AC-3c | `test_scheduling_delay_is_charged_to_the_turn` (`time.sleep(1.5)` after spawn, 2 s lifetime, ends near 2 s) |
| AC-4 | `test_lifetime_not_above_orchestrator_cap_is_rejected` (+ default test) |
| AC-5 | Run the module on a scratch worktree of `origin/main` (new test files copied in, `PYTHONPATH` at its `src`). AC-1 bypass, AC-2, AC-3a, AC-4 must fail. Record both runs in the PR. |

Also covered: shutdown cancels and awaits; a cancellation-suppressing task becomes `leaked`,
stays in the registry, logs `chat_stream.task_leaked`, and logs `was_leaked=True` when it ends.

## Steps

### 1. Settings — `src/personal_agent/config/settings.py` (after `orchestrator_turn_lifetime_seconds`)

```python
service_request_task_lifetime_seconds: int = Field(
    default_factory=lambda data: data.get("orchestrator_turn_lifetime_seconds", 1800) + 300,
    ge=1,
    description="ADR-0143 D2: stage-one wall-clock bound ...",
)
service_request_close_timeout_seconds: int = Field(default=10, ge=1, description="ADR-0143 D3 ...")
```

`@model_validator(mode="after") _validate_service_lifetime_exceeds_orchestrator_cap` raises
`ValueError` naming both fields when `service_request_task_lifetime_seconds <=
orchestrator_turn_lifetime_seconds`. Verify: `uv run pytest tests/personal_agent/service/test_request_tasks.py -k lifetime`.

### 2. Registry — `src/personal_agent/service/request_tasks.py` (new)

- `RequestOutcome = Literal["completed","timed_out","cancelled","failed"]`, `RequestTaskState = Literal["live","leaked"]`.
- `class RequestTaskEntryError(RuntimeError)`.
- `@dataclass(eq=False) class RequestTask`: `trace_id`, `session_id`, `started_at`,
  `stage_one_deadline`, `stage_two_deadline` (= stage one + close timeout), `task`, `state`,
  `outcome`, `close_won` (False; used by FRE-1410), `_timer`. Mutable by design: state and outcome
  change over the record's life. Methods `stamp_timed_out()`, `stamp_failed()`.
- `class RequestTaskRegistry`:
  - `spawn(coro, *, trace_id, session_id) -> RequestTask`: duplicate guard; build record from
    `loop.time()` and settings; set the contextvar, `asyncio.create_task(coro)`, reset; store the
    record; arm `call_at(stage_one_deadline, self._on_stage_one_deadline, record)`;
    `task.add_done_callback(partial(self._on_done, record))`; log the gauge.
  - `_on_stage_one_deadline(record)`: if the task is not done → `record.stamp_timed_out()`, log
    `chat_stream.task_stage_one_deadline`.
  - `_on_done(record, task)`: cancel timer; pop record; outcome = record outcome, else derived
    (`task.cancelled()` → cancelled; `task.exception()` → failed, with `error_type`; else completed);
    log `chat_stream.task_ended` (`trace_id`, `session_id`, `elapsed_seconds`, `outcome`,
    `was_leaked`); log the gauge.
  - `get`, `records`, `counts`.
  - `async shutdown()`: cancel live tasks; `asyncio.wait(..., timeout=close_timeout)`; each task
    still pending → `state="leaked"`, log `chat_stream.task_leaked`; log the gauge.
- `get_request_task_registry()` singleton; `current_request_task(trace_id)`.
- Every log call carries `trace_id` and `session_id`.

### 3. Service — `src/personal_agent/service/app.py`

- `_process_chat_stream_background`: first statement `request_record = current_request_task(trace_id)`.
  Create `deadline = asyncio.timeout_at(request_record.stage_one_deadline)` before the `try`; the
  `try` body becomes `async with deadline:`.
- Move the existing `except Exception` body into `_report_background_failure(event, session_id, trace_id, error)`
  (log + push the same error delta). Add `except TimeoutError as e:` before it:
  `deadline.expired()` → `stamp_timed_out()` and report with event `chat_stream.request_task_timed_out`;
  else `stamp_failed()` and report with `chat_stream.background_failed`. The `except Exception`
  stamps `failed` and reports with `chat_stream.background_failed`.
- The inner orchestrator `except Exception` adds `request_record.stamp_failed()`.
- `chat_stream_endpoint`: replace `asyncio.create_task(...)` with
  `get_request_task_registry().spawn(_process_chat_stream_background(...), trace_id=..., session_id=...)`.
- Lifespan shutdown: after the cleanup-task cancels and before the consumers stop,
  `await get_request_task_registry().shutdown()`.

### 4. Existing tests

Replace `await _process_chat_stream_background(...)` with `await run_turn(...)` in
`test_chat_stream_contextvars_propagation.py`, `test_first_token_ms.py`,
`test_chat_stream_request_completed.py`, `test_seshat_effort_chat_paths.py`; the cancel test in
`test_chat_stream_request_completed.py` uses `spawn(...).task`. The contextvars test asserts the
teardown clear on `record.task.get_context()`, because the binding now lives in the task's context.
`test_chat_trace_identity.py` patches `get_request_task_registry`.

### 5. Gates

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
Commit, then `feature-dev:code-reviewer` on `git diff origin/main...HEAD`. Run `security-review`
only if the diff touches inputs, subprocess, files, auth, secrets or network (it does not).

### 6. AC-5 evidence

```bash
git worktree add <scratch>/main-check origin/main
cp tests/personal_agent/service/test_request_tasks.py tests/_helpers/request_tasks.py <same paths>
cd <scratch>/main-check && PYTHONPATH=$PWD/src uv run --no-sync pytest tests/personal_agent/service/test_request_tasks.py
```

Check `personal_agent.__file__` points into the scratch tree before trusting the run.

## Diff class

Escalate? The diff is a service task-lifecycle change. It has no production write path of its own
beyond the existing turn body, no destructive code, no schema change, no cost or governance code.
Self-serve. A turn that exceeds the new bound is now cancelled — a behaviour change at 2100 s
(1800 + 300) by default.

## Codex plan review, round 1 — dispositions

| Finding | Disposition |
|---------|-------------|
| High: the contextvar guard passes for a wrapper or a child task that inherits the context | Fixed. The body also requires `asyncio.current_task() is record.task`. A child task or a bare task fails. A wrapper coroutine that was passed to `spawn` is a registry creation, so it passes. `spawn` inserts the record before `create_task` and resets the contextvar in `finally`. |
| High: the error report after a timeout awaits `_push_event` with no bound | Accepted as a property of stage one. ADR-0143 states that stage one is cooperative. D3 (FRE-1410) bounds the close. AC-3(a) blocks the reply push once and passes later pushes. The test name and the PR say so. No new bound is added here. |
| High: AC-5 tests fail on `main` only by import error | Fixed. AC-2 and AC-3 drive `chat_stream_endpoint` (the production path) and assert on `chat_stream.task_ended` logs. Registry reads use a feature-detect helper that returns an empty result when the module is missing. Settings are set through a helper that skips unknown fields. The AC-1 bypass test imports nothing new. The AC-1 registry-path test and the shutdown tests need the module and are not in the AC-5 set. |
| Medium: `uv run --no-sync` in a fresh worktree may lack dependencies | Fixed. Run `<build2>/.venv/bin/python -m pytest` with `PYTHONPATH=<scratch>/src`. Print `personal_agent.__file__` first. |
| Medium: shutdown does not close streams, which D1 asks for | Deliberate. The ticket text says FRE-1410 adds the immediate close. The PR and handoff say this ticket is a partial D1 and do not claim full D1 compliance. |
| Medium: the `timed_out` stamp at the exact boundary depends on callback order | Accepted. `timed_out` means that the stage-one timer saw the task pending. No test asserts at the exact boundary. The docstring says so. |
| Low: an eager task factory could run the body before the record is stored | Covered by insert-before-`create_task`. Nothing in the repository sets an eager factory. |
