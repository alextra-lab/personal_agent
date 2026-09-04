# FRE-1380 — Serialize the sub-agent fan-out

Backing ADR: ADR-0036 (expansion-controller). Owner decision 2026-09-04: "Serialize." Goal is
context management, not latency.

## Scope confirmation (before coding)

- `expansion_controller.py:420-490` — the enforced-mode dispatch block. This is the fan-out that
  fires by default (`settings.orchestration_mode` defaults to `"enforced"`). **In scope.**
- `expansion.py:79-145` (`execute_hybrid`) — checked directly: this is **also** a concurrent
  `asyncio.gather` fan-out with a semaphore, not a single call as the ticket assumes. It only
  fires under `orchestration_mode == "autonomous"` (non-default). **Left untouched per the
  ticket's explicit scope; the factual discrepancy is flagged in the PR/handoff and a Backlog
  ticket is filed** — this is a materially separate call site with its own tests, and widening
  silently would contradict "Do not widen."
- AC-4/AC-5 ask for numbers from a real live multi-task expansion. Confirmed unavailable from
  this build worktree: `:8000`/`:9000` refuse connections, `make ps` fails
  (`GRAFANA_ADMIN_PASSWORD` missing — this worktree isn't provisioned for the full stack), and a
  direct query against the real `agent-logs-2026-08`/`2026-09` ES indices returns zero
  `hybrid_expansion_start` / `sub_agent_complete` hits — no historical run exists either. The
  code will emit everything needed to answer both ACs from one real call; the actual numbers are
  a post-deploy step for master (commands below).

## Steps

1. **`src/personal_agent/orchestrator/expansion_types.py`** — add `SubAgentInterval` (frozen
   dataclass: `task_name: str`, `start_monotonic: float`, `end_monotonic: float`) so AC-1 has a
   real, independently-checkable record of each sub-agent's wall-clock window.

2. **`src/personal_agent/orchestrator/expansion_controller.py`**
   - `ExpansionResult`: add `dispatch_intervals: list[SubAgentInterval] = field(default_factory=list)`.
   - `_run_dispatch`: replace the semaphore + `asyncio.gather` + admission-race block
     (`dispatch_semaphore`, `_not_admitted_result`, `_NOT_ADMITTED_ERROR_PREFIX`,
     `admission_budget`, the ceiling lookup) with a plain sequential `for` loop over
     `zip(plan.tasks, specs, strict=True)`. Per iteration: record `interval_start =
     time.monotonic()`, `try`/`except Exception` (not `BaseException`, so a genuine
     `CancelledError` still propagates and stops the loop) the `phase_span(SUB_AGENT)`-wrapped
     `run_sub_agent` call, appending the `SubAgentResult` or the caught exception to
     `raw_results` — then in a `finally` (codex review: an `except`-only append skips the
     interval when the call raises) append `SubAgentInterval(task.name, interval_start,
     time.monotonic())` regardless of outcome, so a failed task is still represented in the
     timeline. Keep the existing `isinstance(r, SubAgentResult)` filter + the
     `expansion_dispatch_partial_failure` warning log (codex review: don't drop this — it's
     the only signal a raw non-`SubAgentResult` exception occurred). Log
     `expansion_dispatch_intervals` once with the full interval list (relative to
     `dispatch_start`) — this is AC-1's instrumentation. Remove the now-dead `uuid4` and
     `get_inference_concurrency_controller` imports. Rewrite the docstring and the
     ADR-0123-§1/AC-8 comment block above the loop — both currently describe N *concurrent*
     children and a `gather()` — to describe sequential dispatch instead.
   - Delete the `not_admitted_count` / `report_degradation("Fan-out window exceeded...")` block —
     structurally impossible now (AC-3).

3. **`src/personal_agent/config/settings.py`** — delete the `worker_global_timeout_seconds`
   field (its only reader was the deleted admission race), and edit
   `worker_hard_deadline_seconds`'s own description (it currently name-checks
   `worker_global_timeout_seconds` in its last sentence — codex review caught this; the plan's
   own grep check would otherwise still find a hit) so no reference to the deleted field
   survives. Confirm via `grep -rn worker_global_timeout_seconds src/` returning nothing (AC-3).

4. **Tests — `tests/personal_agent/orchestrator/test_expansion_controller.py`**
   - Delete `TestFanOutRespectsCeiling` and `TestAdmittedWorkerIndependentOfGlobalBound` — both
     test the removed concurrency-ceiling/admission-race behavior.
   - Add `TestSerializedDispatch`:
     - `test_intervals_never_overlap` (AC-1): 3-task plan, `run_sub_agent` stub sleeps a fixed
       amount per task and independently records its own `(observed_start, observed_end)` via
       `time.monotonic()`. Assert: `result.dispatch_intervals` has 3 entries, in plan order;
       `intervals[i+1].start_monotonic >= intervals[i].end_monotonic` for each consecutive pair
       (not an all-pairs check — order matters); and each controller-recorded interval
       brackets the stub's independently-observed window
       (`interval.start_monotonic <= observed_start` and `interval.end_monotonic >=
       observed_end`) — this is the codex-flagged cross-check that stops a mis-wired recording
       from reporting a falsely non-overlapping timeline.
     - `test_interval_recorded_even_on_failure` (AC-1 completeness): stub raises on task 2 of 3,
       succeeds on 1 and 3; assert `dispatch_intervals` still has 3 entries (the `finally`
       guarantee) and the loop continued past the raise.
     - `test_max_observed_concurrency_is_one` (AC-1, belt-and-braces): reuse the existing
       `_tracking_run_sub_agent`-style stub (in-flight counter) and assert
       `max(observed) == 1`.
     - `test_all_tasks_admitted_beyond_old_ceiling` (AC-2): hand-built plan with 8 tasks
       (exceeds HYBRID's 4 and DECOMPOSE's 6 old caps — `_run_dispatch` is called directly, so
       `_validate_plan_json`'s cap doesn't apply); assert `len(results) == 8`, every result
       `success is True`, no `error` contains `"Not dispatched"`, and `expansion_result.degraded
       is False`.
   - Add `test_synthesis_context_never_includes_full_output` (AC-4 lock): build a
     `SubAgentResult` with a short `summary` and a long, distinct `full_output`; assert the
     synthesis context contains the summary text and does **not** contain the full-output text.
   - `TestExpansionPhaseEvents`: update the now-inaccurate "staggered... not lockstep" comment
     and class docstring's "dispatch concurrency + phase pairing" → "phase pairing" (the
     assertions themselves — parent-ends-last, distinct phase ids, count — still hold
     unchanged under sequential dispatch, confirmed by inspection).

5. **`docs/architecture_decisions/ADR-0036-expansion-controller.md`** — update the state-machine
   diagram's dispatch box ("spawn sub-agents in parallel / per-worker + global timeout" →
   "dispatch sub-agents sequentially / per-worker timeout only").

6. **PR body / Linear handoff** — state explicitly:
   - AC-1, AC-2, AC-3, AC-4 (lock half) are proven by the new tests above.
   - AC-4 (numbers) / AC-5: not measurable from this build worktree (evidence above). Hand
     master the exact commands: trigger one real HYBRID query with ≥3 tasks through the deployed
     service, then `curl` the `agent-logs-*` index for that trace's `sub_agent_complete` events
     (`full_output_chars`, `digest_chars`) and `expansion_dispatch_intervals` (wall-clock), and
     compare total dispatch wall-clock against the pre-change baseline computable from
     `docs/reference/SLM_SERVER_CLIENT_SEMANTICS.md`'s own benchmark (34.2 vs 40.6 tok/s,
     2.57x per-request slowdown at concurrency 3 — the ticket's own cited numbers).
   - File the Backlog ticket for `expansion.py`'s untouched autonomous-mode concurrent fan-out.

## Verification

- `make test-file FILE=tests/personal_agent/orchestrator/test_expansion_controller.py`
- `make test` (full suite)
- `make mypy` / `make ruff-check` / `make ruff-format`
- `grep -rn "worker_global_timeout_seconds\|_NOT_ADMITTED_ERROR_PREFIX\|_not_admitted_result" src/` → no hits
