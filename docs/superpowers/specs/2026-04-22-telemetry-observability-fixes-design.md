# Design: Telemetry Observability Fixes

**Date:** 2026-04-22
**Status:** Approved
**Issues fixed:** 3 bugs + 1 missing feature

---

## Background

The agent's `self_telemetry_query` tool returned nulls for CPU/mem in health snapshots, nulls for all latency percentiles, and had no token usage data. Root-cause investigation identified two bugs and one missing feature.

---

## Fix 1 — CPU/mem always null (bug)

**File:** `src/personal_agent/tools/self_telemetry.py` — `_get_system_status()`

**Root cause:** `sensors.py` logs `SYSTEM_METRICS_SNAPSHOT` with fields `cpu_load` and `memory_used`, but `_get_system_status()` queries for `cpu_load_percent` and `mem_used_percent` — keys that never exist in the log.

**Change:** Two `.get()` key corrections:
- `latest.get("cpu_load_percent")` → `latest.get("cpu_load")`
- `latest.get("mem_used_percent")` → `latest.get("memory_used")`

---

## Fix 2 — Latency percentiles always null (bug)

**File:** `src/personal_agent/tools/self_telemetry.py` — `_compute_latency_stats()`

**Root cause:** The `n < 20` guard requires 20+ samples before computing percentiles. This system rarely has 20 interactions in a query window.

**Change:** Lower threshold to `n < 4`. With 4 or more interactions, percentiles (p50/p75/p90/p95) carry real signal.

---

## Fix 3 — Token usage not tracked (missing feature)

Token counts are extracted per LLM call in `executor.py` (lines 1376–1378) and logged to the event stream, but never accumulated into `TaskCapture`.

**Changes (3 files):**

### `src/personal_agent/captains_log/capture.py`
Add three fields to `TaskCapture`:
```python
prompt_tokens: int = 0
completion_tokens: int = 0
total_tokens: int = 0
```

### `src/personal_agent/orchestrator/executor.py`
Before writing the capture, sum token counts from `ctx.steps`:
```python
total_tokens = sum(s.get("metadata", {}).get("tokens", 0) for s in ctx.steps if s["type"] == "llm_call")
```
Assign `prompt_tokens`, `completion_tokens`, `total_tokens` on the capture.

### `src/personal_agent/tools/self_telemetry.py`
In `_execute_performance_query()`, add token aggregation over captures:
- `avg_tokens_per_interaction`
- `total_tokens_in_window`

---

## Testing

- Unit: existing `test_self_telemetry.py` — add cases for key lookup and `n < 20` → `n < 4`
- Unit: existing `test_linear.py` unaffected
- Manual: call `self_telemetry_query(query_type="health")` via API and verify `cpu_avg`/`mem_avg` are non-null
- Manual: call `self_telemetry_query(query_type="performance")` and verify percentiles appear with ≥4 captures
