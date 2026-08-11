"""Tests for the daily kg_stats projection job's cron parsing and scheduler wiring (FRE-1210 T6.1).

Mirrors ``test_freshness_review_schedule.py``'s shape for the parser tests, and
``test_scheduler.py::TestQualityMonitorScheduling``'s pattern for the
lifecycle-loop trigger tests -- deliberately a separate file rather than
edits to either of those, since this is a genuinely separate job with its
own schedule, not an extension of the weekly freshness review.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

import personal_agent.brainstem.scheduler as scheduler_module
from personal_agent.brainstem.jobs.kg_stats_projection import parse_kg_stats_projection_schedule
from personal_agent.brainstem.scheduler import BrainstemScheduler


def test_parse_default_daily_0400_utc() -> None:
    """``0 4 * * *`` maps to 04:00 UTC, no weekday component."""
    m, h = parse_kg_stats_projection_schedule("0 4 * * *")
    assert (m, h) == (0, 4)


def test_parse_ignores_day_of_week_field() -> None:
    """A non-``*`` dow field is ignored -- this job always runs daily."""
    m, h = parse_kg_stats_projection_schedule("15 6 * * 1")
    assert (m, h) == (15, 6)


def test_parse_invalid_falls_back() -> None:
    """Malformed cron falls back to the default window."""
    m, h = parse_kg_stats_projection_schedule("garbage")
    assert (m, h) == (0, 4)


@pytest_asyncio.fixture
async def scheduler():
    """Scheduler instance with a non-None memory_service (the trigger's gate)."""
    with (
        patch.object(scheduler_module.settings, "second_brain_resource_gating_enabled", True),
        patch.object(scheduler_module.settings, "second_brain_idle_time_seconds", 300.0),
        patch.object(scheduler_module.settings, "second_brain_min_interval_seconds", 3600),
    ):
        sched = BrainstemScheduler(memory_service=MagicMock())
        yield sched
        if sched.running:
            await sched.stop()


@pytest.mark.asyncio
class TestKgStatsProjectionScheduling:
    """Lifecycle-loop trigger for the daily kg_stats projection job."""

    async def test_lifecycle_loop_triggers_projection_daily(self, scheduler) -> None:
        """Runs once when the configured hour/minute match and no run has happened today."""
        scheduler.running = True
        scheduler._last_kg_stats_projection_date = None
        scheduler._backfill_es_logger = None
        scheduler._last_disk_check = datetime.now(timezone.utc)
        now = datetime.now(timezone.utc)

        async def stop_after_first_sleep(_: float) -> None:
            scheduler.running = False

        with (
            patch("personal_agent.brainstem.scheduler.asyncio.sleep", new=stop_after_first_sleep),
            patch("personal_agent.brainstem.scheduler.settings") as mock_settings,
            patch(
                "personal_agent.brainstem.jobs.kg_stats_projection.run_kg_stats_projection",
                new=AsyncMock(),
            ) as mock_projection,
        ):
            mock_settings.data_lifecycle_enabled = False
            mock_settings.insights_enabled = False
            mock_settings.freshness_enabled = True
            mock_settings.kg_stats_projection_schedule_cron = f"{now.minute} {now.hour} * * *"
            mock_settings.freshness_review_schedule_cron = "0 3 * * 0"  # never matches "now"

            await scheduler._lifecycle_loop()

            mock_projection.assert_awaited_once()
            assert scheduler._last_kg_stats_projection_date == now.date()

    async def test_lifecycle_loop_does_not_rerun_same_day(self, scheduler) -> None:
        """A run already recorded for today does not fire a second time."""
        scheduler.running = True
        scheduler._last_kg_stats_projection_date = date.today()
        scheduler._backfill_es_logger = None
        scheduler._last_disk_check = datetime.now(timezone.utc)
        now = datetime.now(timezone.utc)

        async def stop_after_first_sleep(_: float) -> None:
            scheduler.running = False

        with (
            patch("personal_agent.brainstem.scheduler.asyncio.sleep", new=stop_after_first_sleep),
            patch("personal_agent.brainstem.scheduler.settings") as mock_settings,
            patch(
                "personal_agent.brainstem.jobs.kg_stats_projection.run_kg_stats_projection",
                new=AsyncMock(),
            ) as mock_projection,
        ):
            mock_settings.data_lifecycle_enabled = False
            mock_settings.insights_enabled = False
            mock_settings.freshness_enabled = True
            mock_settings.kg_stats_projection_schedule_cron = f"{now.minute} {now.hour} * * *"
            mock_settings.freshness_review_schedule_cron = "0 3 * * 0"

            await scheduler._lifecycle_loop()

            mock_projection.assert_not_awaited()
