# Brainstem Sensors

**Location**: `src/personal_agent/brainstem/sensors/`

**Purpose**: Homeostasis control loop sensors for continuous system monitoring and self-regulation.

## Overview

The `sensors/` module implements the **Sensor** component of the homeostasis control loops (see `../../docs/architecture/HOMEOSTASIS_MODEL.md`). Sensors continuously monitor system state and feed data to Control Centers for decision-making.

```
┌──────────────────────────────────────────────────────────────┐
│                   HOMEOSTASIS CONTROL LOOP                    │
├──────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌─────────┐     ┌───────────┐     ┌──────────┐             │
│  │ SENSOR  │────▶│  CONTROL  │────▶│ EFFECTOR │             │
│  │         │     │  CENTER   │     │          │             │
│  └─────────┘     └───────────┘     └──────────┘             │
│       ▲                                   │                  │
│       │                                   │                  │
│       └───────────── FEEDBACK ────────────┘                  │
│                                                               │
└──────────────────────────────────────────────────────────────┘
```

## Architecture

### Core Principle: Biological Inspiration

Just like an organism maintains homeostasis (temperature, pH, glucose), the agent maintains operational health through continuous sensing and feedback.

**Key Properties:**
- **Continuous**: Sensors run in background during requests
- **Non-blocking**: Never interfere with main task execution
- **Graceful degradation**: Failures don't crash requests
- **Trace-correlated**: All metrics tagged with `trace_id`

## Sensors

### 1. RequestMonitor (ADR-0012)

**File**: `request_monitor.py`

**Purpose**: Request-scoped system metrics monitoring for performance awareness and mode transitions.

**Lifecycle:**
```python
# Automatically managed by orchestrator
Request starts → monitor.start()
              ↓
              Background polling (every 5s)
              ↓
              Threshold checking
              ↓
Request ends → monitor.stop()
              ↓
              Summary attached to ExecutionContext
```

**Key Features:**
- Async background polling at configurable intervals
- CPU, Memory, GPU metrics collection
- Threshold violation detection (CPU > 85%, Memory > 90%)
- Aggregated statistics (min/max/avg)
- Automatic integration with Captain's Log

**Configuration:**
```python
from personal_agent.config import settings

# Enable/disable monitoring
settings.request_monitoring_enabled = True  # Default: True

# Adjust polling frequency
settings.request_monitoring_interval_seconds = 5.0  # Default: 5.0

# Include/exclude GPU metrics
settings.request_monitoring_include_gpu = True  # Default: True
```

**Usage (Automatic):**
```python
# Orchestrator automatically manages lifecycle
# No manual intervention needed

# Access metrics in ExecutionContext
ctx.metrics_summary = {
    "duration_seconds": 17.2,
    "samples_collected": 4,
    "cpu_avg": 42.1,
    "cpu_min": 23.4,
    "cpu_max": 67.8,
    "memory_avg": 60.4,
    "memory_min": 58.1,
    "memory_max": 62.3,
    "gpu_avg": 9.7,
    "gpu_min": 5.2,
    "gpu_max": 15.8,
    "threshold_violations": ["CPU_HIGH"]  # If any
}
```

**Usage (Manual - for testing):**
```python
from personal_agent.brainstem.sensors.request_monitor import RequestMonitor
from personal_agent.telemetry import TraceContext

# Create monitor
trace_ctx = TraceContext()
monitor = RequestMonitor(
    trace_ctx=trace_ctx,
    interval_seconds=5.0,
    include_gpu=True
)

# Start monitoring
await monitor.start()

# Do work...
await my_task()

# Stop and get summary
metrics_summary = await monitor.stop()
```

**Telemetry Events:**
- `request_monitor_started`: Monitor begins polling
- `request_monitor_stopped`: Monitor completes
- `request_metrics_summary`: Final aggregated metrics
- `SYSTEM_METRICS_SNAPSHOT`: Each polling sample (tagged with trace_id)
- `metrics_threshold_violated`: When thresholds exceeded
- `monitor_loop_error`: Background polling errors

**Threshold Detection:**
```python
# Automatic threshold checking on each sample
CPU > 85% → "CPU_HIGH" violation
Memory > 90% → "MEMORY_HIGH" violation

# All violations logged and included in summary
```

**Performance Impact:**
- CPU overhead: <1% on average
- Latency impact: Negligible (fully async)
- Storage: Same as existing telemetry retention

**Error Handling:**
- Graceful degradation: monitoring failures never crash requests

### 2. Sensor-Level Caching (ADR-0014, ADR-0015)

**Files**: `sensors.py`

**Purpose**: Transparent caching at the sensor level to avoid expensive repeated polls to hardware sensors (especially GPU metrics via macmon/powermetrics).

**Performance Problem** (Identified ADR-0015):
- GPU metrics polling via macmon takes ~3.6 seconds (subprocess call)
- RequestMonitor polls every 5 seconds in background
- Tools (e.g., `system_metrics_snapshot`) re-poll independently
- Result: Redundant expensive I/O operations

**Solution**: Module-level cache with TTL

```python
# In sensors.py (implementation detail, transparent to callers)
_METRICS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 10.0  # 2x RequestMonitor polling interval
_cache_lock = threading.Lock()
```

**Key Properties:**
- **Transparent**: Callers don't know about cache (no coupling)
- **Thread-safe**: Protected by lock for concurrent access
- **TTL-based**: 10-second expiration (ensures freshness)
- **Independent keys**: `poll_system_metrics()` uses "system", `get_system_metrics_snapshot()` uses "snapshot"
- **Copy-on-return**: Returns copy to prevent mutation

**Performance Impact:**
```
Before:
- Tool execution: 3.6s (fresh poll)
- RequestMonitor: 3.6s per sample

After:
- Tool execution: 0.1s (cache hit, >95% of time)
- RequestMonitor: 3.6s first poll, 0.1s subsequent
- Speedup: 97% reduction in tool latency
```

**Cache Behavior:**

```python
# Example: RequestMonitor and tool running concurrently

# T=0s: RequestMonitor starts, polls sensors (cache miss)
metrics1 = poll_system_metrics()  # 3.6s, populates cache

# T=5s: RequestMonitor polls again (cache hit if < 10s)
metrics2 = poll_system_metrics()  # 0.1s, from cache

# T=7s: Tool executes (cache hit)
tool_metrics = get_system_metrics_snapshot()  # 0.1s, from cache

# T=12s: RequestMonitor polls (cache expired)
metrics3 = poll_system_metrics()  # 3.6s, fresh poll, updates cache
```

**No Coupling:**
- RequestMonitor doesn't manage cache
- Tools don't depend on RequestMonitor
- Both call same sensor functions (`poll_system_metrics()`, `get_system_metrics_snapshot()`)
- Cache is internal implementation detail at sensor level

**Architecture (Option A - Chosen):**

```
Layer 5: Orchestrator
         ↓
Layer 4: Consumers (independent, no coupling)
         ├─→ RequestMonitor (request-scoped aggregation)
         └─→ system_metrics_snapshot tool (user-facing)
         ↓
Layer 3: Sensor API with transparent caching
         poll_system_metrics() [cache inside]
         get_system_metrics_snapshot() [cache inside]
         ↓
Layer 2: Sensor Polling
         poll_base_metrics() (psutil)
         poll_apple_metrics() (macmon)
         ↓
Layer 1: Platform APIs
         macmon, psutil, powermetrics
```

**Telemetry:**
```python
# Cache hit
log.debug("sensor_cache_hit", age_seconds=3.2, ttl_seconds=10.0)

# Cache miss
log.debug("sensor_cache_miss", reason="expired or empty", ttl_seconds=10.0)

# Cache updated
log.debug(SENSOR_POLL, ..., cache_updated=True)
```

**Edge Cases:**
- **Platform errors**: Cache works even if GPU polling fails (base metrics cached)
- **Concurrent access**: Lock prevents race conditions
- **Cache expiration**: Fresh poll after TTL ensures up-to-date data
- **First call**: Always cache miss (expected), subsequent calls benefit

**Testing:**
```bash
# Run cache tests
pytest tests/test_brainstem/test_sensors_cache.py -v

# Key tests:
# - Cache hit/miss scenarios
# - TTL expiration
# - Thread safety
# - Copy-on-return (no mutation)
# - Independent cache keys
```

**Configuration:**
```python
# Cache TTL is hardcoded (10s) but can be adjusted:
# src/personal_agent/brainstem/sensors/sensors.py
_CACHE_TTL_SECONDS = 10.0  # Adjust if needed
```

**Benefits:**
- ⚡ 97% faster tool execution (3.6s → 0.1s)
- 🎯 >95% cache hit rate (typical workload)
- 💾 Negligible memory overhead (<1KB per cache entry)
- 🔒 Thread-safe for concurrent requests
- 🏗️ No architectural coupling (clean layering)

**Related:**
- ADR-0014: Structured Metrics in Captain's Log (motivation for optimization)
- ADR-0015: Tool Call Performance Optimization (identified 3.6s tool latency)
- Comprehensive logging: all errors logged with context
- Best-effort metrics: partial failures still return available data

**Integration with Captain's Log:**
```python
# Metrics automatically enriched in reflection telemetry summary
{
  "telemetry_summary": "**System Performance (Request-Scoped)**:\n
    - Duration: 17.2s\n
    - Samples: 4 metric snapshots\n
    - CPU: avg=42.1%, min=23.4%, max=67.8%\n
    - Memory: avg=60.4%, min=58.1%, max=62.3%\n
    - GPU: avg=9.7%, min=5.2%, max=15.8%\n
    - Threshold Violations: CPU_HIGH"
}
```

## Testing

### Unit Tests
```bash
# Test individual sensors
pytest tests/test_brainstem/test_sensors/ -v
```

### Integration Tests
```bash
# Test full request lifecycle with monitoring
pytest tests/integration/test_request_monitoring.py -v
```

### Manual Testing
```bash
# Run CLI and check telemetry logs
python -m personal_agent.ui.cli "test system monitoring"

# Grep for monitoring events
rg "request_monitor" telemetry/logs/agent.log
rg "SYSTEM_METRICS_SNAPSHOT" telemetry/logs/agent.log

# Check Captain's Log includes metrics
cat telemetry/captains_log/CL-*.json | jq '.telemetry_summary'
```

## Future Sensors

As per `../../docs/architecture/HOMEOSTASIS_MODEL.md`, additional sensors planned:

- **LLM Health Sensor**: Model availability, latency, error rates
- **Tool Health Sensor**: Tool success rates, latency patterns
- **Storage Sensor**: Disk usage, log rotation triggers
- **Network Sensor**: API rate limits, connection health

## Common Patterns

### Creating a New Sensor

```python
from personal_agent.telemetry import TraceContext, get_logger

log = get_logger(__name__)

class MySensor:
    """Sensor for monitoring [specific aspect]."""

    def __init__(self, trace_ctx: TraceContext, config: dict):
        self.trace_ctx = trace_ctx
        self.config = config
        self._running = False

    async def start(self) -> None:
        """Begin monitoring."""
        log.info("my_sensor_started", trace_id=self.trace_ctx.trace_id)
        self._running = True
        asyncio.create_task(self._monitor_loop())

    async def stop(self) -> dict:
        """Stop monitoring and return summary."""
        self._running = False
        log.info("my_sensor_stopped", trace_id=self.trace_ctx.trace_id)
        return {"summary": "data"}

    async def _monitor_loop(self) -> None:
        """Background monitoring loop."""
        while self._running:
            try:
                metrics = self._collect_metrics()
                log.info(
                    "MY_SENSOR_SNAPSHOT",
                    **metrics,
                    trace_id=self.trace_ctx.trace_id
                )
                await asyncio.sleep(self.config["interval"])
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("my_sensor_error", error=str(e))

    def _collect_metrics(self) -> dict:
        """Collect current metrics."""
        return {"metric": "value"}
```

### Sensor Requirements Checklist

- [ ] Async background operation (non-blocking)
- [ ] Graceful error handling (never crash requests)
- [ ] Trace ID correlation for all events
- [ ] Configurable via `settings.py`
- [ ] Summary statistics on stop()
- [ ] Comprehensive telemetry logging
- [ ] Integration tests for lifecycle
- [ ] Documentation in AGENTS.md

## Debugging

### Common Issues

**Monitoring not starting:**
```python
# Check configuration
from personal_agent.config import settings
print(settings.request_monitoring_enabled)  # Should be True

# Check orchestrator logs
rg "request_monitor_start_failed" telemetry/logs/agent.log
```

**Missing metrics in summary:**
```python
# Check if monitor stopped successfully
rg "request_monitor_stop_failed" telemetry/logs/agent.log

# Verify samples were collected
rg "SYSTEM_METRICS_SNAPSHOT" telemetry/logs/agent.log | grep trace_id=YOUR_TRACE_ID
```

**High monitoring overhead:**
```python
# Increase polling interval (trade-off: less granular data)
settings.request_monitoring_interval_seconds = 10.0

# Disable GPU monitoring if not needed
settings.request_monitoring_include_gpu = False
```

## Non-Negotiables

- **Never block the main request** - sensors are background tasks
- **Always tag with trace_id** - for correlation and filtering
- **Graceful degradation** - partial failures are acceptable
- **Configurable** - users can disable/tune sensors
- **Comprehensive logging** - all state changes logged
- **Test lifecycle** - start/stop cycles must be tested

## Related Documentation

- `../../docs/architecture/HOMEOSTASIS_MODEL.md` - Control loop architecture
- `../../docs/architecture_decisions/ADR-0012-request-scoped-metrics-monitoring.md` - RequestMonitor design
- `src/personal_agent/orchestrator/AGENTS.md` - How orchestrator integrates sensors
- `src/personal_agent/captains_log/AGENTS.md` - How metrics enrich reflections
- `src/personal_agent/governance/AGENTS.md` - How sensors trigger mode transitions (future)
