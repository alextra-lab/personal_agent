# System Health Monitoring - Complete Data Structures Specification

**Related**: ADR-0012, ADR-0013
**Date**: 2026-01-17
**Status**: Reference Implementation

---

## Overview

This document defines all data structures used in the system health monitoring enhancement with exact types, field descriptions, and validation rules.

---

## 1. Request Monitoring Data Structures

### 1.1 MetricStats

**File**: `src/personal_agent/brainstem/sensors/types.py`

```python
from typing import TypedDict


class MetricStats(TypedDict):
    """Statistical summary for a single metric over time.
    
    Computed from multiple samples during request execution.
    All values are floats representing the metric's natural unit
    (e.g., percentage for CPU, GB for memory).
    """
    min: float  # Minimum value observed
    max: float  # Maximum value observed
    avg: float  # Arithmetic mean of all samples
```

**Example**:
```python
cpu_stats: MetricStats = {
    "min": 23.4,
    "max": 89.2,
    "avg": 45.7
}
```

### 1.2 MetricSnapshot

**File**: `src/personal_agent/brainstem/sensors/types.py`

```python
from typing import TypedDict


class MetricSnapshot(TypedDict):
    """Single point-in-time snapshot of system metrics.
    
    Captured during request monitoring at regular intervals.
    All metric fields are optional (None if not available).
    """
    timestamp: str  # ISO 8601 UTC timestamp (e.g., "2026-01-17T10:23:45.123456+00:00")
    
    # CPU metrics
    perf_system_cpu_load: float | None  # CPU usage percentage (0-100)
    perf_system_cpu_count: int | None  # Number of CPU cores
    perf_system_load_avg: tuple[float, float, float] | None  # 1/5/15 min load averages
    
    # Memory metrics
    perf_system_mem_used: float | None  # Memory usage percentage (0-100)
    perf_system_mem_total_gb: float | None  # Total RAM in GB
    perf_system_mem_available_gb: float | None  # Available RAM in GB
    
    # Disk metrics
    perf_system_disk_used: float | None  # Disk usage percentage (0-100)
    perf_system_disk_total_gb: float | None  # Total disk space in GB
    perf_system_disk_free_gb: float | None  # Free disk space in GB
    
    # GPU metrics (Apple Silicon, if available)
    perf_system_gpu_load: float | None  # GPU utilization percentage (0-100)
    perf_system_gpu_power_w: float | None  # GPU power consumption in watts
    perf_system_gpu_temp_c: float | None  # GPU temperature in Celsius
```

**Example**:
```python
snapshot: MetricSnapshot = {
    "timestamp": "2026-01-17T10:23:45.123456+00:00",
    "perf_system_cpu_load": 45.2,
    "perf_system_cpu_count": 8,
    "perf_system_load_avg": (2.1, 1.8, 1.5),
    "perf_system_mem_used": 62.5,
    "perf_system_mem_total_gb": 16.0,
    "perf_system_mem_available_gb": 6.0,
    "perf_system_disk_used": 78.1,
    "perf_system_disk_total_gb": 500.0,
    "perf_system_disk_free_gb": 109.5,
    "perf_system_gpu_load": 15.3,
    "perf_system_gpu_power_w": 2.4,
    "perf_system_gpu_temp_c": 48.5
}
```

### 1.3 MetricsSummary

**File**: `src/personal_agent/brainstem/sensors/types.py`

```python
from typing import TypedDict, NotRequired


class MetricsSummary(TypedDict):
    """Aggregated metrics summary from request monitoring.
    
    Returned by RequestMonitor.stop() after monitoring completes.
    Provides statistical analysis of metrics over the request duration.
    """
    # Timing information (REQUIRED)
    duration_seconds: float  # Total monitoring duration
    sample_count: int  # Number of samples collected
    start_time: str  # ISO 8601 UTC timestamp when monitoring started
    end_time: str  # ISO 8601 UTC timestamp when monitoring stopped
    
    # Statistical summaries (OPTIONAL - only present if samples collected)
    cpu: NotRequired[MetricStats]  # CPU statistics
    memory: NotRequired[MetricStats]  # Memory statistics
    disk: NotRequired[MetricStats]  # Disk statistics
    gpu: NotRequired[MetricStats]  # GPU statistics (if available)
    
    # Control loop information (REQUIRED)
    threshold_violations: list[str]  # List of violated threshold names
```

**Example**:
```python
summary: MetricsSummary = {
    "duration_seconds": 12.4,
    "sample_count": 3,
    "start_time": "2026-01-17T10:23:45.000000+00:00",
    "end_time": "2026-01-17T10:23:57.400000+00:00",
    "cpu": {
        "min": 34.2,
        "max": 89.4,
        "avg": 56.7
    },
    "memory": {
        "min": 58.1,
        "max": 64.3,
        "avg": 61.2
    },
    "gpu": {
        "min": 12.1,
        "max": 18.7,
        "avg": 15.4
    },
    "threshold_violations": ["cpu_overload"]
}
```

---

## 2. Enhanced System Health Tool Data Structures

### 2.1 Tool Parameters (Input)

**File**: `src/personal_agent/tools/system_health.py`

```python
from typing import TypedDict


class SystemHealthParams(TypedDict, total=False):
    """Parameters for system_metrics_snapshot tool.
    
    All parameters are optional. Default behavior is current snapshot only.
    """
    window_str: str | None  # Time window (e.g., "30m", "1h", "24h")
    trace_id: str | None  # Trace ID for specific request
    include_history: bool  # Whether to include full time-series data (default: False)
    stat_summary: bool  # Whether to include statistical summary (default: True)
```

**Validation Rules**:
- `window_str` must match pattern: `^\d+[smhd]$` (number + unit)
- `window_str` max value: 24h (prevent expensive queries)
- `trace_id` must be valid UUID format if provided
- If both `window_str` and `trace_id` provided, `trace_id` takes precedence

**Examples**:
```python
# Current only (default)
params: SystemHealthParams = {}

# Last hour with summary
params: SystemHealthParams = {
    "window_str": "1h",
    "stat_summary": True
}

# Specific request with full history
params: SystemHealthParams = {
    "trace_id": "abc-123-xyz-789",
    "include_history": True
}
```

### 2.2 Tool Response (Output)

**File**: `src/personal_agent/tools/system_health.py`

```python
from typing import TypedDict, NotRequired


class CurrentMetrics(TypedDict):
    """Current system metrics snapshot."""
    timestamp: str  # ISO 8601 UTC
    perf_system_cpu_load: float
    perf_system_mem_used: float
    perf_system_disk_used: float
    perf_system_cpu_count: int
    perf_system_mem_total_gb: float
    perf_system_mem_available_gb: float
    perf_system_disk_total_gb: float
    perf_system_disk_free_gb: float
    perf_system_load_avg: tuple[float, float, float] | None
    perf_system_gpu_load: float | None
    perf_system_gpu_power_w: float | None
    perf_system_gpu_temp_c: float | None


class HistoricalSummary(TypedDict):
    """Statistical summary of historical metrics."""
    duration_seconds: float
    sample_count: int
    start_time: str  # ISO 8601 UTC
    end_time: str  # ISO 8601 UTC
    cpu: NotRequired[MetricStats]
    memory: NotRequired[MetricStats]
    disk: NotRequired[MetricStats]
    gpu: NotRequired[MetricStats]


class SystemHealthResponse(TypedDict):
    """Response from system_metrics_snapshot tool."""
    success: bool
    error: str | None
    
    # Current snapshot (ALWAYS present if success=True)
    current: CurrentMetrics | None
    
    # Historical data (only if window_str or trace_id provided)
    history: NotRequired[list[MetricSnapshot]]  # Full time series
    summary: NotRequired[HistoricalSummary]  # Statistical summary
```

**Example - Current Only**:
```python
response: SystemHealthResponse = {
    "success": True,
    "error": None,
    "current": {
        "timestamp": "2026-01-17T10:23:45.123456+00:00",
        "perf_system_cpu_load": 45.2,
        "perf_system_mem_used": 62.5,
        # ... other fields
    }
}
```

**Example - With History**:
```python
response: SystemHealthResponse = {
    "success": True,
    "error": None,
    "current": { /* ... */ },
    "summary": {
        "duration_seconds": 3600.0,
        "sample_count": 720,  # One per 5 seconds for an hour
        "start_time": "2026-01-17T09:23:45.000000+00:00",
        "end_time": "2026-01-17T10:23:45.000000+00:00",
        "cpu": {
            "min": 23.4,
            "max": 89.2,
            "avg": 42.1
        },
        # ... other metrics
    },
    "history": [  # Only if include_history=True
        { /* snapshot 1 */ },
        { /* snapshot 2 */ },
        # ...
    ]
}
```

**Example - Error**:
```python
response: SystemHealthResponse = {
    "success": False,
    "error": "Invalid time window: '25h' exceeds maximum of 24h",
    "current": None
}
```

---

## 3. Orchestrator Integration Data Structures

### 3.1 ExecutionContext Extension

**File**: `src/personal_agent/orchestrator/types.py`

**Add this field to ExecutionContext dataclass**:

```python
@dataclass
class ExecutionContext:
    """Mutable state container passed through execution steps."""
    
    # ... existing fields ...
    
    # Request monitoring (ADR-0012)
    metrics_summary: MetricsSummary | None = None
    """
    Aggregated metrics summary from request monitoring.
    Populated by RequestMonitor.stop() in executor.py.
    Used by Captain's Log for performance context.
    None if monitoring disabled or not yet completed.
    """
```

### 3.2 Telemetry Event

**File**: `src/personal_agent/telemetry/events.py`

**Add this constant**:

```python
# Request monitoring events (ADR-0012)
REQUEST_METRICS_SUMMARY = "request_metrics_summary"
```

**Event Structure**:
```python
{
    "event": "request_metrics_summary",
    "timestamp": "2026-01-17T10:23:57.400000+00:00",
    "trace_id": "abc-123-xyz",
    "session_id": "session-456",
    "duration_seconds": 12.4,
    "sample_count": 3,
    "cpu_avg": 56.7,
    "cpu_max": 89.4,
    "memory_avg": 61.2,
    "memory_max": 64.3,
    "gpu_avg": 15.4,  # If available
    "threshold_violations": ["cpu_overload"]
}
```

---

## 4. Control Loop Data Structures (Phase 3)

### 4.1 Threshold Configuration

**File**: `config/governance/modes.yaml` (existing, reference only)

```yaml
modes:
  NORMAL:
    thresholds:
      cpu_load_percent: 85.0  # float
      memory_used_percent: 80.0  # float
      policy_violations_per_10min: 3  # int
```

### 4.2 Control Signal

**File**: `src/personal_agent/brainstem/sensors/request_monitor.py` (Phase 3)

```python
from typing import TypedDict


class ControlSignal(TypedDict):
    """Control signal emitted when threshold violated."""
    signal_type: str  # e.g., "cpu_overload", "memory_pressure"
    metric_name: str  # e.g., "perf_system_cpu_load"
    metric_value: float  # Current value that triggered signal
    threshold_value: float  # Threshold that was exceeded
    trace_id: str  # Request that triggered signal
    timestamp: str  # ISO 8601 UTC
```

**Example**:
```python
signal: ControlSignal = {
    "signal_type": "cpu_overload",
    "metric_name": "perf_system_cpu_load",
    "metric_value": 89.2,
    "threshold_value": 85.0,
    "trace_id": "abc-123-xyz",
    "timestamp": "2026-01-17T10:23:50.000000+00:00"
}
```

---

## 5. Telemetry Query Data Structures (Phase 5)

### 5.1 Query Parameters

**File**: `src/personal_agent/telemetry/metrics.py`

```python
from typing import TypedDict


class EventQueryParams(TypedDict, total=False):
    """Parameters for query_events() function."""
    event: str | None  # Event name filter (e.g., "system_metrics_snapshot")
    window_str: str | None  # Time window (e.g., "1h")
    component: str | None  # Component name filter (e.g., "brainstem")
    trace_id: str | None  # Trace ID filter (NEW in Phase 5)
    limit: int | None  # Maximum number of results
```

### 5.2 Log Entry Structure

**File**: `src/personal_agent/telemetry/metrics.py`

```python
from typing import TypedDict, Any


class LogEntry(TypedDict):
    """Structure of a telemetry log entry."""
    timestamp: str  # ISO 8601 UTC
    level: str  # "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
    event: str  # Semantic event name
    component: str  # Component that emitted log
    trace_id: str | None  # Trace ID for correlation
    span_id: str | None  # Span ID for nested operations
    # ... additional fields vary by event type
```

---

## 6. Captain's Log Integration (Phase 4)

### 6.1 Enhanced Reflection Context

**File**: `src/personal_agent/captains_log/reflection.py`

```python
from typing import TypedDict
from personal_agent.brainstem.sensors.types import MetricsSummary
from personal_agent.orchestrator.types import ExecutionContext


class ReflectionContext(TypedDict):
    """Context data for Captain's Log reflection."""
    user_message: str
    steps_count: int
    steps_summary: str  # Formatted summary of steps
    metrics_summary: MetricsSummary | None  # NEW: Performance context
    trace_id: str
    session_id: str
    success: bool
    error: str | None
```

---

## 7. Validation Utilities

### 7.1 Time Window Validation

**File**: `src/personal_agent/telemetry/metrics.py`

```python
import re
from datetime import timedelta


def validate_time_window(window_str: str) -> timedelta:
    """Validate and parse time window string.
    
    Args:
        window_str: Time window (e.g., "30m", "1h", "24h")
    
    Returns:
        Timedelta object
    
    Raises:
        ValueError: If format invalid or exceeds maximum
    """
    pattern = r'^(\d+)([smhd])$'
    match = re.match(pattern, window_str.lower())
    
    if not match:
        raise ValueError(
            f"Invalid time window format: {window_str}. "
            f"Expected format: <number><unit> (e.g., '30m', '1h', '24h')"
        )
    
    value = int(match.group(1))
    unit = match.group(2)
    
    unit_map = {
        's': timedelta(seconds=1),
        'm': timedelta(minutes=1),
        'h': timedelta(hours=1),
        'd': timedelta(days=1),
    }
    
    delta = value * unit_map[unit]
    
    # Enforce maximum (24 hours)
    max_delta = timedelta(hours=24)
    if delta > max_delta:
        raise ValueError(
            f"Time window {window_str} exceeds maximum of 24h"
        )
    
    return delta
```

### 7.2 Trace ID Validation

**File**: `src/personal_agent/telemetry/trace.py`

```python
import uuid


def validate_trace_id(trace_id: str) -> bool:
    """Validate trace ID format.
    
    Args:
        trace_id: Trace ID string
    
    Returns:
        True if valid UUID format
    """
    try:
        uuid.UUID(trace_id)
        return True
    except ValueError:
        return False
```

---

## 8. Type Annotations Summary

All data structures use Python 3.12+ type hints:

- `TypedDict` for structured dictionaries
- `NotRequired` for optional TypedDict fields
- `X | None` for nullable types
- `list[X]` for homogeneous lists
- `dict[K, V]` for typed dictionaries
- `tuple[X, Y, Z]` for fixed-size tuples

**Type Checking**:
```bash
# Verify all types are correct
mypy src/personal_agent/brainstem/sensors/
mypy src/personal_agent/tools/system_health.py
mypy src/personal_agent/telemetry/metrics.py
```

---

## 9. JSON Serialization

All data structures are JSON-serializable for:
- Logging to JSONL files
- Tool responses
- API responses (future)

**Serialization Helpers**:

```python
import json
from datetime import datetime, timezone
from typing import Any


def serialize_summary(summary: MetricsSummary) -> str:
    """Serialize MetricsSummary to JSON string."""
    return json.dumps(summary, indent=2)


def deserialize_summary(json_str: str) -> MetricsSummary:
    """Deserialize JSON string to MetricsSummary."""
    return json.loads(json_str)


def format_timestamp() -> str:
    """Format current time as ISO 8601 UTC."""
    return datetime.now(timezone.utc).isoformat()
```

---

## 10. Migration Guide

When adding new fields to existing structures:

1. **Use `NotRequired`** for optional fields
2. **Provide defaults** in code for backward compatibility
3. **Update documentation** with field descriptions
4. **Add validation** if field has constraints
5. **Test serialization** to ensure JSON compatibility

**Example**:
```python
class MetricsSummary(TypedDict):
    # Existing fields...
    
    # NEW field (Phase 3)
    threshold_violations: NotRequired[list[str]]  # Defaults to empty list
```

---

**This specification provides exact types and structures for all phases.**
