"""Cache-erosion monitor — Jaccard hash-stability check (FRE-406 P2).

Queries ``prompt_static_prefix_hash`` values from ``model_call_completed``
events in ``agent-logs-*`` for a rolling window of calendar days, then
computes the Jaccard similarity of consecutive-day hash sets per callsite.

Gate: similarity >= 0.9 = stable; < 0.9 = erosion (alert).

Designed to run from the brainstem scheduler (hourly) or the CLI (manual).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch

log = get_logger(__name__)

EROSION_THRESHOLD: float = 0.9
DEFAULT_WINDOW_DAYS: int = 2
MONITORED_CALLSITES: tuple[str, ...] = ("orchestrator.primary", "gateway.chat")


@dataclass(frozen=True)
class DayHashes:
    """Hash set for one callsite on one calendar day.

    Attributes:
        day: UTC calendar date.
        callsite: Prompt callsite identifier.
        hashes: Distinct ``prompt_static_prefix_hash`` values observed.
        call_count: Total ``model_call_completed`` events that day.
    """

    day: date
    callsite: str
    hashes: frozenset[str]
    call_count: int


@dataclass(frozen=True)
class CallsiteResult:
    """Erosion verdict for one callsite over a consecutive-day pair.

    Attributes:
        callsite: Prompt callsite identifier.
        day_a: Earlier of the two days compared.
        day_b: Later of the two days compared.
        hashes_a: Hash set on day_a.
        hashes_b: Hash set on day_b.
        jaccard: Jaccard similarity between hash sets (0.0–1.0).
        status: ``"stable"`` when jaccard >= threshold, else ``"eroded"``.
        threshold: Threshold used (echoed for the dashboard).
    """

    callsite: str
    day_a: date
    day_b: date
    hashes_a: frozenset[str]
    hashes_b: frozenset[str]
    jaccard: float
    status: Literal["stable", "eroded", "insufficient_data"]
    threshold: float


@dataclass(frozen=True)
class ErosionReport:
    """Overall erosion report for all monitored callsites.

    Attributes:
        computed_at: When the report was generated.
        results: Per-callsite verdicts.
        any_eroded: True when at least one callsite shows erosion.
        threshold: Threshold used.
    """

    computed_at: datetime
    results: list[CallsiteResult]
    any_eroded: bool
    threshold: float


async def compute_erosion_report(
    es: "AsyncElasticsearch",
    *,
    logs_prefix: str = "agent-logs",
    callsites: tuple[str, ...] = MONITORED_CALLSITES,
    window_days: int = DEFAULT_WINDOW_DAYS,
    threshold: float = EROSION_THRESHOLD,
    hours_ago: int | None = None,
    now: datetime | None = None,
) -> ErosionReport:
    """Compute the cache-erosion report for monitored callsites.

    Two windowing modes:

    * **Calendar-day (default):** compares the most recent consecutive-day hash
      sets per callsite via Jaccard. Day granularity is blind to a same-morning,
      same-session deploy (it collapses into one bucket).
    * **Hours-ago (``hours_ago`` set, ADR-0081 D4):** aggregates *all* distinct
      prefix hashes in the last *N* hours per callsite, with no day bucketing,
      and grades on the distinct-hash count directly (gate: exactly 1). This is
      what lets D4 be verified in the same session it deploys.

    Args:
        es: Connected AsyncElasticsearch client.
        logs_prefix: Index prefix for ``agent-logs-*``.
        callsites: Callsite identifiers to monitor.
        window_days: How many consecutive days to compare (must be >= 2).
        threshold: Jaccard similarity threshold; < threshold = eroded.
        hours_ago: When set, use a sub-day window of the last *N* hours and grade
            on distinct-hash count instead of consecutive-day Jaccard.
        now: Override current time for testing.

    Returns:
        :class:`ErosionReport` with per-callsite verdicts.
    """
    now = now or datetime.now(timezone.utc)

    if hours_ago is not None:
        return await _compute_window_report(
            es,
            logs_prefix=logs_prefix,
            callsites=callsites,
            hours_ago=hours_ago,
            threshold=threshold,
            now=now,
        )

    start = (now - timedelta(days=window_days)).replace(hour=0, minute=0, second=0, microsecond=0)

    response = await es.search(
        index=f"{logs_prefix}-*",
        size=0,
        query={
            "bool": {
                "filter": [
                    {"term": {"event_type": "model_call_completed"}},
                    {"terms": {"prompt_callsite": list(callsites)}},
                    {"exists": {"field": "prompt_static_prefix_hash"}},
                    {"range": {"@timestamp": {"gte": start.isoformat()}}},
                ]
            }
        },
        aggs={
            "by_callsite": {
                "terms": {"field": "prompt_callsite", "size": len(callsites)},
                "aggs": {
                    "by_day": {
                        "date_histogram": {
                            "field": "@timestamp",
                            "calendar_interval": "1d",
                            "min_doc_count": 1,
                        },
                        "aggs": {
                            "hashes": {
                                "terms": {
                                    "field": "prompt_static_prefix_hash",
                                    "size": 1000,
                                }
                            }
                        },
                    }
                },
            }
        },
        ignore_unavailable=True,
        allow_no_indices=True,
    )

    day_hashes = _parse_day_hashes(dict(response))
    results = _compute_verdicts(day_hashes, callsites=callsites, threshold=threshold)
    return ErosionReport(
        computed_at=now,
        results=results,
        any_eroded=any(r.status == "eroded" for r in results),
        threshold=threshold,
    )


async def _compute_window_report(
    es: "AsyncElasticsearch",
    *,
    logs_prefix: str,
    callsites: tuple[str, ...],
    hours_ago: int,
    threshold: float,
    now: datetime,
) -> ErosionReport:
    """Compute a sub-day, distinct-hash-count erosion report (ADR-0081 D4).

    Aggregates all distinct ``prompt_static_prefix_hash`` values in the last
    *hours_ago* hours per callsite (no day bucketing) and grades on the distinct
    count: exactly 1 = stable, >1 = eroded, 0 calls = insufficient_data.

    Args:
        es: Connected AsyncElasticsearch client.
        logs_prefix: Index prefix for ``agent-logs-*``.
        callsites: Callsite identifiers to monitor.
        hours_ago: Window size in hours.
        threshold: Echoed into the result for the dashboard.
        now: Current time (window end).

    Returns:
        :class:`ErosionReport` with one verdict per callsite.
    """
    start = now - timedelta(hours=hours_ago)
    response = await es.search(
        index=f"{logs_prefix}-*",
        size=0,
        query={
            "bool": {
                "filter": [
                    {"term": {"event_type": "model_call_completed"}},
                    {"terms": {"prompt_callsite": list(callsites)}},
                    {"exists": {"field": "prompt_static_prefix_hash"}},
                    {"range": {"@timestamp": {"gte": start.isoformat()}}},
                ]
            }
        },
        aggs={
            "by_callsite": {
                "terms": {"field": "prompt_callsite", "size": len(callsites)},
                "aggs": {"hashes": {"terms": {"field": "prompt_static_prefix_hash", "size": 1000}}},
            }
        },
        ignore_unavailable=True,
        allow_no_indices=True,
    )

    hashes_by_callsite: dict[str, frozenset[str]] = {}
    counts_by_callsite: dict[str, int] = {}
    for cs_bucket in (
        dict(response).get("aggregations", {}).get("by_callsite", {}).get("buckets", [])
    ):
        callsite = cs_bucket["key"]
        hashes_by_callsite[callsite] = frozenset(
            b["key"] for b in cs_bucket.get("hashes", {}).get("buckets", [])
        )
        counts_by_callsite[callsite] = int(cs_bucket.get("doc_count", 0))

    results = _compute_window_verdicts(
        hashes_by_callsite,
        counts_by_callsite,
        callsites=callsites,
        threshold=threshold,
        window_day=now.date(),
    )
    return ErosionReport(
        computed_at=now,
        results=results,
        any_eroded=any(r.status == "eroded" for r in results),
        threshold=threshold,
    )


def _parse_day_hashes(response: dict[str, Any]) -> list[DayHashes]:
    buckets_by_callsite = response.get("aggregations", {}).get("by_callsite", {}).get("buckets", [])
    out: list[DayHashes] = []
    for cs_bucket in buckets_by_callsite:
        callsite = cs_bucket["key"]
        for day_bucket in cs_bucket.get("by_day", {}).get("buckets", []):
            raw_day = day_bucket.get("key_as_string") or day_bucket.get("key")
            if isinstance(raw_day, int):
                day_dt = datetime.fromtimestamp(raw_day / 1000.0, tz=timezone.utc)
            else:
                day_dt = datetime.fromisoformat(str(raw_day).replace("Z", "+00:00"))
            hashes = frozenset(b["key"] for b in day_bucket.get("hashes", {}).get("buckets", []))
            out.append(
                DayHashes(
                    day=day_dt.date(),
                    callsite=callsite,
                    hashes=hashes,
                    call_count=int(day_bucket.get("doc_count", 0)),
                )
            )
    return out


def _compute_verdicts(
    day_hashes: list[DayHashes],
    *,
    callsites: tuple[str, ...],
    threshold: float,
) -> list[CallsiteResult]:
    results: list[CallsiteResult] = []
    by_callsite: dict[str, list[DayHashes]] = {}
    for dh in day_hashes:
        by_callsite.setdefault(dh.callsite, []).append(dh)

    for callsite in callsites:
        days = sorted(by_callsite.get(callsite, []), key=lambda d: d.day)
        if len(days) < 2:
            results.append(
                CallsiteResult(
                    callsite=callsite,
                    day_a=days[0].day if days else date.today(),
                    day_b=date.today(),
                    hashes_a=days[0].hashes if days else frozenset(),
                    hashes_b=frozenset(),
                    jaccard=1.0,
                    status="insufficient_data",
                    threshold=threshold,
                )
            )
            continue
        # Compare the most recent consecutive pair.
        a, b = days[-2], days[-1]
        jaccard = _jaccard(a.hashes, b.hashes)
        results.append(
            CallsiteResult(
                callsite=callsite,
                day_a=a.day,
                day_b=b.day,
                hashes_a=a.hashes,
                hashes_b=b.hashes,
                jaccard=jaccard,
                status="eroded" if jaccard < threshold else "stable",
                threshold=threshold,
            )
        )
    return results


def _compute_window_verdicts(
    hashes_by_callsite: dict[str, frozenset[str]],
    counts_by_callsite: dict[str, int],
    *,
    callsites: tuple[str, ...],
    threshold: float,
    window_day: date,
) -> list[CallsiteResult]:
    """Grade each callsite on distinct-hash count within a sub-day window.

    The D4 cache gate is ``distinct_prefixes == 1`` across the session. A score
    of ``1.0 / distinct`` maps that onto the existing Jaccard threshold: exactly
    1 hash → 1.0 (stable); N hashes → 1/N (eroded for N >= 2 at threshold 0.9).

    Args:
        hashes_by_callsite: Distinct hash set per callsite in the window.
        counts_by_callsite: Total call count per callsite in the window.
        callsites: Callsites to report (in order).
        threshold: Jaccard floor echoed into each result.
        window_day: Date stamped onto the result for rendering.

    Returns:
        One :class:`CallsiteResult` per callsite.
    """
    results: list[CallsiteResult] = []
    for callsite in callsites:
        hashes = hashes_by_callsite.get(callsite, frozenset())
        call_count = counts_by_callsite.get(callsite, 0)
        distinct = len(hashes)
        if call_count == 0 or distinct == 0:
            status: Literal["stable", "eroded", "insufficient_data"] = "insufficient_data"
            score = 1.0
        elif distinct == 1:
            status = "stable"
            score = 1.0
        else:
            status = "eroded"
            score = 1.0 / distinct
        results.append(
            CallsiteResult(
                callsite=callsite,
                day_a=window_day,
                day_b=window_day,
                hashes_a=hashes,
                hashes_b=hashes,
                jaccard=score,
                status=status,
                threshold=threshold,
            )
        )
    return results


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity of two hash sets.

    Args:
        a: First hash set.
        b: Second hash set.

    Returns:
        Float in [0.0, 1.0]; 1.0 when both sets are empty.
    """
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def render_report(report: ErosionReport) -> str:
    """Render an ASCII summary of the erosion report for stdout / Linear.

    Args:
        report: Output of :func:`compute_erosion_report`.

    Returns:
        Multi-line string.
    """
    lines: list[str] = []
    for r in report.results:
        hashes_a_count = len(r.hashes_a)
        hashes_b_count = len(r.hashes_b)
        lines.append(
            f"{r.callsite:35s}  {r.day_a} → {r.day_b}"
            f"  hashes={hashes_a_count}/{hashes_b_count}"
            f"  jaccard={r.jaccard:.3f}"
            f"  [{r.status.upper()}]"
        )
    suffix = (
        "STATUS: GREEN — all callsites stable"
        if not report.any_eroded
        else f"STATUS: RED — prefix erosion detected (Jaccard < {report.threshold:.2f})"
    )
    lines.append(suffix)
    return "\n".join(lines)
