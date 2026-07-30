"""Unit tests for the telemetry delivery-ratio probe (FRE-1051).

The probe adds ADR-0090's missing fourth corner: *delivery*. The existing three
corners (emit, mapping, dashboard) all audit whether a field is shaped correctly
once it arrives; none asks whether the emitted event arrived at all.

The measured failure these tests encode: on 2026-07-23/26/27 the Postgres
``api_costs`` ledger held 144/201/361 rows while ``agent-logs-*`` held 25/105/172
``api_cost_recorded`` documents. The shortfall was whole processes whose telemetry
never reached Elasticsearch.
"""

from __future__ import annotations

from datetime import date

import pytest

from personal_agent.observability.delivery_ratio.probe import (
    DEFAULT_MIN_RATIO,
    DeliveryReport,
    FamilyDelivery,
    ZeroCause,
    classify_zero,
    compute_report,
    render_report,
)

# ---------------------------------------------------------------------------
# FamilyDelivery — the ratio itself
# ---------------------------------------------------------------------------


class TestFamilyDelivery:
    """Ratio arithmetic and verdicts for a single event family."""

    def test_full_delivery_is_ratio_one_and_passes(self) -> None:
        f = FamilyDelivery(
            family="api_cost_recorded",
            oracle="postgres:api_costs",
            oracle_count=103,
            es_count=103,
            min_ratio=DEFAULT_MIN_RATIO,
        )
        assert f.ratio == 1.0
        assert f.status == "pass"

    def test_measured_2026_07_23_loss_is_a_breach(self) -> None:
        """The real 82.6% loss day must not be reported as passing."""
        f = FamilyDelivery(
            family="api_cost_recorded",
            oracle="postgres:api_costs",
            oracle_count=144,
            es_count=25,
            min_ratio=DEFAULT_MIN_RATIO,
        )
        assert f.ratio == pytest.approx(25 / 144)
        assert f.lost == 119
        assert f.status == "breach"

    def test_oracle_zero_is_unverifiable_not_a_pass(self) -> None:
        """No oracle rows means nothing was proven, not that delivery is fine.

        A 0/0 family that reported ``pass`` would let a silent outage read as
        healthy, which is the exact failure mode this probe exists to catch.
        """
        f = FamilyDelivery(
            family="api_cost_recorded",
            oracle="postgres:api_costs",
            oracle_count=0,
            es_count=0,
            min_ratio=DEFAULT_MIN_RATIO,
        )
        assert f.status == "unverifiable"
        assert f.ratio is None

    def test_family_without_an_oracle_is_unverifiable(self) -> None:
        f = FamilyDelivery(
            family="phase_completed",
            oracle=None,
            oracle_count=None,
            es_count=42,
            min_ratio=DEFAULT_MIN_RATIO,
        )
        assert f.status == "unverifiable"
        assert f.ratio is None

    def test_es_exceeding_oracle_is_flagged_not_clamped(self) -> None:
        """More ES docs than ledger rows means double-emit or a bad join.

        Clamping to 1.0 would hide it behind a clean pass.
        """
        f = FamilyDelivery(
            family="api_cost_recorded",
            oracle="postgres:api_costs",
            oracle_count=100,
            es_count=137,
            min_ratio=DEFAULT_MIN_RATIO,
        )
        assert f.status == "over_delivery"
        assert f.ratio == pytest.approx(1.37)


# ---------------------------------------------------------------------------
# classify_zero — the three indistinguishable causes of a clean zero
# ---------------------------------------------------------------------------


class TestClassifyZero:
    """A zero from an ES query has three causes; the probe must separate them.

    Before this ticket only two were on the list. The third — emitted and lost —
    is what made every log-derived conclusion rest on unverified completeness.
    """

    def test_no_such_data_when_oracle_is_also_empty(self) -> None:
        assert classify_zero(oracle_count=0, es_count=0, field_present=True) is ZeroCause.NO_DATA

    def test_wrong_field_name_when_field_absent_from_mapping(self) -> None:
        assert (
            classify_zero(oracle_count=144, es_count=0, field_present=False)
            is ZeroCause.FIELD_ABSENT
        )

    def test_emitted_and_lost_when_oracle_has_rows_and_field_exists(self) -> None:
        assert (
            classify_zero(oracle_count=144, es_count=0, field_present=True)
            is ZeroCause.EMITTED_AND_LOST
        )

    def test_not_a_zero_at_all(self) -> None:
        assert classify_zero(oracle_count=144, es_count=25, field_present=True) is None


# ---------------------------------------------------------------------------
# compute_report — aggregation and exit semantics
# ---------------------------------------------------------------------------


def _family(name: str, oracle_count: int | None, es_count: int) -> FamilyDelivery:
    return FamilyDelivery(
        family=name,
        oracle="postgres:api_costs" if oracle_count is not None else None,
        oracle_count=oracle_count,
        es_count=es_count,
        min_ratio=DEFAULT_MIN_RATIO,
    )


class TestComputeReport:
    """Window-level verdict across families."""

    def test_all_families_delivering_exits_zero(self) -> None:
        report = compute_report(
            since=date(2026, 7, 24),
            until=date(2026, 7, 25),
            families=[_family("api_cost_recorded", 103, 103)],
        )
        assert report.status == "pass"
        assert report.exit_code == 0

    def test_any_breach_exits_nonzero(self) -> None:
        report = compute_report(
            since=date(2026, 7, 23),
            until=date(2026, 7, 24),
            families=[
                _family("api_cost_recorded", 144, 25),
                _family("turn.model_call_completed", 144, 144),
            ],
        )
        assert report.status == "breach"
        assert report.exit_code != 0

    def test_unverifiable_alone_does_not_report_pass(self) -> None:
        """An all-unverifiable window proved nothing; it must not read as green."""
        report = compute_report(
            since=date(2026, 7, 23),
            until=date(2026, 7, 24),
            families=[_family("phase_completed", None, 10)],
        )
        assert report.status == "unverifiable"
        assert report.exit_code != 0

    def test_worst_family_is_surfaced_first(self) -> None:
        report = compute_report(
            since=date(2026, 7, 23),
            until=date(2026, 7, 28),
            families=[
                _family("a", 103, 103),
                _family("b", 361, 172),
                _family("c", 201, 105),
            ],
        )
        assert [f.family for f in report.ranked_families][:2] == ["b", "c"]


# ---------------------------------------------------------------------------
# render_report — the human/JSON surface
# ---------------------------------------------------------------------------


class TestRenderReport:
    """Rendering must state the loss, never round it away."""

    def test_reports_percentage_and_absolute_loss(self) -> None:
        report = compute_report(
            since=date(2026, 7, 23),
            until=date(2026, 7, 24),
            families=[_family("api_cost_recorded", 144, 25)],
        )
        out = render_report(report)
        assert "119" in out
        assert "api_cost_recorded" in out

    def test_unverifiable_is_named_not_shown_as_zero_percent(self) -> None:
        """Asserts the family's own ROW, not the whole report.

        The previous version checked ``"UNVERIFIABLE" in out.upper()``, which passed
        even with the cell rendered as ``0.0%`` — the header line reads
        ``Overall: UNVERIFIABLE`` and satisfied the substring on its own. A mutation
        replacing the cell text survived it.
        """
        report = compute_report(
            since=date(2026, 7, 23),
            until=date(2026, 7, 24),
            families=[_family("phase_completed", None, 10)],
        )
        row = next(line for line in render_report(report).splitlines() if "phase_completed" in line)
        assert "UNVERIFIABLE" in row
        assert "0.0%" not in row

    def test_json_payload_round_trips(self) -> None:
        report = compute_report(
            since=date(2026, 7, 23),
            until=date(2026, 7, 24),
            families=[_family("api_cost_recorded", 144, 25)],
        )
        payload = report.to_dict()
        assert payload["status"] == "breach"
        assert payload["families"][0]["lost"] == 119


class TestMutationGaps:
    """Cases added because a mutation of the implementation survived without them.

    Each names the mutation it pins. Without these the property is asserted nowhere and
    the implementation could regress silently.
    """

    def test_over_delivery_alone_still_breaches_the_window(self) -> None:
        """Pins: dropping ``over_delivery`` from the report-level breach condition.

        A window whose only anomaly is a double emit must not report ``pass``.
        """
        report = compute_report(
            since=date(2026, 7, 23),
            until=date(2026, 7, 24),
            families=[_family("good", 100, 100), _family("doubled", 100, 137)],
        )
        assert report.status == "breach"
        assert report.exit_code != 0

    def test_over_delivery_reports_zero_lost_never_negative(self) -> None:
        """Pins: removing the ``max(0, ...)`` floor on ``lost``.

        Without the floor an over-delivering family renders a negative loss.
        """
        f = _family("doubled", 100, 137)
        assert f.lost == 0

    def test_over_delivery_outranks_a_passing_family(self) -> None:
        """Pins: ranking on the raw ratio ascending.

        Ratio 1.37 sorted *after* ratio 1.00, so the anomaly the probe promises to
        surface appeared below a clean family in the table a human reads.
        """
        report = compute_report(
            since=date(2026, 7, 23),
            until=date(2026, 7, 24),
            families=[_family("clean", 100, 100), _family("doubled", 100, 137)],
        )
        assert [f.family for f in report.ranked_families][0] == "doubled"

    def test_unverifiable_sorts_below_even_a_total_loss(self) -> None:
        """Pins: dropping the unverifiable term from the ranking key."""
        report = compute_report(
            since=date(2026, 7, 23),
            until=date(2026, 7, 24),
            families=[_family("nooracle", None, 5), _family("total_loss", 100, 0)],
        )
        assert [f.family for f in report.ranked_families] == ["total_loss", "nooracle"]

    def test_ratio_exactly_at_the_floor_passes(self) -> None:
        """Pins: flipping ``>=`` to ``>`` on the floor comparison."""
        f = FamilyDelivery(
            family="api_cost_recorded",
            oracle="postgres:api_costs",
            oracle_count=100,
            es_count=99,
            min_ratio=0.99,
        )
        assert f.ratio == pytest.approx(0.99)
        assert f.status == "pass"

    def test_absent_field_wins_over_no_data_when_both_hold(self) -> None:
        """Pins: the precedence between FIELD_ABSENT and NO_DATA.

        With no oracle rows AND no mapping, the field is the actionable fact: the query
        is broken, so the empty oracle tells you nothing.
        """
        assert (
            classify_zero(oracle_count=0, es_count=0, field_present=False) is ZeroCause.FIELD_ABSENT
        )


class TestFieldAbsentIsNotReportedAsLoss:
    """A zero from a missing field must not be dressed up as a delivery failure.

    This is the probe's own thesis turned on itself: the classification existed and was
    unit-tested but was not wired into the collector, so a renamed field would have been
    reported as "0% delivered, 404 lost" — blaming the pipeline for a broken query.
    """

    def _absent(self) -> FamilyDelivery:
        return FamilyDelivery(
            family="api_cost_recorded",
            oracle="postgres:api_costs",
            oracle_count=144,
            es_count=0,
            zero_cause=ZeroCause.FIELD_ABSENT,
        )

    def test_status_is_field_absent_not_breach(self) -> None:
        assert self._absent().status == "field_absent"

    def test_window_still_alarms(self) -> None:
        report = compute_report(
            since=date(2026, 7, 23), until=date(2026, 7, 24), families=[self._absent()]
        )
        assert report.status == "breach"
        assert report.exit_code != 0

    def test_render_names_the_cause_instead_of_a_percentage(self) -> None:
        report = compute_report(
            since=date(2026, 7, 23), until=date(2026, 7, 24), families=[self._absent()]
        )
        row = next(
            line
            for line in render_report(report).splitlines()
            if line.startswith("api_cost_recorded")
        )
        assert "FIELD-ABSENT" in row
        assert "0.0%" not in row

    def test_field_absent_leads_the_ranking(self) -> None:
        report = compute_report(
            since=date(2026, 7, 23),
            until=date(2026, 7, 24),
            families=[_family("breached", 100, 10), self._absent()],
        )
        assert [f.family for f in report.ranked_families][0] == "api_cost_recorded"

    def test_zero_cause_is_carried_into_the_json(self) -> None:
        report = compute_report(
            since=date(2026, 7, 23), until=date(2026, 7, 24), families=[self._absent()]
        )
        assert report.to_dict()["families"][0]["zero_cause"] == "field_absent"

    def test_emitted_and_lost_is_still_a_plain_breach(self) -> None:
        """The other attributed zero keeps blaming delivery, which is correct."""
        f = FamilyDelivery(
            family="api_cost_recorded",
            oracle="postgres:api_costs",
            oracle_count=144,
            es_count=0,
            zero_cause=ZeroCause.EMITTED_AND_LOST,
        )
        assert f.status == "breach"


class TestDeliveryReportWindow:
    """The window must be stated in the output — a ratio without one is unreadable."""

    def test_window_is_carried_on_the_report(self) -> None:
        report: DeliveryReport = compute_report(
            since=date(2026, 7, 23),
            until=date(2026, 7, 28),
            families=[_family("api_cost_recorded", 1303, 899)],
        )
        assert report.since == date(2026, 7, 23)
        assert report.until == date(2026, 7, 28)
        assert "2026-07-23" in render_report(report)
