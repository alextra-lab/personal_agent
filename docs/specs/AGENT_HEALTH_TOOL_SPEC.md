# Agent Health Tool Spec

**Date**: 2026-03-13
**Status**: Implemented (FRE-108, FRE-109, FRE-110 — March 2026)
**Phase**: 2.3 Homeostasis & Feedback
**Findings**: `docs/specs/FRE107_FINDINGS.md`
**Related**: `docs/specs/SELF_TELEMETRY_TOOL_SPEC.md`, ADR-0004, ADR-0020

---

## Problem

The `self_telemetry_query` tool (implemented per `SELF_TELEMETRY_TOOL_SPEC.md`) can retrieve raw telemetry events, but users and the agent itself need **actionable summaries**, not raw data. The FRE-107 findings document identifies 10 gaps; this spec addresses gaps G1-G8 (the high and medium severity ones).

The core issue: the tool returns raw log events and offloads interpretation to the LLM, which is expensive, unreliable, and context-window-hostile. The fix is to compute aggregations in the executor and return structured summaries.

---

## Design

### Approach: Extend Existing Tool

Add new `query_type` values to `self_telemetry_query` rather than creating a separate tool. The original spec's rationale (keep tool list compact, one tool with dispatch) still applies.

### New Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `query_type` | string | **Extended.** Now supports: `health`, `errors`, `interactions`, `performance`, `events`, `trace`, `latency` |
| `last_n` | number | **New.** Query last N interactions (for `health`, `errors`, `interactions`). Mutually exclusive with `window`. |
| `window` | string | **Extended.** Now also accepts: `today`, `yesterday`, `this_week` alongside `Xh`/`Xm`/`Xd` |
| `trace_id` | string | Unchanged. Required for `trace` and `latency`. |
| `event` | string | Unchanged. Filter for `events` query type. |
| `component` | string | Unchanged. Filter for `events` query type. |
| `limit` | number | Unchanged. Max results for `events` query type. |

### Query Type Dispatch

| `query_type` | Data Source | Required Params | Default Scope |
|-------------|-------------|-----------------|---------------|
| `health` | Captures + JSONL + system metrics | none | `window=1h` |
| `errors` | Captures + JSONL events | none | `window=24h` |
| `interactions` | Captures | none | `last_n=10` |
| `performance` | Captures + ES (optional) | none | `window=24h` |
| `events` | JSONL (unchanged) | none | no default |
| `trace` | JSONL (unchanged) | `trace_id` | n/a |
| `latency` | JSONL (unchanged) | `trace_id` | n/a |

---

## Output Schemas

### `health` — Operational Health Report

```python
{
    "success": True,
    "output": {
        "status": "healthy",       # "healthy" | "degraded" | "unhealthy"
        "window": "1h",
        "assessed_at": "2026-03-13T10:30:00Z",
        "interactions": {
            "total": 12,
            "succeeded": 11,
            "failed": 1,
            "timed_out": 0,
            "success_rate": 0.917,
        },
        "latency": {
            "avg_ms": 3200,
            "min_ms": 800,
            "max_ms": 12000,
            "p95_ms": 8100,         # None if < 20 samples
        },
        "components": {
            "llm": {
                "status": "healthy",
                "calls": 24,
                "errors": 0,
                "avg_latency_ms": 1200,
            },
            "tools": {
                "status": "healthy",
                "calls": 8,
                "errors": 0,
            },
            "system": {
                "cpu_avg_pct": 15.2,
                "memory_avg_pct": 54.3,
                "mode": "NORMAL",
            },
        },
        "alerts": [
            "1 task timeout in tool_execution phase (trace: abc-123)"
        ],
    },
    "error": None,
}
```

**Status determination logic:**

- `healthy`: success_rate >= 0.9 AND no component in error state AND mode == NORMAL
- `degraded`: success_rate >= 0.7 OR any component with errors OR mode in (ALERT, DEGRADED)
- `unhealthy`: success_rate < 0.7 OR mode in (LOCKDOWN) OR multiple component failures

### `errors` — Error Analysis

```python
{
    "success": True,
    "output": {
        "scope": "24h",           # or "last_10_interactions"
        "total_errors": 3,
        "by_type": {
            "model_call_error": 1,
            "tool_call_failed": 1,
            "task_failed": 1,
        },
        "by_component": {
            "llm_client": 1,
            "tools": 1,
            "orchestrator": 1,
        },
        "recent": [
            {
                "timestamp": "2026-03-13T10:23:45Z",
                "type": "model_call_error",
                "component": "llm_client",
                "trace_id": "abc-123",
                "summary": "Connection timeout after 30s",
                "user_message_preview": "What is the weather...",
            },
        ],
        "trend": "stable",        # "increasing" | "stable" | "decreasing"
    },
    "error": None,
}
```

**Trend calculation:** Compare error count in current window vs. preceding window of equal length. Increasing if current > 1.5x preceding; decreasing if current < 0.5x preceding; stable otherwise.

### `interactions` — Recent Activity

```python
{
    "success": True,
    "output": {
        "count": 5,
        "interactions": [
            {
                "trace_id": "abc-123",
                "timestamp": "2026-03-13T10:23:45Z",
                "user_message_preview": "What is the weather...",
                "outcome": "completed",
                "duration_ms": 3200,
                "tools_used": ["mcp_perplexity_ask"],
                "steps": 3,
                "had_errors": False,
            },
        ],
        "summary": {
            "success_rate": 1.0,
            "avg_duration_ms": 2800,
            "most_used_tools": ["mcp_perplexity_ask", "search_memory"],
        },
    },
    "error": None,
}
```

### `performance` — Latency & Throughput

```python
{
    "success": True,
    "output": {
        "window": "24h",
        "throughput": {
            "total_interactions": 42,
            "avg_per_hour": 1.75,
        },
        "latency": {
            "avg_ms": 3200,
            "p50_ms": 2100,
            "p75_ms": 3400,
            "p90_ms": 5600,
            "p95_ms": 8100,
        },
        "by_outcome": {
            "completed": {"count": 40, "avg_duration_ms": 2900},
            "failed": {"count": 2, "avg_duration_ms": 8500},
        },
        "top_tools": [
            {"name": "search_memory", "count": 28, "avg_ms": 120},
            {"name": "read_file", "count": 15, "avg_ms": 45},
        ],
        "bottleneck": "llm_inference",  # phase with highest % of total duration
    },
    "error": None,
}
```

---

## Implementation

### File Changes

| File | Action | Description |
|------|--------|-------------|
| `src/personal_agent/tools/self_telemetry.py` | **Modify** | Add new query types, `last_n` parameter, named time windows, executor functions |
| `src/personal_agent/telemetry/metrics.py` | **Modify** | Extend `_parse_time_window()` with named periods |
| `tests/test_tools/test_self_telemetry.py` | **Modify** | Add tests for new query types |

### Tool Definition Changes

Add `last_n` parameter:

```python
ToolParameter(
    name="last_n",
    type="number",
    description=(
        "Query last N interactions (for health, errors, interactions). "
        "Mutually exclusive with window."
    ),
    required=False,
),
```

Update tool description to include all query types with examples:

```python
description=(
    "Query this agent's operational health and telemetry. "
    "query_type options:\n"
    "- 'health': Overall status (success rate, component health, alerts). "
    "Default window=1h.\n"
    "- 'errors': Error analysis with grouping by type/component. "
    "Default window=24h. Use last_n for interaction-count scope.\n"
    "- 'interactions': Recent interaction history. Default last_n=10.\n"
    "- 'performance': Latency percentiles and throughput. Default window=24h.\n"
    "- 'events': Raw event query (filter by event/component/time).\n"
    "- 'trace': Reconstruct one trace (requires trace_id).\n"
    "- 'latency': Phase breakdown for one trace (requires trace_id).\n"
    "Window accepts: '1h', '30m', '2d', 'today', 'yesterday', 'this_week'."
),
```

### Named Time Window Extension

In `metrics.py`, extend `_parse_time_window()`:

```python
_NAMED_WINDOWS = {
    "today": lambda: datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ),
    "yesterday": lambda: datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=1),
    "this_week": lambda: datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=datetime.now(timezone.utc).weekday()),
}
```

Named windows return an absolute `start_time` rather than a `timedelta`. The executor handles both cases.

### Health Query Executor

```python
def _execute_health_query(
    window: str | None = None,
    last_n: int | None = None,
) -> dict[str, Any]:
    """Compute operational health report.

    Uses Captain's Log captures as primary data source, supplemented
    by JSONL events for component-level detail and system metrics
    for resource health.
    """
    # 1. Load captures (by time window or last_n)
    captures = _load_captures(window=window or "1h", last_n=last_n)

    # 2. Compute interaction stats from captures
    total = len(captures)
    succeeded = sum(1 for c in captures if c.outcome == "completed")
    failed = sum(1 for c in captures if c.outcome == "failed")
    timed_out = sum(1 for c in captures if c.outcome == "timeout")
    success_rate = succeeded / total if total > 0 else 1.0

    # 3. Compute latency stats from captures
    durations = [c.duration_ms for c in captures if c.duration_ms is not None]
    latency = _compute_latency_stats(durations)

    # 4. Query JSONL for component health (LLM errors, tool errors)
    components = _assess_component_health(window=window or "1h")

    # 5. Get current system metrics
    system = _get_system_status()

    # 6. Determine overall status
    status = _determine_health_status(success_rate, components, system)

    # 7. Generate alerts
    alerts = _generate_alerts(captures, components)

    return { ... }
```

### Captures Loading Helper

```python
def _load_captures(
    window: str | None = None,
    last_n: int | None = None,
) -> list[TaskCapture]:
    """Load TaskCapture records by time window or interaction count.

    Args:
        window: Time window string (e.g., '1h', 'today').
        last_n: Number of most recent interactions to load.

    Returns:
        List of TaskCapture records, newest first.
    """
    if last_n is not None:
        return read_captures(limit=last_n)

    start_time = _resolve_window_to_datetime(window or "1h")
    return read_captures(start_date=start_time)
```

### Component Health Assessment

```python
def _assess_component_health(window: str) -> dict[str, dict[str, Any]]:
    """Assess health of each major component from JSONL events."""
    start_time = _resolve_window_to_datetime(window)

    # Query error events for each component
    llm_errors = query_events(event="model_call_error", window_str=window)
    llm_calls = query_events(event="model_call_completed", window_str=window)
    tool_errors = query_events(event="tool_call_failed", window_str=window)
    tool_calls = query_events(event="tool_call_completed", window_str=window)

    return {
        "llm": {
            "status": "healthy" if len(llm_errors) == 0 else "degraded",
            "calls": len(llm_calls) + len(llm_errors),
            "errors": len(llm_errors),
            "avg_latency_ms": _avg_field(llm_calls, "latency_ms"),
        },
        "tools": {
            "status": "healthy" if len(tool_errors) == 0 else "degraded",
            "calls": len(tool_calls) + len(tool_errors),
            "errors": len(tool_errors),
        },
    }
```

### Error Grouping

```python
def _execute_errors_query(
    window: str | None = None,
    last_n: int | None = None,
) -> dict[str, Any]:
    """Compute error analysis with grouping."""
    # 1. Load failed captures
    captures = _load_captures(window=window or "24h", last_n=last_n)
    failed_captures = [c for c in captures if c.outcome != "completed"]

    # 2. Query JSONL for error events in the same window
    error_events = []
    for event_type in ("task_failed", "model_call_error", "tool_call_failed"):
        error_events.extend(
            query_events(event=event_type, window_str=window or "24h")
        )

    # 3. Group by type and component
    by_type = _count_by_field(error_events, "event")
    by_component = _count_by_field(error_events, "component")

    # 4. Build recent error list (max 10, with context from captures)
    recent = _build_recent_errors(error_events, captures, limit=10)

    # 5. Compute trend
    trend = _compute_error_trend(window or "24h")

    return { ... }
```

---

## Internal Use Cases

### Session Start Health Check

The orchestrator can optionally call `health` at session start to calibrate behavior:

```python
# In orchestrator setup phase
health = self_telemetry_query_executor(query_type="health", window="1h")
if health["output"]["status"] == "degraded":
    log.warning("session_start_degraded", alerts=health["output"]["alerts"])
```

### Post-Failure Investigation

After a `task_failed` state, the orchestrator can auto-investigate:

```python
# In executor error handling
trace_result = self_telemetry_query_executor(
    query_type="trace", trace_id=current_trace_id
)
```

### Brainstem Integration

The scheduler can query health periodically for mode transition decisions:

```python
# In brainstem scheduler
health = self_telemetry_query_executor(query_type="health", window="30m")
if health["output"]["interactions"]["success_rate"] < 0.5:
    trigger_mode_transition("DEGRADED", reason="high_failure_rate")
```

### Captain's Log Enrichment

Reflection can include health context:

```python
# In reflection generation
perf = self_telemetry_query_executor(query_type="performance", window="1h")
# Include in telemetry_summary for richer reflection
```

---

## Chat User Conversation Examples

**"How are you doing?"**

```json
{"name": "self_telemetry_query", "arguments": {"query_type": "health"}}
```

→ Agent responds: "I'm healthy — 95% success rate over the last hour, 12 interactions completed, average response time 3.2 seconds. All components green."

**"Any errors recently?"**

```json
{"name": "self_telemetry_query", "arguments": {"query_type": "errors", "window": "today"}}
```

→ Agent responds: "2 errors today: one LLM timeout and one tool execution failure. Both were isolated incidents — error rate is stable."

**"Were there problems in the last 5 interactions?"**

```json
{"name": "self_telemetry_query", "arguments": {"query_type": "errors", "last_n": 5}}
```

→ Agent responds: "No errors in the last 5 interactions. All completed successfully."

**"Why was that last response slow?"**

```json
{"name": "self_telemetry_query", "arguments": {"query_type": "latency", "trace_id": "<current>"}}
```

**"Show me what you've been working on"**

```json
{"name": "self_telemetry_query", "arguments": {"query_type": "interactions", "last_n": 5}}
```

**"Am I getting slower?"**

```json
{"name": "self_telemetry_query", "arguments": {"query_type": "performance", "window": "today"}}
```

---

## Acceptance Criteria

- [ ] `query_type=health` returns structured health report with status verdict.
- [ ] `query_type=errors` returns grouped error analysis with trend.
- [ ] `query_type=interactions` returns recent interaction list from Captain's Log captures.
- [ ] `query_type=performance` returns latency percentiles and throughput metrics.
- [ ] `last_n` parameter scopes queries by interaction count (reads Captain's Log captures).
- [ ] Named time windows (`today`, `yesterday`, `this_week`) are supported.
- [ ] Existing `events`, `trace`, `latency` query types continue to work unchanged.
- [ ] Health status determination uses defined thresholds (healthy/degraded/unhealthy).
- [ ] Error trend detection compares current vs. preceding window.
- [ ] Tool description updated with examples for all query types.
- [ ] Output is capped to protect context window (existing 50-entry cap for raw queries; summary queries are inherently bounded).
- [ ] Unit tests cover all new query types with synthetic Captain's Log captures.
- [ ] Graceful handling when no captures exist (returns empty/healthy defaults).

---

## Non-Goals

- Elasticsearch-only queries (ES is optional acceleration, not required).
- Writing or modifying telemetry/capture data.
- Real-time streaming or push notifications.
- Replacing Kibana dashboards (those serve visual/human analysis; this serves conversational access).
- Memory graph health queries (separate tool, separate concern).

---

## Implementation Plan

### Issue 1: Core Health Queries (health, errors, interactions)

**Scope**: Add `health`, `errors`, `interactions` query types. Wire in Captain's Log `read_captures()`. Add `last_n` parameter. Add named time windows.

**Estimated effort**: Medium (new executor functions, captures integration, tests).

### Issue 2: Performance Query and Trend Detection

**Scope**: Add `performance` query type. Implement trend detection (current vs. preceding window comparison). Optionally wire in `TelemetryQueries` for ES-backed percentiles.

**Estimated effort**: Medium (aggregation logic, optional ES integration, tests).

### Issue 3: Tool Description and Conversation Tuning

**Scope**: Update tool description to guide LLM usage. Add conversation-specific examples. Test end-to-end with representative queries from the repro matrix.

**Estimated effort**: Small (description text, integration testing).

---

## References

- Findings: `docs/specs/FRE107_FINDINGS.md`
- Current tool spec: `docs/specs/SELF_TELEMETRY_TOOL_SPEC.md`
- Captain's Log captures: `src/personal_agent/captains_log/capture.py`
- Telemetry metrics: `src/personal_agent/telemetry/metrics.py`
- ES queries: `src/personal_agent/telemetry/queries.py`
- ADR-0004: Telemetry & Metrics Strategy
- ADR-0020: Request Traceability
