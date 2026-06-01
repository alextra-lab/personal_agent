"""Unit tests for the cache-erosion monitor (FRE-406 P2)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.observability.cache_erosion.monitor import (
    EROSION_THRESHOLD,
    CallsiteResult,
    ErosionReport,
    _jaccard,
    _parse_day_hashes,
    compute_erosion_report,
    render_report,
)

# ---------------------------------------------------------------------------
# _jaccard
# ---------------------------------------------------------------------------


class TestJaccard:
    """Tests for Jaccard similarity helper."""

    def test_identical_sets(self) -> None:
        a = frozenset({"a", "b", "c"})
        assert _jaccard(a, a) == 1.0

    def test_disjoint_sets(self) -> None:
        assert _jaccard(frozenset({"a"}), frozenset({"b"})) == 0.0

    def test_partial_overlap(self) -> None:
        a = frozenset({"x", "y"})
        b = frozenset({"y", "z"})
        assert _jaccard(a, b) == pytest.approx(1 / 3)

    def test_both_empty(self) -> None:
        assert _jaccard(frozenset(), frozenset()) == 1.0

    def test_one_empty(self) -> None:
        assert _jaccard(frozenset({"a"}), frozenset()) == 0.0


# ---------------------------------------------------------------------------
# _parse_day_hashes
# ---------------------------------------------------------------------------


class TestParseDayHashes:
    """Tests for the ES aggregation parser."""

    def test_parses_callsite_and_day_buckets(self) -> None:
        response = {
            "aggregations": {
                "by_callsite": {
                    "buckets": [
                        {
                            "key": "orchestrator.primary",
                            "doc_count": 10,
                            "by_day": {
                                "buckets": [
                                    {
                                        "key_as_string": "2026-05-30T00:00:00.000Z",
                                        "doc_count": 5,
                                        "hashes": {
                                            "buckets": [
                                                {"key": "aaa", "doc_count": 3},
                                                {"key": "bbb", "doc_count": 2},
                                            ]
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                }
            }
        }
        result = _parse_day_hashes(response)
        assert len(result) == 1
        dh = result[0]
        assert dh.callsite == "orchestrator.primary"
        assert dh.day == date(2026, 5, 30)
        assert dh.hashes == frozenset({"aaa", "bbb"})
        assert dh.call_count == 5

    def test_empty_aggregation_returns_empty(self) -> None:
        assert _parse_day_hashes({}) == []


# ---------------------------------------------------------------------------
# compute_erosion_report (integration with mocked ES)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stable_when_hashes_unchanged() -> None:
    """Same hashes on both days → stable."""
    es = _build_mock_es(
        callsite="orchestrator.primary",
        days=[
            ("2026-05-29T00:00:00.000Z", ["hash_a", "hash_b"]),
            ("2026-05-30T00:00:00.000Z", ["hash_a", "hash_b"]),
        ],
    )
    report = await compute_erosion_report(
        es,
        callsites=("orchestrator.primary",),
        logs_prefix="agent-logs",
    )
    assert not report.any_eroded
    assert report.results[0].status == "stable"
    assert report.results[0].jaccard == 1.0


@pytest.mark.asyncio
async def test_eroded_when_hashes_diverge() -> None:
    """Completely different hashes on consecutive days → eroded."""
    es = _build_mock_es(
        callsite="orchestrator.primary",
        days=[
            ("2026-05-29T00:00:00.000Z", ["hash_a"]),
            ("2026-05-30T00:00:00.000Z", ["hash_b"]),
        ],
    )
    report = await compute_erosion_report(
        es,
        callsites=("orchestrator.primary",),
        logs_prefix="agent-logs",
    )
    assert report.any_eroded
    assert report.results[0].status == "eroded"
    assert report.results[0].jaccard == 0.0


@pytest.mark.asyncio
async def test_insufficient_data_when_only_one_day() -> None:
    """Only one day of data → insufficient_data (not eroded)."""
    es = _build_mock_es(
        callsite="orchestrator.primary",
        days=[
            ("2026-05-30T00:00:00.000Z", ["hash_a"]),
        ],
    )
    report = await compute_erosion_report(
        es,
        callsites=("orchestrator.primary",),
        logs_prefix="agent-logs",
    )
    assert not report.any_eroded
    assert report.results[0].status == "insufficient_data"


@pytest.mark.asyncio
async def test_partial_overlap_below_threshold_is_eroded() -> None:
    """Jaccard 0.5 < 0.9 threshold → eroded."""
    es = _build_mock_es(
        callsite="orchestrator.primary",
        days=[
            ("2026-05-29T00:00:00.000Z", ["a", "b"]),
            ("2026-05-30T00:00:00.000Z", ["a", "c"]),
        ],
    )
    report = await compute_erosion_report(
        es,
        callsites=("orchestrator.primary",),
        logs_prefix="agent-logs",
        threshold=0.9,
    )
    assert report.any_eroded
    assert report.results[0].jaccard == pytest.approx(1 / 3)


# ---------------------------------------------------------------------------
# compute_erosion_report — hours-ago window mode (ADR-0081 D4)
# ---------------------------------------------------------------------------


def _build_mock_es_window(callsite: str, hashes: list[str], doc_count: int) -> MagicMock:
    """Mock ES for the sub-day window agg (no by_day bucketing)."""
    es = MagicMock()
    es.search = AsyncMock(
        return_value={
            "aggregations": {
                "by_callsite": {
                    "buckets": [
                        {
                            "key": callsite,
                            "doc_count": doc_count,
                            "hashes": {"buckets": [{"key": h, "doc_count": 1} for h in hashes]},
                        }
                    ]
                }
            }
        }
    )
    return es


@pytest.mark.asyncio
async def test_window_single_hash_is_stable() -> None:
    """Exactly 1 distinct hash in the window → stable (the D4 gate)."""
    es = _build_mock_es_window("orchestrator.primary", ["hash_a"], doc_count=23)
    report = await compute_erosion_report(
        es,
        callsites=("orchestrator.primary",),
        logs_prefix="agent-logs",
        hours_ago=6,
    )
    assert not report.any_eroded
    assert report.results[0].status == "stable"
    assert report.results[0].jaccard == 1.0


@pytest.mark.asyncio
async def test_window_multiple_hashes_is_eroded() -> None:
    """6 distinct hashes across the window → eroded, score 1/6 (post-D1 baseline)."""
    es = _build_mock_es_window(
        "orchestrator.primary",
        [f"hash_{i}" for i in range(6)],
        doc_count=23,
    )
    report = await compute_erosion_report(
        es,
        callsites=("orchestrator.primary",),
        logs_prefix="agent-logs",
        hours_ago=6,
    )
    assert report.any_eroded
    assert report.results[0].status == "eroded"
    assert report.results[0].jaccard == pytest.approx(1 / 6)


@pytest.mark.asyncio
async def test_window_no_calls_is_insufficient_data() -> None:
    """No calls in the window → insufficient_data (not eroded)."""
    es = _build_mock_es_window("orchestrator.primary", [], doc_count=0)
    report = await compute_erosion_report(
        es,
        callsites=("orchestrator.primary",),
        logs_prefix="agent-logs",
        hours_ago=6,
    )
    assert not report.any_eroded
    assert report.results[0].status == "insufficient_data"


# ---------------------------------------------------------------------------
# render_report
# ---------------------------------------------------------------------------


def test_render_stable() -> None:
    report = _make_report(status="stable", jaccard=1.0)
    text = render_report(report)
    assert "STABLE" in text
    assert "STATUS: GREEN" in text


def test_render_eroded() -> None:
    report = _make_report(status="eroded", jaccard=0.0)
    text = render_report(report)
    assert "ERODED" in text
    assert "STATUS: RED" in text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_mock_es(callsite: str, days: list[tuple[str, list[str]]]) -> MagicMock:
    day_buckets = [
        {
            "key_as_string": ts,
            "doc_count": len(hashes),
            "hashes": {"buckets": [{"key": h, "doc_count": 1} for h in hashes]},
        }
        for ts, hashes in days
    ]
    es = MagicMock()
    es.search = AsyncMock(
        return_value={
            "aggregations": {
                "by_callsite": {
                    "buckets": [
                        {
                            "key": callsite,
                            "doc_count": sum(len(h) for _, h in days),
                            "by_day": {"buckets": day_buckets},
                        }
                    ]
                }
            }
        }
    )
    return es


def _make_report(
    status: str,
    jaccard: float,
) -> ErosionReport:
    result = CallsiteResult(
        callsite="orchestrator.primary",
        day_a=date(2026, 5, 29),
        day_b=date(2026, 5, 30),
        hashes_a=frozenset({"a"}),
        hashes_b=frozenset({"b"}) if status == "eroded" else frozenset({"a"}),
        jaccard=jaccard,
        status=status,  # type: ignore[arg-type]
        threshold=EROSION_THRESHOLD,
    )
    return ErosionReport(
        computed_at=datetime(2026, 5, 31, tzinfo=timezone.utc),
        results=[result],
        any_eroded=(status == "eroded"),
        threshold=EROSION_THRESHOLD,
    )
