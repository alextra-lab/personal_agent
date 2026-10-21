# FRE-1635 — Run HYBRID helpers in parallel on a cloud deployment

**Ticket:** FRE-1635 (owner directive 2026-10-20). Narrows FRE-1380 (serial fan-out) to the local
model. Keeps FRE-1397 (turn deadline), FRE-1374 (admission), FRE-1501 (origin error) and
FRE-1389 (gap replacement) properties.

**Tier:** Standard. Codex plan review is required (orchestrator concurrency, cost gate).

## Design

### When the fan-out is parallel

`_run_dispatch` computes one number, the fan-out limit, before the loop:

```python
def _fan_out_limit(llm_client: Any, task_count: int, expansion_budget: int | None) -> int:
    if getattr(llm_client, "placement", None) is not Placement.CLOUD:
        return 1                                    # slm_local, a stub, or unknown → serial
    ceiling = get_inference_concurrency_controller().effective_ceiling(
        _planner_deployment(llm_client), default=1  # unknown deployment → serial
    )
    limit = min(task_count, ceiling)
    if expansion_budget is not None:
        limit = min(limit, expansion_budget)
    return max(1, limit)
```

* `placement` is set on every `LiteLLMClient` from the catalog provider (`Placement.LOCAL` for
  `slm_local`, `Placement.CLOUD` for `openai`, `anthropic`, `ovhcloud`). The catalog has no other
  local provider, so this is the ticket's "provider other than `slm_local`" test, read from the
  declared fact instead of a provider name.
* `effective_ceiling` is the smaller of the deployment's `max_concurrency` and its provider
  ceiling — the two limits that `request_slot` enforces. A fan-out at this size never queues
  behind its own siblings in the inference controller.
* `expansion_budget` (FRE-1382, "number of safe concurrent sub-agents") is the brainstem's
  load-shed signal. It is now passed from `execute` to `_run_dispatch`. A budget of 0 gives 1
  (serial), because a plan with tasks still runs today on that budget.
* Every unknown (a stub with no `placement`, an unregistered deployment) gives 1. The existing
  tests use `AsyncMock()` clients, so they run the serial path unchanged (AC-5).

### One per-task coroutine, two schedulers

The current loop body moves into a nested coroutine `_dispatch_task(task, spec) -> _TaskOutcome`.
`_TaskOutcome` is a private frozen dataclass: `results` (original + optional replacement),
`intervals`, `raw` (results and caught exceptions), `skip_reason` (`None` or a `SkipReason`).
The body is unchanged: origin-error check, FRE-1397 deadline check and cap, the `SUB_AGENT`
phase span, `run_sub_agent`, the `origin_error` flag, and `_maybe_redispatch_on_gap` (it now
appends to the task's own interval list).

* **Serial (limit 1):** `for task, spec in ...: outcomes.append(await _dispatch_task(task, spec))`.
  The behavior is the same as today, statement for statement.
* **Parallel (limit > 1):** an `asyncio.Semaphore(limit)` and an `asyncio.TaskGroup`. Each child
  does `async with semaphore: return await _dispatch_task(task, spec)`. The `EXPANSION` parent
  span wraps the task group, so the parent ends after the last child.

After either scheduler, the outcomes merge **in plan order**: `sub_results`, `dispatch_intervals`,
`skipped_tasks`, and `skip_reason` (the last skip in plan order, as the serial loop sets it).
The digests reach `_build_synthesis_context` in plan order, whatever the finish order (AC-4).

### Turn deadline (FRE-1397) on the parallel path

The deadline check runs inside `_dispatch_task`, **after** the task holds a semaphore slot. The
task reads `remaining = deadline - now` at that moment:

* `remaining <= 0` → the task is skipped and recorded in `skipped_tasks` (`turn_budget`), with no
  `SubAgentResult`.
* otherwise `max_deadline_seconds = remaining`, the same cap as the serial path.

A task that waits for a slot waits for a sibling that holds a deadline `<= remaining`. So the
wait ends at most one sibling's overhead after the turn deadline, and the task then skips.

The overhead is the same as on the serial path today: the `SUB_AGENT` phase-start emit, the
synchronous setup in `run_sub_agent` before its `wait_for` (no `await` between the function entry
and the `wait_for`), and the post-`wait_for` cleanup. The parallel path adds no new term.
(Codex review: the old wording named the cleanup only. That was incomplete for both paths.)

### Admission (FRE-1374)

There is no admission timeout and no "not admitted" result. A task waits on the semaphore
without a timer. When it gets a slot it either runs with a fresh deadline cap or skips with a
recorded reason. No path drops a task silently: every plan task ends as a result, a caught
exception (in `dispatch_intervals` and the partial-failure log), or a `skipped_tasks` entry.
The limit is at most the inference controller's own ceiling for the deployment, so siblings
never queue for an inference slot because of each other. A slot held by another session is
waited for inside `run_sub_agent`, under that task's deadline cap — the same as on the serial
path today.

### Origin error (FRE-1501) on the parallel path

The `origin_failed` flag is shared (a `nonlocal`) and is a **one-way latch**: code only sets it to
`True`, never back to `False`. Today the replacement path assigns
`origin_failed = replacement.stop_reason == "origin_error"`. On the serial path the flag is always
`False` at that point, so the latch is equivalent. On the parallel path the old assignment could
clear a flag that a sibling set (Codex review). A sibling already in flight runs to its end. A
task that gets a slot after the flag is set skips with `origin_error`.

### Phases

Each child opens its own `SUB_AGENT` `phase_span` with `parent_id` = the `EXPANSION` span id. Each
span has its own `phase_id`, `PHASE_START` and `PHASE_END`. `phase_span` keeps no shared state,
and its docstring already states the `asyncio.gather` child case (ADR-0123 §1/AC-8). The spans
overlap on the parallel path.

### Exceptions and cancellation

`_dispatch_task` catches `Exception` around `run_sub_agent`, as today. A raise outside that
`try` (a bug in grant lookup, for example) aborts the turn today, and later tasks never start.

On the parallel path no child raises. Each child catches `Exception` from `_dispatch_task` and
records it in `fatal[plan_index]`. A task that gets a slot while `fatal` is not empty starts
nothing. Siblings already in flight run to their end, under their deadline caps. After the task
group, if `fatal` is not empty, the scheduler raises `fatal[min(fatal)]`: one original exception,
the lowest plan index, never an `ExceptionGroup` (Codex review: a single-member unwrap was not
enough, because two children can fail at the same time). The task group then cancels children
only on turn cancellation, the same path as today.

**Fold-in — permit leak on cancellation** (`llm_client/concurrency.py`, `_PrioritySemaphore.acquire`).
The method removes its waiter on `TimeoutError` only. On `CancelledError` the waiter stays queued.
A later `release()` hands the permit to the cancelled waiter, and nobody releases it. This is live
today: the worker's `wait_for` deadline cancels an LLM call that waits for an inference slot.
The fix: on `CancelledError`, remove the waiter, or pass on a grant that already arrived, then
re-raise. The code is synchronous (no `await`), so a second cancellation cannot interrupt it.

### Cost gate under concurrency

No change. `CostGate.reserve` runs in one Postgres transaction that takes `SELECT … FOR UPDATE`
on every cap row for the role. Two concurrent reservations serialize on those row locks.
(Codex review: the rows are locked one at a time in configuration order. The sort by `id` runs
after the locks. Helpers in one turn reserve for the same role, so they lock in the same order.
The `_lock_counters` docstring claims `id` order. That is a separate, pre-existing inaccuracy,
noted in the handoff.) The second one reads the first one's increment and
checks `running_total + amount <= cap_usd`. So the sum of concurrent reservations cannot exceed
any cap. A difference from the serial path: serial workers commit (and so release the gap
between estimate and actual) before the next worker reserves. Parallel workers hold their
estimates at the same time. Near a cap, a parallel worker can get `BudgetDenied` where a serial
one would pass. That fails closed, and the worker reports its failure as today.

### Shared state

`run_sub_agent` builds its own messages, context and trace span per call. The pre-FRE-1380 code
ran it under `asyncio.gather`. `result.dispatched_count += 1` is safe: it is one event loop and
the statement has no `await`.

## Steps

1. **Tests first** — `tests/personal_agent/orchestrator/test_expansion_controller.py`, new class
   `TestCloudParallelDispatch`. A fake client: `SimpleNamespace(placement=Placement.CLOUD,
   model_key="fake-cloud")`, plus a real `InferenceConcurrencyController` with
   `register_model("fake-cloud", max_concurrency=10, provider="fake")`, patched in as
   `expansion_controller.get_inference_concurrency_controller`.
   * AC-1 `test_cloud_helpers_overlap`: 3 tasks of 0.2 s. Assert from the recorded intervals:
     the latest start is before the earliest end, and the dispatch takes under 0.4 s.
   * AC-2 `test_local_helpers_run_one_after_another`: the same with `Placement.LOCAL`. Assert
     no overlap and at least 0.6 s.
   * AC-3 `test_deadline_skips_without_a_fabricated_result[local|cloud]`: 4 tasks of 0.1 s, the
     cloud ceiling set to 2, deadline 0.05 s. Assert that tasks 2 and 3 are in `skipped_tasks`,
     no result names them, and each call got `max_deadline_seconds <= 0.05`. Plus: an
     already-passed deadline on the cloud path skips everything and calls nothing.
   * AC-4 `test_digests_reach_synthesis_in_plan_order`: cloud, task durations 0.15/0.10/0.05
     (finish order 2, 1, 0). Through `execute()`. Assert the finish order is reversed and that
     `sub_agent_results`, `dispatch_intervals` and the `### task_i` headers in
     `synthesis_context` are in plan order.
   * Limit tests for `_fan_out_limit`: deployment ceiling 2 → 2; `expansion_budget=1` → 1;
     `expansion_budget=0` → 1; no `placement` → 1; unregistered deployment → 1.
   * Origin error on the cloud path, ceiling 2, 3 tasks: task 0 returns `origin_error`
     at once, task 1 runs, task 2 skips with `origin_error`.
   * `execute` forwards `expansion_budget` to `_run_dispatch`.
   * A raise outside `run_sub_agent` on the cloud path keeps its type. Two concurrent raises
     surface the lowest plan index.
   * The origin latch: a non-origin gap replacement that ends after a sibling's `origin_error`
     does not clear the flag.
   * A gap replacement holds its task's slot: never more than the limit in flight, and the
     replacement follows its original in `sub_results`.
   * The deadline test uses a stub that honors `max_deadline_seconds`, and asserts the
     dispatch wall time.
   * `tests/personal_agent/llm_client/` (the concurrency test file): a cancelled waiter does not
     leak a permit, in both the queued case and the granted-then-cancelled case.
   * Verify: `make test-file FILE=tests/personal_agent/orchestrator/test_expansion_controller.py`
     fails on the new tests only.
2. **Implement** the `_PrioritySemaphore.acquire` cancellation fix, then in
   `src/personal_agent/orchestrator/expansion_controller.py`: `_fan_out_limit`,
   `_TaskOutcome`, the `_dispatch_task` extraction, the two schedulers, the plan-order merge, the
   `expansion_budget` parameter, and the docstring updates (`_run_dispatch`,
   `ExpansionResult.dispatch_intervals`, `skipped_tasks`).
   Verify: the same test file passes, with the FRE-1380/FRE-1397 classes unchanged (AC-5).
3. **Docs:** update the stale "serialized" statements that this changes. Find them with
   `grep -rn "serializ" docs/ src/personal_agent/orchestrator/ --include=*.md --include=*.py`.
4. **Gates:** `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`.

## Acceptance criteria → proof

| AC | Proof |
|----|-------|
| AC-1 cloud fan-out takes about one task's time | `test_cloud_helpers_overlap` |
| AC-2 `slm_local` runs one after another | `test_local_helpers_run_one_after_another` |
| AC-3 deadline skips, no fabricated result, both paths | `test_deadline_skips_without_a_fabricated_result[local,cloud]` + cloud already-passed deadline |
| AC-4 plan order when finish order is reversed | `test_digests_reach_synthesis_in_plan_order` |
| AC-5 FRE-1380/FRE-1397 tests unchanged for local | `TestSerializedDispatch`, `TestTurnBudgetBound`, `TestOriginErrorStopsDispatch` pass with no edit |
| AC-6 live overlap in `session_events` | master, post-deploy (runbook in the handoff) |

## Deploy

`seshat-gateway` rebuild, do-and-report (ticket). Diff class: escalated — the change is in the
production turn path and in cost-bearing dispatch.
