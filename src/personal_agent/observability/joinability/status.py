"""7-day green-gate computation for the joinability probe (ADR-0074 Phase 5).

Aggregates probe results from ``agent-monitors-joinability-*`` indices and
decides whether ADR-0074 has met its acceptance bar: zero orphans across
seven consecutive days of probe runs with at least 12 runs per day.

The :func:`compute_seven_day_gate` function returns a :class:`SevenDayGate`
that the CLI / Make target / Kibana dashboard surface to a human reviewer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch

log = get_logger(__name__)

DEFAULT_MIN_RUNS_PER_DAY = 12
DEFAULT_WINDOW_DAYS = 7


@dataclass(frozen=True)
class DayBucket:
    """One day's worth of probe runs reduced to a single verdict.

    Attributes:
        day: UTC date.
        runs: Number of non-skipped probe runs that day.
        worst_outcome: ``"green"`` / ``"yellow"`` / ``"red"`` — worst observed
            outcome across all non-skipped runs that day.
    """

    day: date
    runs: int
    worst_outcome: Literal["green", "yellow", "red"]


@dataclass(frozen=True)
class SevenDayGate:
    """Outcome of the 7-day green-gate computation.

    Attributes:
        status: ``"green"`` only when every required predicate holds.
        reason: Short, machine-readable reason when status is not green.
        buckets: Per-day breakdown for display.
        min_runs_per_day: Threshold used (echoed for the dashboard).
    """

    status: Literal["green", "yellow", "red"]
    reason: str | None
    buckets: list[DayBucket]
    min_runs_per_day: int


async def compute_seven_day_gate(
    es: "AsyncElasticsearch",
    *,
    prefix: str,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_runs_per_day: int = DEFAULT_MIN_RUNS_PER_DAY,
) -> SevenDayGate:
    """Compute the 7-day green-gate verdict from probe results in ES.

    Args:
        es: Connected AsyncElasticsearch client.
        prefix: Index prefix (e.g. ``agent-monitors-joinability``).
        now: Override for the current time (testing).
        window_days: Lookback window. Defaults to 7.
        min_runs_per_day: Minimum non-skipped runs required per day.

    Returns:
        A :class:`SevenDayGate` summarising whether the bar is met.
    """
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=window_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    response = await es.search(
        index=f"{prefix}-*",
        size=0,
        query={
            "bool": {
                "filter": [
                    {"range": {"started_at": {"gte": start.isoformat()}}},
                    # Skipped runs are excluded from the bucket aggregation —
                    # they neither prove green nor count toward the floor.
                    {"bool": {"must_not": [{"term": {"outcome": "skipped"}}]}},
                ]
            }
        },
        aggs={
            "by_day": {
                "date_histogram": {
                    "field": "started_at",
                    "calendar_interval": "1d",
                    "min_doc_count": 0,
                    "extended_bounds": {
                        "min": start.isoformat(),
                        "max": now.isoformat(),
                    },
                },
                "aggs": {
                    "by_outcome": {"terms": {"field": "outcome", "size": 4}},
                },
            }
        },
        ignore_unavailable=True,
        allow_no_indices=True,
    )

    buckets = _parse_buckets(dict(response))
    return _decide(buckets, min_runs_per_day=min_runs_per_day, window_days=window_days)


# ---------------------------------------------------------------------------
# Parsing & decision logic — pure functions so they're trivially testable.
# ---------------------------------------------------------------------------


def _parse_buckets(response: dict[str, Any]) -> list[DayBucket]:
    raw = response.get("aggregations", {}).get("by_day", {}).get("buckets", [])
    buckets: list[DayBucket] = []
    for entry in raw:
        day_value = entry.get("key_as_string") or entry.get("key")
        if isinstance(day_value, int):
            # ES returns ms since epoch when key_as_string absent.
            day_dt = datetime.fromtimestamp(day_value / 1000.0, tz=timezone.utc)
        else:
            day_dt = datetime.fromisoformat(str(day_value).replace("Z", "+00:00"))
        outcomes = {
            b["key"]: int(b["doc_count"]) for b in entry.get("by_outcome", {}).get("buckets", [])
        }
        runs = sum(outcomes.values())
        worst: Literal["green", "yellow", "red"]
        if outcomes.get("red", 0) > 0:
            worst = "red"
        elif outcomes.get("yellow", 0) > 0:
            worst = "yellow"
        else:
            worst = "green"
        buckets.append(DayBucket(day=day_dt.date(), runs=runs, worst_outcome=worst))
    return buckets


def _decide(
    buckets: list[DayBucket],
    *,
    min_runs_per_day: int,
    window_days: int,
) -> SevenDayGate:
    # Keep the final ``window_days`` buckets even if extended_bounds gives us
    # one extra (date_histogram includes the open boundary day).
    relevant = buckets[-window_days:]
    if len(relevant) < window_days:
        return SevenDayGate(
            status="yellow",
            reason=f"insufficient_history (have {len(relevant)} days, need {window_days})",
            buckets=relevant,
            min_runs_per_day=min_runs_per_day,
        )
    for b in relevant:
        if b.worst_outcome == "red":
            return SevenDayGate(
                status="red",
                reason=f"red_on_{b.day.isoformat()}",
                buckets=relevant,
                min_runs_per_day=min_runs_per_day,
            )
    # Drop to yellow if any day was yellow or under the runs floor.
    for b in relevant:
        if b.worst_outcome == "yellow":
            return SevenDayGate(
                status="yellow",
                reason=f"yellow_on_{b.day.isoformat()}",
                buckets=relevant,
                min_runs_per_day=min_runs_per_day,
            )
        if b.runs < min_runs_per_day:
            return SevenDayGate(
                status="yellow",
                reason=f"low_runs_on_{b.day.isoformat()} ({b.runs} < {min_runs_per_day})",
                buckets=relevant,
                min_runs_per_day=min_runs_per_day,
            )
    return SevenDayGate(
        status="green",
        reason=None,
        buckets=relevant,
        min_runs_per_day=min_runs_per_day,
    )


def render_table(gate: SevenDayGate) -> str:
    """Render a 7-line ASCII table summarising the gate verdict.

    Args:
        gate: Result from :func:`compute_seven_day_gate`.

    Returns:
        Multi-line string suitable for stdout / Linear comment.
    """
    lines: list[str] = []
    for b in gate.buckets:
        lines.append(
            f"{b.day.isoformat()}  {b.worst_outcome:6s}  "
            f"{b.runs:3d}/{gate.min_runs_per_day:2d} runs"
        )
    suffix = (
        "STATUS: GREEN — ADR-0074 acceptance criterion satisfied"
        if gate.status == "green"
        else f"STATUS: {gate.status.upper()} — {gate.reason}"
    )
    lines.append(suffix)
    return "\n".join(lines)
