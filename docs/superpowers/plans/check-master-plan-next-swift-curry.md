# FRE-387: Eval Session Side-Effect Isolation

## Context

The recovery harness (`scripts/eval/recovery_harness.py`) sends `POST /chat?channel=EVAL`, which sets `eval_mode=True` on `ExecutionContext` and `TraceContext`. Today, only `create_linear_issue` (tools/linear.py:434) checks this flag. All other write paths — Captain's Log captures, CL reflections, Neo4j entity extraction, ES trace indexing, insights — fire normally during eval sessions, polluting production substrates with synthetic data.

FRE-375 isolated the **test** substrate (conftest.py URI redirection, settings validator). FRE-387 completes the picture for **eval** sessions hitting the live gateway.

## Approach: Gate at the Capture Write Point

**Key insight**: Entity extraction, Neo4j writes, and insights are all **downstream** of the capture write. The consolidator reads captures from disk; `RequestCapturedEvent` triggers the consolidation scheduler. If eval sessions produce no captures and no captured events, the entire downstream pipeline (consolidation → entity extraction → Neo4j → insights → promotion) never fires for eval data. One gate covers five write paths.

## Changes

### 1. Primary gate — executor.py (lines 843-928)

**File**: `src/personal_agent/orchestrator/executor.py`

Wrap the capture + event + reflection block in `if not ctx.eval_mode:`. The `TASK_COMPLETED` log (lines 834-841) still fires — it's operational telemetry, not a substrate write.

```python
        if state == TaskState.COMPLETED:
            log.info(TASK_COMPLETED, ...)

            if not ctx.eval_mode:
                # Fast capture (Phase 2.2) — lines 843-921
                try:
                    ...write_capture(capture)...
                    ...publish RequestCapturedEvent...
                except Exception as e:
                    ...

                # Captain's Log reflection — line 923-928
                run_in_background(_trigger_captains_log_reflection(ctx))
            else:
                log.info(
                    "eval_mode_side_effects_suppressed",
                    trace_id=ctx.trace_id,
                    session_id=ctx.session_id,
                )
```

**Suppresses**: `write_capture()`, `schedule_es_index()` (inside capture), `RequestCapturedEvent` publish, `_trigger_captains_log_reflection()`. Transitively blocks: consolidation, entity extraction, Neo4j writes, insights, promotion.

### 2. Defense-in-depth — `_trigger_captains_log_reflection` early return

**File**: `src/personal_agent/orchestrator/executor.py` (line 647)

Add early return at the top of `_trigger_captains_log_reflection()` so even direct callers are protected:

```python
async def _trigger_captains_log_reflection(ctx: ExecutionContext) -> None:
    if ctx.eval_mode:
        return
    # ... rest unchanged ...
```

### 3. ES trace indexing gate — RequestCompletedEvent + handler

The `RequestCompletedEvent` (published from app.py:1442) drives two consumers: the ES trace handler and the session writer. The session writer **must** still run for multi-turn eval sessions. The ES trace handler should be skipped.

**File**: `src/personal_agent/events/models.py` (line 210)

Add `eval_mode` field to `RequestCompletedEvent`:

```python
class RequestCompletedEvent(EventBase):
    ...existing fields...
    eval_mode: bool = False
```

**File**: `src/personal_agent/service/app.py` (line 1444)

Pass `eval_mode` when constructing the event:

```python
RequestCompletedEvent(
    trace_id=trace_id,
    session_id=sid,
    assistant_response=response_content,
    trace_summary=timer.to_trace_summary(),
    trace_breakdown=timer.to_breakdown(),
    source_component="service.app",
    eval_mode=(channel.upper() == "EVAL"),
)
```

**File**: `src/personal_agent/events/request_completed_handlers.py` (line 24)

Gate the ES trace handler:

```python
async def handler(event: EventBase) -> None:
    if not isinstance(event, RequestCompletedEvent):
        return
    if event.eval_mode:
        return
    if not es_handler or not getattr(es_handler, "_connected", False):
        return
    ...
```

The session writer handler (`build_session_writer_handler`) is **not** gated — multi-turn eval requires DB persistence of assistant messages.

### 4. What is NOT gated (and why)

**Structlog → ES handler** (`es_handler.py`): Operational telemetry (structlog events) is not gated. Reasons:
- The eval docker-compose stack (`docker-compose.eval.yml`) already routes to isolated ES on port 9202
- Operational logs are needed for eval debugging and harness diagnostics
- The AC targets production **substrate** pollution (captures, reflections, Neo4j entities), not operational telemetry
- Eval traces carry `trace_id` and can be filtered/deleted by the existing cleanup scripts

### 5. Tests

**New file**: `tests/test_orchestrator/test_eval_isolation.py`

| Test | What it verifies |
|------|------------------|
| `test_eval_mode_suppresses_capture_and_reflection` | With `eval_mode=True`, `write_capture` not called, `RequestCapturedEvent` not published, `_trigger_captains_log_reflection` not called |
| `test_non_eval_mode_writes_capture_normally` | With `eval_mode=False`, all three fire normally |
| `test_reflection_function_returns_early_for_eval` | Direct call to `_trigger_captains_log_reflection(ctx)` with `eval_mode=True` returns without LLM call or file write |
| `test_es_trace_handler_skips_eval_events` | `build_request_trace_es_handler` with `eval_mode=True` event does not call `index_request_trace_from_snapshot` |
| `test_session_writer_runs_for_eval_events` | `build_session_writer_handler` with `eval_mode=True` event still appends the assistant message |

## Files Modified

| File | Change |
|------|--------|
| `src/personal_agent/orchestrator/executor.py` | `if not ctx.eval_mode:` around lines 843-928; early return in `_trigger_captains_log_reflection` |
| `src/personal_agent/events/models.py` | `eval_mode: bool = False` on `RequestCompletedEvent` |
| `src/personal_agent/service/app.py` | Pass `eval_mode=` to `RequestCompletedEvent` constructor |
| `src/personal_agent/events/request_completed_handlers.py` | Gate `build_request_trace_es_handler` on `event.eval_mode` |
| `tests/test_orchestrator/test_eval_isolation.py` | New — 5 tests |

## Verification

1. `make test` — all existing tests pass (no behavior change for non-eval paths)
2. `make mypy` — type-clean
3. `make ruff-check && make ruff-format` — lint-clean
4. Manual: start dev server (`make dev`), send `POST /chat?channel=EVAL&message=hello` — confirm:
   - No new files in `telemetry/captains_log/captures/` for the eval trace_id
   - No new `CL-*.json` reflections for the eval trace_id
   - `eval_mode_side_effects_suppressed` appears in logs
   - Response still returns normally (assistant message persisted for session continuity)
