# Captain's Log

Self-reflective learning system for the Personal AI Agent.

**Spec**: `../../docs/architecture/CAPTAINS_LOG_SPEC_v0.1.md`
**ADR**: `../../docs/architecture_decisions/ADR-0010-structured-llm-outputs-via-pydantic.md`
**Prototype**: `experiments/dspy_prototype/test_case_a_reflection.py` (E-008)

## File Naming Convention

Entries are stored as JSON files with sortable, traceable filenames:

**Format**: `CL-<TIMESTAMP>-<TRACE_PREFIX>-<SEQ>-<TITLE>.json`

- **TIMESTAMP**: `YYYYMMDD-HHMMSS` for chronological sorting
- **TRACE_PREFIX**: First 8 chars of trace_id for scenario grouping (enables test comparison)
- **SEQ**: 3-digit sequence number for same-second entries
- **TITLE**: Sanitized task title (max 50 chars)

**Examples**:
```
CL-20260117-170613-a9e965fb-001-task-what-is-python.json
CL-20260117-170614-b2c45de8-001-task-system-health-check.json
CL-20260117-170614-b2c45de8-002-task-system-health-check-retry.json
```

**Benefits**:
- ✅ **Chronological sorting**: Files sort by timestamp automatically
- ✅ **Scenario tracking**: Group by trace_id prefix for test comparison
- ✅ **Sequence clarity**: No more duplicate `001` numbers
- ✅ **Test analysis**: Compare multiple runs of same scenario

**Querying entries by scenario**:
```bash
# Find all entries for a specific trace/scenario:
ls telemetry/captains_log/ | grep "a9e965fb"

# Group by scenario for analysis:
for trace in $(ls telemetry/captains_log/ | cut -d'-' -f4 | sort -u); do
  echo "Scenario: $trace"
  ls telemetry/captains_log/ | grep "$trace"
done
```

## Responsibilities

- Generate LLM-based reflections on task execution
- Identify patterns, inefficiencies, and improvement opportunities
- Propose concrete, actionable changes to system behavior
- Track proposed changes through approval workflow

## Structure

```
captains_log/
├── __init__.py           # Exports: CaptainLogManager
├── manager.py            # Log entry management, file I/O
├── models.py             # Pydantic models (CaptainLogEntry, ProposedChange)
├── reflection.py         # Main reflection generation (DSPy + manual fallback)
├── reflection_dspy.py    # DSPy ChainOfThought implementation (ADR-0010)
└── AGENTS.md             # This file
```

## Reflection Generation (ADR-0010)

Captain's Log uses **DSPy ChainOfThought** for structured reflection generation:

### Architecture

```
┌─────────────────────────────────────────┐
│  generate_reflection_entry()            │
│  (orchestrator/executor.py)             │
└──────────────┬──────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────┐
│  reflection.py:generate_reflection_entry()       │
│  - Try DSPy ChainOfThought (if available)        │
│  - Fallback to manual JSON parsing               │
│  - Final fallback: basic reflection              │
└──────────────┬───────────────────────────────────┘
               │
               ├─── DSPy available? ──────────────┐
               │                                   │
               ▼ YES                              ▼ NO
┌──────────────────────────────┐   ┌─────────────────────────┐
│  reflection_dspy.py          │   │  Manual JSON Parsing    │
│  - GenerateReflection sig    │   │  - REFLECTION_PROMPT    │
│  - DSPy ChainOfThought       │   │  - _parse_reflection()  │
│  - 100% reliability (E-008)  │   │  - Fallback option      │
└──────────────────────────────┘   └─────────────────────────┘
```

### DSPy Signature

The `GenerateReflection` signature (from E-008 Test Case A):

```python
class GenerateReflection(dspy.Signature):
    """Generate structured reflection on task execution."""

    # Inputs
    user_message: str
    trace_id: str
    steps_count: int
    final_state: str
    reply_length: int
    telemetry_summary: str  # Key events, LLM calls, tool calls, errors

    # Outputs (structured, validated)
    rationale: str
    proposed_change_what: str  # Empty string if no change
    proposed_change_why: str
    proposed_change_how: str
    supporting_metrics: str  # Comma-separated
    impact_assessment: str  # Empty string if none
```

### Usage Pattern

**In application code (orchestrator):**

```python
from personal_agent.captains_log.reflection import generate_reflection_entry

# After task completion
entry = await generate_reflection_entry(
    user_message=user_message,
    trace_id=trace_id,
    steps_count=len(state_machine.states),
    final_state=state_machine.current_state,
    reply_length=len(reply),
)

# Entry is automatically saved by CaptainLogManager
```

**DSPy implementation (reflection_dspy.py):**

```python
# Configure DSPy with REASONING model
# Use dspy.context() for background tasks (reflection runs in background)
lm = llm_client.get_dspy_lm(role=ModelRole.REASONING)

# Use context manager to avoid async task conflicts
with dspy.context(lm=lm):
    # Create ChainOfThought predictor
    reflection_generator = dspy.ChainOfThought(GenerateReflection)

# Generate structured reflection
result = reflection_generator(
    user_message=user_message[:200],
    trace_id=trace_id,
    steps_count=steps_count,
    final_state=final_state,
    reply_length=reply_length,
    telemetry_summary=telemetry_summary,
)

# Convert to CaptainLogEntry (type-safe, no manual parsing)
entry = CaptainLogEntry(
    rationale=result.rationale,
    proposed_change=ProposedChange(
        what=result.proposed_change_what,
        why=result.proposed_change_why,
        how=result.proposed_change_how,
    ) if result.proposed_change_what.strip() else None,
    supporting_metrics=[m.strip() for m in result.supporting_metrics.split(",")],
    impact_assessment=result.impact_assessment if result.impact_assessment.strip() else None,
    # ... other fields ...
)
```

### Fallback Strategy

**3-tier fallback for robustness:**

1. **DSPy ChainOfThought** (preferred)
   - 100% reliability (E-008: 0/5 parse failures)
   - ~30-40% code reduction
   - +21% latency overhead (acceptable: 11.8s → 14.3s)

2. **Manual JSON parsing** (fallback if DSPy fails)
   - Original implementation
   - Handles cases where DSPy unavailable or fails
   - Uses `REFLECTION_PROMPT` with structured JSON instructions

3. **Basic reflection** (final fallback)
   - Minimal metadata only
   - No LLM call
   - Ensures system never fails to create reflection

### Telemetry Integration

DSPy reflection logs events for monitoring:

```python
log.info("attempting_dspy_reflection", trace_id=trace_id)
log.info("dspy_reflection_succeeded", has_proposal=..., metrics_count=...)
log.warning("dspy_reflection_failed_fallback_manual", error_type=..., error_message=...)
```

**Metrics to track:**
- DSPy success rate (target: >95%, E-008 achieved 100%)
- Latency (DSPy vs manual)
- Parse failure rate (target: <5%)
- Fallback frequency

## Performance Characteristics

Based on E-008 Test Case A evaluation:

| Metric | DSPy ChainOfThought | Manual JSON | Improvement |
|--------|---------------------|-------------|-------------|
| Parse Failures | 0% (0/5) | ~5-10% | ✅ 100% reliable |
| Code Complexity | ~25 lines | ~40 lines | ✅ 30-40% reduction |
| Latency | 14.3s avg | 11.8s avg | ⚠️ +21% overhead |
| Maintainability | High (signature-based) | Medium (prompt templates) | ✅ Better |

**Verdict**: +21% latency acceptable for reflection quality and maintainability gains.

## Testing

```bash
# Unit tests (DSPy signature, fallback logic)
pytest tests/test_captains_log/ -v -k reflection

# Integration tests (full reflection generation)
pytest tests/test_captains_log/ -v -k test_generate_reflection_entry --integration

# Measure parse failure rate
pytest tests/test_captains_log/ -v -k test_reflection_parse_failure_rate
```

## Debugging DSPy Reflections

**Inspect DSPy module history:**

```python
import dspy

# After running reflection
lm = dspy.settings.lm  # Get configured LM
print(lm.history)  # View all calls made by DSPy

# Inspect last call
last_call = lm.history[-1]
print(last_call['prompt'])    # Prompt sent to LLM
print(last_call['response'])  # Raw LLM response
```

**Check telemetry logs:**

```bash
# Find DSPy reflection events
rg "dspy_reflection" telemetry/events.jsonl

# Find fallback events (indicates DSPy failure)
rg "fallback_manual" telemetry/events.jsonl
```

## Common Patterns

**Successful DSPy reflection:**

```json
{
  "event": "dspy_reflection_started",
  "trace_id": "abc-123",
  "steps_count": 3
}
{
  "event": "dspy_configured_for_reflection",
  "model_role": "reasoning",
  "trace_id": "abc-123"
}
{
  "event": "dspy_reflection_generated",
  "has_proposed_change": true,
  "supporting_metrics_count": 3,
  "trace_id": "abc-123"
}
{
  "event": "dspy_reflection_succeeded",
  "has_proposal": true,
  "metrics_count": 3,
  "trace_id": "abc-123"
}
```

**DSPy failure with fallback:**

```json
{
  "event": "dspy_reflection_failed",
  "error_type": "ValidationError",
  "error_message": "...",
  "trace_id": "abc-123"
}
{
  "event": "dspy_reflection_failed_fallback_manual",
  "trace_id": "abc-123"
}
{
  "event": "attempting_manual_reflection",
  "dspy_available": true,
  "trace_id": "abc-123"
}
```

## Deterministic Metrics Extraction (ADR-0014)

Captain's Log entries include **both** human-readable and structured metrics for analytics.

### Problem Statement

**Before ADR-0014:**
- LLM generates metrics as comma-separated strings (e.g., `"cpu: 9.3%, duration: 5.4s"`)
- Non-deterministic formatting (LLM may vary output)
- ~2-5% parse failures
- No structured format for analytics/queries

### Solution: Deterministic Extraction

Metrics are extracted **deterministically** from `metrics_summary` dict (no LLM involved):

```python
# metrics_summary comes from RequestMonitor (ADR-0012)
metrics_summary = {
    "duration_seconds": 20.9,  # Already typed/validated
    "cpu_avg": 9.3,
    "memory_avg": 53.4,
    "gpu_avg": 3.2,
    "samples_collected": 4,
}

# Deterministic extraction (metrics_extraction.py)
from personal_agent.captains_log.metrics_extraction import extract_metrics_from_summary

string_metrics, structured_metrics = extract_metrics_from_summary(metrics_summary)

# string_metrics (human-readable)
["duration: 20.9s", "cpu: 9.3%", "memory: 53.4%", "gpu: 3.2%", "samples: 4"]

# structured_metrics (analytics-ready)
[
    Metric(name="duration_seconds", value=20.9, unit="s"),
    Metric(name="cpu_percent", value=9.3, unit="%"),
    Metric(name="memory_percent", value=53.4, unit="%"),
    Metric(name="gpu_percent", value=3.2, unit="%"),
    Metric(name="samples_collected", value=4, unit=None),
]
```

### Metric Model

```python
from personal_agent.captains_log.models import Metric

class Metric(BaseModel):
    """Structured metric with typed value and optional unit."""

    name: str  # e.g., "cpu_percent", "duration_seconds"
    value: float | int | str  # Typed value (prefer numbers)
    unit: str | None  # e.g., "%", "s", "ms", "MB"
```

### CaptainLogEntry Fields

```python
class CaptainLogEntry(BaseModel):
    # ... other fields ...

    # Human-readable (backward compatible)
    supporting_metrics: list[str] = Field(
        default_factory=list,
        description="Human-readable metrics (e.g., 'cpu: 9.3%')"
    )

    # Machine-readable (ADR-0014, optional for backward compatibility)
    metrics_structured: list[Metric] | None = Field(
        None,
        description="Structured metrics for analytics"
    )
```

### Example Entry (JSON)

```json
{
  "entry_id": "CL-20260118-173000-abc12345-001",
  "type": "reflection",
  "title": "Task: Check system health",
  "rationale": "Task completed efficiently with low resource usage...",

  "supporting_metrics": [
    "duration: 20.9s",
    "cpu: 9.3%",
    "memory: 53.4%",
    "gpu: 3.2%",
    "samples: 4"
  ],

  "metrics_structured": [
    {"name": "duration_seconds", "value": 20.9, "unit": "s"},
    {"name": "cpu_percent", "value": 9.3, "unit": "%"},
    {"name": "memory_percent", "value": 53.4, "unit": "%"},
    {"name": "gpu_percent", "value": 3.2, "unit": "%"},
    {"name": "samples_collected", "value": 4, "unit": null}
  ]
}
```

### Benefits

✅ **100% Deterministic**: Same input → same output (no LLM variability)
✅ **0% Parse Failures**: Direct extraction from typed dict
✅ **Analytics-Ready**: Query by metric name/value ranges
✅ **Backward Compatible**: Old entries without `metrics_structured` load fine
✅ **Faster**: No LLM call for metrics (~2s saved)
✅ **Consistent Format**: Standardized metric names (see ADR-0014)

### Standardized Metric Names

| Metric | Name | Unit | Type |
|--------|------|------|------|
| Duration | `duration_seconds` | `s` | `float` |
| CPU Usage | `cpu_percent` | `%` | `float` |
| Memory Usage | `memory_percent` | `%` | `float` |
| GPU Usage | `gpu_percent` | `%` | `float` |
| Samples | `samples_collected` | `null` | `int` |
| Violations | `threshold_violations` | `null` | `int` |
| CPU Peak | `cpu_peak_percent` | `%` | `float` |
| Memory Peak | `memory_peak_percent` | `%` | `float` |
| GPU Peak | `gpu_peak_percent` | `%` | `float` |

### Analytics Examples

**Query by metric name:**
```python
# Find all CPU metrics across entries
cpu_metrics = [
    m for entry in entries
    for m in entry.metrics_structured or []
    if m.name == "cpu_percent"
]
```

**Query by value range:**
```python
# Find entries with CPU > 80%
high_cpu_entries = [
    entry for entry in entries
    if any(
        m.name == "cpu_percent" and m.value > 80
        for m in entry.metrics_structured or []
    )
]
```

**Time-series analysis:**
```python
# Calculate CPU trend over time
cpu_timeline = [
    (entry.timestamp, m.value)
    for entry in sorted(entries, key=lambda e: e.timestamp)
    for m in entry.metrics_structured or []
    if m.name == "cpu_percent"
]
```

### Integration with DSPy

DSPy signature **simplified** (no longer generates metrics):

```python
class GenerateReflection(dspy.Signature):
    # Inputs (metrics pre-formatted)
    user_message: str
    trace_id: str
    metrics_summary: str  # "cpu: 9.3%, duration: 5.4s" (pre-formatted)
    telemetry_summary: str

    # Outputs (metrics removed - extracted deterministically)
    rationale: str
    proposed_change_what: str
    proposed_change_why: str
    proposed_change_how: str
    impact_assessment: str
```

**LLM now focuses on:**
- Analyzing what happened (rationale)
- Proposing improvements (proposed_change)
- Assessing impact (impact_assessment)

**LLM no longer:**
- ❌ Formats metrics as strings
- ❌ Extracts metric names from telemetry
- ❌ Calculates averages (already done by RequestMonitor)

### Testing

```bash
# Test deterministic extraction
pytest tests/test_captains_log/test_metrics_extraction.py -v

# Test Metric model
pytest tests/test_captains_log/test_models_adr_0014.py -v

# Verify backward compatibility
pytest tests/test_captains_log/test_manager.py -v
```

### Migration

**Gradual, non-breaking:**
- New entries: Include both `supporting_metrics` and `metrics_structured`
- Old entries: Load fine (missing `metrics_structured` = `None`)
- No backfilling required

### Related Files

- `metrics_extraction.py`: Deterministic extraction functions
- `models.py`: `Metric` model definition
- `reflection_dspy.py`: DSPy with deterministic extraction
- `reflection.py`: Manual fallback with deterministic extraction

## Dependencies

- `dspy`: Structured LLM outputs via ChainOfThought
- `pydantic`: Data validation (CaptainLogEntry, ProposedChange, Metric)
- `personal_agent.llm_client`: LocalLLMClient + DSPy integration
- `personal_agent.telemetry`: Structured logging
- `personal_agent.brainstem.sensors`: RequestMonitor (metrics_summary source)

## Pre-PR Checklist

```bash
pytest tests/test_captains_log/ -v
mypy src/personal_agent/captains_log/
ruff check src/personal_agent/captains_log/
```

## Critical

- **DSPy preferred, fallback required**: Always maintain manual fallback for robustness
- **Trace all reflections**: Every reflection must have trace_id for debugging
- **Monitor parse failures**: Alert if failure rate >5% (should be 0% with deterministic extraction)
- **Never block on reflection**: Use async, don't fail task if reflection fails
- **Telemetry is essential**: Reflection quality depends on good telemetry summary
- **Deterministic metrics**: Always use `extract_metrics_from_summary()` (no LLM for metrics)

## Related

- `llm_client/AGENTS.md`: DSPy adapter usage patterns
- `llm_client/dspy_adapter.py`: DSPy configuration utilities
- `brainstem/sensors/AGENTS.md`: RequestMonitor and sensor caching
- ADR-0010: Decision rationale for DSPy adoption
- ADR-0012: Request-Scoped Metrics Monitoring (metrics_summary source)
- ADR-0014: Structured Metrics in Captain's Log (deterministic extraction)
- ADR-0015: Tool Call Performance Optimization (sensor caching motivation)
- E-008: Prototype evaluation results
