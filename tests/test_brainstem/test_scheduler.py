"""Tests for brainstem scheduler."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from personal_agent.brainstem.scheduler import BrainstemScheduler


@pytest_asyncio.fixture
async def scheduler():
    """Create scheduler instance for testing."""
    sched = BrainstemScheduler()
    yield sched
    if sched.running:
        await sched.stop()


@pytest.mark.asyncio
class TestSchedulerInitialization:
    """Test scheduler initialization and configuration."""

    async def test_default_initialization(self, scheduler):
        """Test scheduler initializes with default values."""
        assert not scheduler.running
        assert scheduler.consolidator is None
        assert scheduler.last_consolidation is None
        assert scheduler.last_request_time is None

        # Check default thresholds
        assert scheduler.idle_time_seconds == 300  # 5 minutes
        assert scheduler.cpu_threshold == 50.0
        assert scheduler.memory_threshold == 70.0
        assert scheduler.check_interval_seconds == 60
        assert scheduler.min_consolidation_interval_seconds == 3600  # 1 hour

    async def test_initialization_with_custom_settings(self):
        """Test scheduler respects custom settings."""
        # Need to patch settings at module level before import
        mock_obj = MagicMock()
        mock_obj.second_brain_idle_time_seconds = 600
        mock_obj.second_brain_cpu_threshold = 30.0
        mock_obj.second_brain_memory_threshold = 60.0
        mock_obj.second_brain_check_interval_seconds = 120
        mock_obj.second_brain_min_interval_seconds = 7200

        with patch("personal_agent.brainstem.scheduler.settings", mock_obj):
            sched = BrainstemScheduler()

            assert sched.idle_time_seconds == 600
            assert sched.cpu_threshold == 30.0
            assert sched.memory_threshold == 60.0
            assert sched.check_interval_seconds == 120
            assert sched.min_consolidation_interval_seconds == 7200

            if sched.running:
                await sched.stop()


@pytest.mark.asyncio
class TestSchedulerStartStop:
    """Test scheduler start and stop functionality."""

    async def test_start_scheduler(self, scheduler):
        """Test starting the scheduler."""
        await scheduler.start()

        assert scheduler.running is True

    async def test_start_already_running(self, scheduler):
        """Test starting scheduler when already running does nothing."""
        await scheduler.start()
        assert scheduler.running is True

        # Try to start again
        await scheduler.start()
        assert scheduler.running is True  # Still running

    async def test_stop_scheduler(self, scheduler):
        """Test stopping the scheduler."""
        await scheduler.start()
        assert scheduler.running is True

        await scheduler.stop()
        assert scheduler.running is False

    async def test_stop_not_running(self, scheduler):
        """Test stopping scheduler when not running."""
        assert not scheduler.running
        await scheduler.stop()
        assert not scheduler.running


@pytest.mark.asyncio
class TestRequestRecording:
    """Test request time recording."""

    async def test_record_request_updates_timestamp(self, scheduler):
        """Test that record_request updates last_request_time."""
        assert scheduler.last_request_time is None

        before = datetime.now(timezone.utc)
        scheduler.record_request()
        after = datetime.now(timezone.utc)

        assert scheduler.last_request_time is not None
        assert before <= scheduler.last_request_time <= after

    async def test_multiple_record_requests(self, scheduler):
        """Test recording multiple requests."""
        scheduler.record_request()
        first_time = scheduler.last_request_time

        await asyncio.sleep(0.01)  # Small delay

        scheduler.record_request()
        second_time = scheduler.last_request_time

        assert second_time > first_time


@pytest.mark.asyncio
class TestConsolidationTriggerConditions:
    """Test conditions for triggering consolidation."""

    async def test_should_consolidate_no_previous_consolidation_no_requests(self, scheduler):
        """Test consolidation allowed when no previous consolidation and no requests."""
        with patch("personal_agent.brainstem.scheduler.settings") as mock_settings:
            mock_settings.enable_second_brain = True

            # Mock resource check to pass
            with patch("personal_agent.brainstem.scheduler.poll_system_metrics") as mock_metrics:
                mock_metrics.return_value = {
                    "perf_system_cpu_load": 20.0,
                    "perf_system_mem_used": 40.0,
                }

                should = await scheduler._should_consolidate()
                assert should is True

    async def test_should_not_consolidate_too_soon_after_last(self, scheduler):
        """Test consolidation blocked if too soon after last consolidation."""
        # Set last consolidation to 30 minutes ago (less than 1 hour minimum)
        scheduler.last_consolidation = datetime.now(timezone.utc) - timedelta(minutes=30)

        should = await scheduler._should_consolidate()
        assert should is False

    async def test_should_consolidate_after_minimum_interval(self, scheduler):
        """Test consolidation allowed after minimum interval."""
        # Set last consolidation to 2 hours ago
        scheduler.last_consolidation = datetime.now(timezone.utc) - timedelta(hours=2)
        scheduler.last_request_time = datetime.now(timezone.utc) - timedelta(minutes=10)

        with patch("personal_agent.brainstem.scheduler.poll_system_metrics") as mock_metrics:
            mock_metrics.return_value = {
                "perf_system_cpu_load": 20.0,
                "perf_system_mem_used": 40.0,
            }

            should = await scheduler._should_consolidate()
            assert should is True

    async def test_should_not_consolidate_not_idle_long_enough(self, scheduler):
        """Test consolidation blocked if system not idle long enough."""
        # Last request 2 minutes ago (less than 5 minute idle requirement)
        scheduler.last_request_time = datetime.now(timezone.utc) - timedelta(minutes=2)

        should = await scheduler._should_consolidate()
        assert should is False

    async def test_should_consolidate_after_idle_period(self, scheduler):
        """Test consolidation allowed after sufficient idle time."""
        # Last request 10 minutes ago (more than 5 minute requirement)
        scheduler.last_request_time = datetime.now(timezone.utc) - timedelta(minutes=10)

        with patch("personal_agent.brainstem.scheduler.poll_system_metrics") as mock_metrics:
            mock_metrics.return_value = {
                "perf_system_cpu_load": 20.0,
                "perf_system_mem_used": 40.0,
            }

            should = await scheduler._should_consolidate()
            assert should is True

    async def test_should_not_consolidate_cpu_too_high(self, scheduler):
        """Test consolidation blocked if CPU load too high."""
        scheduler.last_request_time = datetime.now(timezone.utc) - timedelta(minutes=10)

        with patch("personal_agent.brainstem.scheduler.poll_system_metrics") as mock_metrics:
            # CPU at 60% (above 50% threshold)
            mock_metrics.return_value = {
                "perf_system_cpu_load": 60.0,
                "perf_system_mem_used": 40.0,
            }

            should = await scheduler._should_consolidate()
            assert should is False

    async def test_should_not_consolidate_memory_too_high(self, scheduler):
        """Test consolidation blocked if memory usage too high."""
        scheduler.last_request_time = datetime.now(timezone.utc) - timedelta(minutes=10)

        with patch("personal_agent.brainstem.scheduler.poll_system_metrics") as mock_metrics:
            # Memory at 80% (above 70% threshold)
            mock_metrics.return_value = {
                "perf_system_cpu_load": 20.0,
                "perf_system_mem_used": 80.0,
            }

            should = await scheduler._should_consolidate()
            assert should is False

    async def test_should_not_consolidate_on_metrics_error(self, scheduler):
        """Test consolidation blocked if resource metrics fail."""
        scheduler.last_request_time = datetime.now(timezone.utc) - timedelta(minutes=10)

        with patch("personal_agent.brainstem.scheduler.poll_system_metrics") as mock_metrics:
            mock_metrics.side_effect = Exception("Metrics collection failed")

            should = await scheduler._should_consolidate()
            assert should is False


@pytest.mark.asyncio
class TestConsolidationExecution:
    """Test consolidation execution."""

    async def test_trigger_consolidation_success(self, scheduler):
        """Test successful consolidation trigger."""
        mock_consolidator = AsyncMock()
        mock_consolidator.consolidate_recent_captures.return_value = {
            "captures_processed": 10,
            "entities_extracted": 25,
        }

        with patch("personal_agent.brainstem.scheduler.SecondBrainConsolidator") as mock_class:
            mock_class.return_value = mock_consolidator

            await scheduler._trigger_consolidation()

            # Verify consolidator was created
            assert scheduler.consolidator is not None

            # Verify consolidation was called
            mock_consolidator.consolidate_recent_captures.assert_called_once_with(days=7, limit=50)

            # Verify last_consolidation timestamp was updated
            assert scheduler.last_consolidation is not None
            assert (datetime.now(timezone.utc) - scheduler.last_consolidation).total_seconds() < 1

    async def test_trigger_consolidation_reuses_consolidator(self, scheduler):
        """Test that consolidator instance is reused."""
        mock_consolidator = AsyncMock()
        mock_consolidator.consolidate_recent_captures.return_value = {"captures_processed": 5}

        scheduler.consolidator = mock_consolidator

        await scheduler._trigger_consolidation()

        # Should reuse existing consolidator
        assert scheduler.consolidator is mock_consolidator
        mock_consolidator.consolidate_recent_captures.assert_called_once()

    async def test_trigger_consolidation_error_handling(self, scheduler):
        """Test consolidation error handling."""
        mock_consolidator = AsyncMock()
        mock_consolidator.consolidate_recent_captures.side_effect = Exception(
            "Consolidation failed"
        )

        with patch("personal_agent.brainstem.scheduler.SecondBrainConsolidator") as mock_class:
            mock_class.return_value = mock_consolidator

            # Should not raise exception
            await scheduler._trigger_consolidation()

            # Consolidator should still be set
            assert scheduler.consolidator is not None


@pytest.mark.asyncio
class TestMonitoringLoop:
    """Test monitoring loop behavior."""

    async def test_monitoring_loop_stops_when_running_false(self, scheduler):
        """Test that monitoring loop exits when running is False."""
        scheduler.check_interval_seconds = 0.1  # Fast checking

        with patch("personal_agent.brainstem.scheduler.settings") as mock_settings:
            mock_settings.enable_second_brain = False

            await scheduler.start()
            await asyncio.sleep(0.2)  # Let it run a bit

            await scheduler.stop()

            # Should exit cleanly
            assert not scheduler.running

    async def test_monitoring_loop_skips_when_disabled(self, scheduler):
        """Test monitoring loop skips consolidation when second brain disabled."""
        scheduler.check_interval_seconds = 0.1

        with patch("personal_agent.brainstem.scheduler.settings") as mock_settings:
            mock_settings.enable_second_brain = False

            with patch.object(scheduler, "_trigger_consolidation") as mock_trigger:
                await scheduler.start()
                await asyncio.sleep(0.3)  # Let it run a few cycles
                await scheduler.stop()

                # Should never trigger consolidation
                mock_trigger.assert_not_called()

    async def test_monitoring_loop_triggers_when_conditions_met(self, scheduler):
        """Test monitoring loop triggers consolidation when conditions met."""
        scheduler.check_interval_seconds = 0.1
        scheduler.last_request_time = datetime.now(timezone.utc) - timedelta(minutes=10)

        with patch("personal_agent.brainstem.scheduler.settings") as mock_settings:
            mock_settings.enable_second_brain = True

            with patch("personal_agent.brainstem.scheduler.poll_system_metrics") as mock_metrics:
                mock_metrics.return_value = {
                    "perf_system_cpu_load": 20.0,
                    "perf_system_mem_used": 40.0,
                }

                with patch.object(scheduler, "_trigger_consolidation") as mock_trigger:
                    mock_trigger.return_value = None

                    await scheduler.start()
                    await asyncio.sleep(0.3)  # Let it check a few times
                    await scheduler.stop()

                    # Should trigger at least once
                    assert mock_trigger.call_count >= 1
