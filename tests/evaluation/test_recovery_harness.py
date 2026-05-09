"""Unit tests for recovery_harness.py — FRE-332 (ES polling) and FRE-333 (pagination).

These tests mock the Elasticsearch client and do not require a running ES instance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from scripts.eval.recovery_harness import (
    _TERMINAL_EVENTS,
    _wait_for_trace_complete,
    fetch_trace_logs,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hit(event_type: str, sort_values: list[object] | None = None) -> dict:
    """Build a minimal ES hit dict."""
    hit: dict = {"_source": {"event_type": event_type, "trace_id": "t-123"}}
    if sort_values is not None:
        hit["sort"] = sort_values
    return hit


def _make_response(hits: list[dict], total: int | None = None) -> dict:
    """Wrap hits in the ES response envelope."""
    if total is None:
        total = len(hits)
    return {"hits": {"total": {"value": total}, "hits": hits}}


def _make_mock_queries(mock_client: AsyncMock) -> MagicMock:
    """Return a MagicMock TelemetryQueries whose _get_client returns mock_client."""
    queries = MagicMock()
    queries._get_client = AsyncMock(return_value=mock_client)
    return queries


# ---------------------------------------------------------------------------
# FRE-332: _wait_for_trace_complete — terminal event detection
# ---------------------------------------------------------------------------


class TestWaitForTraceComplete:
    """Tests for _wait_for_trace_complete() polling logic."""

    @pytest.mark.asyncio
    async def test_returns_on_terminal_event_reply_ready(self) -> None:
        """Polling stops immediately when reply_ready appears in ES results."""
        mock_client = AsyncMock()
        # First poll: no events; second poll: terminal event
        mock_client.search.side_effect = [
            _make_response([_make_hit("request_received")]),
            _make_response([
                _make_hit("request_received"),
                _make_hit("reply_ready"),
            ]),
        ]
        queries = _make_mock_queries(mock_client)
        started_at = datetime.now(timezone.utc)

        # poll_interval=0 so the test doesn't sleep
        await _wait_for_trace_complete(
            queries, "t-123", started_at, poll_interval=0.0, hard_timeout=10.0
        )

        assert mock_client.search.call_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal_event", sorted(_TERMINAL_EVENTS))
    async def test_all_terminal_events_trigger_return(self, terminal_event: str) -> None:
        """Each terminal event in _TERMINAL_EVENTS causes polling to stop."""
        mock_client = AsyncMock()
        mock_client.search.return_value = _make_response([_make_hit(terminal_event)])
        queries = _make_mock_queries(mock_client)

        await _wait_for_trace_complete(
            queries, "t-123", datetime.now(timezone.utc),
            poll_interval=0.0, hard_timeout=10.0,
        )

        assert mock_client.search.call_count == 1

    @pytest.mark.asyncio
    async def test_returns_on_count_stability(self) -> None:
        """Polling stops when event count is unchanged across two consecutive polls."""
        mock_client = AsyncMock()
        # Count grows on poll 1, then stabilises at 3 on polls 2 and 3.
        mock_client.search.side_effect = [
            _make_response([_make_hit("some_event")]),          # count=1
            _make_response([_make_hit("a"), _make_hit("b"), _make_hit("c")]),  # count=3
            _make_response([_make_hit("a"), _make_hit("b"), _make_hit("c")]),  # count=3 stable #1
            _make_response([_make_hit("a"), _make_hit("b"), _make_hit("c")]),  # count=3 stable #2
        ]
        queries = _make_mock_queries(mock_client)

        await _wait_for_trace_complete(
            queries, "t-123", datetime.now(timezone.utc),
            poll_interval=0.0, hard_timeout=10.0,
        )

        # Should stop after the 4th call (two consecutive stable polls at count=3).
        assert mock_client.search.call_count == 4

    @pytest.mark.asyncio
    async def test_logs_timeout_and_returns(self) -> None:
        """When hard_timeout elapses, harness_es_wait_timeout is logged and function returns."""
        mock_client = AsyncMock()
        # Always return 0 events so neither terminal nor stability triggers.
        mock_client.search.return_value = _make_response([])
        queries = _make_mock_queries(mock_client)

        with patch("scripts.eval.recovery_harness.log") as mock_log:
            await _wait_for_trace_complete(
                queries, "t-123", datetime.now(timezone.utc),
                poll_interval=0.0, hard_timeout=0.0,  # immediate timeout
            )
            mock_log.warning.assert_called_once()
            event_name = mock_log.warning.call_args[0][0]
            assert event_name == "harness_es_wait_timeout"

    @pytest.mark.asyncio
    async def test_zero_count_does_not_trigger_stability(self) -> None:
        """Count=0 across polls must NOT count as stable; wait for actual events."""
        mock_client = AsyncMock()
        # Three empty polls, then a terminal event.
        mock_client.search.side_effect = [
            _make_response([]),
            _make_response([]),
            _make_response([_make_hit("reply_ready")]),
        ]
        queries = _make_mock_queries(mock_client)

        await _wait_for_trace_complete(
            queries, "t-123", datetime.now(timezone.utc),
            poll_interval=0.0, hard_timeout=10.0,
        )

        # Stability at count=0 must not fire; terminal event on poll 3 stops it.
        assert mock_client.search.call_count == 3


# ---------------------------------------------------------------------------
# FRE-333: fetch_trace_logs — search_after pagination
# ---------------------------------------------------------------------------


def _make_page(
    count: int,
    start_idx: int = 0,
    include_sort: bool = True,
) -> dict:
    """Build an ES response page with ``count`` hits and synthetic sort keys."""
    hits = []
    for i in range(count):
        idx = start_idx + i
        hit: dict = {
            "_source": {"event_type": f"event_{idx}", "trace_id": "t-456"},
        }
        if include_sort:
            hit["sort"] = [f"2026-01-01T00:00:{idx:02d}.000Z", f"id-{idx}"]
        hits.append(hit)
    return _make_response(hits)


class TestFetchTraceLogs:
    """Tests for paginated fetch_trace_logs()."""

    @pytest.mark.asyncio
    async def test_fetches_1200_events_across_three_pages(self) -> None:
        """1,200 total events across 3 pages are all returned (FRE-333 acceptance criterion)."""
        mock_client = AsyncMock()
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        until = datetime(2026, 1, 2, tzinfo=timezone.utc)

        # page_size=500 default: pages of 500, 500, 200
        mock_client.search.side_effect = [
            _make_page(500, start_idx=0),     # full page → pagination continues
            _make_page(500, start_idx=500),   # full page → pagination continues
            _make_page(200, start_idx=1000),  # partial page → stop
        ]
        queries = _make_mock_queries(mock_client)

        with patch("scripts.eval.recovery_harness.get_settings") as mock_settings:
            mock_settings.return_value.elasticsearch_index_prefix = "agent-logs"
            result = await fetch_trace_logs(queries, "t-456", since, until)

        assert len(result) == 1200
        assert mock_client.search.call_count == 3

    @pytest.mark.asyncio
    async def test_search_after_key_passed_on_subsequent_pages(self) -> None:
        """The sort key from the last hit of page N is forwarded as search_after on page N+1."""
        mock_client = AsyncMock()
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        until = datetime(2026, 1, 2, tzinfo=timezone.utc)

        sort_key_page1 = ["2026-01-01T00:00:00.000Z", "id-499"]
        page1_hits = _make_page(500, start_idx=0)
        page1_hits["hits"]["hits"][-1]["sort"] = sort_key_page1
        page2 = _make_page(1, start_idx=500)  # last page

        mock_client.search.side_effect = [page1_hits, page2]
        queries = _make_mock_queries(mock_client)

        with patch("scripts.eval.recovery_harness.get_settings") as mock_settings:
            mock_settings.return_value.elasticsearch_index_prefix = "agent-logs"
            result = await fetch_trace_logs(queries, "t-456", since, until, page_size=500)

        assert len(result) == 501
        # Second call must include search_after with the sort key from page 1.
        second_call_kwargs = mock_client.search.call_args_list[1][1]
        assert second_call_kwargs.get("search_after") == sort_key_page1

    @pytest.mark.asyncio
    async def test_hard_cap_stops_pagination_and_logs_warning(self) -> None:
        """Pagination stops at hard_cap events and emits harness_es_pagination_capped."""
        mock_client = AsyncMock()
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        until = datetime(2026, 1, 2, tzinfo=timezone.utc)

        # With hard_cap=5 and page_size=3: page 1 returns 3 (total=3, below cap),
        # page 2 returns 3 more (total=6, exceeds cap=5) → stop.
        mock_client.search.side_effect = [
            _make_page(3, start_idx=0),
            _make_page(3, start_idx=3),
        ]
        queries = _make_mock_queries(mock_client)

        with (
            patch("scripts.eval.recovery_harness.get_settings") as mock_settings,
            patch("scripts.eval.recovery_harness.log") as mock_log,
        ):
            mock_settings.return_value.elasticsearch_index_prefix = "agent-logs"
            result = await fetch_trace_logs(
                queries, "t-456", since, until, page_size=3, hard_cap=5
            )

        assert len(result) == 6  # all fetched docs returned before stopping
        mock_log.warning.assert_called_once()
        assert mock_log.warning.call_args[0][0] == "harness_es_pagination_capped"

    @pytest.mark.asyncio
    async def test_empty_first_page_returns_empty_list(self) -> None:
        """No hits on the first page → empty list returned, no further pages fetched."""
        mock_client = AsyncMock()
        mock_client.search.return_value = _make_response([])
        queries = _make_mock_queries(mock_client)

        with patch("scripts.eval.recovery_harness.get_settings") as mock_settings:
            mock_settings.return_value.elasticsearch_index_prefix = "agent-logs"
            result = await fetch_trace_logs(
                queries, "t-456",
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 1, 2, tzinfo=timezone.utc),
            )

        assert result == []
        assert mock_client.search.call_count == 1

    @pytest.mark.asyncio
    async def test_partial_first_page_returns_without_further_pages(self) -> None:
        """A page with fewer hits than page_size signals last page; no next call."""
        mock_client = AsyncMock()
        mock_client.search.return_value = _make_page(42, start_idx=0)
        queries = _make_mock_queries(mock_client)

        with patch("scripts.eval.recovery_harness.get_settings") as mock_settings:
            mock_settings.return_value.elasticsearch_index_prefix = "agent-logs"
            result = await fetch_trace_logs(
                queries, "t-456",
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 1, 2, tzinfo=timezone.utc),
                page_size=500,
            )

        assert len(result) == 42
        assert mock_client.search.call_count == 1
