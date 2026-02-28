# Phase 2: Orchestrator Integration - Complete Implementation Specification

**Goal**: Integrate `RequestMonitor` into orchestrator execution lifecycle
**Timeline**: 1-2 days
**Dependencies**: Phase 1 complete
**Status**: Ready to implement

---

## Overview

This phase wires the `RequestMonitor` into the orchestrator's task execution lifecycle to provide automatic monitoring for every request. The integration must be:
- Non-blocking (monitoring runs in background)
- Robust (monitoring failures don't block requests)
- Traceable (metrics tagged with trace_id)
- Optional (can be disabled via configuration)

---

## File Modifications

### 1. Orchestrator Executor

**File**: `src/personal_agent/orchestrator/executor.py`

**Location**: Lines 485-575 (the `execute_task` function)

**Current Code** (reference):
```python
async def execute_task(ctx: ExecutionContext, session_manager: SessionManager) -> ExecutionContext:
    """Main execution loop: iterate states until terminal."""
    state = ctx.state
    trace_ctx = TraceContext(trace_id=ctx.trace_id)

    log.info(
        TASK_STARTED,
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        user_message=ctx.user_message,
        mode=ctx.mode.value,
        channel=ctx.channel.value,
    )

    # Step function registry
    step_functions = {
        TaskState.INIT: step_init,
        TaskState.PLANNING: step_planning,
        TaskState.LLM_CALL: step_llm_call,
        TaskState.TOOL_EXECUTION: step_tool_execution,
        TaskState.SYNTHESIS: step_synthesis,
    }

    try:
        while state not in {TaskState.COMPLETED, TaskState.FAILED}:
            # ... execution loop ...

        ctx.state = state

        if state == TaskState.COMPLETED:
            log.info(
                TASK_COMPLETED,
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                reply_length=len(ctx.final_reply or ""),
                steps_count=len(ctx.steps),
            )
            # Trigger Captain's Log reflection (LLM-based, background)
            from personal_agent.captains_log.background import run_in_background
            run_in_background(_trigger_captains_log_reflection(ctx))
        else:
            log.warning(
                TASK_FAILED,
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                error=str(ctx.error) if ctx.error else "Unknown error",
            )

    except Exception as e:
        log.error(
            ORCHESTRATOR_FATAL_ERROR,
            trace_id=ctx.trace_id,
            exc_info=True,
        )
        ctx.error = e
        ctx.state = TaskState.FAILED

    return ctx
```

**New Code** (with monitoring integrated):
```python
async def execute_task(ctx: ExecutionContext, session_manager: SessionManager) -> ExecutionContext:
    """Main execution loop: iterate states until terminal.

    Automatically starts request-scoped metrics monitoring if enabled
    in configuration. Monitoring runs in background and provides
    performance context for Captain's Log reflections.
    """
    state = ctx.state
    trace_ctx = TraceContext(trace_id=ctx.trace_id)

    # ========== START MONITORING (NEW) ==========
    # Import here to avoid circular dependencies
    from personal_agent.config import settings

    monitor = None
    if settings.request_monitoring_enabled:
        try:
            from personal_agent.brainstem.sensors.request_monitor import RequestMonitor

            monitor = RequestMonitor(
                trace_id=ctx.trace_id,
                interval_seconds=settings.request_monitoring_interval_seconds
            )
            await monitor.start()

            log.debug(
                "request_monitoring_started",
                trace_id=ctx.trace_id,
                interval_seconds=settings.request_monitoring_interval_seconds
            )
        except Exception as e:
            # Log error but don't block request execution
            log.error(
                "request_monitoring_start_failed",
                trace_id=ctx.trace_id,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True
            )
            monitor = None  # Ensure monitor is None if start failed
    # ========== END MONITORING SETUP ==========

    log.info(
        TASK_STARTED,
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        user_message=ctx.user_message,
        mode=ctx.mode.value,
        channel=ctx.channel.value,
    )

    # Step function registry
    step_functions = {
        TaskState.INIT: step_init,
        TaskState.PLANNING: step_planning,
        TaskState.LLM_CALL: step_llm_call,
        TaskState.TOOL_EXECUTION: step_tool_execution,
        TaskState.SYNTHESIS: step_synthesis,
    }

    try:
        while state not in {TaskState.COMPLETED, TaskState.FAILED}:
            log.info(
                STATE_TRANSITION,
                trace_id=ctx.trace_id,
                from_state=state.value,
            )
            ctx.state = state

            step_func = step_functions.get(state)
            if not step_func:
                log.error(
                    UNKNOWN_STATE,
                    trace_id=ctx.trace_id,
                    state=state.value,
                )
                ctx.error = ValueError(f"Unknown state: {state}")
                state = TaskState.FAILED
                break

            # Execute step function
            state = await step_func(ctx, session_manager, trace_ctx)

        ctx.state = state

        if state == TaskState.COMPLETED:
            log.info(
                TASK_COMPLETED,
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                reply_length=len(ctx.final_reply or ""),
                steps_count=len(ctx.steps),
            )
            # Trigger Captain's Log reflection (LLM-based, background)
            # Note: ctx.metrics_summary will be populated in finally block below
            from personal_agent.captains_log.background import run_in_background
            run_in_background(_trigger_captains_log_reflection(ctx))
        else:
            log.warning(
                TASK_FAILED,
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                error=str(ctx.error) if ctx.error else "Unknown error",
            )

    except Exception as e:
        log.error(
            ORCHESTRATOR_FATAL_ERROR,
            trace_id=ctx.trace_id,
            exc_info=True,
        )
        ctx.error = e
        ctx.state = TaskState.FAILED

    # ========== STOP MONITORING (NEW) ==========
    finally:
        # Always stop monitoring and get summary, even if request failed
        if monitor is not None:
            try:
                metrics_summary = await monitor.stop()

                # Attach summary to context for Captain's Log
                ctx.metrics_summary = metrics_summary

                # Log summary for telemetry analysis
                log.info(
                    REQUEST_METRICS_SUMMARY,
                    trace_id=ctx.trace_id,
                    session_id=ctx.session_id,
                    duration_seconds=metrics_summary['duration_seconds'],
                    sample_count=metrics_summary['sample_count'],
                    cpu_avg=metrics_summary.get('cpu', {}).get('avg'),
                    cpu_max=metrics_summary.get('cpu', {}).get('max'),
                    memory_avg=metrics_summary.get('memory', {}).get('avg'),
                    memory_max=metrics_summary.get('memory', {}).get('max'),
                    gpu_avg=metrics_summary.get('gpu', {}).get('avg'),
                    threshold_violations=metrics_summary.get('threshold_violations', [])
                )

                log.debug(
                    "request_monitoring_stopped",
                    trace_id=ctx.trace_id,
                    summary=metrics_summary
                )
            except Exception as e:
                # Log error but don't propagate (monitoring failure shouldn't fail request)
                log.error(
                    "request_monitoring_stop_failed",
                    trace_id=ctx.trace_id,
                    error=str(e),
                    error_type=type(e).__name__,
                    exc_info=True
                )
    # ========== END MONITORING CLEANUP ==========

    return ctx
```

**Key Changes**:
1. Import `settings` to check if monitoring enabled
2. Create and start `RequestMonitor` before TASK_STARTED log
3. Wrap monitor.start() in try-except (don't block request if monitoring fails)
4. Add `finally` block to ensure monitor.stop() is always called
5. Attach metrics_summary to ctx for Captain's Log
6. Log REQUEST_METRICS_SUMMARY event with key metrics
7. Wrap monitor.stop() in try-except (monitoring failure shouldn't fail request)

### 2. ExecutionContext Type

**File**: `src/personal_agent/orchestrator/types.py`

**Location**: After line 159 (after `tool_iteration_count` field)

**Add this field**:
```python
@dataclass
class ExecutionContext:
    """Mutable state container passed through execution steps."""

    # ... existing fields ...

    # Tool loop governance (per-request)
    tool_iteration_count: int = 0

    # ========== ADD THIS FIELD (NEW) ==========
    # Request monitoring (ADR-0012)
    metrics_summary: dict[str, Any] | None = None
    """Aggregated metrics summary from request monitoring.

    Populated by RequestMonitor.stop() in executor.py finally block.
    Provides performance context for Captain's Log reflections.

    Structure matches MetricsSummary TypedDict:
    - duration_seconds: float
    - sample_count: int
    - cpu/memory/disk/gpu: dict with min/max/avg
    - threshold_violations: list[str]

    None if monitoring disabled or not yet completed.
    """
    # ========== END NEW FIELD ==========
```

**Import additions** (at top of file):
```python
from typing import TYPE_CHECKING, Any, TypedDict  # Add 'Any' if not present
```

### 3. Telemetry Events

**File**: `src/personal_agent/telemetry/events.py`

**Location**: After line 30 (after `SYSTEM_METRICS_SNAPSHOT`)

**Add this constant**:
```python
# Brainstem events
MODE_TRANSITION = "mode_transition"
SENSOR_POLL = "sensor_poll"
SYSTEM_METRICS_SNAPSHOT = "system_metrics_snapshot"

# ========== ADD THIS (NEW) ==========
# Request monitoring events (ADR-0012)
REQUEST_METRICS_SUMMARY = "request_metrics_summary"
# ========== END NEW CONSTANT ==========
```

**Update exports** (at top of file):
```python
from personal_agent.telemetry.events import (
    # ... existing exports ...
    REQUEST_METRICS_SUMMARY,  # Add this
)
```

---

## Integration Tests

### File: `tests/test_orchestrator/test_request_monitoring.py` (NEW)

```python
"""Integration tests for request monitoring in orchestrator."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.executor import execute_task
from personal_agent.orchestrator.session import SessionManager
from personal_agent.orchestrator.types import ExecutionContext, TaskState


class TestRequestMonitoringIntegration:
    """Test request monitoring integration with orchestrator."""

    @pytest.mark.asyncio
    async def test_monitoring_starts_and_stops_with_task(self):
        """Test that monitoring starts when task begins and stops when complete."""
        session_manager = SessionManager()
        session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)

        ctx = ExecutionContext(
            session_id=session_id,
            trace_id="test-trace-123",
            user_message="Hello",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        with patch('personal_agent.config.settings.request_monitoring_enabled', True):
            with patch('personal_agent.config.settings.request_monitoring_interval_seconds', 0.1):
                # Execute task (will use router which handles directly or delegates)
                result_ctx = await execute_task(ctx, session_manager)

        # Verify monitoring ran
        assert result_ctx.metrics_summary is not None
        assert result_ctx.metrics_summary['sample_count'] > 0
        assert result_ctx.metrics_summary['duration_seconds'] > 0
        assert 'cpu' in result_ctx.metrics_summary or 'memory' in result_ctx.metrics_summary

    @pytest.mark.asyncio
    async def test_monitoring_disabled_via_config(self):
        """Test that monitoring doesn't run when disabled in config."""
        session_manager = SessionManager()
        session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)

        ctx = ExecutionContext(
            session_id=session_id,
            trace_id="test-trace-456",
            user_message="Hello",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        with patch('personal_agent.config.settings.request_monitoring_enabled', False):
            result_ctx = await execute_task(ctx, session_manager)

        # Verify monitoring didn't run
        assert result_ctx.metrics_summary is None

    @pytest.mark.asyncio
    async def test_monitoring_continues_through_state_transitions(self):
        """Test that monitoring continues throughout task execution."""
        session_manager = SessionManager()
        session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)

        ctx = ExecutionContext(
            session_id=session_id,
            trace_id="test-trace-789",
            user_message="What is Python?",  # Will require LLM call
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        with patch('personal_agent.config.settings.request_monitoring_enabled', True):
            with patch('personal_agent.config.settings.request_monitoring_interval_seconds', 0.2):
                result_ctx = await execute_task(ctx, session_manager)

        # Task should have gone through multiple states
        assert result_ctx.state in {TaskState.COMPLETED, TaskState.FAILED}

        # Monitoring should have captured multiple samples
        assert result_ctx.metrics_summary is not None
        assert result_ctx.metrics_summary['sample_count'] >= 2

    @pytest.mark.asyncio
    async def test_monitoring_cleanup_on_error(self):
        """Test that monitoring stops cleanly even if task fails."""
        session_manager = SessionManager()
        session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)

        ctx = ExecutionContext(
            session_id=session_id,
            trace_id="test-trace-error",
            user_message="Trigger error",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        # Mock step function to raise error
        with patch('personal_agent.orchestrator.executor.step_init') as mock_step:
            mock_step.side_effect = RuntimeError("Test error")

            with patch('personal_agent.config.settings.request_monitoring_enabled', True):
                with patch('personal_agent.config.settings.request_monitoring_interval_seconds', 0.1):
                    result_ctx = await execute_task(ctx, session_manager)

        # Task should have failed
        assert result_ctx.state == TaskState.FAILED
        assert result_ctx.error is not None

        # But monitoring should still have summary
        assert result_ctx.metrics_summary is not None
        assert result_ctx.metrics_summary['duration_seconds'] > 0

    @pytest.mark.asyncio
    async def test_monitoring_failure_doesnt_block_task(self):
        """Test that monitoring failures don't prevent task execution."""
        session_manager = SessionManager()
        session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)

        ctx = ExecutionContext(
            session_id=session_id,
            trace_id="test-trace-robust",
            user_message="Hello",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        # Mock RequestMonitor.start to raise error
        with patch('personal_agent.brainstem.sensors.request_monitor.RequestMonitor.start') as mock_start:
            mock_start.side_effect = RuntimeError("Monitor start failed")

            with patch('personal_agent.config.settings.request_monitoring_enabled', True):
                # Task should still execute successfully
                result_ctx = await execute_task(ctx, session_manager)

        # Task should complete despite monitoring failure
        assert result_ctx.state in {TaskState.COMPLETED, TaskState.FAILED}
        # metrics_summary will be None since monitoring failed to start
        assert result_ctx.metrics_summary is None


class TestMetricsSummaryLogging:
    """Test that metrics summary is logged correctly."""

    @pytest.mark.asyncio
    async def test_request_metrics_summary_event_logged(self, caplog):
        """Test that REQUEST_METRICS_SUMMARY event is logged."""
        session_manager = SessionManager()
        session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)

        ctx = ExecutionContext(
            session_id=session_id,
            trace_id="test-trace-logging",
            user_message="Test",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        with patch('personal_agent.config.settings.request_monitoring_enabled', True):
            with patch('personal_agent.config.settings.request_monitoring_interval_seconds', 0.1):
                await execute_task(ctx, session_manager)

        # Verify log contains REQUEST_METRICS_SUMMARY
        # Note: Actual log checking depends on your logging configuration
        # This is a placeholder - adjust based on your test setup
        pass


class TestCaptainLogIntegration:
    """Test that metrics summary is passed to Captain's Log."""

    @pytest.mark.asyncio
    async def test_metrics_summary_available_for_captains_log(self):
        """Test that metrics_summary is attached to context for Captain's Log."""
        session_manager = SessionManager()
        session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)

        ctx = ExecutionContext(
            session_id=session_id,
            trace_id="test-trace-captains-log",
            user_message="Test",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )

        with patch('personal_agent.config.settings.request_monitoring_enabled', True):
            with patch('personal_agent.config.settings.request_monitoring_interval_seconds', 0.1):
                result_ctx = await execute_task(ctx, session_manager)

        # Verify metrics_summary is available
        assert result_ctx.metrics_summary is not None

        # Verify it has expected structure
        assert 'duration_seconds' in result_ctx.metrics_summary
        assert 'sample_count' in result_ctx.metrics_summary
        assert 'threshold_violations' in result_ctx.metrics_summary

        # Captain's Log reflection will receive this context
        # (actual reflection happens in background task)
```

---

## Configuration Validation

Ensure configuration settings are loaded correctly:

```python
# In tests/test_config/test_settings.py (add these tests)

def test_request_monitoring_settings_defaults():
    """Test that request monitoring settings have correct defaults."""
    from personal_agent.config import settings

    assert settings.request_monitoring_enabled is True
    assert settings.request_monitoring_interval_seconds == 5.0
    assert settings.request_monitoring_include_gpu is True


def test_request_monitoring_interval_validation():
    """Test that interval validation works."""
    from personal_agent.config.settings import AppConfig
    from pydantic import ValidationError
    import pytest

    # Valid intervals
    AppConfig(request_monitoring_interval_seconds=0.1)  # Min
    AppConfig(request_monitoring_interval_seconds=60.0)  # Max
    AppConfig(request_monitoring_interval_seconds=5.0)  # Default

    # Invalid intervals
    with pytest.raises(ValidationError):
        AppConfig(request_monitoring_interval_seconds=0.0)  # Too low

    with pytest.raises(ValidationError):
        AppConfig(request_monitoring_interval_seconds=61.0)  # Too high
```

---

## Acceptance Criteria Checklist

After implementation, verify:

- [ ] `execute_task` function modified in `orchestrator/executor.py`
- [ ] `metrics_summary` field added to `ExecutionContext`
- [ ] `REQUEST_METRICS_SUMMARY` constant added to `telemetry/events.py`
- [ ] Monitoring starts when task begins
- [ ] Monitoring stops when task completes (success or failure)
- [ ] `metrics_summary` attached to context
- [ ] `REQUEST_METRICS_SUMMARY` event logged
- [ ] Monitoring can be disabled via configuration
- [ ] Monitoring failures don't block request execution
- [ ] All 5+ integration tests pass
- [ ] Existing orchestrator tests still pass (no regression)
- [ ] Type checking clean (`mypy src/personal_agent/orchestrator/`)
- [ ] Linting clean (`ruff check src/personal_agent/orchestrator/`)

---

## Common Implementation Pitfalls

### 1. Import Placement
**Problem**: Circular imports if RequestMonitor imported at module level
**Solution**: Import inside execute_task function (where used)

### 2. Error Handling
**Problem**: Monitoring failures blocking request execution
**Solution**: Wrap both monitor.start() and monitor.stop() in try-except

### 3. Finally Block
**Problem**: Not cleaning up monitor on early return or exception
**Solution**: Use finally block to ensure monitor.stop() always called

### 4. None Checks
**Problem**: Calling monitor.stop() when monitor is None
**Solution**: Check `if monitor is not None` before calling stop()

### 5. Captain's Log Timing
**Problem**: Captain's Log triggered before metrics_summary populated
**Solution**: metrics_summary is populated in finally block before Captain's Log background task runs

---

## Debugging Tips

### Verify Monitoring is Running
```python
# In execute_task, after monitor.start():
log.debug("monitor_started", trace_id=ctx.trace_id, task_name=monitor._task.get_name())
```

### Check Summary Contents
```python
# In finally block, after monitor.stop():
log.debug("monitor_summary", trace_id=ctx.trace_id, summary=metrics_summary)
```

### Verify Configuration Loaded
```python
# At top of execute_task:
from personal_agent.config import settings
log.debug("monitoring_config",
    enabled=settings.request_monitoring_enabled,
    interval=settings.request_monitoring_interval_seconds)
```

---

## Next Steps After Phase 2

Once Phase 2 is complete:

1. **Run all tests** (unit + integration)
2. **Manual testing**: Execute a request and verify metrics_summary in logs
3. **Performance check**: Measure overhead with `time` command
4. **Proceed to Phase 3** (Control Loop Integration)

---

**This specification provides exact code changes for Phase 2 integration.**
