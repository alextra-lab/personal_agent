# System Health Monitoring - Implementation Quick Reference

**For**: Code generation models and developers
**Status**: Complete implementation specifications
**Date**: 2026-01-17

---

## Document Structure

This implementation is documented in **5 comprehensive files**:

### 1. 📋 ADRs (Architecture Decisions)
- **ADR-0012**: Request-Scoped Metrics Monitoring
  - Path: `../architecture_decisions/ADR-0012-request-scoped-metrics-monitoring.md`
  - Content: Context, decision rationale, consequences, alternatives

- **ADR-0013**: Enhanced System Health Tool
  - Path: `../architecture_decisions/ADR-0013-enhanced-system-health-tool.md`
  - Content: Historical query capabilities, usage examples

### 2. 📊 Data Structures (REFERENCE FIRST)
- **File**: `../architecture/SYSTEM_HEALTH_MONITORING_DATA_STRUCTURES_v0.1.md`
- **Content**: Every TypedDict, dataclass, validation rule
- **Use**: Reference this before implementing any phase

### 3. 🔧 Component Specifications
- **RequestMonitor**: `../architecture/REQUEST_MONITOR_SPEC_v0.1.md`
  - Complete RequestMonitor class with all methods
  - 14+ test cases with exact implementations
  - Configuration changes

- **Orchestrator Integration**: `../architecture/REQUEST_MONITOR_ORCHESTRATOR_INTEGRATION_v0.1.md`
  - Exact code changes to executor.py (line-by-line)
  - Integration tests
  - Error handling patterns

### 4. 📋 Main Implementation Plan
- **File**: `./SYSTEM_HEALTH_MONITORING_IMPLEMENTATION_PLAN.md`
- **Content**: 6 phases with detailed tasks, acceptance criteria, file lists

### 5. 📚 Roadmap Integration
- **File**: `./IMPLEMENTATION_ROADMAP.md`
- **Content**: High-level context and scheduling

---

## Implementation Workflow

### Step 1: Read Data Structures
```bash
# ALWAYS START HERE
open ../architecture/SYSTEM_HEALTH_MONITORING_DATA_STRUCTURES_v0.1.md
```

**Why**: Understand exact types before writing code

### Step 2: Implement Phase 1
```bash
# Follow this specification exactly
open ../architecture/REQUEST_MONITOR_SPEC_v0.1.md

# Create files in this order:
1. src/personal_agent/brainstem/sensors/types.py
2. src/personal_agent/brainstem/sensors/request_monitor.py
3. tests/test_brainstem/test_request_monitor.py
4. Update: src/personal_agent/config/settings.py
5. Update: src/personal_agent/brainstem/sensors/__init__.py

# Run tests after each file
pytest tests/test_brainstem/test_request_monitor.py -v
```

### Step 3: Implement Phase 2
```bash
# Follow this specification exactly
open ../architecture/REQUEST_MONITOR_ORCHESTRATOR_INTEGRATION_v0.1.md

# Modify files in this order:
1. src/personal_agent/orchestrator/types.py (add metrics_summary field)
2. src/personal_agent/telemetry/events.py (add REQUEST_METRICS_SUMMARY)
3. src/personal_agent/orchestrator/executor.py (integrate monitoring)
4. tests/test_orchestrator/test_request_monitoring.py (new tests)

# Run tests after each change
pytest tests/test_orchestrator/test_request_monitoring.py -v
pytest tests/test_orchestrator/ -v  # Regression check
```

### Step 4: Implement Phases 3-6
```bash
# Follow main implementation plan
open ./SYSTEM_HEALTH_MONITORING_IMPLEMENTATION_PLAN.md

# Phase 3: Control loops
# Phase 4: Captain's Log enrichment
# Phase 5: Enhanced system health tool
# Phase 6: Documentation and validation
```

---

## Quick File Lookup

### New Files to Create

**Phase 1**:
```
src/personal_agent/brainstem/sensors/types.py
src/personal_agent/brainstem/sensors/request_monitor.py
tests/test_brainstem/test_request_monitor.py
```

**Phase 2**:
```
tests/test_orchestrator/test_request_monitoring.py
```

**Phase 3**:
```
tests/test_brainstem/test_control_loops.py
```

**Phase 4**:
```
tests/test_captains_log/test_metrics_integration.py
```

**Phase 5**:
```
tests/test_tools/test_system_health_enhanced.py
```

**Phase 6**:
```
docs/SYSTEM_HEALTH_MONITORING.md
docs/USER_GUIDE_DEBUGGING.md
tests/evaluation/system_health_evaluation.py
```

### Files to Modify

**Phase 1**:
```
src/personal_agent/config/settings.py (add monitoring config)
src/personal_agent/brainstem/sensors/__init__.py (add exports)
```

**Phase 2**:
```
src/personal_agent/orchestrator/executor.py (integrate monitoring)
src/personal_agent/orchestrator/types.py (add metrics_summary field)
src/personal_agent/telemetry/events.py (add REQUEST_METRICS_SUMMARY)
```

**Phase 3**:
```
src/personal_agent/brainstem/sensors/request_monitor.py (add threshold checking)
src/personal_agent/brainstem/mode_manager.py (add evaluate_transitions_from_metrics)
```

**Phase 4**:
```
src/personal_agent/captains_log/reflection.py (enhance prompts with metrics)
```

**Phase 5**:
```
src/personal_agent/telemetry/metrics.py (add trace_id filtering)
src/personal_agent/tools/system_health.py (add historical query modes)
```

---

## Key Type Definitions (Quick Reference)

### MetricStats
```python
{"min": float, "max": float, "avg": float}
```

### MetricsSummary
```python
{
    "duration_seconds": float,
    "sample_count": int,
    "start_time": str,  # ISO 8601
    "end_time": str,  # ISO 8601
    "cpu": MetricStats,  # Optional
    "memory": MetricStats,  # Optional
    "gpu": MetricStats,  # Optional
    "threshold_violations": list[str]
}
```

### SystemHealthResponse
```python
{
    "success": bool,
    "error": str | None,
    "current": CurrentMetrics,
    "summary": HistoricalSummary,  # Optional
    "history": list[MetricSnapshot]  # Optional
}
```

---

## Testing Strategy Summary

### Unit Tests
- **Phase 1**: 14+ tests for RequestMonitor
- **Phase 2**: 5+ tests for orchestrator integration
- **Phase 3**: 6+ tests for control loops
- **Phase 4**: 3+ tests for Captain's Log
- **Phase 5**: 8+ tests for enhanced tool

### Integration Tests
- End-to-end request execution with monitoring
- Concurrent monitors
- Real system metrics collection
- Mode transition triggering

### Performance Tests
- Monitoring overhead < 1% CPU
- Historical query latency < 2 seconds
- No user-perceivable impact

---

## Common Code Patterns

### Pattern 1: Async Task Management
```python
# Start task
self._task = asyncio.create_task(
    self._polling_loop(),
    name=f"request_monitor_{self._trace_id}"
)

# Stop task with timeout
try:
    await asyncio.wait_for(self._task, timeout=self._interval + 1.0)
except asyncio.TimeoutError:
    self._task.cancel()
    try:
        await self._task
    except asyncio.CancelledError:
        pass
```

### Pattern 2: Graceful Error Handling
```python
try:
    # Monitoring operation
    monitor = RequestMonitor(...)
    await monitor.start()
except Exception as e:
    # Log but don't block
    log.error("monitoring_failed", error=str(e), exc_info=True)
    monitor = None
```

### Pattern 3: Statistics Calculation
```python
values = [s.get("metric") for s in snapshots if s.get("metric") is not None]
if values:
    summary["metric"] = {
        "min": min(values),
        "max": max(values),
        "avg": sum(values) / len(values)
    }
```

### Pattern 4: ISO 8601 Timestamps
```python
from datetime import datetime, timezone

timestamp = datetime.now(timezone.utc).isoformat()
# Example: "2026-01-17T10:23:45.123456+00:00"
```

---

## Validation Checklist

### After Each Phase

- [ ] All tests pass
- [ ] Type checking clean (`mypy`)
- [ ] Linting clean (`ruff check`)
- [ ] Imports updated in `__init__.py`
- [ ] No regression in existing tests
- [ ] Acceptance criteria met

### Before Merging

- [ ] All 6 phases complete
- [ ] Performance overhead measured (<1%)
- [ ] Documentation complete
- [ ] User guide written
- [ ] Evaluation script runs successfully
- [ ] Code reviewed

---

## Configuration Reference

### Enable/Disable Monitoring
```python
# In .env or environment
REQUEST_MONITORING_ENABLED=true
REQUEST_MONITORING_INTERVAL_SECONDS=5.0
REQUEST_MONITORING_INCLUDE_GPU=true
```

### In Code
```python
from personal_agent.config import settings

if settings.request_monitoring_enabled:
    monitor = RequestMonitor(...)
```

---

## Debugging Commands

### Check Type Errors
```bash
mypy src/personal_agent/brainstem/sensors/
mypy src/personal_agent/orchestrator/
mypy src/personal_agent/tools/
```

### Run Specific Tests
```bash
# Phase 1
pytest tests/test_brainstem/test_request_monitor.py -v

# Phase 2
pytest tests/test_orchestrator/test_request_monitoring.py -v

# All phases
pytest tests/test_brainstem/ tests/test_orchestrator/ tests/test_tools/ -v
```

### Check Coverage
```bash
pytest tests/ --cov=src/personal_agent --cov-report=term-missing
```

### Lint Check
```bash
ruff check src/personal_agent/brainstem/sensors/
ruff format src/personal_agent/brainstem/sensors/
```

---

## Performance Validation

### Measure Monitoring Overhead
```python
import time

# Without monitoring
start = time.time()
# ... execute request ...
baseline = time.time() - start

# With monitoring
start = time.time()
# ... execute request with monitoring ...
with_monitoring = time.time() - start

overhead = ((with_monitoring - baseline) / baseline) * 100
print(f"Overhead: {overhead:.2f}%")  # Should be < 1%
```

### Measure Query Latency
```python
import time

start = time.time()
result = system_metrics_snapshot(window_str="1h")
latency = time.time() - start
print(f"Query latency: {latency:.2f}s")  # Should be < 2s
```

---

## Success Criteria Summary

### Functional
- ✅ Monitoring runs automatically for every request
- ✅ Metrics tagged with trace_id
- ✅ Control loops trigger mode transitions
- ✅ Captain's Log includes performance context
- ✅ Enhanced tool supports historical queries
- ✅ Backward compatibility maintained

### Quality
- ✅ 80%+ code coverage
- ✅ All tests passing
- ✅ Type checking clean
- ✅ Linting clean
- ✅ Documentation complete

### Performance
- ✅ Monitoring overhead < 1%
- ✅ No user-perceivable latency
- ✅ Historical queries < 2s

---

## Next Steps

1. **Start with Phase 1** using detailed specification
2. **Verify each phase** before moving to next
3. **Run full test suite** after each phase
4. **Measure performance** after Phase 2
5. **Complete all 6 phases** following specifications

---

**Everything you need to implement is in these documents. Start with the Quick Reference, then Data Structures in `../architecture/`, then component specs in `../architecture/`.**
