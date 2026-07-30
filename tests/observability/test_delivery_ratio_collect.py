"""Unit tests for delivery-ratio live collection (FRE-1051).

Substrate is mocked; these assert the *contract* the collector holds — that the oracle
count drives the verdict, that declared-but-unwired families are reported rather than
omitted, and that the index prefix comes from settings instead of a hardcoded string
(FRE-375).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

import pytest

from personal_agent.observability.delivery_ratio.collect import (
    UNWIRED_FAMILIES,
    collect_report,
    field_is_mapped,
)


class _FakePool:
    """Minimal asyncpg stand-in exposing ``fetchval``."""

    def __init__(self, value: int) -> None:
        self.value = value
        self.calls: list[tuple] = []

    async def fetchval(self, sql: str, *args: object) -> int:
        self.calls.append((sql, *args))
        return self.value


def _es_mock(*, counts: dict[str, int]) -> AsyncMock:
    """Build an ES mock whose ``count`` answers per event family."""
    es = AsyncMock()

    async def _count(*, index: str, query: dict) -> dict[str, int]:
        family = query["bool"]["must"][0]["term"]["event_type"]
        return {"count": counts.get(family, 0)}

    es.count = AsyncMock(side_effect=_count)
    return es


class TestCollectReport:
    """End-to-end collection against mocked substrate."""

    @pytest.mark.asyncio
    async def test_full_delivery_passes(self) -> None:
        es = _es_mock(counts={"api_cost_recorded": 103})
        report = await collect_report(
            es,
            _FakePool(103),
            logs_prefix="agent-logs",
            since=date(2026, 7, 24),
            until=date(2026, 7, 24),
        )
        assert report.status == "pass"
        assert report.exit_code == 0

    @pytest.mark.asyncio
    async def test_reproduces_the_measured_2026_07_23_breach(self) -> None:
        """144 ledger rows against 25 documents must come back as a breach of 119."""
        es = _es_mock(counts={"api_cost_recorded": 25})
        report = await collect_report(
            es,
            _FakePool(144),
            logs_prefix="agent-logs",
            since=date(2026, 7, 23),
            until=date(2026, 7, 23),
        )
        assert report.status == "breach"
        cost = next(f for f in report.families if f.family == "api_cost_recorded")
        assert cost.lost == 119
        assert cost.ratio == pytest.approx(25 / 144)

    @pytest.mark.asyncio
    async def test_unwired_families_are_reported_not_omitted(self) -> None:
        """Silent omission would let the report imply coverage it does not have."""
        es = _es_mock(counts={"api_cost_recorded": 103})
        report = await collect_report(
            es,
            _FakePool(103),
            logs_prefix="agent-logs",
            since=date(2026, 7, 24),
            until=date(2026, 7, 24),
        )
        reported = {f.family for f in report.families}
        for unwired in UNWIRED_FAMILIES:
            assert unwired.family in reported
            entry = next(f for f in report.families if f.family == unwired.family)
            assert entry.status == "unverifiable"

    @pytest.mark.asyncio
    async def test_index_prefix_comes_from_the_caller_not_a_literal(self) -> None:
        """FRE-375: substrate targets are configured, never hardcoded."""
        es = _es_mock(counts={"api_cost_recorded": 1})
        await collect_report(
            es,
            _FakePool(1),
            logs_prefix="test-logs",
            since=date(2026, 7, 24),
            until=date(2026, 7, 24),
        )
        for call in es.count.await_args_list:
            assert call.kwargs["index"] == "test-logs-*"

    @pytest.mark.asyncio
    async def test_oracle_bounds_are_tz_aware_utc_datetimes(self) -> None:
        """Regression: ISO strings raised DataError, and a bare literal on a
        ``timestamptz`` column would be read in the session timezone and shift the
        window off the UTC day the Elasticsearch side counts.
        """
        pool = _FakePool(1)
        await collect_report(
            _es_mock(counts={"api_cost_recorded": 1}),
            pool,
            logs_prefix="agent-logs",
            since=date(2026, 7, 23),
            until=date(2026, 7, 23),
        )
        _sql, lower, upper = pool.calls[0]
        assert lower == datetime(2026, 7, 23, tzinfo=timezone.utc)
        assert upper == datetime(2026, 7, 24, tzinfo=timezone.utc)

    @pytest.mark.asyncio
    async def test_window_is_inclusive_of_the_until_day(self) -> None:
        """A window of one day must cover that whole day, not stop at midnight."""
        es = _es_mock(counts={"api_cost_recorded": 1})
        await collect_report(
            es,
            _FakePool(1),
            logs_prefix="agent-logs",
            since=date(2026, 7, 23),
            until=date(2026, 7, 23),
        )
        rng = es.count.await_args_list[0].kwargs["query"]["bool"]["must"][1]["range"]["@timestamp"]
        assert rng["gte"] == "2026-07-23"
        assert rng["lt"] == "2026-07-24"


class TestFieldIsMapped:
    """Mapping presence separates 'wrong field name' from 'emitted and lost'."""

    @pytest.mark.asyncio
    async def test_true_when_field_caps_reports_the_field(self) -> None:
        es = AsyncMock()
        es.field_caps = AsyncMock(return_value={"fields": {"trace_id": {"keyword": {}}}})
        assert await field_is_mapped(es, logs_prefix="agent-logs", field_name="trace_id") is True

    @pytest.mark.asyncio
    async def test_false_when_field_absent(self) -> None:
        es = AsyncMock()
        es.field_caps = AsyncMock(return_value={"fields": {}})
        assert await field_is_mapped(es, logs_prefix="agent-logs", field_name="nope") is False
