# Fix `cost_record_missing_identity` — Thread session_id to All LLM Call Sites

## Context

ADR-0074 (FRE-376) made `(trace_id, session_id)` a hard precondition on cost records. The guard in `litellm_client.py:411-418` logs `cost_record_missing_identity` and **skips** cost recording when `trace_ctx.session_id is None`.

Recent commits (`c369cf0`, `c5c9607`) fixed entity_extraction and session_summary by threading `session_id` into their `SystemTraceContext` calls. **But 6 more code paths still construct `TraceContext(trace_id=trace_id)` without `session_id`**, causing 17 errors in the last 24h (most recent at 19:32 UTC today). Cost attribution is degraded — real spend goes unrecorded.

## Approach

**Minimal fix (Approach A):** Add `session_id: str | None = None` to each affected function signature and pass it through to `TraceContext(...)` construction. This:
- Matches the established pattern (c5c9607)
- Is backward-compatible (default `None` doesn't break callers)
- Directly addresses the production alert

A future refactor can pass full `TraceContext` objects instead of `trace_id` strings.

## Changes (8 files)

### 1. `src/personal_agent/captains_log/reflection.py` (trivial — 1 line)

Already has `session_id` in scope (function param on line 222). Just thread it:

- **Line 391:** `SystemTraceContext.new("captains_log_reflection")` → `SystemTraceContext.new("captains_log_reflection", session_id=session_id)`

### 2. `src/personal_agent/orchestrator/sub_agent.py`

- **`run_sub_agent` sig (line 36):** Add `session_id: str | None = None`
- **Line 106:** `TraceContext(trace_id=trace_id)` → `TraceContext(trace_id=trace_id, session_id=session_id)`
- **`_run_tooled_loop` sig (line ~174):** Add `session_id: str | None = None`
- **Line 200:** `TraceContext(trace_id=trace_id)` → `TraceContext(trace_id=trace_id, session_id=session_id)`
- **Line ~89 (call to `_run_tooled_loop`):** Pass `session_id=session_id`

### 3. `src/personal_agent/orchestrator/expansion_controller.py`

- **`execute` sig (line 99):** Add `session_id: str | None = None`
- **`_run_planner` sig (line ~188):** Add `session_id: str | None = None`
- **Line 231:** `TraceContext(trace_id=trace_id)` → `TraceContext(trace_id=trace_id, session_id=session_id)`
- **`_run_dispatch` sig (line ~302):** Add `session_id: str | None = None`
- **Lines 349-353 (`run_sub_agent` calls):** Pass `session_id=session_id`
- **Thread from `execute` → `_run_planner` and `execute` → `_run_dispatch`**

### 4. `src/personal_agent/orchestrator/expansion.py`

- **`execute_hybrid` sig (line 79):** Add `session_id: str | None = None`
- **Line 118-121 (`run_sub_agent` call):** Pass `session_id=session_id`

### 5. `src/personal_agent/orchestrator/skills.py`

- **`route_skills` sig (line 392):** Add `session_id: str | None = None`
- **Lines 444-445:** `TraceContext(trace_id=trace_id)` → `TraceContext(trace_id=trace_id, session_id=session_id)` and `SystemTraceContext.new("skill_routing")` → `SystemTraceContext.new("skill_routing", session_id=session_id)`

### 6. `src/personal_agent/orchestrator/context_compressor.py`

- **`compress_turns` sig (line 129):** Add `session_id: str | None = None`
- **Lines 168-170:** `TraceContext(trace_id=trace_id)` → `TraceContext(trace_id=trace_id, session_id=session_id)` and `SystemTraceContext.new("context_compressor")` → `SystemTraceContext.new("context_compressor", session_id=session_id)`
- **`summarize_middle` sig (line ~351):** Add `session_id: str | None = None`
- **Line 375:** Pass `session_id=session_id` to `compress_turns`

### 7. `src/personal_agent/orchestrator/executor.py` (callers)

- **Line 1124-1131 (ExpansionController.execute):** Add `session_id=ctx.session_id`
- **Line 1538-1543 (route_skills):** Add `session_id=ctx.session_id`
- **Line 1962-1966 (execute_hybrid):** Add `session_id=ctx.session_id`

### 8. `src/personal_agent/orchestrator/within_session_compression.py`

- **Line 280 (`summarize_middle` call):** Add `session_id=session_id` (already available as function param on line 208)

## Verification

1. `make ruff-check && make mypy` — no type errors
2. `make test` — existing tests pass (all new params default to `None`)
3. Add a focused regression test: mock `llm_client.respond`, call each function with `session_id="test-sid"`, assert the `trace_ctx` kwarg carries it
4. **Post-deploy:** Monitor Elasticsearch for `cost_record_missing_identity` — user-request-path (`kind=user`) events should drop to zero. System-kind paths (`kind=system:*`) without session context are expected and acceptable per ADR-0074.

## Expected Residual

After this fix, the only remaining `cost_record_missing_identity` events should be from truly sessionless system operations (scheduler probes, background tasks fired without a user session). These are informational, not bugs.
