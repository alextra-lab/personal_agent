"""FRE-1611 AC-2 — ``feedback_polling_enabled`` gates the feedback poll.

The flag is one of three that production holds at ``false`` (Streams 2 and 4). One
scheduler tick runs with the poller armed and due. With the flag false, the tick
must not poll Linear. The ``true`` case is the control that proves the tick reaches
the poll when the flag allows it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.brainstem.scheduler import BrainstemScheduler


async def _run_one_tick(*, polling_enabled: bool) -> AsyncMock:
    """Run one ``_lifecycle_loop`` tick with a due poller and return its check mock."""
    scheduler = BrainstemScheduler()
    scheduler.running = True
    now = datetime.now(timezone.utc)
    # Skip every other conditional job this tick.
    scheduler._last_disk_check = now
    scheduler._backfill_es_logger = None
    scheduler._last_embedding_backfill_run = now
    scheduler.joinability_probe_enabled = False
    scheduler.slm_health_probe_enabled = False
    scheduler.cache_erosion_probe_enabled = False
    scheduler.delivery_ratio_probe_enabled = False
    scheduler._last_domain_guard_warm_run = now
    scheduler._last_freshness_review_week = now.isocalendar()[:2]
    scheduler._last_kg_stats_projection_date = now.date()
    scheduler._linear_client = None
    scheduler.quality_monitor_enabled = False

    # A poller that is due: its hour is this hour and it has not run today.
    poller = MagicMock()
    poller.check_for_feedback = AsyncMock(return_value=[])
    poller.process_feedback = AsyncMock()
    scheduler.feedback_poller = poller
    scheduler.feedback_polling_hour_utc = now.hour
    scheduler._last_feedback_date = None

    async def stop_after_first_sleep(_: float) -> None:
        scheduler.running = False

    with (
        patch("personal_agent.brainstem.scheduler.asyncio.sleep", new=stop_after_first_sleep),
        patch("personal_agent.brainstem.scheduler.settings") as mock_settings,
    ):
        mock_settings.data_lifecycle_enabled = False
        mock_settings.freshness_enabled = False
        mock_settings.feedback_polling_enabled = polling_enabled
        await scheduler._lifecycle_loop()

    return poller.check_for_feedback


@pytest.mark.asyncio
@pytest.mark.parametrize(("enabled", "expected_polls"), [(False, 0), (True, 1)])
async def test_feedback_polling_enabled_gates_the_poll(enabled: bool, expected_polls: int) -> None:
    """With the flag false the tick does not poll Linear for feedback."""
    check = await _run_one_tick(polling_enabled=enabled)
    assert check.await_count == expected_polls
