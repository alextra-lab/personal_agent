# System Metrics Storage & Access Guide

**Date**: 2026-01-17
**Component**: Request-Scoped Metrics Monitoring (ADR-0012)

## Summary

✅ **Metrics ARE being captured and provided to Captain's Log**
✅ **Metrics ARE stored in multiple locations**
✅ **Metrics ARE enriched in reflections**

## Where Metrics Are Stored

### 1. **Telemetry Event Logs** (Streaming)

**Location**: Logged to stdout/stderr (JSON format)

**Event Types**:
- `SYSTEM_METRICS_SNAPSHOT` - Individual metric samples
- `request_metrics_summary` - Aggregated summaries

**Example Output**:
```json
{
  "timestamp": "2026-01-17T17:26:52.975887Z",
  "event": "system_metrics_snapshot",
  "component": "request_monitor",
  "trace_id": "88fb153e-41b2-4ee4-83da-f9d40bd79530",
  "cpu_percent": 9.3,
  "memory_percent": 53.4,
  "gpu_percent": 3.2
}
```

```json
{
  "timestamp": "2026-01-17T17:26:54.688225Z",
  "event": "request_metrics_summary",
  "component": "executor",
  "trace_id": "88fb153e-41b2-4ee4-83da-f9d40bd79530",
  "duration_seconds": 5.4,
  "samples_collected": 1,
  "cpu_avg": 9.3,
  "cpu_min": 9.3,
  "cpu_max": 9.3,
  "memory_avg": 53.4,
  "memory_min": 53.4,
  "memory_max": 53.4,
  "gpu_avg": 3.2,
  "gpu_min": 3.2,
  "gpu_max": 3.2,
  "threshold_violations": []
}
```

**Access Method**:
```bash
# View live metrics during runtime
python -m personal_agent.ui.cli "test" 2>&1 | grep system_metrics_snapshot

# Query by trace_id
grep "88fb153e" output.log | grep system_metrics_snapshot
```

**Persistence**: Not persisted by default (stdout/stderr). Can be redirected to file.

---

### 2. **Captain's Log JSON Files** (Persistent)

**Location**: `telemetry/captains_log/CL-<TIMESTAMP>-<TRACE>-<SEQ>-<TITLE>.json`

**Metrics Included**:
- In `supporting_metrics` array (high-level)
- In LLM's `telemetry_summary` context (detailed, passed to reflection LLM)

**Example**:
```json
{
  "entry_id": "CL-20260117-172712-88fb153e-001",
  "trace_id": "88fb153e-41b2-4ee4-83da-f9d40bd79530",
  "supporting_metrics": [
    "llm_calls: 2",
    "total_duration: 5.4s",
    "avg_llm_call_duration: 2701ms"
  ],
  "rationale": "While the task completed successfully, the response to a simple greeting required unnecessary model invocations..."
}
```

**Note**: The detailed metrics (CPU/memory/GPU) are in the `telemetry_summary` string that's passed to the LLM during reflection generation. The LLM sees:

```
**System Performance (Request-Scoped)**:
- Duration: 5.4s
- Samples: 1 metric snapshots
- CPU: avg=9.3%, min=9.3%, max=9.3%
- Memory: avg=53.4%, min=53.4%, max=53.4%
- GPU: avg=3.2%, min=3.2%, max=3.2%
```

**Access Method**:
```bash
# View all Captain's Log entries
ls -lt telemetry/captains_log/

# Read specific entry
cat telemetry/captains_log/CL-20260117-172712-88fb153e-001-*.json | jq

# Filter by trace_id
ls telemetry/captains_log/ | grep "88fb153e"

# Extract metrics from recent entries
cat telemetry/captains_log/CL-202601*.json | jq '.supporting_metrics'
```

**Persistence**: ✅ Persistent (JSON files on disk)

---

### 3. **ExecutionContext (Runtime Only)**

**Location**: In-memory during task execution

**Field**: `ctx.metrics_summary`

**Structure**:
```python
{
    "duration_seconds": 5.4,
    "samples_collected": 1,
    "cpu_avg": 9.3,
    "cpu_min": 9.3,
    "cpu_max": 9.3,
    "memory_avg": 53.4,
    "memory_min": 53.4,
    "memory_max": 53.4,
    "gpu_avg": 3.2,
    "gpu_min": 3.2,
    "gpu_max": 3.2,
    "threshold_violations": []
}
```

**Access Method**:
```python
# Within orchestrator or tools
from personal_agent.orchestrator.types import ExecutionContext

async def my_function(ctx: ExecutionContext):
    if ctx.metrics_summary:
        cpu_avg = ctx.metrics_summary.get("cpu_avg")
        print(f"Average CPU: {cpu_avg}%")
```

**Persistence**: ❌ Runtime only (not saved after task completes)

---

## Historical Query Support

### Current Status

**What's Available Now**:
1. ✅ **Per-Request Metrics**: Captured and tagged with `trace_id`
2. ✅ **Captain's Log Storage**: Metrics preserved in JSON files
3. ✅ **Trace Correlation**: Can query by trace_id to find related metrics

**What's NOT Yet Implemented** (from ADR-0013):
1. ❌ **Time-Series Database**: No dedicated metrics storage (e.g., SQLite, InfluxDB)
2. ❌ **Historical Query API**: No `system_health(trace_id=X, time_range=Y)` function
3. ❌ **Aggregated Analytics**: No cross-request trend analysis

### Querying Historical Metrics (Manual Method)

**Query by Trace ID**:
```bash
# Find Captain's Log entry
trace_id="88fb153e"
file=$(ls telemetry/captains_log/ | grep "$trace_id")
cat "telemetry/captains_log/$file" | jq '.supporting_metrics'
```

**Query by Time Range**:
```bash
# Find entries from specific date
ls telemetry/captains_log/CL-20260117-* | while read file; do
  echo "=== $file ==="
  cat "$file" | jq '{entry_id, timestamp, supporting_metrics}'
done
```

**Query Recent Trends**:
```bash
# Aggregate CPU usage from recent entries
cat telemetry/captains_log/CL-202601*.json | jq -r '.supporting_metrics[]' | grep -o 'cpu_avg=[0-9.]*'
```

---

## Metrics Flow Architecture

```
┌───────────────────────────────────────────────┐
│ 1. REQUEST STARTS                              │
│    RequestMonitor.start()                      │
└────────────────┬──────────────────────────────┘
                 │
                 ▼
┌───────────────────────────────────────────────┐
│ 2. BACKGROUND POLLING (every 5s)               │
│    - poll_system_metrics()                     │
│    - Log SYSTEM_METRICS_SNAPSHOT              │
│    - Tag with trace_id                         │
│    - Store in _samples[]                       │
└────────────────┬──────────────────────────────┘
                 │
                 ▼
┌───────────────────────────────────────────────┐
│ 3. REQUEST COMPLETES                           │
│    RequestMonitor.stop()                       │
│    - Compute summary (min/max/avg)             │
│    - Attach to ctx.metrics_summary             │
│    - Log request_metrics_summary               │
└────────────────┬──────────────────────────────┘
                 │
                 ▼
┌───────────────────────────────────────────────┐
│ 4. CAPTAIN'S LOG REFLECTION                    │
│    generate_reflection_entry(metrics_summary)  │
│    - Enrich telemetry_summary with metrics     │
│    - LLM sees detailed performance context     │
│    - Store in JSON file                        │
└───────────────────────────────────────────────┘
```

---

## Accessing Metrics Programmatically

### Python API

```python
from personal_agent.brainstem.sensors.sensors import poll_system_metrics

# Get current metrics
metrics = poll_system_metrics()
print(f"CPU: {metrics['perf_system_cpu_load']}%")
print(f"Memory: {metrics['perf_system_mem_used']}%")
print(f"GPU: {metrics.get('perf_system_gpu_load', 'N/A')}%")
```

### Query Captain's Log

```python
import json
from pathlib import Path

# Find all entries for a trace
trace_id = "88fb153e"
log_dir = Path("telemetry/captains_log")

for file in log_dir.glob(f"*{trace_id[:8]}*.json"):
    entry = json.loads(file.read_text())
    print(f"Entry: {entry['entry_id']}")
    print(f"Metrics: {entry['supporting_metrics']}")
```

---

## Future Enhancements (ADR-0013)

### Planned: Time-Series Storage

```python
# Future API design
from personal_agent.tools.system_health import query_metrics

# Query historical metrics
metrics = await query_metrics(
    trace_id="88fb153e",
    metric_names=["cpu", "memory", "gpu"],
    time_range_minutes=60
)

# Analyze trends
for snapshot in metrics:
    print(f"{snapshot['timestamp']}: CPU={snapshot['cpu']}%")
```

### Planned: Enhanced system_health Tool

```yaml
tools:
  - name: system_health
    parameters:
      - trace_id: str (optional) - Filter by specific trace
      - time_range_minutes: int (optional) - Historical window
      - metric_names: list[str] (optional) - Filter specific metrics
```

---

## Storage Locations Summary

| Location | Type | Persistence | Query Method | Contains |
|----------|------|-------------|--------------|----------|
| **Telemetry Events** | Stdout/stderr | No (unless redirected) | `grep` | Individual snapshots |
| **Captain's Log JSON** | File (JSON) | ✅ Yes | `ls`, `cat`, `jq` | Aggregated summaries |
| **ExecutionContext** | In-memory | No | Python API | Runtime summary |
| **Future: TimeSeries DB** | SQLite/InfluxDB | ✅ Yes | Query API | Full history |

---

## Testing

```bash
# Run test scenario and observe metrics
python -m personal_agent.ui.cli "test message" 2>&1 | grep -E "system_metrics_snapshot|request_metrics_summary"

# Check Captain's Log includes metrics
ls -lt telemetry/captains_log/ | head -1 | awk '{print $NF}' | xargs cat | jq '.supporting_metrics'

# Query specific trace
trace_id="YOUR_TRACE_ID_HERE"
grep "$trace_id" output.log | grep system_metrics_snapshot
```

---

## Key Findings

✅ **Metrics ARE captured** - Fixed key mismatch issue
✅ **Metrics ARE logged** - Tagged with trace_id
✅ **Metrics ARE persistent** - Stored in Captain's Log JSON files
✅ **Metrics ARE enriched** - Provided to LLM during reflection
❌ **Time-series queries NOT yet implemented** - Manual queries required

All core functionality is working! ADR-0013 enhancements (time-series DB, query API) are optional future improvements.
