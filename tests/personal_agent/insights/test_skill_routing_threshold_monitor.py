"""Unit tests for SkillRoutingThresholdMonitor — FRE-335 / ADR-0066 D2.

All four acceptance-criteria cases:
1. Under threshold — no ticket filed.
2. Over threshold for 1 day — no ticket yet.
3. Over threshold for 2+ consecutive days — ticket filed.
4. Over threshold for 2+ days but open ticket already exists — no duplicate.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.insights.skill_routing_threshold_monitor import (
    SkillRoutingThresholdMonitor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_THRESHOLD = 6000  # tokens


def _make_monitor(
    tmp_path: Path,
    p95_chars_return: float,
    linear_issues: list[dict] | None = None,
    ticket_identifier: str | None = "FRE-999",
) -> tuple[SkillRoutingThresholdMonitor, AsyncMock, AsyncMock]:
    """Build a monitor with mocked ES queries and Linear client."""
    mock_queries = MagicMock()
    mock_queries.get_skill_index_p95_chars = AsyncMock(return_value=p95_chars_return)

    mock_linear = AsyncMock()
    mock_linear.list_issues = AsyncMock(return_value=linear_issues or [])
    mock_linear.create_issue = AsyncMock(return_value=ticket_identifier)

    monitor = SkillRoutingThresholdMonitor(
        queries=mock_queries,
        linear_client=mock_linear,
        output_dir=tmp_path,
        threshold_tokens=_THRESHOLD,
    )
    return monitor, mock_linear, mock_queries


def _seed_readings(monitor: SkillRoutingThresholdMonitor, readings: list[dict]) -> None:
    """Pre-populate the state file with given readings."""
    state = {"readings": readings, "last_ticket_identifier": None, "last_ticket_filed_date": None}
    monitor._state_path.parent.mkdir(parents=True, exist_ok=True)
    monitor._state_path.write_text(json.dumps(state))


# ---------------------------------------------------------------------------
# Case 1: Under threshold — no ticket
# ---------------------------------------------------------------------------


class TestUnderThreshold:
    @pytest.mark.asyncio
    async def test_no_ticket_when_p95_below_threshold(self, tmp_path: Path) -> None:
        """p95 well below threshold: no ticket filed, state written."""
        # 12,000 chars ÷ 4 = 3,000 tokens — below 6,000 threshold
        monitor, mock_linear, _ = _make_monitor(tmp_path, p95_chars_return=12_000.0)

        await monitor.run()

        mock_linear.create_issue.assert_not_called()

    @pytest.mark.asyncio
    async def test_state_file_written_with_reading(self, tmp_path: Path) -> None:
        """State file captures today's reading even when under threshold."""
        monitor, _, _ = _make_monitor(tmp_path, p95_chars_return=8_000.0)

        await monitor.run()

        state = json.loads(monitor._state_path.read_text())
        assert len(state["readings"]) == 1
        reading = state["readings"][0]
        assert reading["date"] == date.today().isoformat()
        assert reading["p95_chars"] == pytest.approx(8_000.0)
        assert reading["p95_tokens"] == pytest.approx(2_000.0)


# ---------------------------------------------------------------------------
# Case 2: Over threshold for exactly 1 day — no ticket yet
# ---------------------------------------------------------------------------


class TestOneConsecutiveDay:
    @pytest.mark.asyncio
    async def test_no_ticket_after_one_day_over_threshold(self, tmp_path: Path) -> None:
        """Threshold exceeded today only (no yesterday reading): no ticket filed."""
        # 28,000 chars ÷ 4 = 7,000 tokens — above 6,000 threshold
        monitor, mock_linear, _ = _make_monitor(tmp_path, p95_chars_return=28_000.0)
        # No prior readings → consecutive count = 1

        await monitor.run()

        mock_linear.create_issue.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_ticket_when_yesterday_was_under_threshold(self, tmp_path: Path) -> None:
        """Yesterday was under threshold; today is over: still only 1 consecutive day."""
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        monitor, mock_linear, _ = _make_monitor(tmp_path, p95_chars_return=28_000.0)
        _seed_readings(monitor, [
            {"date": yesterday, "p95_chars": 10_000.0, "p95_tokens": 2_500.0},  # under
        ])

        await monitor.run()

        mock_linear.create_issue.assert_not_called()


# ---------------------------------------------------------------------------
# Case 3: Over threshold for 2+ consecutive days — file ticket
# ---------------------------------------------------------------------------


class TestTwoConsecutiveDays:
    @pytest.mark.asyncio
    async def test_ticket_filed_after_two_consecutive_days(self, tmp_path: Path) -> None:
        """Threshold exceeded yesterday AND today: ticket is filed."""
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        monitor, mock_linear, _ = _make_monitor(
            tmp_path,
            p95_chars_return=28_000.0,  # 7,000 tokens — over threshold
            ticket_identifier="FRE-999",
        )
        _seed_readings(monitor, [
            {"date": yesterday, "p95_chars": 26_000.0, "p95_tokens": 6_500.0},  # over
        ])

        await monitor.run()

        mock_linear.create_issue.assert_called_once()
        call_kwargs = mock_linear.create_issue.call_args[1]
        assert "Skill index p95 threshold exceeded" in call_kwargs["title"]
        assert call_kwargs["state"] == "Needs Approval"

    @pytest.mark.asyncio
    async def test_state_file_records_ticket_identifier(self, tmp_path: Path) -> None:
        """After filing, state file stores the ticket identifier and date."""
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        monitor, _, _ = _make_monitor(
            tmp_path,
            p95_chars_return=28_000.0,
            ticket_identifier="FRE-999",
        )
        _seed_readings(monitor, [
            {"date": yesterday, "p95_chars": 26_000.0, "p95_tokens": 6_500.0},
        ])

        await monitor.run()

        state = json.loads(monitor._state_path.read_text())
        assert state["last_ticket_identifier"] == "FRE-999"
        assert state["last_ticket_filed_date"] == date.today().isoformat()

    @pytest.mark.asyncio
    async def test_ticket_description_contains_trend_table(self, tmp_path: Path) -> None:
        """Ticket description includes the 14-day trend markdown table."""
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        monitor, mock_linear, _ = _make_monitor(tmp_path, p95_chars_return=28_000.0)
        _seed_readings(monitor, [
            {"date": yesterday, "p95_chars": 26_000.0, "p95_tokens": 6_500.0},
        ])

        await monitor.run()

        description = mock_linear.create_issue.call_args[1]["description"]
        assert "14-day p95 trend" in description
        assert yesterday in description


# ---------------------------------------------------------------------------
# Case 4: Over threshold 2+ days but existing open ticket — no duplicate
# ---------------------------------------------------------------------------


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_no_duplicate_when_open_ticket_exists(self, tmp_path: Path) -> None:
        """If an open 'Needs Approval' trigger ticket exists, do not file a duplicate."""
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        existing_ticket = {
            "identifier": "FRE-998",
            "title": "Skill index p95 threshold exceeded: 7000 tokens",
            "state": {"name": "Needs Approval"},
        }
        monitor, mock_linear, _ = _make_monitor(
            tmp_path,
            p95_chars_return=28_000.0,
            linear_issues=[existing_ticket],
        )
        _seed_readings(monitor, [
            {"date": yesterday, "p95_chars": 26_000.0, "p95_tokens": 6_500.0},
        ])

        await monitor.run()

        mock_linear.list_issues.assert_called_once()
        mock_linear.create_issue.assert_not_called()

    @pytest.mark.asyncio
    async def test_ticket_filed_when_previous_closed(self, tmp_path: Path) -> None:
        """If prior trigger ticket is closed (not in Needs Approval), file a new one."""
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        # list_issues returns empty → no open Needs Approval ticket for the marker title
        monitor, mock_linear, _ = _make_monitor(
            tmp_path,
            p95_chars_return=28_000.0,
            linear_issues=[],  # no open tickets
        )
        _seed_readings(monitor, [
            {"date": yesterday, "p95_chars": 26_000.0, "p95_tokens": 6_500.0},
        ])

        await monitor.run()

        mock_linear.create_issue.assert_called_once()


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------


class TestCountConsecutiveExceeded:
    def _monitor(self, tmp_path: Path) -> SkillRoutingThresholdMonitor:
        return SkillRoutingThresholdMonitor(
            queries=MagicMock(), linear_client=None,
            output_dir=tmp_path, threshold_tokens=_THRESHOLD,
        )

    def test_zero_when_no_readings(self, tmp_path: Path) -> None:
        m = self._monitor(tmp_path)
        assert m._count_consecutive_exceeded([], date.today()) == 0

    def test_one_for_today_only(self, tmp_path: Path) -> None:
        m = self._monitor(tmp_path)
        readings = [{"date": date.today().isoformat(), "p95_tokens": 7000.0}]
        assert m._count_consecutive_exceeded(readings, date.today()) == 1

    def test_two_for_two_consecutive_days(self, tmp_path: Path) -> None:
        m = self._monitor(tmp_path)
        readings = [
            {"date": (date.today() - timedelta(days=1)).isoformat(), "p95_tokens": 7000.0},
            {"date": date.today().isoformat(), "p95_tokens": 7500.0},
        ]
        assert m._count_consecutive_exceeded(readings, date.today()) == 2

    def test_resets_on_gap_day(self, tmp_path: Path) -> None:
        """A day with no reading breaks the consecutive streak."""
        m = self._monitor(tmp_path)
        readings = [
            {"date": (date.today() - timedelta(days=2)).isoformat(), "p95_tokens": 7000.0},
            # day -1 missing → gap
            {"date": date.today().isoformat(), "p95_tokens": 7500.0},
        ]
        assert m._count_consecutive_exceeded(readings, date.today()) == 1

    def test_resets_on_under_threshold_day(self, tmp_path: Path) -> None:
        """A day under threshold breaks the streak even with no gap."""
        m = self._monitor(tmp_path)
        readings = [
            {"date": (date.today() - timedelta(days=2)).isoformat(), "p95_tokens": 7000.0},
            {"date": (date.today() - timedelta(days=1)).isoformat(), "p95_tokens": 4000.0},
            {"date": date.today().isoformat(), "p95_tokens": 7500.0},
        ]
        assert m._count_consecutive_exceeded(readings, date.today()) == 1
