# FRE-576 — Context Compression Cross-Audit: 5 Discrepancies

**Ticket**: FRE-576 (Approved, Tier-1:Opus — work is mechanical enough for Sonnet post-audit)
**ADR refs**: ADR-0061 (within-session), ADR-0081 (frozen layout / scheduler)
**Branch**: `fre-576-compression-cross-audit`

## Scope

Fix F2 (bug), F3 (observability), F4 (dead-code comments), F5 (cap reconciliation). F1 defers to FRE-577.

### Findings vs current HEAD (2026-06-20)

| Finding | File | Current | Problem |
|---------|------|---------|---------|
| F2 🐞 | `within_session_compression.py:47` | `SUMMARY_ROLE = "system"` | `_validate_and_fix_conversation_roles` keeps only first system msg; mid-list summary is silently dropped |
| F2 🐞 (side) | `compression_manager.py:170` | `msg.get("role") == "system"` lookup | Inconsistent with fix — should match SUMMARY_ROLE |
| F3 ⚙️ | `executor.py:937-944` `_derive_reset_inputs` | No `quality_slope` key in returned dict | Defaults to `0.0` → quality term always zero; invisible |
| F3 ⚙️ | `executor.py:970-972` `_maybe_frozen_reset` | Only logs when reset fires | No observability for hold decisions |
| F4 📝 | `executor.py:1836-1839` + `context_window.py:78-82` | No comment on dead-by-default | Reader sees live-looking path; gating is already correct but undocumented |
| F5 📝 | `context_compressor.py:182` | `max_tokens=512` | Prompt says "≤200 words" (≈300 tokens); 512 is 2× the stated cap |

## Steps

### Step 1 — Write failing tests (TDD)

**File**: `tests/test_orchestrator/test_within_session_compression.py`

Add `TestSummaryRoleSurvivesRoleFixer` class with one test:
- Build a compressed message list (patching `compress_turns` to return a summary)
- Run the result through `_validate_and_fix_conversation_roles` (imported from `executor`)
- Assert the summary content is still present in the output

The test will FAIL before F2 is fixed (summary dropped), PASS after.

Run: `make test-file FILE=tests/test_orchestrator/test_within_session_compression.py`
Expected before fix: `FAILED test_summary_role_survives_role_fixer`

### Step 2 — F2: Fix SUMMARY_ROLE

**File**: `src/personal_agent/orchestrator/within_session_compression.py:47`
- Change `SUMMARY_ROLE = "system"` → `SUMMARY_ROLE = "assistant"`

**File**: `src/personal_agent/orchestrator/compression_manager.py:168-176`
- Change `msg.get("role") == "system"` → `msg.get("role") == "assistant"` in `_run_compression`
  (or import `SUMMARY_ROLE` constant from `within_session_compression` and use it)
- Note: this code path is dead when `cache_frozen_layout_enabled=True` but should be consistent

**File**: `tests/test_orchestrator/test_within_session_compression.py:253`
- Change `assert compressed[2]["role"] == "system"` → `assert compressed[2]["role"] == "assistant"`

Run: `make test-file FILE=tests/test_orchestrator/test_within_session_compression.py`
Expected: all pass, including new test

### Step 3 — F3: Observability for quality_slope

**File**: `src/personal_agent/orchestrator/executor.py` `_derive_reset_inputs` (~line 937)
- Add explicit `"quality_slope": 0.0` to the return dict
- Add a comment: `# quality_slope: not yet wired from FRE-554/570/572; 0.0 = token-ceiling-only degenerate case`

**File**: `src/personal_agent/orchestrator/executor.py` `_maybe_frozen_reset` (~line 970)
- After `decision = should_reset(...)`, compute `c` and log it for ALL decisions (not just fires):

```python
inputs = _derive_reset_inputs(ctx.messages, backend)
decision = should_reset(**inputs)
_c = marginal_hold_cost(
    inputs["delta_turn_tokens"],
    inputs.get("quality_slope", 0.0),
    inputs["quality_token_weight"],
)
log.info(
    "cache_reset_decision",
    trace_id=ctx.trace_id,
    session_id=ctx.session_id,
    backend=backend,
    should_reset=decision.should_reset,
    reason=decision.reason,
    optimal_run_length=decision.optimal_run_length
    if decision.optimal_run_length != math.inf
    else None,
    quality_slope=inputs.get("quality_slope", 0.0),
    marginal_hold_cost=round(_c, 2),
    turns_since_reset=inputs["turns_since_reset"],
)
```

Requires importing `marginal_hold_cost` from `cache_reset_scheduler` + `math`.

**File**: `src/personal_agent/config/settings.py` `cache_quality_token_weight` description
- Append to description: "Currently inert — quality_slope is hardwired to 0.0 pending FRE-554/570/572."

Run: `make test-file FILE=tests/test_orchestrator/test_cache_reset_scheduler.py`
Expected: all pass (no behavior change, only new logging)

### Step 4 — F4: Clarifying comments for dead-by-default path

**File**: `src/personal_agent/orchestrator/executor.py:1830-1840`
- Add inline comment above the `_summary = ...` block explaining it's dead when frozen layout is on

**File**: `src/personal_agent/orchestrator/context_window.py` `apply_context_window` docstring
- In `compressed_summary` param description: add note "Dead-by-default when `cache_frozen_layout_enabled=True` (the production default — ADR-0081 §D3 Decision 4). The executor gate is at `executor.step_context_window`."

No behavior change; no new tests needed.

### Step 5 — F5: Reconcile max_tokens vs prompt wording

**File**: `src/personal_agent/orchestrator/context_compressor.py:182`
- Change `max_tokens=512` → `max_tokens=320`
  (200 words ≈ 260-300 tokens; 320 is a ~7% margin above the stated cap, matching the prompt's intent)

Run: `make test-file FILE=tests/test_orchestrator/test_async_compression.py`
Expected: all pass (only the token limit changes; mock-based tests unaffected)

### Step 6 — Quality gates

```bash
make test-file FILE=tests/test_orchestrator/test_within_session_compression.py
make test-file FILE=tests/test_orchestrator/test_cache_reset_scheduler.py
make test
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Acceptance Criteria (pre-merge)

- [ ] `SUMMARY_ROLE = "assistant"` in `within_session_compression.py`
- [ ] New test `test_summary_role_survives_role_fixer` passes: summary content present after `_validate_and_fix_conversation_roles`
- [ ] Existing test at `test_within_session_compression.py:253` updated to assert `"assistant"` role
- [ ] `_derive_reset_inputs` emits explicit `quality_slope=0.0`
- [ ] `_maybe_frozen_reset` logs `cache_reset_decision` for every evaluation (fire + hold)
- [ ] `cache_quality_token_weight` description notes "currently inert"
- [ ] Comments added at `executor.py` `_summary` block and `context_window.py` `compressed_summary` param
- [ ] `max_tokens=320` in `context_compressor.py`
- [ ] `make test` passes clean
- [ ] `make mypy` passes clean
