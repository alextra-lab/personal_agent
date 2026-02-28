# System Health Monitoring Enhancement Implementation Plan

**ADRs**: ADR-0012 (Request-Scoped Metrics Monitoring), ADR-0013 (Enhanced System Health Tool)
**Date**: 2026-01-17
**Priority**: High (Enables homeostasis control loops)
**Status**: Ready to implement

---

## Overview

This plan implements two complementary enhancements to system observability:

1. **Request-Scoped Metrics Monitoring** (ADR-0012): Automatic background monitoring during every request
2. **Enhanced System Health Tool** (ADR-0013): Historical query capabilities for debugging and analysis

These enhancements are foundational for the homeostasis model and enable:
- Control loops for mode transitions (NORMAL → ALERT → DEGRADED)
- Captain's Log enrichment with performance context
- User-facing debugging of performance issues
- Request correlation for root cause analysis

---

## Dependencies

### Completed (✅)
- Orchestrator execution lifecycle (executor.py)
- Brainstem sensor polling (sensors.py, platforms/)
- Telemetry infrastructure (trace.py, logger.py, metrics.py)
- Captain's Log Manager (manager.py, reflection.py)
- ModeManager (mode_manager.py)
- System health tool (system_health.py)

### New Components Required
- RequestMonitor class (brainstem/sensors/request_monitor.py)
- Extended query_events() with trace_id filtering
- Enhanced system_metrics_snapshot_executor()
- Control loop threshold checking integration

---

## Reference Documentation

Before implementing, review these detailed specifications:

1. **📊 Data Structures**: `../architecture/SYSTEM_HEALTH_MONITORING_DATA_STRUCTURES_v0.1.md`
   - Complete type definitions for all data structures
   - Validation rules and examples
   - **READ THIS FIRST** before implementing any phase

2. **🔧 RequestMonitor Component Spec**: `../architecture/REQUEST_MONITOR_SPEC_v0.1.md`
   - Complete RequestMonitor class implementation
   - 14+ test cases with exact code
   - Configuration changes

3. **🔧 Orchestrator Integration Spec**: `../architecture/REQUEST_MONITOR_ORCHESTRATOR_INTEGRATION_v0.1.md`
   - Line-by-line code changes to executor.py
   - Integration tests
   - Error handling patterns

4. **📋 Quick Reference**: `./IMPLEMENTATION_QUICK_REFERENCE.md`
   - Workflow guide
   - File lookup table
   - Common patterns

---

## Implementation Phases

### Phase 1: Request Monitor Foundation (2-3 days)

**Goal**: Create `RequestMonitor` class with basic polling and lifecycle management

**📖 Detailed Specification**: See `../architecture/REQUEST_MONITOR_SPEC_v0.1.md` for complete implementation

#### Tasks

1. **Create RequestMonitor class** (`src/personal_agent/brainstem/sensors/request_monitor.py`)
   ```python
   class RequestMonitor:
       """Background system metrics monitor scoped to a specific request."""

       def __init__(self, trace_id: str, interval_seconds: float = 5.0):
           self._trace_id = trace_id
           self._interval = interval_seconds
           self._task: asyncio.Task | None = None
           self._snapshots: list[dict[str, Any]] = []
           self._start_time: datetime | None = None
           self._stop_requested = False

       async def start(self) -> None:
           """Start background monitoring task."""

       async def stop(self) -> dict[str, Any]:
           """Stop monitoring and return aggregated summary."""

       async def _polling_loop(self) -> None:
           """Main polling loop (runs in background)."""
   ```

2. **Implement basic polling loop**
   - Poll `get_system_metrics_snapshot()` at configured interval
   - Tag each snapshot with `trace_id`
   - Log `SYSTEM_METRICS_SNAPSHOT` events
   - Store snapshots in memory for summary calculation
   - Handle exceptions gracefully (don't block request)

3. **Add configuration settings**
   ```python
   # In src/personal_agent/config/settings.py
   class AppConfig(BaseSettings):
       # Request monitoring settings
       request_monitoring_enabled: bool = True
       request_monitoring_interval_seconds: float = 5.0
       request_monitoring_include_gpu: bool = True
   ```

4. **Create comprehensive tests** (`tests/test_brainstem/test_request_monitor.py`)
   - Test monitor lifecycle (start/stop)
   - Test polling interval accuracy
   - Test snapshot collection
   - Test summary calculation
   - Test exception handling (sensor failures don't crash monitor)
   - Test cleanup on abrupt stop

**Acceptance Criteria**:
- ✅ Monitor can start, poll metrics at interval, and stop cleanly
- ✅ All metrics tagged with trace_id
- ✅ Summary includes duration, sample count, min/max/avg for each metric
- ✅ Exceptions in polling don't crash monitor or block request
- ✅ Tests: 8+ tests covering lifecycle, polling, summary, errors

**Files Created/Modified**:
- `src/personal_agent/brainstem/sensors/request_monitor.py` (NEW)
- `src/personal_agent/config/settings.py` (MODIFIED - add monitoring config)
- `tests/test_brainstem/test_request_monitor.py` (NEW)

---

### Phase 2: Orchestrator Integration (1-2 days)

**Goal**: Integrate `RequestMonitor` into orchestrator execution lifecycle

**📖 Detailed Specification**: See `../architecture/REQUEST_MONITOR_ORCHESTRATOR_INTEGRATION_v0.1.md` for exact code changes

#### Tasks

1. **Modify executor.py to start/stop monitoring**
   ```python
   # In src/personal_agent/orchestrator/executor.py

   async def execute_task(ctx: ExecutionContext, session_manager: SessionManager) -> ExecutionContext:
       """Main execution loop with request-scoped monitoring."""
       state = ctx.state
       trace_ctx = TraceContext(trace_id=ctx.trace_id)

       # Start request-scoped monitoring
       monitor = None
       if settings.request_monitoring_enabled:
           from personal_agent.brainstem.sensors.request_monitor import RequestMonitor
           monitor = RequestMonitor(
               trace_id=ctx.trace_id,
               interval_seconds=settings.request_monitoring_interval_seconds
           )
           await monitor.start()
           log.debug("request_monitoring_started", trace_id=ctx.trace_id)

       log.info(TASK_STARTED, trace_id=ctx.trace_id, ...)

       try:
           # ... existing execution logic ...

       except Exception as e:
           log.error(ORCHESTRATOR_FATAL_ERROR, trace_id=ctx.trace_id, exc_info=True)
           ctx.error = e
           ctx.state = TaskState.FAILED

       finally:
           # Always stop monitoring and get summary
           if monitor:
               metrics_summary = await monitor.stop()
               ctx.metrics_summary = metrics_summary

               # Log summary
               log.info(
                   REQUEST_METRICS_SUMMARY,
                   trace_id=ctx.trace_id,
                   duration_seconds=metrics_summary['duration_seconds'],
                   sample_count=metrics_summary['sample_count'],
                   cpu_avg=metrics_summary.get('cpu', {}).get('avg'),
                   memory_avg=metrics_summary.get('memory', {}).get('avg'),
                   threshold_violations=metrics_summary.get('threshold_violations', [])
               )

       return ctx
   ```

2. **Add REQUEST_METRICS_SUMMARY event constant**
   ```python
   # In src/personal_agent/telemetry/events.py
   REQUEST_METRICS_SUMMARY = "request_metrics_summary"
   ```

3. **Update ExecutionContext to include metrics_summary**
   ```python
   # In src/personal_agent/orchestrator/types.py
   @dataclass
   class ExecutionContext:
       # ... existing fields ...
       metrics_summary: dict[str, Any] | None = None
   ```

4. **Add integration tests**
   - Test monitoring starts/stops with request execution
   - Test metrics_summary attached to context
   - Test monitoring continues through state transitions
   - Test cleanup on both success and failure paths
   - Test monitoring can be disabled via config

**Acceptance Criteria**:
- ✅ Monitor starts when task begins, stops when complete
- ✅ Metrics summary attached to ExecutionContext
- ✅ REQUEST_METRICS_SUMMARY event logged with summary
- ✅ Monitoring works through all execution paths (success/failure)
- ✅ Can be disabled via configuration
- ✅ Tests: 5+ integration tests covering orchestrator integration

**Files Modified**:
- `src/personal_agent/orchestrator/executor.py` (MODIFIED)
- `src/personal_agent/orchestrator/types.py` (MODIFIED)
- `src/personal_agent/telemetry/events.py` (MODIFIED)
- `tests/test_orchestrator/test_request_monitoring.py` (NEW)

---

### Phase 3: Control Loop Integration (2-3 days)

**Goal**: Enable mode transitions based on threshold violations

#### Tasks

1. **Add threshold checking to RequestMonitor**
   ```python
   def _check_thresholds(self, metrics: dict[str, Any]) -> list[str]:
       """Check metrics against mode transition thresholds.

       Returns list of violated threshold names.
       """
       violations = []

       # Load thresholds from governance config
       from personal_agent.governance.config_loader import load_governance_config
       gov_config = load_governance_config()

       current_mode = get_current_mode()
       mode_config = gov_config.modes.get(current_mode)
       if not mode_config:
           return violations

       thresholds = mode_config.thresholds

       # Check CPU threshold
       cpu_load = metrics.get('perf_system_cpu_load')
       if cpu_load and cpu_load > thresholds.get('cpu_load_percent', 100):
           violations.append('cpu_overload')
           self._emit_control_signal('cpu_overload', cpu_load)

       # Check memory threshold
       mem_used = metrics.get('perf_system_mem_used')
       if mem_used and mem_used > thresholds.get('memory_used_percent', 100):
           violations.append('memory_pressure')
           self._emit_control_signal('memory_pressure', mem_used)

       return violations

   def _emit_control_signal(self, signal_type: str, metric_value: float) -> None:
       """Emit control signal for mode manager."""
       log.warning(
           "control_signal_emitted",
           trace_id=self._trace_id,
           signal_type=signal_type,
           metric_value=metric_value
       )
       # Future: Trigger mode manager evaluation
   ```

2. **Wire ModeManager to check control signals**
   ```python
   # In src/personal_agent/brainstem/mode_manager.py

   def evaluate_transitions_from_metrics(self, metrics_summary: dict[str, Any]) -> None:
       """Evaluate if mode transition needed based on metrics summary.

       Args:
           metrics_summary: Summary from RequestMonitor with threshold_violations
       """
       violations = metrics_summary.get('threshold_violations', [])

       if not violations:
           return

       current_mode = self.get_current_mode()

       # Determine if transition needed
       if 'cpu_overload' in violations or 'memory_pressure' in violations:
           if current_mode == Mode.NORMAL:
               self.transition_to(Mode.ALERT, "Resource pressure detected")
           elif current_mode == Mode.ALERT:
               # Check duration - transition to DEGRADED if sustained
               self.transition_to(Mode.DEGRADED, "Sustained resource pressure")
   ```

3. **Call evaluate_transitions_from_metrics in executor**
   ```python
   # In executor.py finally block, after getting metrics_summary
   if monitor:
       metrics_summary = await monitor.stop()
       ctx.metrics_summary = metrics_summary

       # Check for mode transitions based on metrics
       from personal_agent.brainstem.mode_manager import get_mode_manager
       mode_manager = get_mode_manager()
       mode_manager.evaluate_transitions_from_metrics(metrics_summary)

       log.info(REQUEST_METRICS_SUMMARY, ...)
   ```

4. **Add comprehensive tests for control loop integration**
   - Test threshold violation detection
   - Test control signal emission
   - Test mode transition triggering (NORMAL → ALERT)
   - Test sustained violations (ALERT → DEGRADED)
   - Test no transition when below thresholds

**Acceptance Criteria**:
- ✅ Threshold violations detected during monitoring
- ✅ Control signals emitted when thresholds exceeded
- ✅ Mode transitions triggered appropriately
- ✅ Mode transitions logged with reason and sensor data
- ✅ Tests: 6+ tests covering control loop integration

**Files Modified**:
- `src/personal_agent/brainstem/sensors/request_monitor.py` (MODIFIED)
- `src/personal_agent/brainstem/mode_manager.py` (MODIFIED)
- `src/personal_agent/orchestrator/executor.py` (MODIFIED)
- `tests/test_brainstem/test_control_loops.py` (NEW)

---

### Phase 4: Captain's Log Enrichment (1 day)

**Goal**: Include metrics context in Captain's Log reflections

#### Tasks

1. **Enhance reflection prompt with metrics**
   ```python
   # In src/personal_agent/captains_log/reflection.py

   async def _generate_reflection_prompt(ctx: ExecutionContext) -> str:
       """Generate reflection prompt with metrics context."""

       metrics = ctx.metrics_summary or {}

       metrics_section = ""
       if metrics:
           metrics_section = f"""
   **System Performance During Execution**:
   - Duration: {metrics.get('duration_seconds', 'N/A')}s
   - Samples Collected: {metrics.get('sample_count', 0)}
   - CPU Usage: avg={metrics.get('cpu', {}).get('avg', 'N/A')}%, peak={metrics.get('cpu', {}).get('max', 'N/A')}%
   - Memory Usage: avg={metrics.get('memory', {}).get('avg', 'N/A')}%, peak={metrics.get('memory', {}).get('max', 'N/A')}%
   - GPU Usage: avg={metrics.get('gpu', {}).get('avg', 'N/A')}% (if applicable)
   - Threshold Violations: {', '.join(metrics.get('threshold_violations', [])) or 'None'}
   """

       prompt = f"""
   Reflect on the completed task and system performance:

   **User Request**: {ctx.user_message}

   **Execution Steps**: {len(ctx.steps)} steps completed
   {_format_steps_summary(ctx.steps)}
   {metrics_section}

   **Analysis Questions**:
   1. What patterns emerge from the execution and performance data?
   2. Were there any inefficiencies or resource bottlenecks?
   3. Could this task type be optimized in the future?
   4. What should I remember about similar requests?

   Provide insights that would be valuable for future reflection.
   """
       return prompt
   ```

2. **Test Captain's Log includes metrics**
   - Test reflection includes metrics when available
   - Test reflection works without metrics (graceful degradation)
   - Test metrics format in reflection output

**Acceptance Criteria**:
- ✅ Reflections include performance metrics when available
- ✅ Reflections identify performance patterns and bottlenecks
- ✅ Graceful degradation when metrics not available
- ✅ Tests: 3+ tests covering Captain's Log integration

**Files Modified**:
- `src/personal_agent/captains_log/reflection.py` (MODIFIED)
- `tests/test_captains_log/test_metrics_integration.py` (NEW)

---

### Phase 5: Enhanced System Health Tool (2-3 days)

**Goal**: Add historical query capabilities to system_metrics_snapshot tool

#### Tasks

1. **Extend query_events() to support trace_id filtering**
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
           trace_id: Optional trace ID filter.
           limit: Optional maximum number of results.

       Returns:
           List of matching log entries, ordered by timestamp (newest first).
       """
       # ... existing time window parsing ...

       # Apply filters
       filtered = []
       for entry in entries:
           if event and entry.get("event") != event:
               continue
           if component and entry.get("component") != component:
               continue
           if trace_id and entry.get("trace_id") != trace_id:
               continue
           filtered.append(entry)

       # ... rest of implementation ...
   ```

2. **Implement statistical summary calculator**
   ```python
   def _calculate_metrics_summary(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
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

       for name, values in [("cpu", cpu_values), ("memory", mem_values), ("gpu", gpu_values)]:
           if values:
               summary[name] = {
                   "min": min(values),
                   "max": max(values),
                   "avg": sum(values) / len(values)
               }

       return summary
   ```

3. **Enhance system_metrics_snapshot_executor**
   ```python
   # In src/personal_agent/tools/system_health.py

   def system_metrics_snapshot_executor(
       window_str: str | None = None,
       trace_id: str | None = None,
       include_history: bool = False,
       stat_summary: bool = True
   ) -> dict[str, Any]:
       """Get system metrics with optional historical data."""
       try:
           result = {"success": True, "error": None}

           # Always get current snapshot
           current_metrics = get_system_metrics_snapshot()
           result["current"] = current_metrics

           # If historical data requested
           if window_str or trace_id:
               # Query telemetry logs
               from personal_agent.telemetry.metrics import query_events
               from personal_agent.telemetry.events import SYSTEM_METRICS_SNAPSHOT

               if trace_id:
                   history = query_events(
                       event=SYSTEM_METRICS_SNAPSHOT,
                       trace_id=trace_id
                   )
               elif window_str:
                   history = query_events(
                       event=SYSTEM_METRICS_SNAPSHOT,
                       window_str=window_str
                   )

               # Include full history if requested
               if include_history:
                   result["history"] = history

               # Calculate statistical summary
               if stat_summary and history:
                   result["summary"] = _calculate_metrics_summary(history)

           return result

       except Exception as e:
           return {
               "success": False,
               "current": None,
               "error": f"Error getting system metrics: {e}"
           }
   ```

4. **Update tool definition with new parameters**
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
       # ... rest of definition ...
   )
   ```

5. **Add comprehensive tests**
   - Test current snapshot only (backward compatibility)
   - Test time window queries
   - Test trace_id queries
   - Test statistical summary calculation
   - Test include_history flag
   - Test error handling (invalid parameters)
   - Test empty history (no data in window)

**Acceptance Criteria**:
- ✅ Tool returns current snapshot (backward compatible)
- ✅ Tool can query by time window
- ✅ Tool can query by trace_id
- ✅ Statistical summary calculated correctly
- ✅ Full history included when requested
- ✅ Graceful error handling
- ✅ Tests: 8+ tests covering all query modes

**Files Modified**:
- `src/personal_agent/telemetry/metrics.py` (MODIFIED - add trace_id filtering)
- `src/personal_agent/tools/system_health.py` (MODIFIED - enhanced tool)
- `tests/test_tools/test_system_health_enhanced.py` (NEW)
- `tests/test_telemetry/test_metrics.py` (MODIFIED - add trace_id tests)

---

### Phase 6: Documentation & User Testing (1-2 days)

**Goal**: Document new features and validate with user scenarios

#### Tasks

1. **Update documentation**
   - Update `docs/SYSTEM_HEALTH_MONITORING.md` (create if doesn't exist)
   - Add examples to tool documentation
   - Update AGENTS.md files for modified components
   - Add architecture diagrams showing monitoring flow

2. **Create user guide**
   - How to check current system health
   - How to debug slow requests
   - How to analyze performance trends
   - How to interpret Captain's Log performance insights

3. **Manual testing scenarios**
   - Scenario 1: "What is my system health?" (current only)
   - Scenario 2: "Why was my last request slow?" (trace_id query)
   - Scenario 3: "Has CPU been high lately?" (time window query)
   - Scenario 4: Trigger ALERT mode via high CPU
   - Scenario 5: Review Captain's Log reflection with metrics

4. **Create evaluation script** (`tests/evaluation/system_health_evaluation.py`)
   - Automated testing of all monitoring features
   - Performance overhead measurement
   - Control loop validation
   - Report generation

**Acceptance Criteria**:
- ✅ All documentation updated and accurate
- ✅ User guide covers common scenarios
- ✅ Manual testing scenarios pass
- ✅ Evaluation script runs successfully
- ✅ Performance overhead < 1% (measured)

**Files Created/Modified**:
- `docs/SYSTEM_HEALTH_MONITORING.md` (NEW)
- `docs/USER_GUIDE_DEBUGGING.md` (NEW)
- `tests/evaluation/system_health_evaluation.py` (NEW)
- Various AGENTS.md files (MODIFIED)

---

## Testing Strategy

### Unit Tests (Phase-specific)
- Each phase includes targeted unit tests
- Minimum 80% code coverage for new code
- All edge cases covered

### Integration Tests
- Request monitoring + orchestrator lifecycle
- Control loops + mode transitions
- Captain's Log + metrics enrichment
- Enhanced tool + telemetry queries

### E2E Tests
- Full user scenarios from request to Captain's Log reflection
- Mode transition scenarios (NORMAL → ALERT → DEGRADED)
- Performance testing (overhead measurement)

### Regression Testing
- All existing tests must continue to pass
- No performance degradation in baseline scenarios
- Monitoring can be disabled without breaking anything

---

## Success Criteria

### Functional
- ✅ Request-scoped monitoring runs automatically for every request
- ✅ Metrics tagged with trace_id and logged to telemetry
- ✅ Control loops detect threshold violations and trigger mode transitions
- ✅ Captain's Log reflections include performance context
- ✅ Enhanced system health tool supports historical queries
- ✅ User can debug slow requests using trace_id
- ✅ User can analyze performance trends using time windows

### Quality
- ✅ All tests passing (unit, integration, E2E)
- ✅ Code coverage > 80% for new code
- ✅ Type checking clean (mypy)
- ✅ Linting clean (ruff)
- ✅ Documentation complete and accurate

### Performance
- ✅ Monitoring overhead < 1% CPU on average
- ✅ No user-perceivable latency added to requests
- ✅ Historical queries complete in < 2 seconds
- ✅ Log storage growth manageable (covered by existing retention)

---

## Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| Monitoring adds latency | Run in background task, measure overhead, make configurable |
| Storage growth | Existing log retention policies apply, add max samples limit |
| Monitor crashes | Wrap in try-except, ensure cleanup in finally block |
| Mode transition false positives | Tune thresholds, add sustained violation checks |
| Historical queries too slow | Limit max time window (24h), add caching in future |

---

## Rollout Plan

1. **Phase 1-3**: Core monitoring infrastructure (can be disabled)
2. **Phase 4**: Captain's Log enhancement (optional enrichment)
3. **Phase 5**: Enhanced tool (backward compatible)
4. **Phase 6**: Documentation and validation

**Feature Flag**: `request_monitoring_enabled` allows disabling during development

**Gradual Enablement**:
1. Enable monitoring in development
2. Validate performance and stability
3. Enable in production usage
4. Monitor for issues

---

## Timeline Estimate

- Phase 1: 2-3 days (Foundation)
- Phase 2: 1-2 days (Integration)
- Phase 3: 2-3 days (Control Loops)
- Phase 4: 1 day (Captain's Log)
- Phase 5: 2-3 days (Enhanced Tool)
- Phase 6: 1-2 days (Documentation)

**Total**: 9-14 days (2-3 weeks)

**Dependencies**: None (all prerequisites completed)

---

## Next Steps

1. Review and approve ADR-0012 and ADR-0013
2. Create feature branch: `feat/request-scoped-monitoring`
3. Begin Phase 1 implementation
4. Daily standup to track progress
5. Demo after each phase completion

---

**Let's enable the homeostasis model.** 🏥
