"""Result-document model for the delivery-ratio probe (FRE-1051 / FRE-1189).

Persisted to Elasticsearch by :mod:`sink`; produced by :mod:`scheduler_runner` from
:func:`personal_agent.observability.delivery_ratio.collect.collect_report`. Carries
the per-family verdict — including the unverifiable case — not merely that the probe
ran (ADR-0134 D4 rule 2 depends on a document that carries a verdict).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from personal_agent.observability.delivery_ratio.probe import FamilyStatus, ReportStatus

if TYPE_CHECKING:
    from personal_agent.observability.delivery_ratio.probe import DeliveryReport


class FamilyDeliveryRecord(BaseModel):
    """JSON-serialisable per-family delivery verdict.

    Field-by-field mirror of :class:`~personal_agent.observability.delivery_ratio.probe.FamilyDelivery`
    — built explicitly in :func:`from_report` rather than via ``**FamilyDelivery.to_dict()`` so a
    future schema change on either side is a visible diff, not a silently-dropped extra key.

    Attributes:
        family: Event family measured.
        oracle: Independent oracle identifier, or ``None`` when unverifiable.
        oracle_count: Rows the oracle held, or ``None``.
        es_count: Documents ``agent-logs-*`` held.
        ratio: Delivered fraction, or ``None`` when unverifiable.
        lost: Shortfall, or ``None`` when unverifiable.
        status: Verdict for this family.
        min_ratio: Delivery floor used.
        zero_cause: Attribution for a zero-document result, or ``None``.
    """

    model_config = ConfigDict(frozen=True)

    family: str
    oracle: str | None
    oracle_count: int | None
    es_count: int
    ratio: float | None
    lost: int | None
    status: FamilyStatus
    min_ratio: float
    zero_cause: str | None


class DeliveryRatioResultDoc(BaseModel):
    """One delivery-ratio probe run, persisted to Elasticsearch.

    Attributes:
        run_at: When this run executed (distinct from the window measured).
        since: First UTC day of the measured window.
        until: Last UTC day of the measured window.
        status: Window-level verdict.
        families: Per-family verdicts, worst first.
        trace_id: This probe run's own SystemTraceContext trace id.
        kind: Fixed sentinel ``"system:delivery_ratio_probe"`` for index routing.
    """

    model_config = ConfigDict(frozen=True)

    run_at: datetime
    since: date
    until: date
    status: ReportStatus
    families: list[FamilyDeliveryRecord] = Field(default_factory=list)
    trace_id: str
    kind: str = "system:delivery_ratio_probe"


def from_report(
    report: "DeliveryReport", *, run_at: datetime, trace_id: str
) -> DeliveryRatioResultDoc:
    """Build a persistable result doc from a computed delivery report.

    Args:
        report: Output of :func:`collect_report`.
        run_at: Wall-clock time this run executed.
        trace_id: This probe run's own SystemTraceContext trace id.

    Returns:
        The persistable result document, families ordered worst-first.
    """
    return DeliveryRatioResultDoc(
        run_at=run_at,
        since=report.since,
        until=report.until,
        status=report.status,
        families=[
            FamilyDeliveryRecord(
                family=f.family,
                oracle=f.oracle,
                oracle_count=f.oracle_count,
                es_count=f.es_count,
                ratio=f.ratio,
                lost=f.lost,
                status=f.status,
                min_ratio=f.min_ratio,
                zero_cause=f.zero_cause.value if f.zero_cause else None,
            )
            for f in report.ranked_families
        ],
        trace_id=trace_id,
    )
