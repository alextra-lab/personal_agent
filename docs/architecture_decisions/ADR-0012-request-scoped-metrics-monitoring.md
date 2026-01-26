# ADR-0012: Request-Scoped Metrics Monitoring

**Status:** Proposed
**Date:** 2026-01-17
**Decision Owner:** Project Owner

---

## 1. Context

The Personal Local AI Collaborator requires comprehensive observability to support:

1. **Homeostatic control loops**: Brainstem Service needs continuous system metrics to make mode transition decisions (NORMAL → ALERT → DEGRADED)
2. **Request correlation**: Understanding system behavior during specific agent requests for debugging and optimization
3. **Captain's Log enrichment**: LLM-based self-reflections should include performance context to learn patterns
4. **Root cause analysis**: When issues occur, correlating system metrics with request execution provides critical diagnostic information

### Current State

- **System health tool** (`system_metrics_snapshot`) only captures metrics on-demand when explicitly called
- **No background polling** exists, despite ADR-0004 and CONTROL_LOOPS_SENSORS_v0.1.md describing "interval metrics (e.g., every 5s)"
- **No historical data** unless metrics are explicitly logged, creating gaps in observability
- **Control loops are not operational**: Mode transitions cannot be triggered by system metrics because metrics aren't being continuously collected

### Problem Statement

Without continuous, request-scoped metrics monitoring:

1. **Control loops cannot function**: Brainstem Service cannot detect CPU > 85% or memory pressure to trigger mode transitions
2. **Blind spots during execution**: No visibility into system behavior between explicit tool calls
3. **Poor correlation**: Difficult to correlate system metrics with specific requests via `trace_id`
4. **Limited self-reflection**: Captain's Log reflections lack performance context
5. **Debugging challenges**: Cannot reconstruct system state during problematic requests

### Requirements

1. **Automatic activation**: Monitoring starts when request begins, stops when completed
2. **Lightweight**: Minimal overhead on system and request latency
3. **Trace correlation**: All metrics tagged with `trace_id` for log correlation
4. **Configurable intervals**: Polling frequency should be configurable (default: 5s per spec)
5. **Control loop integration**: Metrics feed homeostasis control loops for mode transitions
6. **Graceful cleanup**: Monitoring stops properly on both success and failure paths

---

## 2. Decision

### 2.1 Request Monitor Component

Create a new `RequestMonitor` class in `src/personal_agent/brainstem/sensors/request_monitor.py` that:

- Runs as a background async task
- Polls system metrics at configurable intervals (default: 5 seconds)
- Tags all metrics with `trace_id` for correlation
- Logs `SYSTEM_METRICS_SNAPSHOT` events with trace context
- Provides aggregated summary on completion

**Architecture:**

```python
class RequestMonitor:
    """Background system metrics monitor scoped to a specific request.
    
    Collects metrics at regular intervals and tags them with trace_id
    for correlation with logs and Captain's Log reflections.
    """
    
    def __init__(self, trace_id: str, interval_seconds: float = 5.0):
        """Initialize monitor for a specific request.
        
        Args:
            trace_id: Unique identifier for the request
            interval_seconds: Polling interval (default: 5.0)
        """
        
    async def start(self) -> None:
        """Start background monitoring task."""
        
    async def stop(self) -> dict[str, Any]:
        """Stop monitoring and return aggregated summary.
        
        Returns:
            Summary dict with:
            - duration_seconds: Total monitoring duration
            - samples_collected: Number of metric snapshots
            - cpu_avg/min/max: CPU statistics
            - memory_avg/min/max: Memory statistics
            - gpu_avg/min/max: GPU statistics (if available)
            - threshold_violations: List of control loop thresholds exceeded
        """
        
    def _should_trigger_alert(self, metrics: dict[str, Any]) -> bool:
        """Check if metrics exceed thresholds for mode transitions."""
```

### 2.2 Integration with Orchestrator

Modify `src/personal_agent/orchestrator/executor.py` to start/stop monitoring:

```python
async def execute_task(ctx: ExecutionContext, session_manager: SessionManager) -> ExecutionContext:
    """Main execution loop with request-scoped monitoring."""
    state = ctx.state
    trace_ctx = TraceContext(trace_id=ctx.trace_id)
    
    # Start request-scoped monitoring
    from personal_agent.brainstem.sensors.request_monitor import RequestMonitor
    monitor = RequestMonitor(trace_id=ctx.trace_id, interval_seconds=5.0)
    await monitor.start()
    
    log.info(TASK_STARTED, trace_id=ctx.trace_id, ...)
    
    try:
        # ... existing execution logic ...
        
        if state == TaskState.COMPLETED:
            log.info(TASK_COMPLETED, trace_id=ctx.trace_id, ...)
            
    except Exception as e:
        log.error(ORCHESTRATOR_FATAL_ERROR, trace_id=ctx.trace_id, exc_info=True)
        ctx.error = e
        ctx.state = TaskState.FAILED
        
    finally:
        # Always stop monitoring and get summary
        metrics_summary = await monitor.stop()
        
        # Attach to context for Captain's Log
        ctx.metrics_summary = metrics_summary
        
        # Log summary for analysis
        log.info(
            REQUEST_METRICS_SUMMARY,
            trace_id=ctx.trace_id,
            duration_seconds=metrics_summary['duration_seconds'],
            cpu_avg=metrics_summary['cpu_avg'],
            memory_avg=metrics_summary['memory_avg'],
            threshold_violations=metrics_summary['threshold_violations']
        )
    
    return ctx
```

### 2.3 Control Loop Integration

The `RequestMonitor` checks metrics against thresholds defined in `config/governance/modes.yaml`:

```python
def _check_thresholds(self, metrics: dict[str, Any]) -> list[str]:
    """Check metrics against mode transition thresholds.
    
    Returns list of violated threshold names.
    """
    violations = []
    
    # Load thresholds from governance config
    mode_config = load_mode_config()
    thresholds = mode_config['modes']['NORMAL']['thresholds']
    
    # Check CPU threshold
    if metrics['perf_system_cpu_load'] > thresholds['cpu_load_percent']:
        violations.append('cpu_overload')
        # Emit control signal for mode manager
        self._emit_control_signal('cpu_overload', metrics)
    
    # Check memory threshold
    if metrics['perf_system_mem_used'] > thresholds['memory_used_percent']:
        violations.append('memory_pressure')
        self._emit_control_signal('memory_pressure', metrics)
    
    return violations
```

The `ModeManager` subscribes to these control signals and triggers mode transitions.

### 2.4 Captain's Log Integration

Enhance Captain's Log reflection prompts with metrics summary:

```python
async def _trigger_captains_log_reflection(ctx: ExecutionContext):
    """Generate reflection with performance context."""
    
    # Extract metrics summary
    metrics = ctx.metrics_summary or {}
    
    reflection_prompt = f"""
Reflect on the task execution:

**User Request**: {ctx.user_message}

**Execution Steps**: {len(ctx.steps)} steps completed
{format_steps_summary(ctx.steps)}

**System Performance During Execution**:
- Duration: {metrics.get('duration_seconds', 'N/A')}s
- CPU Usage: avg={metrics.get('cpu_avg', 'N/A')}%, peak={metrics.get('cpu_max', 'N/A')}%
- Memory Usage: avg={metrics.get('memory_avg', 'N/A')}%, peak={metrics.get('memory_max', 'N/A')}%
- Threshold Violations: {metrics.get('threshold_violations', [])}

**Analysis Questions**:
1. What patterns emerge from the execution and performance data?
2. Were there any inefficiencies or resource bottlenecks?
3. How could similar requests be optimized in the future?
4. What should I remember about this task type?
"""
    # ... continue with LLM reflection call
```

### 2.5 Configuration

Add monitoring configuration to `src/personal_agent/config/settings.py`:

```python
class AppConfig(BaseSettings):
    # ... existing fields ...
    
    # Request monitoring settings
    request_monitoring_enabled: bool = True
    request_monitoring_interval_seconds: float = 5.0
    request_monitoring_include_gpu: bool = True
```

---

## 3. Consequences

### Positive

1. **Control loops operational**: Enables homeostatic mode transitions based on real-time metrics
2. **Complete observability**: No blind spots during request execution
3. **Better correlation**: All metrics tagged with `trace_id` for easy log correlation
4. **Enhanced self-reflection**: Captain's Log can analyze performance patterns
5. **Debugging improvements**: Full system state available for any request
6. **Low implementation cost**: Reuses existing sensor polling infrastructure
7. **Configurable**: Can be disabled or tuned via settings

### Negative

1. **Minor overhead**: Background polling adds ~0.1-0.5% CPU overhead
2. **Storage growth**: More JSONL log entries (mitigated by retention policies)
3. **Complexity**: Additional async task lifecycle management
4. **Testing complexity**: Integration tests need to handle async monitoring

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Monitoring task crashes | Low | Medium | Wrap in try-except, log errors, don't block request |
| Increased storage usage | Medium | Low | Existing log retention policies apply |
| Performance overhead | Low | Low | Configurable interval, can be disabled |
| Race conditions on cleanup | Medium | Low | Use asyncio locks, ensure cleanup in finally block |

---

## 4. Alternatives Considered

### Alternative 1: Global Background Poller (Not Request-Scoped)

**Description**: Run a single background task that polls metrics continuously, independent of requests.

**Pros**:
- Simpler lifecycle management
- Always collecting data

**Cons**:
- Cannot correlate metrics with specific requests
- Wastes resources when agent is idle
- Doesn't provide request-scoped summaries for Captain's Log
- Cannot clean up after request completion

**Decision**: Rejected because request correlation is a key requirement.

### Alternative 2: Only Poll on Tool Calls

**Description**: Keep current behavior where metrics are only captured when system_health tool is explicitly called.

**Pros**:
- No new complexity
- Zero overhead when not used

**Cons**:
- Control loops cannot function (defeats homeostasis model)
- No automatic monitoring
- Requires user to explicitly request health checks
- No historical data for debugging

**Decision**: Rejected because it doesn't address the core problem.

### Alternative 3: Separate Daemon Process

**Description**: Run monitoring as a separate OS process that writes metrics to shared storage.

**Pros**:
- Isolation from main agent process
- Can continue monitoring even if agent crashes

**Cons**:
- Complex inter-process communication
- Difficult to correlate with trace_id
- Harder to deploy and manage
- Overkill for MVP

**Decision**: Rejected due to complexity. Could revisit in future if needed.

### Alternative 4: Use External Monitoring Tools (prometheus_client, etc.)

**Description**: Integrate existing monitoring libraries.

**Pros**:
- Battle-tested implementations
- Rich ecosystem

**Cons**:
- Adds external dependencies
- Not designed for request-scoped monitoring
- Requires separate scraper/exporter
- Overhead of full metrics system

**Decision**: Rejected to keep stack minimal and aligned with file-based telemetry approach.

---

## 5. Implementation Plan

See updated IMPLEMENTATION_ROADMAP.md for detailed sequencing.

**Summary**:
1. **Phase 1**: Implement `RequestMonitor` class with basic polling
2. **Phase 2**: Integrate with orchestrator executor (start/stop lifecycle)
3. **Phase 3**: Add threshold checking and control signal emission
4. **Phase 4**: Wire into ModeManager for mode transitions
5. **Phase 5**: Enhance Captain's Log reflections with metrics context
6. **Phase 6**: Add configuration and testing

---

## 6. References

- ADR-0004: Telemetry & Metrics Implementation Strategy
- ADR-0005: Governance Configuration & Operational Modes
- `../architecture/HOMEOSTASIS_MODEL.md` - Control loop architecture
- `../architecture/CONTROL_LOOPS_SENSORS_v0.1.md` - Sensor specifications (defines 5s interval)
- `config/governance/modes.yaml` - Mode transition thresholds
- `src/personal_agent/orchestrator/executor.py` - Request execution lifecycle
- `src/personal_agent/captains_log/manager.py` - Self-reflection integration point

---

## 7. Open Questions

1. **Should monitoring continue during background Captain's Log reflections?**
   - Proposal: No, only monitor user-facing requests (task execution)
   - Rationale: Background reflections are async and not time-critical

2. **How to handle monitoring when agent is in LOCKDOWN mode?**
   - Proposal: Continue monitoring but reduce interval to minimize overhead
   - Rationale: Still need metrics to detect when it's safe to transition to RECOVERY

3. **Should we expose monitoring controls to the user?**
   - Proposal: Yes, via CLI flag or config: `--disable-monitoring` for debugging
   - Rationale: Useful for isolating issues or performance testing

---

**Next Steps**: Create ADR-0013 for Enhanced System Health Tool, then update implementation roadmap.
