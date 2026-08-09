"""Unit tests for the cache-erosion scheduler runner (ADR-0078 / FRE-1189)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_report(any_eroded: bool = False) -> "ErosionReport":
    from personal_agent.observability.cache_erosion.monitor import ErosionReport

    return ErosionReport(
        computed_at=datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc),
        results=[],
        any_eroded=any_eroded,
        threshold=0.9,
    )


class TestRunScheduledCacheErosionProbe:
    """run_scheduled_cache_erosion_probe drives compute → sink."""

    @pytest.mark.asyncio
    async def test_writes_to_es_when_client_provided(self) -> None:
        report = _make_report()
        es = AsyncMock()

        with (
            patch(
                "personal_agent.observability.cache_erosion.scheduler_runner.compute_erosion_report",
                new=AsyncMock(return_value=report),
            ),
            patch(
                "personal_agent.observability.cache_erosion.scheduler_runner.write_result",
                new=AsyncMock(),
            ) as mock_write,
        ):
            from personal_agent.observability.cache_erosion.scheduler_runner import (
                run_scheduled_cache_erosion_probe,
            )

            result = await run_scheduled_cache_erosion_probe(es_client=es)

        assert result is not None
        assert result.any_eroded is False
        assert result.run_at == report.computed_at
        mock_write.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self) -> None:
        cfg = MagicMock()
        cfg.cache_erosion_probe_enabled = False

        with patch(
            "personal_agent.observability.cache_erosion.scheduler_runner.get_settings",
            return_value=cfg,
        ):
            from personal_agent.observability.cache_erosion.scheduler_runner import (
                run_scheduled_cache_erosion_probe,
            )

            result = await run_scheduled_cache_erosion_probe(es_client=None)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_and_skips_compute_when_es_client_is_none(self) -> None:
        with patch(
            "personal_agent.observability.cache_erosion.scheduler_runner.compute_erosion_report",
            new=AsyncMock(),
        ) as mock_compute:
            from personal_agent.observability.cache_erosion.scheduler_runner import (
                run_scheduled_cache_erosion_probe,
            )

            result = await run_scheduled_cache_erosion_probe(es_client=None)

        assert result is None
        mock_compute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_swallows_es_write_error(self) -> None:
        """An ES write failure must not propagate — scheduler must not crash — and the doc
        is still returned, since a write failure is not the same as "the probe did not run"
        (the scheduler's advance-timestamp check reads only the return value).
        """
        report = _make_report()
        es = AsyncMock()

        with (
            patch(
                "personal_agent.observability.cache_erosion.scheduler_runner.compute_erosion_report",
                new=AsyncMock(return_value=report),
            ),
            patch(
                "personal_agent.observability.cache_erosion.scheduler_runner.write_result",
                new=AsyncMock(side_effect=RuntimeError("ES down")),
            ),
        ):
            from personal_agent.observability.cache_erosion.scheduler_runner import (
                run_scheduled_cache_erosion_probe,
            )

            result = await run_scheduled_cache_erosion_probe(es_client=es)

        assert result is not None
