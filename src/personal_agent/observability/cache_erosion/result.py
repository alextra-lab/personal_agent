"""Result-document model for the cache-erosion probe (ADR-0078 / FRE-1189).

Persisted to Elasticsearch by :mod:`sink`; produced by :mod:`scheduler_runner` from
:func:`personal_agent.observability.cache_erosion.monitor.compute_erosion_report`.
Mirrors :class:`personal_agent.observability.slm_health.snapshot.SlmHealthSnapshot`:
one document per probe run, id'd by a fresh UUID at write time.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from personal_agent.observability.cache_erosion.monitor import ErosionReport


class CallsiteErosionRecord(BaseModel):
    """Erosion verdict for one monitored callsite, JSON-serialisable for ES.

    Attributes:
        callsite: Prompt callsite identifier.
        day_a: Earlier of the two days compared.
        day_b: Later of the two days compared.
        hash_count_a: Distinct prefix hashes observed on day_a.
        hash_count_b: Distinct prefix hashes observed on day_b.
        jaccard: Jaccard similarity between the two days' hash sets.
        status: ``"stable"``, ``"eroded"``, or ``"insufficient_data"``.
        threshold: Jaccard floor used for this verdict.
    """

    model_config = ConfigDict(frozen=True)

    callsite: str
    day_a: date
    day_b: date
    hash_count_a: int
    hash_count_b: int
    jaccard: float
    status: Literal["stable", "eroded", "insufficient_data"]
    threshold: float


class CacheErosionResultDoc(BaseModel):
    """One cache-erosion probe run, persisted to Elasticsearch.

    Attributes:
        run_at: When this run's report was computed.
        window_days: Consecutive-day window used.
        threshold: Jaccard floor used.
        any_eroded: True when at least one callsite eroded.
        results: Per-callsite verdicts.
        trace_id: This probe run's own SystemTraceContext trace id.
        kind: Fixed sentinel ``"system:cache_erosion_probe"`` for index routing.
    """

    model_config = ConfigDict(frozen=True)

    run_at: datetime
    window_days: int
    threshold: float
    any_eroded: bool
    results: list[CallsiteErosionRecord] = Field(default_factory=list)
    trace_id: str
    kind: str = "system:cache_erosion_probe"


def from_report(
    report: "ErosionReport", *, window_days: int, trace_id: str
) -> CacheErosionResultDoc:
    """Build a persistable result doc from a computed erosion report.

    Converts each callsite's raw hash sets (``frozenset[str]``, not JSON-serialisable)
    to their counts.

    Args:
        report: Output of :func:`compute_erosion_report`.
        window_days: Consecutive-day window used for the run.
        trace_id: This probe run's own SystemTraceContext trace id.

    Returns:
        The persistable result document. ``run_at`` is taken from
        ``report.computed_at`` — the probe's own canonical timestamp.
    """
    return CacheErosionResultDoc(
        run_at=report.computed_at,
        window_days=window_days,
        threshold=report.threshold,
        any_eroded=report.any_eroded,
        results=[
            CallsiteErosionRecord(
                callsite=r.callsite,
                day_a=r.day_a,
                day_b=r.day_b,
                hash_count_a=len(r.hashes_a),
                hash_count_b=len(r.hashes_b),
                jaccard=r.jaccard,
                status=r.status,
                threshold=r.threshold,
            )
            for r in report.results
        ],
        trace_id=trace_id,
    )
