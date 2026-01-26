# ADR-0014: Structured Metrics in Captain's Log

**Status**: Accepted  
**Date**: 2026-01-17  
**Deciders**: System Architect  
**Related**: ADR-0010 (Structured LLM Outputs), ADR-0012 (Request-Scoped Metrics)

## Context

Captain's Log entries currently store metrics as human-readable strings (e.g., `"cpu: 9.3%"`, `"duration: 5.4s"`). While this format is readable and LLM-friendly, it has limitations:

**Current Implementation**:
```json
{
  "supporting_metrics": [
    "llm_calls: 2",
    "duration: 5.4s",
    "cpu_utilization: 9.3%"
  ]
}
```

**Problems Identified**:
1. ❌ Difficult to aggregate numerically across entries
2. ❌ No type safety - values are strings
3. ❌ Parsing required for analytics and visualization
4. ❌ Inconsistent formats (units embedded in strings)
5. ❌ Cannot efficiently query by metric value ranges
6. ⚠️ Quote escaping issues when LLM generates metrics

**Use Cases Requiring Structured Data**:
- Time-series analysis of system performance
- Anomaly detection (CPU spikes, memory leaks)
- Performance regression testing
- Cross-request trend analysis
- Automated alerts based on thresholds
- Dashboard visualizations

## Decision

**Adopt a hybrid approach** that maintains human-readable strings while adding optional structured metrics for programmatic analysis.

### Key Principles

1. **Backward Compatibility**: Existing entries with string-only metrics continue to work
2. **Non-Breaking**: New field is optional, gradual migration
3. **Human-First**: Preserve readable format for manual review
4. **Machine-Friendly**: Add structured format for analytics
5. **LLM-Friendly**: Keep simple comma-separated string for LLM generation

## Solution Design

### Data Model

```python
from pydantic import BaseModel, Field

class Metric(BaseModel):
    """Structured metric with typed value and optional unit."""
    
    name: str = Field(..., description="Metric identifier (e.g., 'cpu_percent', 'duration_seconds')")
    value: float | int | str = Field(..., description="Metric value (prefer numbers when possible)")
    unit: str | None = Field(None, description="Unit of measurement (e.g., '%', 's', 'ms', 'MB')")
    
    class Config:
        json_schema_extra = {
            "examples": [
                {"name": "cpu_percent", "value": 9.3, "unit": "%"},
                {"name": "duration_seconds", "value": 5.4, "unit": "s"},
                {"name": "llm_calls", "value": 2, "unit": None}
            ]
        }

class CaptainLogEntry(BaseModel):
    # ... existing fields ...
    
    # Human-readable (keep for backward compatibility and manual review)
    supporting_metrics: list[str] = Field(
        default_factory=list,
        description="Human-readable metrics (e.g., 'cpu: 9.3%')"
    )
    
    # Machine-readable (new, optional for analytics)
    metrics_structured: list[Metric] | None = Field(
        None,
        description="Structured metrics for programmatic analysis (ADR-0014)"
    )
```

### Example Entry

```json
{
  "entry_id": "CL-20260117-173000-abc12345-001",
  "type": "reflection",
  "title": "Task: System health check",
  "rationale": "Task completed efficiently with low resource usage...",
  
  "supporting_metrics": [
    "llm_calls: 2",
    "duration: 5.4s",
    "cpu: 9.3%",
    "memory: 53.4%",
    "gpu: 3.2%"
  ],
  
  "metrics_structured": [
    {"name": "llm_calls", "value": 2, "unit": null},
    {"name": "duration_seconds", "value": 5.4, "unit": "s"},
    {"name": "cpu_percent", "value": 9.3, "unit": "%"},
    {"name": "memory_percent", "value": 53.4, "unit": "%"},
    {"name": "gpu_percent", "value": 3.2, "unit": "%"}
  ]
}
```

### Metric Naming Convention

**Standard Metric Names** (for consistency):

| Metric | Name | Unit | Type |
|--------|------|------|------|
| LLM Calls | `llm_calls` | `null` | `int` |
| Duration | `duration_seconds` | `s` | `float` |
| CPU Usage | `cpu_percent` | `%` | `float` |
| Memory Usage | `memory_percent` | `%` | `float` |
| GPU Usage | `gpu_percent` | `%` | `float` |
| Tool Executions | `tool_calls` | `null` | `int` |
| Threshold Violations | `threshold_violations` | `null` | `int` |
| Samples Collected | `samples_collected` | `null` | `int` |

**Benefits**:
- Consistent naming across all entries
- Clear type expectations
- Queryable by standardized names
- Extensible for future metrics

## Implementation

### Phase 1: Schema Update (Week 6, Day 33-34)

1. Add `Metric` model to `src/personal_agent/captains_log/models.py`
2. Add `metrics_structured` field to `CaptainLogEntry`
3. Update Pydantic validators if needed
4. Add unit tests for new models

**Files Modified**:
- `src/personal_agent/captains_log/models.py`
- `tests/test_captains_log/test_models.py`

### Phase 2: DSPy Integration (Week 6, Day 35)

Update DSPy reflection to populate both fields:

```python
# Extract from metrics_summary
def _extract_structured_metrics(
    metrics_summary: dict[str, Any] | None,
    telemetry_summary: str
) -> tuple[list[str], list[Metric]]:
    """Extract both string and structured metrics."""
    
    string_metrics = []
    structured_metrics = []
    
    if metrics_summary:
        # Duration
        if "duration_seconds" in metrics_summary:
            dur = metrics_summary["duration_seconds"]
            string_metrics.append(f"duration: {dur:.1f}s")
            structured_metrics.append(
                Metric(name="duration_seconds", value=dur, unit="s")
            )
        
        # CPU
        if "cpu_avg" in metrics_summary:
            cpu = metrics_summary["cpu_avg"]
            string_metrics.append(f"cpu: {cpu:.1f}%")
            structured_metrics.append(
                Metric(name="cpu_percent", value=cpu, unit="%")
            )
        
        # Memory
        if "memory_avg" in metrics_summary:
            mem = metrics_summary["memory_avg"]
            string_metrics.append(f"memory: {mem:.1f}%")
            structured_metrics.append(
                Metric(name="memory_percent", value=mem, unit="%")
            )
        
        # GPU
        if "gpu_avg" in metrics_summary:
            gpu = metrics_summary["gpu_avg"]
            string_metrics.append(f"gpu: {gpu:.1f}%")
            structured_metrics.append(
                Metric(name="gpu_percent", value=gpu, unit="%")
            )
    
    return string_metrics, structured_metrics
```

**Files Modified**:
- `src/personal_agent/captains_log/reflection_dspy.py`
- `src/personal_agent/captains_log/reflection.py`

### Phase 3: Analytics Utilities (Week 6, Day 36)

Add helper functions for querying and analyzing structured metrics:

```python
# src/personal_agent/captains_log/analytics.py

from pathlib import Path
import json
from typing import Iterator
from personal_agent.captains_log.models import CaptainLogEntry, Metric

def query_metrics(
    metric_name: str,
    log_dir: Path | None = None,
    time_range_hours: int | None = None
) -> list[tuple[str, float | int]]:
    """Query metric values across all Captain's Log entries.
    
    Returns list of (entry_id, value) tuples.
    """
    # Implementation...

def get_metric_statistics(
    metric_name: str,
    log_dir: Path | None = None
) -> dict[str, float]:
    """Get statistical summary (min/max/avg/p50/p95) for a metric."""
    # Implementation...

def detect_anomalies(
    metric_name: str,
    threshold_stddev: float = 2.0
) -> list[str]:
    """Detect anomalous metric values (> N standard deviations)."""
    # Implementation...
```

**Files Added**:
- `src/personal_agent/captains_log/analytics.py`
- `tests/test_captains_log/test_analytics.py`

### Phase 4: Documentation (Week 6, Day 36)

Update documentation:
- `src/personal_agent/captains_log/AGENTS.md`
- `METRICS_STORAGE_GUIDE.md`
- `./captains_log/README.md`

## Migration Strategy

### Gradual Migration (Non-Breaking)

**Week 1 (Implementation)**:
- New entries include both `supporting_metrics` and `metrics_structured`
- Old entries remain unchanged (missing field = `None`)
- No re-processing of historical data

**Week 2-4 (Adoption)**:
- Analytics tools check for `metrics_structured` first
- Fallback to parsing `supporting_metrics` if not available
- Gradual adoption in analysis scripts

**Future (Optional)**:
- Consider backfilling old entries (low priority)
- Deprecate string-only format (after 6+ months)

### Backward Compatibility

```python
def get_cpu_usage(entry: CaptainLogEntry) -> float | None:
    """Get CPU usage, checking structured first, then parsing string."""
    
    # Prefer structured
    if entry.metrics_structured:
        for metric in entry.metrics_structured:
            if metric.name == "cpu_percent":
                return float(metric.value)
    
    # Fallback to parsing
    for m in entry.supporting_metrics:
        if "cpu" in m.lower():
            # Parse "cpu: 9.3%" format
            try:
                return float(m.split(":")[1].strip().rstrip("%"))
            except (ValueError, IndexError):
                continue
    
    return None
```

## Consequences

### Positive

✅ **Analytics-Ready**: Direct access to numerical values  
✅ **Type-Safe**: Pydantic validation ensures correct types  
✅ **Backward Compatible**: No breaking changes to existing entries  
✅ **Future-Proof**: Easy to add new metrics  
✅ **Queryable**: Can filter/aggregate by metric values  
✅ **Consistent**: Standardized naming convention  
✅ **Human-Friendly**: Readable strings preserved  

### Negative

⚠️ **Slight Redundancy**: Both string and structured formats  
⚠️ **Maintenance**: Must keep both formats in sync  
⚠️ **Storage**: ~20% larger JSON files (minimal impact)  

### Neutral

ℹ️ **Optional**: Old code continues to work  
ℹ️ **Gradual**: Can migrate over time  

## Alternatives Considered

### Alternative 1: Pure Structured Format

Replace `list[str]` with `list[Metric]` entirely.

**Rejected Because**:
- Breaking change for all existing entries
- Less human-readable in JSON
- Harder for LLM to generate correctly
- Higher implementation risk

### Alternative 2: Parse-on-Read

Keep current format, parse strings when needed.

**Rejected Because**:
- Fragile parsing logic
- No validation
- Performance overhead
- Error-prone for edge cases

### Alternative 3: Separate Metrics File

Store structured metrics in separate files (e.g., `CL-*.metrics.json`).

**Rejected Because**:
- Breaks cohesion (split data)
- More complex to query
- Synchronization issues
- File management overhead

## Validation Criteria

**Acceptance Criteria**:
1. ✅ New entries have both `supporting_metrics` and `metrics_structured`
2. ✅ Old entries load without errors (missing field handled)
3. ✅ Analytics can query by metric name and value
4. ✅ JSON schema validates correctly
5. ✅ Unit tests cover all metric types
6. ✅ Documentation updated

**Success Metrics**:
- Zero breaking changes to existing code
- <5% increase in entry file size
- Analytics queries <100ms for 1000 entries
- 100% test coverage for new models

## References

- **ADR-0010**: Structured LLM Outputs via Pydantic Models
- **ADR-0012**: Request-Scoped Metrics Monitoring
- **E-008**: DSPy Prototype Evaluation
- **METRICS_FORMAT_PROPOSAL.md**: Original analysis
- **METRICS_STORAGE_GUIDE.md**: Storage architecture

## Implementation Timeline

**Target**: Week 6 (Days 33-36)  
**Effort**: ~8-12 hours  
**Priority**: Medium (enhances analytics, not blocking)

---

**Decision**: Approved for implementation in Week 6  
**Next Steps**: Add to `IMPLEMENTATION_ROADMAP.md` and schedule implementation
