# FRE-1374 — sub-agent fan-out ignores the concurrency ceiling, and the 60s budget is charged for queue time

## Ticket
https://linear.app/frenchforest/issue/FRE-1374

**Revised after codex plan-review** (see review notes at the end). Original wave-batching design
for Defect 2 replaced with a ceiling-sized semaphore — simpler, avoids a slow-wave-member blocking
an otherwise-free slot, avoids a shrinking-timeout-budget-across-waves mechanism, and avoids
silently degrading concurrency in every test that uses a bare mock client.

## Scope (from the ticket)

Two defects in `ExpansionController._run_dispatch` (the enforced-mode HYBRID/DECOMPOSE path):

1. **Defect 1** — `run_sub_agent`'s outer `asyncio.wait_for(..., timeout=spec.timeout_seconds)`
   wraps the whole `llm_client.respond()` call, including any wait for a concurrency slot. A
   sub-agent that queues for 22.8s is left with 37.2s of the nominal 60s budget, then killed —
   and the timeout message still reports 60.0s.
2. **Defect 2** — the controller fans out `len(plan.tasks)` sub-agents (up to 4 for HYBRID, 6 for
   DECOMPOSE) with no reference to the sub-agent deployment's concurrency ceiling
   (`slm_local` = 3 today). Any plan larger than the ceiling guarantees a queue.

Out of scope: `orchestrator/expansion.py` (`execute_hybrid`, the `orchestration_mode=autonomous`
path) — not the default mode, not in the ticket's reference list. It already bounds fan-out with
its own `asyncio.Semaphore`, sized from `expansion_budget_max` (max decomposition depth) rather
than the real deployment ceiling — the same latent mismatch, in a mode this ticket doesn't cover.
Noted in the handoff as a possible future ticket, not fixed here.

## Design

### Defect 1 — timeout starts at slot acquisition

`LiteLLMClient.respond()` already supports exactly this split and it is unused today. Verified
(including via codex review) for both dispatch branches:
- Cloud: `timeout_s` → `litellm_kwargs["timeout"]` built at `litellm_client.py:906`, consumed by
  `litellm.acompletion()` inside the `request_slot` block at `litellm_client.py:990`/`1017`.
- Local: `timeout_s` → `httpx.Timeout(read=effective_timeout_s, ...)` built at
  `litellm_client.py:1427`, consumed inside `request_slot` at `litellm_client.py:1483`, by the
  streaming call at `1520-1522`.

Neither timeout starts while `request_slot` is waiting for a slot — both start strictly after
acquisition. Caveat from review: locally this is a **read-inactivity** timeout on a streaming
call (llama.cpp emits `stream_options.include_usage` chunks), not a hard total-generation cap —
a generation that keeps emitting chunks can run past 60s. That is unchanged from today and not
something this ticket needs to fix; the outer hard deadline (below) is what remains a true
wall-clock bound.

Changes:
- `run_sub_agent` passes `timeout_s=spec.timeout_seconds` to `llm_client.respond(...)` (currently
  not passed at all).
- `SubAgentSpec` gains `hard_deadline_seconds: float | None = None` — the "separate, larger,
  explicitly named budget" the ticket asks for. Any caller that doesn't set it (i.e. the
  out-of-scope `expansion.py` path) keeps today's exact behavior.
- `run_sub_agent`'s outer `asyncio.wait_for` uses
  `max(spec.hard_deadline_seconds or spec.timeout_seconds, spec.timeout_seconds)` — a clamp, not
  just a fallback, so a misconfigured/smaller `hard_deadline_seconds` (e.g. an env override) can
  never re-create the original bug by being *below* the generation budget it's supposed to sit
  above (review finding: no invariant existed between the two in the first draft).
- `expansion_controller._run_dispatch` sets `hard_deadline_seconds=settings.worker_hard_deadline_seconds`
  on every spec it builds.
- New setting `worker_hard_deadline_seconds: float = 85.0` in `config/settings.py`, next to
  `worker_timeout_seconds`. Documented as a defense-in-depth spawn-to-completion cap for a client
  that ignores `timeout_s` — not the primary timeout mechanism. Sized as 60s generation + 25s
  queue-wait absorption (the live incident's worst observed wait was 22.8s).

  **Master's ticket amendment (2026-09-04 04:57) flagged the interaction this needs to respect**:
  `worker_global_timeout_seconds` (180s, pre-existing, wraps the whole dispatch phase) must still
  bound the total even with Defect 2's ceiling-aware batching now serialising some plans into
  multiple sequential batches. With today's catalog (`sub_agent` → `qwen3.8-flash-next-instruct`,
  ceiling 3) and `_MAX_TASKS` (HYBRID 4, DECOMPOSE 6), the worst case is 2 sequential batches;
  `2 × 85s = 170s` fits under 180s with margin. An explicit CI guard enforces this arithmetic
  against the real catalog (see Tests) so a future change to any of the three inputs can't
  silently recreate the same "budget charged for queue time" shape one level up.
- On the outer `asyncio.TimeoutError` (`run_sub_agent`), the error message reports the *measured*
  `duration_ms`, not the nominal budget: `f"Timeout after {duration_ms / 1000:.1f}s"` (was
  `f"Timeout after {spec.timeout_seconds}s"`). `duration_ms` was already computed on this path,
  just not used in the message. This is AC-2's fix.

### Defect 2 — fan-out respects the ceiling, via a ceiling-sized semaphore

- New method `InferenceConcurrencyController.effective_ceiling(role: str | None, default: int) -> int`
  in `llm_client/concurrency.py`: the tighter of the deployment's own sub-limit and its provider's
  ceiling — the same two constraints `request_slot` already enforces in sequence. `default` is
  **required**, not baked into the controller — an unregistered/unresolvable role (e.g. a test's
  bare `AsyncMock`, whose `model_key` auto-vivifies to a `Mock` object that matches nothing)
  returns exactly `default` rather than silently degrading to some ambient constant. This directly
  addresses the review's risk #4: `_run_dispatch` passes `default=len(specs)`, i.e. "if the
  ceiling can't be determined, don't constrain" — unresolvable-role behavior is unchanged from
  today, so no existing test needs updating for concurrency/timing reasons.
- `_run_dispatch`:
  - resolves `ceiling = get_inference_concurrency_controller().effective_ceiling(getattr(llm_client, "model_key", None), default=len(specs))`
  - builds `semaphore = asyncio.Semaphore(max(1, ceiling))` — mirrors the pattern
    `orchestrator/expansion.py:112` already uses, just sized from the real ceiling instead of
    `expansion_budget_max`
  - `_dispatch_one(spec, parent_id, semaphore)` acquires the semaphore **before** opening the
    child's `SUB_AGENT` phase_span and calling `run_sub_agent` — so a task's own phase/duration
    reflects when it actually started, not when it was queued (same principle as Defect 1,
    applied to the ExpansionController's own gating)
  - the single `phase_span(phase=Phase.EXPANSION, ...)` / single `asyncio.gather(...)` /
    single `worker_global_timeout_seconds` wait_for **structure is unchanged** — every spec is
    still passed to `asyncio.gather` at once (ADR-0123 AC-8's one-parent/N-children shape is
    untouched), the semaphore just makes tasks beyond the ceiling block internally until a slot
    frees. No shrinking-budget-across-waves logic, no partial-result-preservation change — the
    existing `except asyncio.TimeoutError: sub_results = []` behavior on a global timeout is
    unchanged (out of scope; a pre-existing, separate issue the review flagged but the ticket does
    not ask for).
  - a task still queued on the semaphore when the global timeout fires is cancelled cleanly —
    `asyncio.Semaphore.acquire()` is cancellation-safe, and `async with semaphore:` releases
    correctly on cancellation via its context-manager protocol; no special-casing needed.

## Files

- `src/personal_agent/config/settings.py` — add `worker_hard_deadline_seconds`.
- `src/personal_agent/orchestrator/sub_agent_types.py` — add `SubAgentSpec.hard_deadline_seconds`.
- `src/personal_agent/orchestrator/sub_agent.py` — pass `timeout_s`; clamp+use hard deadline for
  outer wait_for; fix the timeout error message.
- `src/personal_agent/orchestrator/expansion_controller.py` — ceiling-aware semaphore in
  `_run_dispatch`, `_dispatch_one` gains a `semaphore` param.
- `src/personal_agent/llm_client/concurrency.py` — `InferenceConcurrencyController.effective_ceiling`.
- Tests (new/updated, TDD — failing first):
  - `tests/personal_agent/orchestrator/test_sub_agent.py` — AC-1/AC-2: a fake client whose
    `respond()` simulates "queued then generating" (sleeps past the nominal timeout_seconds
    *before* honoring it, to prove the outer clamp doesn't cut it short) and one that never
    returns (proves the outer hard deadline still fires and reports real elapsed time, not the
    nominal budget); assert `timeout_s` is passed through to `llm_client.respond`.
  - `tests/personal_agent/orchestrator/test_expansion_controller.py` — AC-3/AC-4: patch
    `get_inference_concurrency_controller` in the module namespace to return a stub whose
    `effective_ceiling` returns a fixed value; run `_run_dispatch` with a 4-task plan and a
    tracking `run_sub_agent` stand-in that records in-flight concurrency; assert max concurrency
    observed `<= ceiling` and `len(result) == 4` with none failed (AC-3), and that ceilings 2 vs 4
    on the identical plan produce different observed max-concurrency (AC-4).
  - `tests/test_llm_client/test_concurrency.py` — `effective_ceiling` unit tests: registered role
    (min of model/provider limits), unregistered role (returns caller's `default`), `role=None`.
  - `tests/personal_agent/orchestrator/test_expansion_controller.py::TestWorkerDeadlineFitsGlobalBudget`
    — the `worker_hard_deadline_seconds` × worst-case-batches ≤ `worker_global_timeout_seconds`
    arithmetic, computed against the real catalog ceiling and `_MAX_TASKS`, not hand-derived
    constants — so a future catalog/setting change that breaks the invariant fails CI.

## Acceptance criteria mapping

- **AC-1**: fake client proves a queued-then-generating call still gets its full nominal
  generation budget once `timeout_s` starts counting (from call entry, standing in for
  slot-acquisition in the real client).
- **AC-2**: outer hard-deadline timeout path; error string reflects measured duration, not
  `spec.timeout_seconds`/`hard_deadline_seconds`.
- **AC-3**: ceiling seeded at 2, plan with 4 tasks; all 4 complete, none time out.
- **AC-4**: same 4-task plan, ceilings 2 and 4 produce different observed max in-flight
  concurrency.

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

## Codex plan-review notes (applied above)

1. Confirmed the `timeout_s`/slot-acquisition ordering for both branches, with the read-inactivity
   caveat (applied to the design section).
2. Confirmed the hard-deadline-as-safety-net shape; existing mock-based tests need updating, not
   removal of the outer bound (applied).
3. Flagged that strict wave-batching is weaker than a ceiling-sized semaphore (one slow wave
   member blocks an otherwise-free slot; extra shrinking-timeout complexity). **Adopted the
   semaphore design instead**, dropping wave-batching and `_batch_specs` entirely.
4. Flagged that an unregistered/mocked role falling back to a baked-in default of 2 would silently
   change dispatch timing/concurrency in every existing test using a bare mock client. **Fixed**:
   `effective_ceiling` takes a required `default` from the caller; `_run_dispatch` passes
   `len(specs)` (i.e. "unknown ceiling → don't constrain"), leaving unrelated existing tests
   unaffected.
5. Confirmed `expansion.py`/autonomous-mode is correctly out of scope for the fix, but noted it has
   the same latent ceiling-vs-`expansion_budget_max` mismatch — recorded above as a possible future
   ticket, not filed (small, same-family issue; per Step 5 "fold in, don't over-ticket" this is
   deliberately *not* folded in since it's a different code path/mode this ticket doesn't touch).
   Also flagged the partial-result-preservation idea from my original wave design would have
   interacted badly with the existing `success=len(sub_results)>0` field — moot now that waves
   were dropped.
