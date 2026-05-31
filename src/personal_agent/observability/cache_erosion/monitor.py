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
    now: datetime | None = None,
) -> ErosionReport:
    """Compute the cache-erosion report for monitored callsites.

    Args:
        es: Connected AsyncElasticsearch client.
        logs_prefix: Index prefix for ``agent-logs-*``.
        callsites: Callsite identifiers to monitor.
        window_days: How many consecutive days to compare (must be >= 2).
        threshold: Jaccard similarity threshold; < threshold = eroded.
        now: Override current time for testing.

    Returns:
        :class:`ErosionReport` with per-callsite verdicts.
    """
    now = now or datetime.now(timezone.utc)
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
