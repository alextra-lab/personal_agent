# ADR-0013: Enhanced System Health Tool with Historical Queries

**Status:** Proposed
**Date:** 2026-01-17
**Decision Owner:** Project Owner

---

## 1. Context

The `system_metrics_snapshot` tool currently provides only current, point-in-time system metrics (CPU, memory, disk, GPU). This creates limitations for:

1. **Debugging performance issues**: Cannot review metrics from when issue occurred
2. **Request correlation**: Cannot easily see system metrics during a specific task execution
3. **Trend analysis**: No way to understand metric patterns over time
4. **Root cause analysis**: When user reports "the agent was slow", no historical data to investigate
5. **Captain's Log analysis**: Self-reflections cannot reference past performance data

### Current Behavior

```python
# Current tool only returns live data
result = system_metrics_snapshot_executor()
# Returns: {"cpu_load": 45.2, "memory_used": 62.5, ...} <- RIGHT NOW
```

### User Scenarios

**Scenario 1: User Debugging**
```
User: "Why was that last request so slow?"
Agent: [calls system_health tool]
Agent: "Your CPU is currently at 23%... but I can't see what it was during the request."
```

**Scenario 2: Request Correlation**
```
User: "Show me system metrics from the last request"
Agent: [needs to query historical data by trace_id, but tool doesn't support this]
```

**Scenario 3: Pattern Analysis**
```
User: "Has memory usage been increasing over the last hour?"
Agent: [needs time-window query, not possible with current tool]
```

### Why This Matters

With ADR-0012 (Request-Scoped Monitoring), the system will automatically log `SYSTEM_METRICS_SNAPSHOT` events during every request. However, these historical snapshots are only accessible by:

1. Manually reading JSONL log files
2. Using `query_events()` programmatically (not accessible to agent as a tool)
3. External analysis tools

The agent itself cannot access this rich historical data when reasoning about performance.

---

## 2. Decision

### 2.1 Enhanced Tool Interface

Extend `system_metrics_snapshot` tool with optional parameters:

```python
def system_metrics_snapshot_executor(
    window_str: str | None = None,
    trace_id: str | None = None,
    include_history: bool = False,
    stat_summary: bool = True
) -> dict[str, Any]:
    """Get system metrics with optional historical data.
    
    Args:
        window_str: Optional time window (e.g., "30m", "1h", "24h").
                   If provided, includes historical snapshots.
        trace_id: Optional trace ID to filter metrics for specific request.
        include_history: Whether to include full time-series data.
        stat_summary: Whether to include statistical summary (min/max/avg).
    
    Returns:
        {
            "success": bool,
            "current": {
                "timestamp": str,
                "cpu_load": float,
                "memory_used": float,
                ...
            },
            "history": [  # If window_str or trace_id provided
                {
                    "timestamp": str,
                    "cpu_load": float,
                    "memory_used": float,
                    ...
                },
                ...
            ],
            "summary": {  # If stat_summary=True and history exists
                "duration_seconds": float,
                "sample_count": int,
                "cpu": {"min": float, "max": float, "avg": float},
                "memory": {"min": float, "max": float, "avg": float},
                "gpu": {"min": float, "max": float, "avg": float},
            },
            "error": str | None
        }
    """
```

### 2.2 Query Modes

**Mode 1: Current Snapshot Only (default, backward compatible)**
```python
system_metrics_snapshot()
# Returns current system state only
```

**Mode 2: Time Window Query**
```python
system_metrics_snapshot(window_str="30m")
# Returns current + last 30 minutes of snapshots
```

**Mode 3: Request-Scoped Query**
```python
system_metrics_snapshot(trace_id="abc-123-xyz")
# Returns all snapshots for specific request
```

**Mode 4: Time Window with Summary Only**
```python
system_metrics_snapshot(window_str="1h", include_history=False)
# Returns current + statistical summary only (no full time series)
```

### 2.3 Implementation

Leverage existing `personal_agent.telemetry.metrics` module:

```python
from personal_agent.telemetry.metrics import query_events
from personal_agent.telemetry.events import SYSTEM_METRICS_SNAPSHOT

def system_metrics_snapshot_executor(...) -> dict[str, Any]:
    try:
        result = {
            "success": True,
            "error": None
        }
        
        # Always get current snapshot
        current_metrics = get_system_metrics_snapshot()
        result["current"] = current_metrics
        
        # If historical data requested
        if window_str or trace_id:
            # Query telemetry logs
            filters = {"event": SYSTEM_METRICS_SNAPSHOT}
            if trace_id:
                # Filter by trace_id if specified
                entries = query_events(
                    event=SYSTEM_METRICS_SNAPSHOT,
                    # Need to add trace_id filtering to query_events
                )
                history = [e for e in entries if e.get("trace_id") == trace_id]
            elif window_str:
                # Query by time window
                history = query_events(
                    event=SYSTEM_METRICS_SNAPSHOT,
                    window_str=window_str
                )
            
            # Include full history if requested
            if include_history:
                result["history"] = history
            
            # Calculate statistical summary
            if stat_summary and history:
                result["summary"] = _calculate_summary(history)
        
        return result
        
    except Exception as e:
        return {
            "success": False,
            "current": None,
            "error": f"Error getting system metrics: {e}"
        }

def _calculate_summary(snapshots: list[dict]) -> dict[str, Any]:
    """Calculate statistical summary from metric snapshots."""
    if not snapshots:
        return {}
    
    # Extract metric values
    cpu_values = [s.get("cpu_load") for s in snapshots if s.get("cpu_load") is not None]
    mem_values = [s.get("memory_used") for s in snapshots if s.get("memory_used") is not None]
    gpu_values = [s.get("gpu_load") for s in snapshots if s.get("gpu_load") is not None]
    
    summary = {
        "duration_seconds": _calculate_duration(snapshots),
        "sample_count": len(snapshots),
    }
    
    if cpu_values:
        summary["cpu"] = {
            "min": min(cpu_values),
            "max": max(cpu_values),
            "avg": sum(cpu_values) / len(cpu_values)
        }
    
    if mem_values:
        summary["memory"] = {
            "min": min(mem_values),
            "max": max(mem_values),
            "avg": sum(mem_values) / len(mem_values)
        }
    
    if gpu_values:
        summary["gpu"] = {
            "min": min(gpu_values),
            "max": max(gpu_values),
            "avg": sum(gpu_values) / len(gpu_values)
        }
    
    return summary
```

### 2.4 Tool Definition Update

Update tool definition to document new parameters:

```python
system_metrics_snapshot_tool = ToolDefinition(
    name="system_metrics_snapshot",
    description="""Get system metrics (CPU, memory, disk, GPU) with optional history.
    
    Use this to:
    - Check current system health
    - Review metrics from a time window (e.g., last 30 minutes)
    - Analyze metrics for a specific request (by trace_id)
    - Investigate performance issues
    
    Examples:
    - Current only: system_metrics_snapshot()
    - Last hour: system_metrics_snapshot(window_str="1h")
    - Specific request: system_metrics_snapshot(trace_id="abc-123")
    """,
    category="read_only",
    parameters=[
        {
            "name": "window_str",
            "type": "string",
            "description": "Optional time window (e.g., '30m', '1h', '24h')",
            "required": False
        },
        {
            "name": "trace_id",
            "type": "string",
            "description": "Optional trace ID to get metrics for specific request",
            "required": False
        },
        {
            "name": "include_history",
            "type": "boolean",
            "description": "Whether to include full time-series data (default: False)",
            "required": False
        },
        {
            "name": "stat_summary",
            "type": "boolean",
            "description": "Whether to include statistical summary (default: True)",
            "required": False
        }
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "LOCKDOWN", "RECOVERY"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=10,
    rate_limit_per_hour=None,
)
```

### 2.5 Telemetry Metrics Enhancement

Extend `query_events()` to support trace_id filtering:

```python
# In src/personal_agent/telemetry/metrics.py

def query_events(
    event: str | None = None,
    window_str: str | None = None,
    component: str | None = None,
    trace_id: str | None = None,  # NEW
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Query log entries with flexible filters.
    
    Args:
        event: Optional event name filter.
        window_str: Optional time window (e.g., "1h", "30m").
        component: Optional component name filter.
        trace_id: Optional trace ID filter.  # NEW
        limit: Optional maximum number of results.
    
    Returns:
        List of matching log entries, ordered by timestamp (newest first).
    """
    # ... existing implementation ...
    
    # Apply filters
    filtered = []
    for entry in entries:
        if event and entry.get("event") != event:
            continue
        if component and entry.get("component") != component:
            continue
        if trace_id and entry.get("trace_id") != trace_id:  # NEW
            continue
        filtered.append(entry)
    
    # ... rest of implementation ...
```

---

## 3. Consequences

### Positive

1. **Debugging capability**: Agent can investigate past performance issues
2. **Request correlation**: Easy to see system state during specific tasks
3. **Backward compatible**: Default behavior unchanged (current snapshot only)
4. **Flexible queries**: Supports time windows, trace IDs, or both
5. **Statistical insights**: Summary stats reduce cognitive load
6. **LLM-friendly**: Structured format easy for reasoning model to interpret
7. **Low overhead**: Historical queries read from existing logs (no new storage)

### Negative

1. **Query latency**: Historical queries require reading/parsing JSONL files (mitigated by indexing in future)
2. **Large responses**: Full history for long time windows could be large (mitigated by `include_history=False`)
3. **Complexity**: More parameters increase tool interface complexity
4. **Testing surface**: More query modes to test

### Trade-offs

| Aspect | Current | Enhanced | Justification |
|--------|---------|----------|---------------|
| Response size | ~1 KB | 1 KB - 100 KB | Manageable with stat_summary option |
| Query latency | <100ms | 100ms - 2s | Acceptable for debugging use case |
| Interface complexity | Simple | Moderate | Worth it for debugging power |
| Storage requirements | None | None | Uses existing logs |

---

## 4. Alternatives Considered

### Alternative 1: Separate Historical Query Tool

**Description**: Create a new tool `query_historical_metrics()` separate from `system_metrics_snapshot`.

**Pros**:
- Cleaner separation of concerns
- Doesn't complicate existing tool

**Cons**:
- Harder for agent to discover (two tools instead of one)
- User confusion about which tool to use
- Duplication of metric extraction logic

**Decision**: Rejected. Single tool with optional parameters is more intuitive.

### Alternative 2: Always Include Last N Samples

**Description**: Always return last 5-10 snapshots by default.

**Pros**:
- No need to request historical data
- Shows recent trend automatically

**Cons**:
- Increases response size for common case (current only)
- Wastes tokens on data agent might not need
- Not flexible enough for debugging

**Decision**: Rejected. Opt-in is better for token efficiency.

### Alternative 3: External Analysis Tool (User-Facing, Not Agent Tool)

**Description**: Build a separate CLI tool for users to analyze metrics, not exposed to agent.

**Pros**:
- Simpler agent interface
- Can have richer analysis features

**Cons**:
- Agent cannot help user debug performance issues
- User must context-switch to external tool
- Doesn't leverage LLM reasoning capabilities

**Decision**: Rejected. Agent should be able to help with debugging.

### Alternative 4: Only Support trace_id Queries (No Time Windows)

**Description**: Only allow querying by specific request trace_id.

**Pros**:
- Simpler interface
- Most common debugging use case

**Cons**:
- Cannot analyze trends over time
- User must know trace_id (not always available)
- Less flexible for exploration

**Decision**: Rejected. Time window queries are valuable for pattern analysis.

---

## 5. Usage Examples

### Example 1: Debugging Slow Request

```
User: "That last request felt slow. What was happening?"

Agent: [calls system_metrics_snapshot with trace_id from context]

Agent: "During that request:
- CPU averaged 78% with a peak of 92%
- Memory was stable around 65%
- The request took 8.3 seconds
- CPU was above 85% for 40% of the duration, which likely caused the slowdown.

The high CPU usage suggests the LLM inference was compute-bound. Consider using a smaller model for simple queries."
```

### Example 2: Trend Analysis

```
User: "Has my Mac been under heavy load lately?"

Agent: [calls system_metrics_snapshot(window_str="1h")]

Agent: "Over the last hour:
- CPU: averaged 42%, peak 89%
- Memory: averaged 58%, peak 71%
- GPU: averaged 15%, peak 34%

Your system has been under moderate load with occasional spikes. The peak CPU of 89% occurred 23 minutes ago and lasted about 30 seconds. No sustained overload detected."
```

### Example 3: Request Correlation

```
User: "Compare system metrics from my last two requests"

Agent: [looks up last two trace_ids, queries both]

Agent: "Request 1 (file search):
- Duration: 2.1s, CPU avg: 35%

Request 2 (code generation):
- Duration: 12.4s, CPU avg: 82%

The code generation request was 6x slower and used significantly more CPU, which is expected for LLM-intensive tasks."
```

---

## 6. Implementation Plan

See updated IMPLEMENTATION_ROADMAP.md for detailed sequencing.

**Summary**:
1. **Phase 1**: Extend `query_events()` to support trace_id filtering
2. **Phase 2**: Implement `_calculate_summary()` helper
3. **Phase 3**: Update `system_metrics_snapshot_executor()` with new parameters
4. **Phase 4**: Update tool definition with parameter descriptions
5. **Phase 5**: Add comprehensive tests for all query modes
6. **Phase 6**: Update documentation and examples

**Dependencies**: Requires ADR-0012 (Request-Scoped Monitoring) to generate historical data.

---

## 7. References

- ADR-0012: Request-Scoped Metrics Monitoring (generates the historical data)
- ADR-0004: Telemetry & Metrics Implementation Strategy
- `src/personal_agent/tools/system_health.py` - Current implementation
- `src/personal_agent/telemetry/metrics.py` - Query infrastructure
- `src/personal_agent/brainstem/sensors/` - Metric collection

---

## 8. Open Questions

1. **Should we limit the maximum time window to prevent expensive queries?**
   - Proposal: Yes, max 24 hours to prevent reading too many log files
   - Rationale: Keeps query latency reasonable, covers most use cases

2. **How to handle queries when monitoring is disabled?**
   - Proposal: Return current snapshot only with warning message
   - Rationale: Tool should degrade gracefully

3. **Should we cache query results for repeated requests?**
   - Proposal: Not for MVP, add in Phase 2 if needed
   - Rationale: Adds complexity, queries should be fast enough

4. **Should summary stats include percentiles (p50, p95, p99)?**
   - Proposal: Start with min/max/avg, add percentiles in future
   - Rationale: Simpler for MVP, can enhance based on user feedback

---

**Next Steps**: Update IMPLEMENTATION_ROADMAP.md to sequence these enhancements.
