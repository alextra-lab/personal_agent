"""Tests for the scheduled sysgraph VACUUM (ANALYZE) maintenance job (ADR-0105 D8, FRE-718)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.brainstem.jobs.sysgraph_maintenance import run_sysgraph_maintenance


@pytest.mark.asyncio
class TestRunSysgraphMaintenance:
    """Unit tests for run_sysgraph_maintenance, mocked repository (mirrors test_outcome_ingestion.py)."""

    async def test_disabled_skips_entirely_and_reports_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The disabled flag skips the pass without constructing a repository.

        Returns True -- nothing to do is not a failure the caller should retry.
        """
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sysgraph_maintenance_enabled", False)
        repo_ctor = MagicMock()
        monkeypatch.setattr("personal_agent.sysgraph.SysgraphRepository", repo_ctor)

        result = await run_sysgraph_maintenance(trace_id="test-trace")

        assert result is True
        repo_ctor.assert_not_called()

    async def test_connect_failure_degrades_gracefully_and_reports_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A connect() failure is caught and logged, not raised.

        Reported as False so the scheduler's daily-hour gate retries rather than marking the
        day done.
        """
        repo = MagicMock()
        repo.connect = AsyncMock(side_effect=RuntimeError("boom"))
        repo.disconnect = AsyncMock()
        monkeypatch.setattr(
            "personal_agent.sysgraph.SysgraphRepository", MagicMock(return_value=repo)
        )

        result = await run_sysgraph_maintenance(trace_id="test-trace")  # must not raise

        assert result is False
        repo.vacuum_analyze_all.assert_not_called()
        # A connect failure returns before the try/finally block -- disconnect() is never called
        # (there is nothing to disconnect).
        repo.disconnect.assert_not_awaited()

    async def test_happy_path_runs_vacuum_then_records_then_disconnects_and_reports_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The happy path calls connect -> vacuum_analyze_all -> record_maintenance_run.

        Then disconnect, and reports True.
        """
        repo = MagicMock()
        repo.connect = AsyncMock()
        repo.disconnect = AsyncMock()
        repo.vacuum_analyze_all = AsyncMock(return_value={"proposal": "ok", "ticket": "ok"})
        repo.record_maintenance_run = AsyncMock()
        monkeypatch.setattr(
            "personal_agent.sysgraph.SysgraphRepository", MagicMock(return_value=repo)
        )

        result = await run_sysgraph_maintenance(trace_id="test-trace")

        assert result is True
        repo.connect.assert_awaited_once()
        repo.vacuum_analyze_all.assert_awaited_once()
        repo.record_maintenance_run.assert_awaited_once_with({"proposal": "ok", "ticket": "ok"})
        repo.disconnect.assert_awaited_once()

    async def test_disconnect_still_runs_and_reports_failure_when_vacuum_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """disconnect() always runs via finally, even when vacuum_analyze_all raises.

        The pass is reported as False.
        """
        repo = MagicMock()
        repo.connect = AsyncMock()
        repo.disconnect = AsyncMock()
        repo.vacuum_analyze_all = AsyncMock(side_effect=RuntimeError("vacuum exploded"))
        repo.record_maintenance_run = AsyncMock()
        monkeypatch.setattr(
            "personal_agent.sysgraph.SysgraphRepository", MagicMock(return_value=repo)
        )

        result = await run_sysgraph_maintenance(trace_id="test-trace")  # must not raise

        assert result is False
        repo.disconnect.assert_awaited_once()
        repo.record_maintenance_run.assert_not_awaited()
