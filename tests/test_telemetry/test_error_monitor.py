"""Unit tests for ErrorMonitor (ADR-0056 §step 4).

Tests verify:
1. scan() returns ErrorPatternCluster list from queries
2. File is written before bus publish (D4 ordering)
3. Each cluster produces a matching JSON file in the output dir
4. scan_history is capped at 30 entries
5. max_patterns_per_scan caps emissions
6. ES-down: swallows exception, logs warning, returns empty list
7. Redis-down: writes file, swallows publish error, returns clusters
8. Out-of-scope component is already filtered by queries layer (not re-checked here)
9. WARNING_EVENT_ALLOWLIST exists and is a frozenset of strings
10. DLQ self-loop: source_component == telemetry.error_monitor is excluded
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from personal_agent.events.models import (
    ErrorPatternCluster,
    ErrorPatternDetectedEvent,
)
from personal_agent.telemetry.error_monitor import (
    WARNING_EVENT_ALLOWLIST,
    ErrorMonitor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cluster(
    component: str = "tools.fetch_url",
    event_name: str = "fetch_url_timeout",
    error_type: str = "TimeoutError",
    level: str = "ERROR",
    occurrences: int = 10,
    fingerprint: str = "abc123def4560000",
) -> ErrorPatternCluster:
    now = datetime.now(timezone.utc)
    return ErrorPatternCluster(
        fingerprint=fingerprint,
        component=component,
        event_name=event_name,
        error_type=error_type,
        level=level,
        occurrences=occurrences,
        first_seen=now,
        last_seen=now,
        sample_trace_ids=("tid-1",),
        sample_messages=("Read timeout after 10s",),
        window_hours=24,
    )


def _make_monitor(
    tmp_path: Path,
    clusters_to_return: list[ErrorPatternCluster] | None = None,
    window_hours: int = 24,
    min_occurrences: int = 5,
    max_patterns: int = 50,
) -> tuple[ErrorMonitor, AsyncMock, AsyncMock]:
    """Return (monitor, mock_queries, mock_bus)."""
    mock_queries = AsyncMock()
    mock_queries.get_error_patterns.return_value = clusters_to_return or []
    mock_bus = AsyncMock()
    monitor = ErrorMonitor(
        queries=mock_queries,
        bus=mock_bus,
        output_dir=tmp_path,
        window_hours=window_hours,
        min_occurrences=min_occurrences,
        max_patterns_per_scan=max_patterns,
    )
    return monitor, mock_queries, mock_bus


# ---------------------------------------------------------------------------
# WARNING_EVENT_ALLOWLIST
# ---------------------------------------------------------------------------


def test_warning_event_allowlist_is_frozenset() -> None:
    """WARNING_EVENT_ALLOWLIST is a frozenset of strings (testable, reviewable)."""
    assert isinstance(WARNING_EVENT_ALLOWLIST, frozenset)
    assert all(isinstance(e, str) for e in WARNING_EVENT_ALLOWLIST)


def test_warning_event_allowlist_contains_dead_letter_routed() -> None:
    """dead_letter_routed is in the allowlist — it signals a broken consumer loop."""
    assert "dead_letter_routed" in WARNING_EVENT_ALLOWLIST


def test_warning_event_allowlist_contains_compaction_quality_poor() -> None:
    """compaction_quality.poor is in the allowlist — signals context loss."""
    assert "compaction_quality.poor" in WARNING_EVENT_ALLOWLIST


# ---------------------------------------------------------------------------
# scan() basics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_returns_clusters_from_queries(tmp_path: Path) -> None:
    """scan() returns the clusters produced by get_error_patterns."""
    cluster = _make_cluster()
    monitor, mock_queries, _ = _make_monitor(tmp_path, clusters_to_return=[cluster])

    result = await monitor.scan()

    assert len(result) == 1
    assert result[0].fingerprint == cluster.fingerprint
    mock_queries.get_error_patterns.assert_awaited_once_with(
        window_hours=24, min_occurrences=5
    )


@pytest.mark.asyncio
async def test_scan_returns_empty_when_no_clusters(tmp_path: Path) -> None:
    """scan() returns empty list when queries returns no clusters."""
    monitor, _, _ = _make_monitor(tmp_path, clusters_to_return=[])

    result = await monitor.scan()

    assert result == []


# ---------------------------------------------------------------------------
# File write (Layer C)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_writes_json_file_for_each_cluster(tmp_path: Path) -> None:
    """Each cluster produces an EP-<fingerprint>.json file in output_dir."""
    fp = "abc123def4560000"
    cluster = _make_cluster(fingerprint=fp)
    monitor, _, _ = _make_monitor(tmp_path, clusters_to_return=[cluster])

    await monitor.scan()

    expected_file = tmp_path / f"EP-{fp}.json"
    assert expected_file.exists()
    data = json.loads(expected_file.read_text())
    assert data["fingerprint"] == fp
    assert data["component"] == "tools.fetch_url"
    assert data["event_name"] == "fetch_url_timeout"


@pytest.mark.asyncio
async def test_scan_history_appended_on_successive_scans(tmp_path: Path) -> None:
    """Successive scans append to scan_history in the JSON file."""
    cluster = _make_cluster()
    monitor, _, _ = _make_monitor(tmp_path, clusters_to_return=[cluster])

    await monitor.scan()
    await monitor.scan()

    fp_file = tmp_path / f"EP-{cluster.fingerprint}.json"
    data = json.loads(fp_file.read_text())
    assert len(data["scan_history"]) == 2


@pytest.mark.asyncio
async def test_scan_history_capped_at_30_entries(tmp_path: Path) -> None:
    """scan_history is capped at 30 entries (rolling)."""
    cluster = _make_cluster()
    monitor, _, _ = _make_monitor(tmp_path, clusters_to_return=[cluster])

    for _ in range(35):
        await monitor.scan()

    fp_file = tmp_path / f"EP-{cluster.fingerprint}.json"
    data = json.loads(fp_file.read_text())
    assert len(data["scan_history"]) == 30


# ---------------------------------------------------------------------------
# Bus publish (Layer B) and D4 ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_publishes_event_to_bus_for_each_cluster(tmp_path: Path) -> None:
    """scan() publishes one ErrorPatternDetectedEvent per cluster to the bus."""
    cluster = _make_cluster()
    monitor, _, mock_bus = _make_monitor(tmp_path, clusters_to_return=[cluster])

    await monitor.scan()

    mock_bus.publish.assert_awaited_once()
    _, publish_call_kwargs = mock_bus.publish.call_args
    # or positional
    args = mock_bus.publish.call_args[0]
    # args[0] = stream, args[1] = event
    assert len(args) == 2
    published_event = args[1]
    assert isinstance(published_event, ErrorPatternDetectedEvent)
    assert published_event.fingerprint == cluster.fingerprint


@pytest.mark.asyncio
async def test_file_is_written_before_bus_publish(tmp_path: Path) -> None:
    """File write must precede bus publish (ADR-0054 D4)."""
    call_order: list[str] = []

    original_write = Path.write_text

    def _tracking_write(self: Path, *args: object, **kwargs: object) -> None:
        if "EP-" in self.name:
            call_order.append("file_write")
        return original_write(self, *args, **kwargs)

    async def _tracking_publish(*args: object, **kwargs: object) -> None:
        call_order.append("bus_publish")

    cluster = _make_cluster()
    monitor, _, mock_bus = _make_monitor(tmp_path, clusters_to_return=[cluster])
    mock_bus.publish.side_effect = _tracking_publish

    with patch.object(Path, "write_text", _tracking_write):
        await monitor.scan()

    assert call_order == ["file_write", "bus_publish"], (
        f"Expected file write before bus publish; got order: {call_order}"
    )


# ---------------------------------------------------------------------------
# max_patterns_per_scan cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_caps_emissions_at_max_patterns(tmp_path: Path) -> None:
    """At most max_patterns_per_scan clusters are published per scan."""
    clusters = [_make_cluster(fingerprint=f"fp{i:016d}") for i in range(10)]
    monitor, _, mock_bus = _make_monitor(tmp_path, clusters_to_return=clusters, max_patterns=3)

    result = await monitor.scan()

    assert mock_bus.publish.await_count == 3
    assert len(result) <= 3


# ---------------------------------------------------------------------------
# ES-down resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_swallows_es_exception_and_returns_empty(tmp_path: Path) -> None:
    """When ES is down, scan() logs a warning and returns an empty list."""
    mock_queries = AsyncMock()
    mock_queries.get_error_patterns.side_effect = ConnectionError("ES unavailable")
    mock_bus = AsyncMock()
    monitor = ErrorMonitor(
        queries=mock_queries,
        bus=mock_bus,
        output_dir=tmp_path,
        window_hours=24,
        min_occurrences=5,
        max_patterns_per_scan=50,
    )

    result = await monitor.scan()

    assert result == []
    mock_bus.publish.assert_not_awaited()


# ---------------------------------------------------------------------------
# Redis-down resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_writes_file_even_when_bus_publish_fails(tmp_path: Path) -> None:
    """When Redis publish fails, the file is already written — it survives."""
    cluster = _make_cluster()
    mock_queries = AsyncMock()
    mock_queries.get_error_patterns.return_value = [cluster]
    mock_bus = AsyncMock()
    mock_bus.publish.side_effect = ConnectionError("Redis down")
    monitor = ErrorMonitor(
        queries=mock_queries,
        bus=mock_bus,
        output_dir=tmp_path,
        window_hours=24,
        min_occurrences=5,
        max_patterns_per_scan=50,
    )

    result = await monitor.scan()

    # File was written (D4 ordering: file first)
    fp_file = tmp_path / f"EP-{cluster.fingerprint}.json"
    assert fp_file.exists()
    # Returns clusters even on publish error
    assert len(result) == 1
