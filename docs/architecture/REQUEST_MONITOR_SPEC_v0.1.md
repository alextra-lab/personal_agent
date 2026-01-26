# Phase 1: Request Monitor - Complete Implementation Specification

**Goal**: Create `RequestMonitor` class with background polling and lifecycle management
**Timeline**: 2-3 days
**Status**: Ready to implement

---

## Data Structures

### 1. MetricsSummary Type Definition

```python
# Add to src/personal_agent/brainstem/sensors/types.py (NEW FILE)

from typing import TypedDict, NotRequired
from datetime import datetime


class MetricStats(TypedDict):
    """Statistics for a single metric over time."""
    min: float
    max: float
    avg: float


class MetricSnapshot(TypedDict):
    """Single metrics snapshot with timestamp."""
    timestamp: str  # ISO 8601 format
    perf_system_cpu_load: float | None
    perf_system_mem_used: float | None
    perf_system_disk_used: float | None
    perf_system_gpu_load: float | None
    perf_system_gpu_power_w: float | None
    perf_system_gpu_temp_c: float | None
    perf_system_cpu_count: int | None
    perf_system_mem_total_gb: float | None
    perf_system_load_avg: tuple[float, float, float] | None


class MetricsSummary(TypedDict):
    """Aggregated metrics summary from request monitoring."""
    duration_seconds: float
    sample_count: int
    start_time: str  # ISO 8601 format
    end_time: str  # ISO 8601 format
    cpu: NotRequired[MetricStats]
    memory: NotRequired[MetricStats]
    disk: NotRequired[MetricStats]
    gpu: NotRequired[MetricStats]
    threshold_violations: list[str]
```

---

## Complete RequestMonitor Implementation

### File: `src/personal_agent/brainstem/sensors/request_monitor.py` (NEW)

```python
"""Request-scoped system metrics monitoring.

This module provides the RequestMonitor class for automatic background
monitoring of system metrics during agent request execution.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any

from personal_agent.brainstem.sensors import get_system_metrics_snapshot
from personal_agent.brainstem.sensors.types import MetricSnapshot, MetricsSummary
from personal_agent.telemetry import SYSTEM_METRICS_SNAPSHOT, get_logger

log = get_logger(__name__)


class RequestMonitor:
    """Background system metrics monitor scoped to a specific request.
    
    Collects metrics at regular intervals and tags them with trace_id
    for correlation with logs and Captain's Log reflections.
    
    Usage:
        monitor = RequestMonitor(trace_id="abc-123", interval_seconds=5.0)
        await monitor.start()
        # ... request execution ...
        summary = await monitor.stop()
    
    Attributes:
        _trace_id: Unique identifier for the request
        _interval: Polling interval in seconds
        _task: Background asyncio task (None when not running)
        _snapshots: List of collected metric snapshots
        _start_time: When monitoring started
        _stop_requested: Flag to signal polling loop to stop
    """
    
    def __init__(self, trace_id: str, interval_seconds: float = 5.0):
        """Initialize monitor for a specific request.
        
        Args:
            trace_id: Unique identifier for the request
            interval_seconds: Polling interval (default: 5.0)
        
        Raises:
            ValueError: If interval_seconds <= 0
        """
        if interval_seconds <= 0:
            raise ValueError(f"interval_seconds must be positive, got {interval_seconds}")
        
        self._trace_id = trace_id
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._snapshots: list[MetricSnapshot] = []
        self._start_time: datetime | None = None
        self._stop_requested = False
    
    async def start(self) -> None:
        """Start background monitoring task.
        
        Creates and starts an asyncio task that polls metrics at the
        configured interval.
        
        Raises:
            RuntimeError: If monitoring is already started
        """
        if self._task is not None:
            raise RuntimeError("Monitor already started")
        
        self._stop_requested = False
        self._start_time = datetime.now(timezone.utc)
        self._snapshots = []
        
        # Create background task
        self._task = asyncio.create_task(
            self._polling_loop(),
            name=f"request_monitor_{self._trace_id}"
        )
        
        log.debug(
            "request_monitoring_started",
            trace_id=self._trace_id,
            interval_seconds=self._interval
        )
    
    async def stop(self) -> MetricsSummary:
        """Stop monitoring and return aggregated summary.
        
        Signals the polling loop to stop and waits for it to complete.
        Returns aggregated statistics over all collected samples.
        
        Returns:
            MetricsSummary with duration, sample count, and statistics
        
        Raises:
            RuntimeError: If monitoring was not started
        """
        if self._task is None:
            raise RuntimeError("Monitor not started")
        
        # Signal polling loop to stop
        self._stop_requested = True
        
        # Wait for polling task to complete (with timeout)
        try:
            await asyncio.wait_for(self._task, timeout=self._interval + 1.0)
        except asyncio.TimeoutError:
            log.warning(
                "request_monitoring_stop_timeout",
                trace_id=self._trace_id,
                message="Polling task did not stop within timeout, cancelling"
            )
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        
        # Calculate summary
        summary = self._calculate_summary()
        
        log.debug(
            "request_monitoring_stopped",
            trace_id=self._trace_id,
            duration_seconds=summary['duration_seconds'],
            sample_count=summary['sample_count']
        )
        
        return summary
    
    async def _polling_loop(self) -> None:
        """Main polling loop (runs in background).
        
        Continuously polls system metrics at the configured interval
        until stop is requested. Handles exceptions gracefully to
        prevent monitoring failures from blocking the request.
        """
        poll_count = 0
        
        while not self._stop_requested:
            poll_count += 1
            
            try:
                # Poll system metrics
                metrics = get_system_metrics_snapshot()
                
                # Create snapshot with timestamp
                snapshot: MetricSnapshot = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "perf_system_cpu_load": metrics.get("perf_system_cpu_load"),
                    "perf_system_mem_used": metrics.get("perf_system_mem_used"),
                    "perf_system_disk_used": metrics.get("perf_system_disk_used"),
                    "perf_system_gpu_load": metrics.get("perf_system_gpu_load"),
                    "perf_system_gpu_power_w": metrics.get("perf_system_gpu_power_w"),
                    "perf_system_gpu_temp_c": metrics.get("perf_system_gpu_temp_c"),
                    "perf_system_cpu_count": metrics.get("perf_system_cpu_count"),
                    "perf_system_mem_total_gb": metrics.get("perf_system_mem_total_gb"),
                    "perf_system_load_avg": metrics.get("perf_system_load_avg"),
                }
                
                # Store snapshot
                self._snapshots.append(snapshot)
                
                # Log with trace_id for correlation
                log.info(
                    SYSTEM_METRICS_SNAPSHOT,
                    trace_id=self._trace_id,
                    poll_number=poll_count,
                    **metrics  # Unpack all metrics
                )
                
            except Exception as e:
                # Log error but continue polling
                log.error(
                    "request_monitoring_poll_error",
                    trace_id=self._trace_id,
                    poll_number=poll_count,
                    error=str(e),
                    error_type=type(e).__name__,
                    exc_info=True
                )
            
            # Sleep until next interval
            await asyncio.sleep(self._interval)
    
    def _calculate_summary(self) -> MetricsSummary:
        """Calculate aggregated summary from collected snapshots.
        
        Computes min/max/avg statistics for each metric type.
        
        Returns:
            MetricsSummary with aggregated statistics
        """
        if not self._start_time:
            raise RuntimeError("Monitoring was never started")
        
        end_time = datetime.now(timezone.utc)
        duration = (end_time - self._start_time).total_seconds()
        
        summary: MetricsSummary = {
            "duration_seconds": duration,
            "sample_count": len(self._snapshots),
            "start_time": self._start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "threshold_violations": []  # Will be populated in Phase 3
        }
        
        # Extract metric values
        cpu_values = [
            s["perf_system_cpu_load"] 
            for s in self._snapshots 
            if s.get("perf_system_cpu_load") is not None
        ]
        mem_values = [
            s["perf_system_mem_used"] 
            for s in self._snapshots 
            if s.get("perf_system_mem_used") is not None
        ]
        disk_values = [
            s["perf_system_disk_used"] 
            for s in self._snapshots 
            if s.get("perf_system_disk_used") is not None
        ]
        gpu_values = [
            s["perf_system_gpu_load"] 
            for s in self._snapshots 
            if s.get("perf_system_gpu_load") is not None
        ]
        
        # Calculate statistics for each metric
        if cpu_values:
            summary["cpu"] = {
                "min": min(cpu_values),
                "max": max(cpu_values),
                "avg": sum(cpu_values) / len(cpu_values)
            }
        
        if mem_values:
            summary["memory"] = {
                "min": min(mem_values),
                "max": max(mem_values),
                "avg": sum(mem_values) / len(mem_values)
            }
        
        if disk_values:
            summary["disk"] = {
                "min": min(disk_values),
                "max": max(disk_values),
                "avg": sum(disk_values) / len(disk_values)
            }
        
        if gpu_values:
            summary["gpu"] = {
                "min": min(gpu_values),
                "max": max(gpu_values),
                "avg": sum(gpu_values) / len(gpu_values)
            }
        
        return summary
```

---

## Configuration Changes

### File: `src/personal_agent/config/settings.py` (MODIFY)

**Location**: After line ~90 (after existing LLM settings)

**Add this section**:

```python
    # ===== Request Monitoring Settings =====
    # Controls automatic background monitoring during request execution
    # See: ADR-0012 (Request-Scoped Metrics Monitoring)
    
    request_monitoring_enabled: bool = Field(
        default=True,
        description="Enable request-scoped metrics monitoring"
    )
    
    request_monitoring_interval_seconds: float = Field(
        default=5.0,
        ge=0.1,  # Minimum 100ms
        le=60.0,  # Maximum 60s
        description="Polling interval for metrics collection (seconds)"
    )
    
    request_monitoring_include_gpu: bool = Field(
        default=True,
        description="Include GPU metrics in monitoring (if available)"
    )
```

---

## Test Suite

### File: `tests/test_brainstem/test_request_monitor.py` (NEW)

```python
"""Tests for request-scoped metrics monitoring."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest

from personal_agent.brainstem.sensors.request_monitor import RequestMonitor
from personal_agent.brainstem.sensors.types import MetricsSummary


class TestRequestMonitorLifecycle:
    """Test monitor lifecycle (start/stop)."""
    
    @pytest.mark.asyncio
    async def test_start_creates_background_task(self):
        """Test that start() creates a background asyncio task."""
        monitor = RequestMonitor(trace_id="test-123", interval_seconds=0.1)
        
        await monitor.start()
        
        assert monitor._task is not None
        assert not monitor._task.done()
        
        # Cleanup
        await monitor.stop()
    
    @pytest.mark.asyncio
    async def test_stop_returns_summary(self):
        """Test that stop() returns a metrics summary."""
        monitor = RequestMonitor(trace_id="test-123", interval_seconds=0.1)
        
        await monitor.start()
        await asyncio.sleep(0.3)  # Let it poll a few times
        summary = await monitor.stop()
        
        assert isinstance(summary, dict)
        assert summary['sample_count'] >= 2
        assert summary['duration_seconds'] >= 0.3
        assert 'start_time' in summary
        assert 'end_time' in summary
    
    @pytest.mark.asyncio
    async def test_start_raises_if_already_started(self):
        """Test that start() raises if called twice."""
        monitor = RequestMonitor(trace_id="test-123", interval_seconds=0.1)
        
        await monitor.start()
        
        with pytest.raises(RuntimeError, match="already started"):
            await monitor.start()
        
        # Cleanup
        await monitor.stop()
    
    @pytest.mark.asyncio
    async def test_stop_raises_if_not_started(self):
        """Test that stop() raises if monitoring was never started."""
        monitor = RequestMonitor(trace_id="test-123", interval_seconds=0.1)
        
        with pytest.raises(RuntimeError, match="not started"):
            await monitor.stop()
    
    @pytest.mark.asyncio
    async def test_stop_waits_for_polling_task(self):
        """Test that stop() waits for polling task to complete."""
        monitor = RequestMonitor(trace_id="test-123", interval_seconds=0.1)
        
        await monitor.start()
        await asyncio.sleep(0.15)  # Let it poll at least once
        
        await monitor.stop()
        
        assert monitor._task.done()
    
    def test_init_validates_interval(self):
        """Test that __init__ validates interval_seconds."""
        with pytest.raises(ValueError, match="must be positive"):
            RequestMonitor(trace_id="test-123", interval_seconds=0.0)
        
        with pytest.raises(ValueError, match="must be positive"):
            RequestMonitor(trace_id="test-123", interval_seconds=-1.0)


class TestRequestMonitorPolling:
    """Test metrics polling behavior."""
    
    @pytest.mark.asyncio
    async def test_polling_interval_accuracy(self):
        """Test that polling occurs at approximately the configured interval."""
        monitor = RequestMonitor(trace_id="test-123", interval_seconds=0.2)
        
        await monitor.start()
        await asyncio.sleep(0.5)  # Should get ~2 samples
        summary = await monitor.stop()
        
        # Allow some tolerance for timing
        assert 2 <= summary['sample_count'] <= 3
    
    @pytest.mark.asyncio
    async def test_snapshots_collected(self):
        """Test that snapshots are collected and stored."""
        monitor = RequestMonitor(trace_id="test-123", interval_seconds=0.1)
        
        await monitor.start()
        await asyncio.sleep(0.35)  # Should get ~3 samples
        await monitor.stop()
        
        assert len(monitor._snapshots) >= 3
        
        # Verify snapshot structure
        snapshot = monitor._snapshots[0]
        assert "timestamp" in snapshot
        assert "perf_system_cpu_load" in snapshot
        assert "perf_system_mem_used" in snapshot
    
    @pytest.mark.asyncio
    async def test_snapshots_tagged_with_trace_id(self, caplog):
        """Test that logged snapshots include trace_id."""
        monitor = RequestMonitor(trace_id="test-trace-456", interval_seconds=0.1)
        
        await monitor.start()
        await asyncio.sleep(0.15)  # Get at least one sample
        await monitor.stop()
        
        # Check that logs contain trace_id
        # Note: Actual log checking depends on your logging setup
        assert monitor._trace_id == "test-trace-456"
    
    @pytest.mark.asyncio
    @patch('personal_agent.brainstem.sensors.request_monitor.get_system_metrics_snapshot')
    async def test_polling_continues_on_exception(self, mock_get_metrics):
        """Test that polling continues even if metrics collection fails."""
        # First call succeeds, second fails, third succeeds
        mock_get_metrics.side_effect = [
            {"perf_system_cpu_load": 45.0},
            Exception("Sensor failure"),
            {"perf_system_cpu_load": 50.0}
        ]
        
        monitor = RequestMonitor(trace_id="test-123", interval_seconds=0.1)
        
        await monitor.start()
        await asyncio.sleep(0.35)  # Should attempt 3 polls
        summary = await monitor.stop()
        
        # Should have 2 successful samples (first and third)
        assert summary['sample_count'] == 2


class TestRequestMonitorSummary:
    """Test summary calculation."""
    
    @pytest.mark.asyncio
    async def test_summary_includes_all_required_fields(self):
        """Test that summary includes all required fields."""
        monitor = RequestMonitor(trace_id="test-123", interval_seconds=0.1)
        
        await monitor.start()
        await asyncio.sleep(0.25)
        summary = await monitor.stop()
        
        # Required fields
        assert 'duration_seconds' in summary
        assert 'sample_count' in summary
        assert 'start_time' in summary
        assert 'end_time' in summary
        assert 'threshold_violations' in summary
        
        # Metric fields (if metrics available)
        if summary['sample_count'] > 0:
            assert 'cpu' in summary or 'memory' in summary
    
    @pytest.mark.asyncio
    async def test_summary_calculates_min_max_avg(self):
        """Test that summary calculates min/max/avg correctly."""
        monitor = RequestMonitor(trace_id="test-123", interval_seconds=0.1)
        
        await monitor.start()
        await asyncio.sleep(0.35)  # Get several samples
        summary = await monitor.stop()
        
        if 'cpu' in summary:
            cpu_stats = summary['cpu']
            assert 'min' in cpu_stats
            assert 'max' in cpu_stats
            assert 'avg' in cpu_stats
            assert cpu_stats['min'] <= cpu_stats['avg'] <= cpu_stats['max']
    
    @pytest.mark.asyncio
    async def test_summary_duration_accurate(self):
        """Test that duration_seconds is accurate."""
        monitor = RequestMonitor(trace_id="test-123", interval_seconds=0.1)
        
        await monitor.start()
        await asyncio.sleep(0.5)
        summary = await monitor.stop()
        
        # Should be approximately 0.5 seconds (allow 10% tolerance)
        assert 0.45 <= summary['duration_seconds'] <= 0.55
    
    @pytest.mark.asyncio
    async def test_summary_handles_no_samples(self):
        """Test that summary works even with no samples collected."""
        monitor = RequestMonitor(trace_id="test-123", interval_seconds=10.0)
        
        await monitor.start()
        # Stop immediately before first poll
        await asyncio.sleep(0.01)
        summary = await monitor.stop()
        
        assert summary['sample_count'] == 0
        assert summary['duration_seconds'] >= 0
        # No metric stats should be present
        assert 'cpu' not in summary or summary['cpu'] == {}


class TestRequestMonitorIntegration:
    """Integration tests with real sensor polling."""
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_monitors_real_system_metrics(self):
        """Test monitoring with real system metrics (integration test)."""
        monitor = RequestMonitor(trace_id="integration-test", interval_seconds=1.0)
        
        await monitor.start()
        await asyncio.sleep(3.5)  # Get ~3 real samples
        summary = await monitor.stop()
        
        assert summary['sample_count'] >= 3
        
        # Should have real CPU and memory data
        assert 'cpu' in summary
        assert 'memory' in summary
        
        # Sanity check values
        assert 0 <= summary['cpu']['avg'] <= 100
        assert 0 <= summary['memory']['avg'] <= 100
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_concurrent_monitors(self):
        """Test that multiple monitors can run concurrently."""
        monitor1 = RequestMonitor(trace_id="concurrent-1", interval_seconds=0.2)
        monitor2 = RequestMonitor(trace_id="concurrent-2", interval_seconds=0.3)
        
        await monitor1.start()
        await monitor2.start()
        
        await asyncio.sleep(0.7)
        
        summary1 = await monitor1.stop()
        summary2 = await monitor2.stop()
        
        # Both should have collected samples
        assert summary1['sample_count'] >= 3
        assert summary2['sample_count'] >= 2
        
        # Each should have its own trace_id
        assert monitor1._trace_id == "concurrent-1"
        assert monitor2._trace_id == "concurrent-2"
```

---

## Import Updates

### File: `src/personal_agent/brainstem/sensors/__init__.py` (MODIFY)

**Add these exports at the end**:

```python
# Request monitoring (ADR-0012)
from personal_agent.brainstem.sensors.request_monitor import RequestMonitor
from personal_agent.brainstem.sensors.types import (
    MetricSnapshot,
    MetricsSummary,
    MetricStats,
)

__all__ = [
    # ... existing exports ...
    # Request monitoring
    "RequestMonitor",
    "MetricSnapshot",
    "MetricsSummary",
    "MetricStats",
]
```

---

## Acceptance Criteria Checklist

After implementation, verify:

- [ ] `RequestMonitor` class created in `brainstem/sensors/request_monitor.py`
- [ ] Type definitions created in `brainstem/sensors/types.py`
- [ ] Configuration settings added to `config/settings.py`
- [ ] All 14+ tests in `tests/test_brainstem/test_request_monitor.py` pass
- [ ] Monitor can start and stop cleanly
- [ ] Polling occurs at configured interval
- [ ] Snapshots tagged with trace_id
- [ ] Summary includes min/max/avg for each metric
- [ ] Exceptions during polling don't crash monitor
- [ ] Type checking clean (`mypy src/personal_agent/brainstem/sensors/`)
- [ ] Linting clean (`ruff check src/personal_agent/brainstem/sensors/`)
- [ ] Imports exported from `__init__.py`

---

## Common Implementation Pitfalls

### 1. Asyncio Task Cancellation
**Problem**: Not handling task cancellation properly
**Solution**: Use try/except for `CancelledError` in stop()

### 2. Empty Snapshots
**Problem**: Division by zero when calculating avg with no samples
**Solution**: Check `if cpu_values:` before calculating statistics

### 3. Timestamp Timezone
**Problem**: Using naive datetime instead of UTC
**Solution**: Always use `datetime.now(timezone.utc)`

### 4. Test Timing
**Problem**: Tests fail intermittently due to timing assumptions
**Solution**: Use ranges for assertions (e.g., `2 <= count <= 3`)

### 5. Logging Overhead
**Problem**: Logging every poll could be expensive
**Solution**: Use INFO level (controlled by config) and structured logging

---

## Next Steps After Phase 1

Once Phase 1 is complete and all tests pass:

1. **Run integration tests** to verify with real system metrics
2. **Measure performance overhead** (<1% target)
3. **Review code** for edge cases and error handling
4. **Proceed to Phase 2** (Orchestrator Integration)

---

**This specification provides everything needed to implement Phase 1 correctly.**
