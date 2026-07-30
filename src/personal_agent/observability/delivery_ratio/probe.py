"""Delivery-ratio probe — the fourth corner of the telemetry surface contract (FRE-1051).

ADR-0090 audits three corners: **emit** (the call site exists), **mapping** (the field
is shaped correctly once it arrives), and **dashboard** (something reads it). All three
presuppose arrival. None asks whether an emitted event *arrived at all*, so a silently
dropped event is indistinguishable from one that was never emitted.

This module supplies the missing corner by comparing an event family in
``agent-logs-*`` against an **independent oracle** — a store written on a different
code path, so the two cannot fail together. Postgres ``api_costs`` is the reference
oracle: an append-only per-call ledger.

Why it exists, stated as the measurement that forced it: over 2026-07-23..28 the
ledger held 144/103/211/201/361/283 rows per day while ``agent-logs-*`` held
25/103/211/105/172/283 ``api_cost_recorded`` documents — three of six days losing
between roughly half and five sixths of the family, and three losing nothing. The
shape was episodic and whole-process, not a sampling rate.

Design rules this module holds to, each earned from that investigation:

- **A ratio without a stated window is unreadable** — the window is carried on the report.
- **``UNVERIFIABLE`` is a first-class verdict, never a silent pass.** A family with no
  oracle, or an oracle with no rows, proved nothing. Reporting that as ``pass`` is how a
  silent outage reads as healthy.
- **Over-delivery is surfaced, not clamped.** More ES documents than ledger rows means a
  double emit or a bad join; clamping the ratio to 1.0 hides it behind a clean pass.
- **A clean zero has three causes, not two** — see :class:`ZeroCause`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from personal_agent.telemetry import get_logger

log = get_logger(__name__)

DEFAULT_MIN_RATIO: float = 0.99
"""Delivery below this fraction of the oracle is a breach.

Not 1.0: an event emitted in the final moments of the window can legitimately be
indexed just after ``until``. It is deliberately tight — the losses this probe exists
to catch were 48–83%, nowhere near a boundary effect.
"""

FamilyStatus = Literal["pass", "breach", "unverifiable", "over_delivery", "field_absent"]
ReportStatus = Literal["pass", "breach", "unverifiable"]

_STATUS_RANK: dict[str, int] = {
    # A meaningless number outranks a bad one: if the queried field is absent the
    # ratio describes the query, not delivery, so it must be read first.
    "field_absent": 0,
    "breach": 1,
    "over_delivery": 1,
    "pass": 2,
    "unverifiable": 3,
}


class ZeroCause(enum.Enum):
    """Why an Elasticsearch query returned zero documents.

    Before FRE-1051 only the first two were on the list, so ``EMITTED_AND_LOST`` was
    routinely misread as ``NO_DATA`` — a clean, plausible, low number with nothing to
    contradict it.

    Attributes:
        NO_DATA: The oracle is also empty. There genuinely was no such activity.
        FIELD_ABSENT: The queried field is not in the mapping — the query names
            something that does not exist, so zero says nothing about delivery.
        EMITTED_AND_LOST: The oracle has rows and the field exists. The event was
            emitted and did not arrive.
    """

    NO_DATA = "no_data"
    FIELD_ABSENT = "field_absent"
    EMITTED_AND_LOST = "emitted_and_lost"


def classify_zero(*, oracle_count: int, es_count: int, field_present: bool) -> ZeroCause | None:
    """Attribute a zero-document result to one of its three causes.

    Args:
        oracle_count: Rows the independent oracle holds for the window.
        es_count: Documents Elasticsearch holds for the window.
        field_present: Whether the queried field exists in the live mapping.

    Returns:
        The :class:`ZeroCause`, or ``None`` when ``es_count`` is not zero.
    """
    if es_count != 0:
        return None
    if not field_present:
        return ZeroCause.FIELD_ABSENT
    if oracle_count == 0:
        return ZeroCause.NO_DATA
    return ZeroCause.EMITTED_AND_LOST


@dataclass(frozen=True)
class FamilyDelivery:
    """Delivery outcome for one event family over one window.

    Attributes:
        family: Event family measured (an ``event_type`` value).
        oracle: Identifier of the independent oracle, or ``None`` when the family has
            no oracle — which makes it structurally unverifiable, not passing.
        oracle_count: Rows the oracle holds, or ``None`` when there is no oracle.
        es_count: Documents ``agent-logs-*`` holds.
        min_ratio: Delivery floor below which this family is a breach.
        zero_cause: Attribution for a zero-document result, set by the collector via
            :func:`classify_zero`. ``None`` when ``es_count`` is non-zero or the
            attribution was not computed.
    """

    family: str
    oracle: str | None
    oracle_count: int | None
    es_count: int
    min_ratio: float = DEFAULT_MIN_RATIO
    zero_cause: ZeroCause | None = None

    @property
    def ratio(self) -> float | None:
        """Delivered fraction of the oracle, or ``None`` when unverifiable.

        Returns:
            ``es_count / oracle_count``, or ``None`` when there is no oracle or the
            oracle is empty. Never clamped — a value above 1.0 is real information.
        """
        if self.oracle_count is None or self.oracle_count == 0:
            return None
        return self.es_count / self.oracle_count

    @property
    def lost(self) -> int | None:
        """Documents the oracle accounts for that Elasticsearch does not hold.

        Returns:
            The shortfall, floored at zero, or ``None`` when unverifiable.
        """
        if self.oracle_count is None:
            return None
        return max(0, self.oracle_count - self.es_count)

    @property
    def status(self) -> FamilyStatus:
        """Verdict for this family.

        ``field_absent`` is checked first and independently of the ratio: when the
        queried field is not in the mapping, every count is zero for a reason that has
        nothing to do with delivery, so reporting that as a 100% loss would blame the
        pipeline for a broken query. It still alarms — it just alarms differently.

        Returns:
            ``"field_absent"`` when the zero is attributed to a missing field,
            ``"unverifiable"`` with no oracle or an empty oracle, ``"over_delivery"``
            when ES holds more than the oracle, ``"pass"`` at or above ``min_ratio``,
            else ``"breach"``.
        """
        if self.zero_cause is ZeroCause.FIELD_ABSENT:
            return "field_absent"
        ratio = self.ratio
        if ratio is None:
            return "unverifiable"
        if self.es_count > (self.oracle_count or 0):
            return "over_delivery"
        return "pass" if ratio >= self.min_ratio else "breach"

    def to_dict(self) -> dict[str, Any]:
        """Render as a JSON-serialisable mapping.

        Returns:
            Mapping with family, oracle, counts, ratio, loss and status.
        """
        return {
            "family": self.family,
            "oracle": self.oracle,
            "oracle_count": self.oracle_count,
            "es_count": self.es_count,
            "ratio": self.ratio,
            "lost": self.lost,
            "status": self.status,
            "min_ratio": self.min_ratio,
            "zero_cause": self.zero_cause.value if self.zero_cause else None,
        }


@dataclass(frozen=True)
class DeliveryReport:
    """Delivery verdict across every measured family for one window.

    Attributes:
        since: First UTC day of the window (inclusive).
        until: Last UTC day of the window (inclusive).
        families: Per-family outcomes, in the order supplied.
    """

    since: date
    until: date
    families: list[FamilyDelivery] = field(default_factory=list)

    @property
    def ranked_families(self) -> list[FamilyDelivery]:
        """Families most-alarming first, so the anomaly leads the output.

        Ranking is by status severity, then by deviation from perfect delivery. Sorting
        on the raw ratio ascending would put an over-delivering family (ratio above 1.0
        — a double emit or a bad join) *below* a cleanly passing one, burying the very
        anomaly this probe promises to surface rather than clamp.

        Returns:
            Families ordered ``field_absent`` → ``breach``/``over_delivery`` (largest
            deviation from 1.0 first) → ``pass`` → ``unverifiable``.
        """

        def key(f: FamilyDelivery) -> tuple[int, float]:
            ratio = f.ratio
            deviation = 0.0 if ratio is None else abs(1.0 - ratio)
            return (_STATUS_RANK[f.status], -deviation)

        return sorted(self.families, key=key)

    @property
    def status(self) -> ReportStatus:
        """Window-level verdict.

        Returns:
            ``"breach"`` if any family breached, over-delivered, or had its field
            absent; otherwise ``"unverifiable"`` if no family was verifiable at all;
            else ``"pass"``.
        """
        statuses = {f.status for f in self.families}
        if statuses & {"breach", "over_delivery", "field_absent"}:
            return "breach"
        if "pass" not in statuses:
            return "unverifiable"
        return "pass"

    @property
    def exit_code(self) -> int:
        """Process exit code for use as a standing check.

        Returns:
            ``0`` only when the window passed. An all-unverifiable window exits
            non-zero: it proved nothing, and silence must not read as green.
        """
        return 0 if self.status == "pass" else 1

    def to_dict(self) -> dict[str, Any]:
        """Render as a JSON-serialisable mapping.

        Returns:
            Mapping with the window, overall status and per-family detail.
        """
        return {
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "status": self.status,
            "exit_code": self.exit_code,
            "families": [f.to_dict() for f in self.ranked_families],
        }


def compute_report(
    *,
    since: date,
    until: date,
    families: list[FamilyDelivery],
) -> DeliveryReport:
    """Assemble the window report from per-family measurements.

    Args:
        since: First UTC day of the window (inclusive).
        until: Last UTC day of the window (inclusive).
        families: Per-family delivery measurements.

    Returns:
        The assembled :class:`DeliveryReport`.
    """
    report = DeliveryReport(since=since, until=until, families=list(families))
    log.info(
        "delivery_ratio_computed",
        since=since.isoformat(),
        until=until.isoformat(),
        status=report.status,
        family_count=len(families),
        component="delivery_ratio",
    )
    return report


def _format_ratio(f: FamilyDelivery) -> str:
    """Format one family's delivery cell.

    Names the reason instead of printing a percentage whenever the percentage would be
    meaningless: ``0.0%`` against an absent field reads as total loss, and against no
    oracle reads as a measurement that was never taken.

    Args:
        f: Family to format.

    Returns:
        A percentage, or ``FIELD-ABSENT`` / ``UNVERIFIABLE``.
    """
    if f.zero_cause is ZeroCause.FIELD_ABSENT:
        return "FIELD-ABSENT"
    if f.ratio is None:
        return "UNVERIFIABLE"
    return f"{f.ratio * 100:.1f}%"


def render_report(report: DeliveryReport) -> str:
    """Render the report as human-readable text.

    Args:
        report: Report to render.

    Returns:
        Multi-line summary, worst family first, stating the window and the absolute
        loss alongside the percentage.
    """
    lines = [
        f"Telemetry delivery ratio — {report.since.isoformat()} .. {report.until.isoformat()} (UTC)",
        f"Overall: {report.status.upper()}",
        "",
        f"{'family':32s} {'oracle':22s} {'oracle':>8s} {'es':>8s} {'lost':>6s} "
        f"{'delivered':>12s}  status",
    ]
    for f in report.ranked_families:
        lines.append(
            f"{f.family:32s} {(f.oracle or '(none)'):22s} "
            f"{('-' if f.oracle_count is None else f.oracle_count):>8} "
            f"{f.es_count:>8} {('-' if f.lost is None else f.lost):>6} "
            f"{_format_ratio(f):>12s}  {f.status}"
        )
    return "\n".join(lines)
