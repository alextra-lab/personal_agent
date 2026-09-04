# FRE-1374 — sub-agent fan-out ignores the concurrency ceiling, and the 60s budget is charged for queue time

## Ticket
https://linear.app/frenchforest/issue/FRE-1374

**Revision history on this plan:**
1. Codex plan-review replaced an original wave-batching design for Defect 2 with a ceiling-sized
   semaphore — simpler, avoids a slow-wave-member blocking an otherwise-free slot, avoids a
   shrinking-timeout-budget-across-waves mechanism.
2. Master's ticket amendment (04:57) flagged that a per-worker hard deadline must reason about
   `worker_global_timeout_seconds` — first addressed by sizing the two to fit together
   arithmetically.
3. **Owner direction (05:04, verbatim): "The timeout need be applied to each individual subagent
   session. not globally."** This superseded (2) entirely: `worker_global_timeout_seconds` must
   never truncate an *admitted* worker, no matter how the arithmetic works out — a per-worker
   deadline sized to "fit inside" the global bound was still the same defect one level up, since a
   late-admitted worker's own budget would still be cut short by a wall shared with everyone else.
   The final design (below) decouples the two entirely instead of sizing one against the other.

## Scope (from the ticket)

Two defects in `ExpansionController._run_dispatch` (the enforced-mode HYBRID/DECOMPOSE path):

1. **Defect 1** — `run_sub_agent`'s outer `asyncio.wait_for(..., timeout=spec.timeout_seconds)`
   wraps the whole `llm_client.respond()` call, including any wait for a concurrency slot. A
   sub-agent that queues for 22.8s is left with 37.2s of the nominal 60s budget, then killed —
   and the timeout message still reports 60.0s.
2. **Defect 2** — the controller fans out `len(plan.tasks)` sub-agents (up to 4 for HYBRID, 6 for
   DECOMPOSE) with no reference to the sub-agent deployment's concurrency ceiling
   (`slm_local` = 3 today). Any plan larger than the ceiling guarantees a queue.

Owner direction folded both defects into one requirement: **no worker may ever be killed for time
it spent waiting, or for time another worker spent working.** Each sub-agent's clock is its own,
full stop.

Out of scope: `orchestrator/expansion.py` (`execute_hybrid`, the `orchestration_mode=autonomous`
path) — not the default mode, not in the ticket's reference list. It already bounds fan-out with
its own `asyncio.Semaphore`, sized from `expansion_budget_max` (max decomposition depth) rather
than the real deployment ceiling — the same latent mismatch, in a mode this ticket doesn't cover.
Noted in the handoff as a possible future ticket, not fixed here.

## Design

### Defect 1 — a worker's own timeout starts at concurrency-slot acquisition

`LiteLLMClient.respond()` already supports exactly this split and it is unused today. Verified
(including via codex review) for both dispatch branches:
- Cloud: `timeout_s` → `litellm_kwargs["timeout"]` built at `litellm_client.py:906`, consumed by
  `litellm.acompletion()` inside the `request_slot` block at `litellm_client.py:990`/`1017`.
- Local: `timeout_s` → `httpx.Timeout(read=effective_timeout_s, ...)` built at
  `litellm_client.py:1427`, consumed inside `request_slot` at `litellm_client.py:1483`, by the
  streaming call at `1520-1522`.

Neither timeout starts while `request_slot` is waiting for a slot — both start strictly after
acquisition. Caveat from review: locally this is a **read-inactivity** timeout on a streaming
call (llama.cpp emits `stream_options.include_usage` chunks), not a hard total-generation cap.
That is unchanged from today.

Changes:
- `run_sub_agent` passes `timeout_s=spec.timeout_seconds` to `llm_client.respond(...)` (currently
  not passed at all).
- `SubAgentSpec` gains `hard_deadline_seconds: float | None = None` — a separate, larger,
  explicitly-named per-worker safety net for a client that ignores `timeout_s`. `None` falls back
  to `timeout_seconds` (today's exact behavior for the out-of-scope `expansion.py` path).
- `run_sub_agent`'s outer `asyncio.wait_for` uses
  `max(spec.hard_deadline_seconds or spec.timeout_seconds, spec.timeout_seconds)` — a clamp, so a
  misconfigured/smaller `hard_deadline_seconds` can never sit *below* the generation budget it's
  supposed to bound (review finding: no invariant existed in the first draft).
- New setting `worker_hard_deadline_seconds: float = 85.0` (60s generation + 25s queue-wait
  absorption — the live incident's worst observed wait was 22.8s). **This value needs no
  arithmetic relationship to `worker_global_timeout_seconds`** — see Defect 2 below for why.
- On the outer `asyncio.TimeoutError` (`run_sub_agent`), the error message reports the *measured*
  `duration_ms`, not the nominal budget: `f"Timeout after {duration_ms / 1000:.1f}s"` (was
  `f"Timeout after {spec.timeout_seconds}s"`). This is AC-2's fix.

### Defect 2 — fan-out respects the ceiling; the global bound only ever gates admission

`InferenceConcurrencyController.effective_ceiling(role: str | None, default: int) -> int`
(`llm_client/concurrency.py`) returns the tighter of the deployment's own sub-limit and its
provider's ceiling — the same two constraints `request_slot` already enforces in sequence.
`default` is **required**, not baked into the controller: an unresolvable role (e.g. a test's
bare `AsyncMock`, whose `model_key` auto-vivifies to a non-matching `Mock`) returns exactly
`default`, never a silently-guessed ceiling. `_run_dispatch` passes `default=len(specs)` — "if the
ceiling can't be determined, don't constrain," matching today's behavior for every existing test
double.

`_run_dispatch` gates fan-out through `asyncio.Semaphore(max(1, ceiling))`. The critical design
change (owner direction): **`worker_global_timeout_seconds` no longer wraps the dispatch as a
group.** It bounds *only* how long an individual worker may wait for a ceiling slot:

```
admission_budget = worker_global_timeout_seconds - (now - dispatch_start)
if admission_budget <= 0: report "not dispatched", stop — never call run_sub_agent
try: await wait_for(semaphore.acquire(), timeout=admission_budget)
except TimeoutError: report "not dispatched", stop
# Admitted. From here on, NOTHING re-checks worker_global_timeout_seconds against this
# worker — its clock is entirely its own (worker_timeout_seconds / worker_hard_deadline_seconds,
# inside run_sub_agent), independent of every other worker's timing.
async with phase_span(...): return await run_sub_agent(...)
```

This is why `worker_hard_deadline_seconds` and `worker_global_timeout_seconds` no longer need
sizing against each other (superseding revision 2 above): an admitted worker's own deadline is
never compared to the global one again. A worker that never gets admitted within the window is
not silently dropped — it comes back as an explicit failed `SubAgentResult` (error prefixed
`"Not dispatched"`), and the expansion result is marked `degraded` with a specific reason,
satisfying AC-3's "reported as not run."

A structural side effect: the old `except asyncio.TimeoutError: sub_results = []` branch (which
discarded *every* result, including ones that had already completed, whenever the group-level
timeout fired) no longer exists — there is no more group-level timeout to raise it. This was a
pre-existing bug the earlier (codex-reviewed) design deliberately left out of scope; the owner's
redesign resolves it as a side effect rather than as a targeted fix.

## Files

- `src/personal_agent/config/settings.py` — add `worker_hard_deadline_seconds`; rewrite
  `worker_global_timeout_seconds`'s docstring (its meaning changed from "bounds the whole
  dispatch" to "bounds only the admission wait").
- `src/personal_agent/orchestrator/sub_agent_types.py` — add `SubAgentSpec.hard_deadline_seconds`.
- `src/personal_agent/orchestrator/sub_agent.py` — pass `timeout_s`; clamp+use hard deadline for
  outer wait_for; fix the timeout error message.
- `src/personal_agent/orchestrator/expansion_controller.py` — ceiling-sized semaphore with a
  per-task admission-window timeout (not a group-level one) in `_run_dispatch`; a `_not_admitted_result`
  helper; degradation reporting for the not-admitted case.
- `src/personal_agent/llm_client/concurrency.py` — `InferenceConcurrencyController.effective_ceiling`.
- Tests (new/updated, TDD — failing first):
  - `tests/personal_agent/orchestrator/test_sub_agent.py` — AC-1/AC-2: `timeout_s` passed through;
    a call that runs longer than the old single nominal budget still completes (hard-deadline
    clamp); the outer hard-deadline path reports measured duration, not the nominal budget; the
    clamp holds even when `hard_deadline_seconds` is misconfigured below `timeout_seconds`.
  - `tests/personal_agent/orchestrator/test_expansion_controller.py::TestFanOutRespectsCeiling` —
    AC-3/AC-4: ceiling seeded low with a plan calling for more — every task completes, none time
    out, observed max concurrency tracks the ceiling; ceilings 2 vs 4 on an identical plan produce
    different observed concurrency; an unresolvable role does not constrain dispatch.
  - `tests/personal_agent/orchestrator/test_expansion_controller.py::TestAdmittedWorkerIndependentOfGlobalBound`
    — AC-1 (owner direction): an admitted worker (ceiling covers all specs — no queuing race to
    make this flaky) is held open via an `asyncio.Event`, not a real sleep-vs-sleep race, well past
    the nominal global window, and still completes; a worker that never gets admitted within the
    window is reported as a failed/not-run result and the result is marked degraded.
  - `tests/test_llm_client/test_concurrency.py` — `effective_ceiling` unit tests: registered role
    (min of model/provider limits), unregistered role (returns caller's `default`), `role=None`.

  **Test-timing note**: two of the tests above assert real-clock behavior against a background VPS
  that runs other concurrent sessions. A first attempt using `asyncio.sleep`-vs-`asyncio.sleep`
  races with ~50-200ms margins was flaky under load — not a code bug (isolated repros confirmed
  the mechanism is correct; the flakiness traced to (a) real scheduler jitter on a loaded shared
  host and (b) a cold-import penalty when `phase_span`'s module hadn't been touched yet by an
  earlier test in the same run). Fixed by: pre-warming that import at module load in the test
  file, using `asyncio.Event`-based holds instead of sleep-vs-sleep races wherever a real timeout
  boundary is being crossed, and generous (order-of-magnitude) margins. Verified stable across 5+
  runs both in isolation (`pytest ... -k`) and as part of the full file.

## Acceptance criteria mapping

- **AC-1**: `TestAdmittedWorkerIndependentOfGlobalBound` — an admitted worker is never killed by
  the global bound, however long it or another worker takes.
- **AC-2**: `test_sub_agent.py` — outer hard-deadline timeout path reports measured duration.
- **AC-3**: `TestFanOutRespectsCeiling` — ceiling seeded low, plan calls for more, every dispatched
  worker completes; `TestAdmittedWorkerIndependentOfGlobalBound::test_never_admitted_worker_is_reported_not_run`
  — a worker that can't be admitted is reported as not-run, not silently dropped.
- **AC-4**: `TestFanOutRespectsCeiling::test_ceiling_change_changes_observed_concurrency` —
  ceilings 2 vs 4 on the same plan produce different observed concurrency.

## Verification

- `make test-file FILE=tests/personal_agent/orchestrator/test_sub_agent.py`
- `make test-file FILE=tests/personal_agent/orchestrator/test_expansion_controller.py`
- `make test-file FILE=tests/test_llm_client/test_concurrency.py`
- `make test`
- `make mypy`
- `make ruff-check` / `make ruff-format`
- `pre-commit run --all-files`

## Fold-ins

None expected. `run_sub_agent`'s existing unused `concurrency_controller` parameter (dead since
ADR-0141 T3 re-homed the controller as a process-wide singleton) is pre-existing dead code, not
touched by this ticket per CLAUDE.md §3 — flagged in the PR/handoff, not removed.

## Diff class

Escalate. Production dispatch/concurrency path for HYBRID/DECOMPOSE turns, in the turn path by
default (`orchestration_mode=enforced`).

## Review notes applied

**Codex plan-review:**
1. Confirmed the `timeout_s`/slot-acquisition ordering for both branches.
2. Confirmed the hard-deadline-as-safety-net shape; existing mock-based tests updated, not the
   outer bound removed.
3. Flagged that strict wave-batching is weaker than a ceiling-sized semaphore. Adopted the
   semaphore design.
4. Flagged that an unregistered/mocked role falling back to a baked-in default would silently
   change dispatch timing in every existing test using a bare mock client. Fixed: `effective_ceiling`
   takes a required `default` from the caller.
5. Confirmed `expansion.py`/autonomous-mode is correctly out of scope; noted its own latent
   ceiling-vs-`expansion_budget_max` mismatch (not folded in — different code path this ticket
   doesn't touch).

**Owner direction (superseded the "fit inside the global bound" approach entirely):** see
Revision 3 at the top. The global bound now gates admission only, never an admitted worker's own
execution.
