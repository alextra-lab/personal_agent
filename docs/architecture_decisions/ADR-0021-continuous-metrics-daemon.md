# ADR-0021: Continuous Metrics Daemon

**Status:** Accepted
**Date:** 2026-02-23
**Deciders:** Alex
**Supersedes:** ADR-0012 (Request-Scoped Metrics Monitoring) — partially; the `RequestMonitor` concept remains but becomes a thin reader, not a poller.

## Context

ADR-0012 introduced `RequestMonitor`, which starts a background polling loop at request start and stops it at request end. This design has two problems:

1. **Event loop blocking.** `poll_system_metrics()` calls `macmon` via subprocess, taking ~3.6 seconds on cache miss. Even with `asyncio.to_thread()` (applied 2026-02-23), this ties up a thread pool thread per poll.
2. **No metrics when idle.** Between requests, no metrics are collected. Kibana dashboards show gaps.

The original intent was that metrics collection runs independently of user requests, with the request handler simply tapping into a cache of recent metrics.

## Decision

### Replace Request-Scoped Polling with a Service-Lifetime Daemon

A `MetricsDaemon` class runs as a single `asyncio.Task` for the entire service lifetime (started in `app.py` lifespan, stopped on shutdown).

**Architecture:**

```
Service Boot → MetricsDaemon.start()
                 ↓
              _poll_loop() runs continuously
                 ↓
              poll_system_metrics() via asyncio.to_thread() every N seconds
                 ↓
              Stores in ring buffer (deque, maxlen=720 → 1 hour at 5s interval)
                 ↓
              Emits SENSOR_POLL to ES every 30s (configurable)
                 ↓
Service Shutdown → MetricsDaemon.stop()
```

**RequestMonitor becomes a reader:**

```python
class RequestMonitor:
    def __init__(self, trace_id: str, daemon: MetricsDaemon): ...

    async def start(self) -> None:
        self._start_time = time.time()

    async def stop(self) -> dict[str, Any]:
        window = self.daemon.get_window(seconds=self._elapsed())
        return self._compute_summary(window)
```

No polling loop in `RequestMonitor`. It reads from the daemon's ring buffer on stop, computing aggregates over the request duration window.

### Configuration

| Setting | Default | Description |
|---|---|---|
| `metrics_daemon_poll_interval_seconds` | `5.0` | How often to poll system metrics |
| `metrics_daemon_es_emit_interval_seconds` | `30.0` | How often to emit `SENSOR_POLL` to ES |
| `metrics_daemon_buffer_size` | `720` | Ring buffer capacity (1 hour at 5s) |

All configurable via `AppConfig` in `settings.py`.

### No External Cache

The daemon lives in the same process as the service. An in-memory `deque` is sufficient. If the agent becomes multi-process in the future, revisit with shared memory or Redis.

## Consequences

- **Positive:** Continuous metrics regardless of request activity. No event loop blocking. Simpler `RequestMonitor` (read-only). Consistent ES data for dashboards.
- **Negative:** Constant CPU overhead from polling (mitigated by 5s interval and caching in `sensors.py`). One additional long-lived asyncio task.
- **Supersedes:** `RequestMonitor._monitor_loop()` polling is removed. The `_check_thresholds()` logic moves to the daemon (or the brainstem scheduler).

## Implementation

- **Spec:** `docs/plans/TRACEABILITY_AND_PERFORMANCE_SPEC.md`
- **New file:** `src/personal_agent/brainstem/sensors/metrics_daemon.py`
- **Modified:** `src/personal_agent/brainstem/sensors/request_monitor.py`, `src/personal_agent/service/app.py`, `src/personal_agent/brainstem/scheduler.py`, `src/personal_agent/config/settings.py`
