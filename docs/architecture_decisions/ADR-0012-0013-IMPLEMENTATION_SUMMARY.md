# ADR-0012/0013 Implementation Summary

**Date**: 2026-01-17
**Status**: Ready for Review
**Related**: ADR-0012 (Request-Scoped Metrics Monitoring), ADR-0013 (Enhanced System Health Tool)

---

## Overview

This document summarizes the comprehensive system health monitoring enhancement, which includes two ADRs and a detailed implementation plan.

## Problem Statement

**Current State**:
- System health tool only provides current snapshots (no historical data)
- No background metrics monitoring despite specs requiring it
- Control loops cannot function (homeostasis model non-functional)
- Cannot debug performance issues ("why was that request slow?")
- Captain's Log lacks performance context for self-reflection

**Impact**:
- Mode transitions cannot be triggered automatically
- Blind spots during request execution
- Poor debugging capabilities
- Limited self-awareness about performance patterns

## Solution Architecture

### Two Complementary Enhancements

#### 1. Request-Scoped Metrics Monitoring (ADR-0012)

**Purpose**: Automatic background monitoring for every agent request

**How It Works**:
```
User Request → Orchestrator starts RequestMonitor
              ↓
              Monitor polls metrics every 5 seconds
              ↓
              Logs SYSTEM_METRICS_SNAPSHOT + trace_id
              ↓
              Checks thresholds → Emits control signals
              ↓
              ModeManager evaluates transitions
              ↓
Request Completes → Monitor stops, returns summary
              ↓
              Summary attached to ExecutionContext
              ↓
              Captain's Log receives performance context
```

**Key Features**:
- Async background task (no latency impact)
- Trace correlation (all metrics tagged with trace_id)
- Threshold monitoring (triggers ALERT/DEGRADED modes)
- Aggregated summaries (duration, min/max/avg)
- Configurable and optional

#### 2. Enhanced System Health Tool (ADR-0013)

**Purpose**: Historical query capabilities for debugging and analysis

**Query Modes**:
```python
# Mode 1: Current only (backward compatible)
system_metrics_snapshot()

# Mode 2: Time window
system_metrics_snapshot(window_str="30m")

# Mode 3: Specific request
system_metrics_snapshot(trace_id="abc-123-xyz")

# Mode 4: Summary only (no full history)
system_metrics_snapshot(window_str="1h", include_history=False)
```

**Returns**:
```json
{
  "success": true,
  "current": { /* live metrics */ },
  "summary": {
    "duration_seconds": 120,
    "sample_count": 24,
    "cpu": {"min": 23.4, "max": 89.2, "avg": 45.7},
    "memory": {"min": 58.1, "max": 71.3, "avg": 62.5},
    "gpu": {"min": 5.2, "max": 34.1, "avg": 15.3}
  },
  "history": [ /* optional full time series */ ]
}
```

## Documents Created

### ADRs
1. **ADR-0012**: Request-Scoped Metrics Monitoring
   - Path: `./ADR-0012-request-scoped-metrics-monitoring.md`
   - Sections: Context, Decision, Consequences, Alternatives, Implementation Plan
   - Key Decision: Run monitoring as request-scoped background task

2. **ADR-0013**: Enhanced System Health Tool with Historical Queries
   - Path: `./ADR-0013-enhanced-system-health-tool.md`
   - Sections: Context, Decision, Consequences, Alternatives, Usage Examples
   - Key Decision: Extend tool with optional parameters (backward compatible)

### Implementation Plans
1. **Detailed Implementation Plan**
   - Path: `../plans/SYSTEM_HEALTH_MONITORING_IMPLEMENTATION_PLAN.md`
   - 6 phases with detailed tasks, acceptance criteria, file changes
   - Timeline: 9-14 days (2-3 weeks)
   - Dependencies, testing strategy, risk mitigation

2. **Main Roadmap Integration**
   - Path: `../plans/IMPLEMENTATION_ROADMAP.md`
   - Added new section before Phase 4
   - Priority: High (enables homeostasis)
   - Status: Ready to implement

## Implementation Phases

### Phase 1: Request Monitor Foundation (2-3 days)
- Create `RequestMonitor` class
- Implement async polling loop
- Add configuration settings
- Tests: 8+ unit tests

### Phase 2: Orchestrator Integration (1-2 days)
- Wire into executor lifecycle
- Add metrics_summary to context
- Tests: 5+ integration tests

### Phase 3: Control Loop Integration (2-3 days)
- Threshold checking
- Control signal emission
- ModeManager integration
- Tests: 6+ control loop tests

### Phase 4: Captain's Log Enrichment (1 day)
- Enhance reflection prompts
- Performance analysis
- Tests: 3+ tests

### Phase 5: Enhanced System Health Tool (2-3 days)
- Extend query_events()
- Add historical query modes
- Statistical summaries
- Tests: 8+ tests

### Phase 6: Documentation & User Testing (1-2 days)
- User guide
- Manual testing scenarios
- Performance validation

**Total**: 9-14 days (2-3 weeks)

## Success Criteria

### Functional Requirements
- ✅ Monitoring runs automatically for every request
- ✅ Metrics tagged with trace_id
- ✅ Control loops trigger mode transitions
- ✅ Captain's Log includes performance context
- ✅ Enhanced tool supports historical queries
- ✅ Backward compatibility maintained

### Quality Requirements
- ✅ All tests passing (80%+ coverage)
- ✅ Type checking clean (mypy)
- ✅ Linting clean (ruff)
- ✅ Documentation complete
- ✅ No regression

### Performance Requirements
- ✅ Monitoring overhead < 1% CPU
- ✅ No user-perceivable latency
- ✅ Historical queries < 2 seconds
- ✅ Storage growth manageable

## Integration with Existing Architecture

### Builds On (✅ Complete)
- Orchestrator execution lifecycle
- Brainstem sensors (CPU, memory, disk, GPU)
- Telemetry infrastructure (logs, metrics, traces)
- Captain's Log Manager
- ModeManager

### Enables (Future)
- Homeostatic control loops (operational)
- Performance pattern learning
- Adaptive resource management
- Proactive mode transitions

## Files Created/Modified

### New Files
```
src/personal_agent/brainstem/sensors/request_monitor.py
tests/test_brainstem/test_request_monitor.py
tests/test_brainstem/test_control_loops.py
tests/test_orchestrator/test_request_monitoring.py
tests/test_captains_log/test_metrics_integration.py
tests/test_tools/test_system_health_enhanced.py
tests/evaluation/system_health_evaluation.py
docs/SYSTEM_HEALTH_MONITORING.md
docs/USER_GUIDE_DEBUGGING.md
./ADR-0012-request-scoped-metrics-monitoring.md
./ADR-0013-enhanced-system-health-tool.md
../plans/SYSTEM_HEALTH_MONITORING_IMPLEMENTATION_PLAN.md
```

### Modified Files
```
src/personal_agent/orchestrator/executor.py
src/personal_agent/orchestrator/types.py
src/personal_agent/brainstem/mode_manager.py
src/personal_agent/captains_log/reflection.py
src/personal_agent/tools/system_health.py
src/personal_agent/telemetry/metrics.py
src/personal_agent/telemetry/events.py
src/personal_agent/config/settings.py
../plans/IMPLEMENTATION_ROADMAP.md
```

## Risk Mitigation

| Risk | Mitigation Strategy |
|------|-------------------|
| Monitoring adds latency | Background async task, measured overhead |
| Storage growth | Existing retention policies, limit max samples |
| Monitor crashes | Exception handling, cleanup in finally block |
| False positive mode transitions | Tuned thresholds, sustained violation checks |
| Slow historical queries | Max time window (24h), pagination |

## Usage Examples

### Example 1: Check Current Health
```
User: "What's my system health?"
Agent: [calls system_metrics_snapshot()]
Agent: "Your Mac is healthy: CPU at 34%, memory at 58%, GPU at 12%"
```

### Example 2: Debug Slow Request
```
User: "Why was that last request so slow?"
Agent: [calls system_metrics_snapshot(trace_id=last_trace_id)]
Agent: "During that request, CPU averaged 82% with a peak of 94%.
       The high CPU usage caused the slowdown. Consider using a
       smaller model for simple queries."
```

### Example 3: Trend Analysis
```
User: "Has CPU been high lately?"
Agent: [calls system_metrics_snapshot(window_str="1h")]
Agent: "Over the last hour, CPU averaged 42% with occasional spikes
       to 89%. No sustained overload detected."
```

### Example 4: Automatic Mode Transition
```
[Request execution starts]
→ Monitor detects CPU > 85% sustained for 30s
→ Control signal emitted
→ ModeManager transitions: NORMAL → ALERT
→ Agent becomes more conservative (reduced concurrency, approval required)
```

### Example 5: Captain's Log Reflection
```
[Request completes]
→ Monitor summary attached to context
→ Captain's Log reflection includes:
   "CPU averaged 78% during this request, peaking at 92% during
    LLM inference. Future similar requests could be optimized by
    using the STANDARD model instead of REASONING for tool
    orchestration."
```

## Next Steps

1. **Review ADRs**
   - Technical review of ADR-0012 and ADR-0013
   - Approve architectural decisions

2. **Create Feature Branch**
   - Branch name: `feat/request-scoped-monitoring`
   - Base: current main branch

3. **Begin Implementation**
   - Start with Phase 1 (Request Monitor Foundation)
   - Daily progress updates
   - Demo after each phase

4. **Testing & Validation**
   - Run evaluation script after Phase 6
   - Measure performance overhead
   - Validate control loops

5. **Documentation**
   - User guide for debugging
   - Updated AGENTS.md files
   - Architecture diagrams

## Timeline

**Estimated Duration**: 2-3 weeks (9-14 days)

**Target Milestones**:
- Week 1: Phases 1-3 (Core monitoring + control loops)
- Week 2: Phases 4-5 (Enrichment + enhanced tool)
- Week 3: Phase 6 + buffer (Documentation + validation)

**Dependencies**: None (all prerequisites complete)

**Blockers**: None

**Status**: ✅ Ready to implement

---

## References

- `../architecture/HOMEOSTASIS_MODEL.md` - Control loop architecture
- `../architecture/CONTROL_LOOPS_SENSORS_v0.1.md` - Sensor specifications
- `./ADR-0004-telemetry-and-metrics.md` - Telemetry architecture
- `./ADR-0005-governance-config-and-modes.md` - Mode definitions
- `config/governance/modes.yaml` - Mode thresholds
- `src/personal_agent/orchestrator/executor.py` - Request execution
- `src/personal_agent/brainstem/sensors/` - Sensor polling
- `src/personal_agent/telemetry/metrics.py` - Metrics queries

---

**This enhancement is foundational for the homeostasis model. Let's build it.** 🏥
