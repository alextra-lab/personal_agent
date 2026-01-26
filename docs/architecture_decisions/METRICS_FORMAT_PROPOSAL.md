# Captain's Log Metrics Format Analysis

## Current Implementation

**Type**: `supporting_metrics: list[str]`

**Example**:
```json
{
  "supporting_metrics": [
    "llm_calls: 2",
    "duration: 5.4s",
    "cpu_utilization: 9.3%",
    "gpu_utilization: 3.2%"
  ]
}
```

**Pros**:
- ✅ Human-readable
- ✅ Flexible (any format)
- ✅ LLM-friendly (natural text)
- ✅ Backward compatible

**Cons**:
- ❌ Harder to query numerically
- ❌ No type safety
- ❌ Can't aggregate across entries
- ❌ Parsing required for analysis

---

## Option 1: Pure Structured (Breaking Change)

**Type**: `supporting_metrics: list[Metric]`

```python
class Metric(BaseModel):
    name: str
    value: float | int | str
    unit: str | None = None
```

**Example**:
```json
{
  "supporting_metrics": [
    {"name": "llm_calls", "value": 2, "unit": null},
    {"name": "duration", "value": 5.4, "unit": "s"},
    {"name": "cpu_utilization", "value": 9.3, "unit": "%"},
    {"name": "gpu_utilization", "value": 3.2, "unit": "%"}
  ]
}
```

**Pros**:
- ✅ Type-safe
- ✅ Easy to query/aggregate
- ✅ Structured for analytics

**Cons**:
- ❌ Breaking change (all existing entries incompatible)
- ❌ Less human-readable
- ❌ Harder for LLM to generate correctly
- ❌ More complex parsing

---

## Option 2: Hybrid (Best of Both) ⭐ RECOMMENDED

**Add optional structured field, keep string version**:

```python
class Metric(BaseModel):
    name: str
    value: float | int | str
    unit: str | None = None

class CaptainLogEntry(BaseModel):
    # ... existing fields ...

    # Human-readable (keep for backward compatibility)
    supporting_metrics: list[str] = Field(default_factory=list)

    # Machine-readable (new, optional)
    metrics_structured: list[Metric] | None = Field(
        None,
        description="Structured metrics for programmatic analysis"
    )
```

**Example**:
```json
{
  "supporting_metrics": [
    "llm_calls: 2",
    "duration: 5.4s",
    "cpu_utilization: 9.3%"
  ],
  "metrics_structured": [
    {"name": "llm_calls", "value": 2, "unit": null},
    {"name": "duration_seconds", "value": 5.4, "unit": "s"},
    {"name": "cpu_percent", "value": 9.3, "unit": "%"}
  ]
}
```

**Pros**:
- ✅ Backward compatible
- ✅ Human-readable strings preserved
- ✅ Structured data for analytics
- ✅ Optional (no breaking changes)
- ✅ Can migrate gradually

**Cons**:
- ⚠️ Slight redundancy
- ⚠️ Need to maintain both

---

## Option 3: Parse-on-Read (No Schema Change)

Keep current format, add utility function:

```python
from typing import NamedTuple

class ParsedMetric(NamedTuple):
    name: str
    value: float | str
    unit: str | None

def parse_metric(metric_str: str) -> ParsedMetric:
    """Parse 'name: value unit' format."""
    # "llm_calls: 2" -> ParsedMetric("llm_calls", 2, None)
    # "duration: 5.4s" -> ParsedMetric("duration", 5.4, "s")
    # "cpu: 9.3%" -> ParsedMetric("cpu", 9.3, "%")

    name, rest = metric_str.split(":", 1)
    # ... parsing logic ...
    return ParsedMetric(name.strip(), value, unit)
```

**Pros**:
- ✅ No schema changes
- ✅ Backward compatible
- ✅ Structured access when needed

**Cons**:
- ❌ Fragile parsing
- ❌ No validation
- ❌ Inconsistent formats

---

## Recommendation: Option 2 (Hybrid)

**Implementation**:

1. Add `Metric` model and `metrics_structured` field
2. Update DSPy reflection to populate both fields
3. Old entries continue to work (missing field = `None`)
4. Analytics can use structured field when available

**Migration Path**:
- Week 1: Add new fields, populate for new entries
- Week 2-4: Gradually adopt in analysis tools
- Future: Consider deprecating string-only format

**Analytics Example**:
```python
# Query CPU usage across all entries
import json
from pathlib import Path

cpu_values = []
for file in Path("telemetry/captains_log").glob("*.json"):
    entry = json.loads(file.read_text())

    # Use structured if available
    if entry.get("metrics_structured"):
        for metric in entry["metrics_structured"]:
            if metric["name"] == "cpu_percent":
                cpu_values.append(metric["value"])
    # Fallback to parsing strings
    else:
        for m in entry.get("supporting_metrics", []):
            if "cpu" in m.lower():
                # Parse string format
                value = float(m.split(":")[1].strip().rstrip("%"))
                cpu_values.append(value)

avg_cpu = sum(cpu_values) / len(cpu_values)
print(f"Average CPU: {avg_cpu:.1f}%")
```

---

## Decision Matrix

| Criterion | Option 1 (Pure) | Option 2 (Hybrid) ⭐ | Option 3 (Parse) |
|-----------|----------------|---------------------|------------------|
| Backward Compatible | ❌ | ✅ | ✅ |
| Type Safe | ✅ | ✅ | ❌ |
| Human Readable | ⚠️ | ✅ | ✅ |
| Analytics Ready | ✅ | ✅ | ⚠️ |
| Implementation Cost | High | Medium | Low |
| Maintenance | Low | Medium | High (parsing bugs) |

**Winner**: Option 2 - Provides structured data for analytics while maintaining backward compatibility and readability.

---

## Implementation Steps (If Approved)

1. Add `Metric` model to `models.py`
2. Add `metrics_structured` field to `CaptainLogEntry`
3. Update DSPy reflection to populate both fields
4. Add helper to extract from `metrics_summary`
5. Update docs and examples

**Estimated Effort**: 1-2 hours
