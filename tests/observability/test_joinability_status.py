"""Unit tests for :mod:`personal_agent.observability.joinability.status`."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.observability.joinability.status import (
    DayBucket,
    SevenDayGate,
    _decide,
    _parse_buckets,
    compute_seven_day_gate,
    render_table,
)

NOW = datetime(2026, 5, 30, 14, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# _parse_buckets — translation of ES date_histogram → DayBucket list
# ---------------------------------------------------------------------------


def _response(buckets: list[dict[str, Any]]) -> dict[str, Any]:
    return {"aggregations": {"by_day": {"buckets": buckets}}}


def _bucket(day: str, *, red: int = 0, yellow: int = 0, green: int = 0) -> dict[str, Any]:
    outcomes = []
    if red:
        outcomes.append({"key": "red", "doc_count": red})
    if yellow:
        outcomes.append({"key": "yellow", "doc_count": yellow})
    if green:
        outcomes.append({"key": "green", "doc_count": green})
    return {
        "key_as_string": f"{day}T00:00:00.000Z",
        "by_outcome": {"buckets": outcomes},
    }


def test_parse_buckets_green_only() -> None:
    resp = _response([_bucket("2026-05-24", green=15)])
    [bucket] = _parse_buckets(resp)
    assert bucket == DayBucket(date(2026, 5, 24), runs=15, worst_outcome="green")


def test_parse_buckets_red_dominates() -> None:
    resp = _response([_bucket("2026-05-25", green=20, yellow=2, red=1)])
    [bucket] = _parse_buckets(resp)
    assert bucket.worst_outcome == "red"
    assert bucket.runs == 23


def test_parse_buckets_yellow_when_no_red() -> None:
    resp = _response([_bucket("2026-05-26", green=20, yellow=3)])
    [bucket] = _parse_buckets(resp)
    assert bucket.worst_outcome == "yellow"


# ---------------------------------------------------------------------------
# _decide — pure decision logic
# ---------------------------------------------------------------------------


def _seven_green(per_day_runs: int = 20) -> list[DayBucket]:
    return [
        DayBucket(date(2026, 5, 23) + timedelta(days=i), runs=per_day_runs, worst_outcome="green")
        for i in range(7)
    ]


def test_decide_green_when_all_seven_green_with_runs() -> None:
    gate = _decide(_seven_green(), min_runs_per_day=12, window_days=7)
    assert gate.status == "green"
    assert gate.reason is None


def test_decide_red_dominates() -> None:
    buckets = _seven_green()
    buckets[3] = DayBucket(date(2026, 5, 26), runs=20, worst_outcome="red")
    gate = _decide(buckets, min_runs_per_day=12, window_days=7)
    assert gate.status == "red"
    assert "red_on_2026-05-26" == gate.reason


def test_decide_yellow_drops_when_yellow_seen() -> None:
    buckets = _seven_green()
    buckets[2] = DayBucket(date(2026, 5, 25), runs=20, worst_outcome="yellow")
    gate = _decide(buckets, min_runs_per_day=12, window_days=7)
    assert gate.status == "yellow"
    assert gate.reason is not None and "yellow_on_2026-05-25" in gate.reason


def test_decide_yellow_when_runs_below_floor() -> None:
    buckets = _seven_green(per_day_runs=20)
    buckets[5] = DayBucket(date(2026, 5, 28), runs=5, worst_outcome="green")
    gate = _decide(buckets, min_runs_per_day=12, window_days=7)
    assert gate.status == "yellow"
    assert "low_runs_on_2026-05-28" in (gate.reason or "")


def test_decide_yellow_when_insufficient_history() -> None:
    gate = _decide(_seven_green()[:5], min_runs_per_day=12, window_days=7)
    assert gate.status == "yellow"
    assert "insufficient_history" in (gate.reason or "")


# ---------------------------------------------------------------------------
# compute_seven_day_gate — integration of parse + decide with mocked ES
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_returns_green_with_seven_clean_buckets() -> None:
    es = MagicMock()
    es.search = AsyncMock(
        return_value=_response(
            [
                _bucket((date(2026, 5, 23) + timedelta(days=i)).isoformat(), green=18)
                for i in range(7)
            ]
        )
    )
    gate = await compute_seven_day_gate(es, prefix="agent-monitors-joinability", now=NOW)
    assert gate.status == "green"


@pytest.mark.asyncio
async def test_compute_returns_red_when_any_red_day() -> None:
    es = MagicMock()
    es.search = AsyncMock(
        return_value=_response(
            [
                _bucket((date(2026, 5, 23) + timedelta(days=i)).isoformat(), green=18)
                for i in range(6)
            ]
            + [_bucket("2026-05-29", green=15, red=2)]
        )
    )
    gate = await compute_seven_day_gate(es, prefix="agent-monitors-joinability", now=NOW)
    assert gate.status == "red"


# ---------------------------------------------------------------------------
# render_table — humans read the output
# ---------------------------------------------------------------------------


def test_render_table_green() -> None:
    gate = SevenDayGate(status="green", reason=None, buckets=_seven_green(), min_runs_per_day=12)
    out = render_table(gate)
    assert "2026-05-23  green" in out
    assert "STATUS: GREEN" in out


def test_render_table_red() -> None:
    buckets = _seven_green()
    buckets[3] = DayBucket(date(2026, 5, 26), runs=20, worst_outcome="red")
    gate = SevenDayGate(
        status="red",
        reason="red_on_2026-05-26",
        buckets=buckets,
        min_runs_per_day=12,
    )
    out = render_table(gate)
    assert "2026-05-26  red" in out
    assert "STATUS: RED — red_on_2026-05-26" in out
