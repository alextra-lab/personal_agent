# FRE-1381 — Serialize `execute_hybrid`'s autonomous-mode fan-out

Backing precedent: FRE-1380 (owner decision 2026-09-04, "Serialize... the goal is context
management, not latency"). ADR-0036 documents `orchestration_mode == "autonomous"` as a live
research path (Decision 5) — this ticket does not touch that decision, only the internal
dispatch mechanics of `execute_hybrid()`, the same class of change FRE-1380 made to
`expansion_controller.py`'s enforced-mode dispatch.

No formal Acceptance Criteria section exists on FRE-1381 (only a "Suggested scope"). Deriving
outcome-level criteria from that scope and from FRE-1380's own ACs, to state explicitly in the
PR/handoff:

- **AC-1**: sub-agent calls in `execute_hybrid` never overlap — proven by real timestamps, not
  by reading the loop structure.
- **AC-2**: partial failure still returns every result (unchanged from today).
- **AC-3**: `max_concurrent` — the parameter that only ever bounded the deleted semaphore — is
  removed, and its one caller (`executor.py`) is updated to match, rather than left as a dead
  knob that reads load-bearing and is not (the FRE-1379/1380 precedent this repo already
  avoids).
- **AC-4**: real-timestamp interval evidence is logged (`hybrid_expansion_intervals`), matching
  FRE-1380's `SubAgentInterval` instrumentation, reusing the existing frozen dataclass in
  `expansion_types.py` rather than inventing a second one.

## Scope confirmation

- `src/personal_agent/orchestrator/expansion.py:79-145` (`execute_hybrid`) — in scope.
- `src/personal_agent/orchestrator/executor.py:6171-6177` — the only call site; its
  `max_concurrent=max_sub` argument must be dropped when the parameter is removed.
- `settings.expansion_budget_max` — NOT touched. It is read in 3 places
  (`expansion.py`, `request_gateway/pipeline.py`, `service/app.py`); only `expansion.py`'s use
  of it was concurrency-specific (the semaphore-size fallback). The other two uses are
  unrelated (decomposition-depth budget) and stay exactly as they are.
- ADR-0036 — not touched. Its state diagram already collapses the autonomous branch to a
  single opaque box ("Current behavior (LLM decides)"); unlike FRE-1380's enforced-mode
  dispatch box, there is no autonomous-mode dispatch detail in the diagram to correct.

## Codex plan-review findings (applied below)

- **`run_sub_agent` is NOT exception-safe before its own internal `try`.** Setup code in
  `sub_agent.py` (summarizing input context, logging, `get_shared_tool_execution_layer()`,
  `_build_tool_defs()`) runs *before* the catch-all `try` at `sub_agent.py:659` and can raise
  directly. The original plan's claim that only `CancelledError` escapes was wrong. FRE-1380's
  own loop (`bcd7abd7:expansion_controller.py:437-442`) wraps the call in `try/except
  Exception ... finally` for exactly this reason — the sequential loop here needs the same
  shape, not a bare `await`.
- **A second caller was missed**: `tests/personal_agent/orchestrator/test_gateway_integration.py:175-180`
  calls a mocked `execute_hybrid` with `max_concurrent=2`. It is an `AsyncMock` with no `spec=`,
  so it will not fail on the extra kwarg, but leaving it would misdocument the real call shape
  the test is simulating — update it alongside the two other call sites.
- **Test coverage gaps** vs. FRE-1380's own suite: no many-task regression, no raw (pre-catch)
  exception mid-batch case, no cross-check of the logged interval against an independently
  observed call window. Added below.
- **Doc staleness is real but pre-existing and out of scope**: `docs/specs/
  COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` §4.7/4.8, ADR-0094/0095/0123, and
  `config/model_roles.yaml:153` already describe HYBRID as N-concurrent-workers *after*
  FRE-1380 serialized the (production-default) enforced-mode path without updating them — this
  ticket does not fix that corpus (a multi-doc alignment sweep, not a fold-in); file a Backlog
  ticket instead (Step 5). The two `docs/research/*` hits (an eval dataset description, a past
  run's brief) are historical snapshots, not executable — confirmed no `scripts/eval/` code
  reads `max_concurrent` or `CP-17` — so they need no code-adjacent fix either.

## Steps

1. **`src/personal_agent/orchestrator/expansion.py`**
   - Drop the `import asyncio` semaphore/gather usage; add `import time`.
   - `execute_hybrid`: remove the `max_concurrent: int | None = None` parameter and the
     `asyncio.Semaphore` + `_run_with_semaphore` + `asyncio.gather` block. Replace with a plain
     sequential `for spec in specs` loop, matching FRE-1380's shape: record `interval_start =
     time.monotonic()`; `try: sub_result = await run_sub_agent(...)` `except Exception as exc:`
     append `exc` to a `raw_results` list `else:` append `sub_result` `finally:` append
     `SubAgentInterval(spec.task[:80], interval_start, time.monotonic())` regardless of outcome
     (a genuine `CancelledError`, a `BaseException`, still propagates and stops the loop,
     matching today's cancellation behavior). After the loop, filter `raw_results` down to
     `isinstance(r, SubAgentResult)` for the return value — a raw exception is dropped here,
     matching the prior `asyncio.gather(..., return_exceptions=False)`'s behavior of never
     surfacing one as a result (it would have propagated the whole batch before; now the
     surviving tasks still complete, a strict improvement documented in the PR, not silently).
   - After the loop, log `hybrid_expansion_intervals` once with the full interval list
     (`task`, `start_s`/`end_s` relative to a `dispatch_start = time.monotonic()` captured
     before the loop) — same shape as `expansion_controller.py`'s
     `expansion_dispatch_intervals` event, so both fan-out paths are queryable the same way.
   - Import `SubAgentInterval` from `personal_agent.orchestrator.expansion_types`.
   - Update the module docstring (line 5, "runs them concurrently") and the function's
     docstring (currently "Uses an asyncio.Semaphore to limit concurrent sub-agent calls") to
     describe sequential dispatch.

2. **`src/personal_agent/orchestrator/executor.py:6171-6177`** — drop the
   `max_concurrent=max_sub,` argument from the `execute_hybrid(...)` call (parameter no longer
   exists). `max_sub` is still used above for `parse_decomposition_plan`'s `max_sub_agents=`
   — unaffected.

3. **Tests — `tests/personal_agent/orchestrator/test_expansion.py`**
   - Remove `max_concurrent=2` / `max_concurrent=1` kwargs from the three existing
     `TestExecuteHybrid` calls (API no longer accepts it); `test_respects_max_concurrent`
     (currently asserts `max_observed <= 1` under `max_concurrent=1`) is replaced outright —
     serialization makes concurrency-*bounding* meaningless, so the successor test asserts
     *actual* sequential execution instead.
   - Add `test_tasks_run_sequentially_not_concurrently` (AC-1): a `run_sub_agent`-shaped stub
     (patch `personal_agent.orchestrator.expansion.run_sub_agent`) that independently records
     its own `(time.monotonic() start, end)` per call; run 3 specs through `execute_hybrid`;
     assert each call's recorded start is `>=` the previous call's recorded end (order-specific,
     not an all-pairs check) — proof from real timestamps, not from reading the loop. Also
     cross-check the *logged* interval (captured via `structlog.testing.capture_logs()`)
     brackets each stub-observed `(start, end)` pair — the FRE-1380-style guard against a
     mis-wired recording producing a falsely non-overlapping timeline.
   - Add `test_max_observed_concurrency_is_one` (AC-1, belt-and-braces): in-flight counter stub
     (same shape as FRE-1380's), assert `max(observed) == 1` across 3 specs.
   - Add `test_intervals_logged_for_every_spec` (AC-4): wrap the `execute_hybrid` call in
     `structlog.testing.capture_logs()`; assert one `hybrid_expansion_intervals` event exists
     whose `intervals` list has one entry per spec, in spec order.
   - Add `test_raw_exception_mid_batch_does_not_abort_loop` (AC-2, strengthened per codex
     finding #2): stub `run_sub_agent` to *raise* `RuntimeError` directly (not a caught-internal
     failure converted to a `SubAgentResult`) on the second of 3 specs; assert the loop
     continues to the third spec, the raising spec is absent from the returned results (2 of 3
     `SubAgentResult`s), and all 3 intervals were still recorded (the `finally` guarantee).
   - Add `test_many_tasks_all_succeed` (regression, no artificial cap): 8 specs, all succeed via
     a simple stub; assert `len(results) == 8` and `all(r.success for r in results)` — proves
     removing the semaphore did not introduce any other implicit ceiling.
   - `test_partial_failure_returns_all_results` (AC-2): unchanged assertions, only the
     `max_concurrent=2` kwarg removed — still proves one *caught* failure doesn't abort the
     batch (distinct from the new raw-exception test above).

4. **`tests/personal_agent/orchestrator/test_gateway_integration.py:175-180`** — drop
   `max_concurrent=2` from the mocked `execute_hybrid(...)` call so the simulated call shape
   matches the real one after step 2.

5. **Backlog ticket** (filed at handoff, not implemented here): consolidate the doc-staleness
   finding — `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` §4.7/4.8, ADR-0094/0095/0123,
   `config/model_roles.yaml:153` — all still describe HYBRID/expansion as N-concurrent-workers,
   stale since FRE-1380 and now doubly stale after FRE-1381. One consolidated doc-alignment
   ticket, not fixed in either serialization PR.

## Verification

- `make test-file FILE=tests/personal_agent/orchestrator/test_expansion.py`
- `make test-file FILE=tests/personal_agent/orchestrator/test_gateway_integration.py`
- `make test` (full suite — confirm `executor.py`'s call site still passes)
- `make mypy` / `make ruff-check` / `make ruff-format`
- `rg '\bexecute_hybrid\b|\bmax_concurrent\b' .` → only the (now-parameterless) definition/call
  sites and the plan/PR text itself; no stray `max_concurrent=` kwarg anywhere
