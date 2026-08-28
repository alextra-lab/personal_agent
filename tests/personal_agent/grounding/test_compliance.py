"""The D5 per-model compliance metric (ADR-0138 D5, FRE-1284).

AC-2 through AC-6 live here — they are properties of the metric's own arithmetic and are
decidable without any corpus. AC-1 is a different kind of claim (agreement with
**independent** labelling of real turns) and is scored in
``test_fre1284_compliance_corpus.py`` against ``scripts/eval/fre1284_compliance``.

The bars asserted in :class:`TestAC6PreRegisteredBars` are load-bearing rather than
decorative: they are the pre-registration. A future change to a default must break this
test, because that is the only mechanism by which "the parameters were fixed before the
results were seen" survives contact with a later session.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from personal_agent.captains_log.turn_evidence import GroundingRecord
from personal_agent.config.settings import AppConfig
from personal_agent.grounding.compliance import (
    ComplianceObservation,
    ComplianceWindow,
    UnmeasuredReason,
    classify,
    configured_window,
    is_unconfounded_observation,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
MODEL = "gemma-3-27b"

WINDOW = ComplianceWindow(size=20, min_samples=10, max_age=timedelta(days=14), bar=0.95)


def _observations(
    *, compliant: int, non_compliant: int, age: timedelta = timedelta(hours=1)
) -> list[ComplianceObservation]:
    """Build a batch of observations, newest first, one minute apart.

    Args:
        compliant: How many compliant observations to emit.
        non_compliant: How many non-compliant observations to emit.
        age: How far before ``NOW`` the newest observation sits.

    Returns:
        The observations, newest first.
    """
    rows: list[ComplianceObservation] = []
    for index in range(compliant + non_compliant):
        rows.append(
            ComplianceObservation(
                model_key=MODEL,
                observed_at=NOW - age - timedelta(minutes=index),
                compliant=index < compliant,
            )
        )
    return rows


def _record(
    *,
    available: bool = True,
    non_exempt_count: int = 3,
    retrieval_forced: bool = False,
    first_generation_compliant: bool = True,
    degraded_extraction: bool = False,
    attempts: int = 1,
) -> GroundingRecord:
    """Build a grounding record with the fields the predicate reads."""
    return GroundingRecord(
        mode="observe",
        available=available,
        non_exempt_count=non_exempt_count,
        passed_count=non_exempt_count if first_generation_compliant else 0,
        retrieval_forced=retrieval_forced,
        first_generation_compliant=first_generation_compliant,
        degraded_extraction=degraded_extraction,
        attempts=attempts,
    )


class TestAC2Unconfounded:
    """AC-2 — pre-forced turns are absent from the denominator."""

    def test_pre_forced_turn_is_not_an_observation(self) -> None:
        """The round-2 finding, stated as a predicate.

        Heavy enforcement supplies sources before generation, so a compliant pre-forced
        turn measures the enforcement. Counting it is what produces the promote/demote
        oscillation D5 exists to prevent.
        """
        assert not is_unconfounded_observation(
            _record(retrieval_forced=True, first_generation_compliant=True)
        )

    def test_a_pre_forced_turn_never_reaches_the_denominator(self) -> None:
        """The exclusion holds end-to-end, not merely in the predicate."""
        records = [_record(retrieval_forced=True) for _ in range(50)]
        eligible = [r for r in records if is_unconfounded_observation(r)]
        assert eligible == []

        result = classify(MODEL, [], window=WINDOW, now=NOW)
        assert not result.measured
        assert result.sample_count == 0

    def test_unforced_turn_is_an_observation(self) -> None:
        """The predicate must not reject everything — a vacuous AC-2 proves nothing."""
        assert is_unconfounded_observation(_record())

    def test_turn_without_a_non_exempt_span_is_excluded(self) -> None:
        """D5's denominator is 'turns containing at least one non-exempt span'."""
        assert not is_unconfounded_observation(_record(non_exempt_count=0))

    def test_unavailable_verification_is_excluded(self) -> None:
        """A denied budget or a broken extractor is not evidence about the model."""
        assert not is_unconfounded_observation(
            _record(available=False, first_generation_compliant=False)
        )

    def test_degraded_extraction_is_still_counted(self) -> None:
        """Degraded extraction fails *safe*, so it can only depress the rate.

        Excluding these turns is therefore the choice that inflates, and inflation is the
        failure that matters: an inflated rate promotes a model that has not earned it.
        """
        assert is_unconfounded_observation(_record(degraded_extraction=True))


class TestAC3Responsive:
    """AC-3 — the rate moves with observed behaviour, in both directions."""

    def test_rate_rises_as_compliant_turns_arrive(self) -> None:
        floor = classify(
            MODEL, _observations(compliant=0, non_compliant=10), window=WINDOW, now=NOW
        )
        assert floor.measured
        assert floor.rate == 0.0

        mixed = classify(
            MODEL, _observations(compliant=10, non_compliant=10), window=WINDOW, now=NOW
        )
        assert mixed.rate == 0.5

        ceiling = classify(
            MODEL, _observations(compliant=20, non_compliant=0), window=WINDOW, now=NOW
        )
        assert ceiling.rate == 1.0

    def test_rate_falls_as_non_compliant_turns_arrive(self) -> None:
        """The downward direction, which a hand-set value would fail."""
        before = classify(
            MODEL, _observations(compliant=20, non_compliant=0), window=WINDOW, now=NOW
        )
        after = classify(
            MODEL, _observations(compliant=0, non_compliant=20), window=WINDOW, now=NOW
        )
        assert before.rate == 1.0
        assert after.rate == 0.0

    def test_window_bounds_what_counts(self) -> None:
        """Older observations fall out of the window rather than lingering forever."""
        stale_good = _observations(compliant=40, non_compliant=0, age=timedelta(hours=5))
        recent_bad = _observations(compliant=0, non_compliant=20, age=timedelta(hours=1))
        result = classify(MODEL, [*recent_bad, *stale_good], window=WINDOW, now=NOW)
        assert result.rate == 0.0, "the newest window.size observations decide the rate"


class TestAC4Staleness:
    """AC-4 — a window aged past its maximum reverts the model to unmeasured."""

    def test_aged_favourable_window_reverts_to_unmeasured(self) -> None:
        """Compliance is re-earned, never banked."""
        aged = _observations(compliant=20, non_compliant=0, age=timedelta(days=15))
        result = classify(MODEL, aged, window=WINDOW, now=NOW)
        assert not result.measured
        assert result.reason is UnmeasuredReason.STALE_WINDOW
        assert result.rate is None
        assert not result.meets_bar

    def test_same_window_is_measured_before_it_ages(self) -> None:
        """The negative control: staleness, not some other rejection, did the work."""
        fresh = _observations(compliant=20, non_compliant=0, age=timedelta(days=13))
        result = classify(MODEL, fresh, window=WINDOW, now=NOW)
        assert result.measured
        assert result.rate == 1.0

    def test_partial_refresh_below_minimum_is_still_unmeasured(self) -> None:
        """A trickle of new turns does not resurrect an aged window.

        Enough fresh observations to be non-empty, too few to be a sample: the model that
        has gone quiet must not coast on what it earned a fortnight ago.
        """
        aged = _observations(compliant=15, non_compliant=0, age=timedelta(days=15))
        trickle = _observations(compliant=2, non_compliant=0, age=timedelta(hours=1))
        result = classify(MODEL, [*trickle, *aged], window=WINDOW, now=NOW)
        assert not result.measured
        assert result.reason is UnmeasuredReason.STALE_WINDOW

    def test_never_measured_model_says_so(self) -> None:
        """Distinct from staleness: nothing aged out, nothing ever arrived."""
        result = classify(MODEL, [], window=WINDOW, now=NOW)
        assert result.reason is UnmeasuredReason.NO_OBSERVATIONS


class TestAC5MinSamples:
    """AC-5 — below the minimum sample count, a model reports unmeasured, not a rate."""

    def test_one_short_of_the_minimum_is_unmeasured(self) -> None:
        result = classify(
            MODEL,
            _observations(compliant=WINDOW.min_samples - 1, non_compliant=0),
            window=WINDOW,
            now=NOW,
        )
        assert not result.measured
        assert result.reason is UnmeasuredReason.INSUFFICIENT_SAMPLES
        assert result.rate is None

    def test_exactly_the_minimum_is_measured(self) -> None:
        """The boundary is inclusive — and this is the negative control for the test above."""
        result = classify(
            MODEL,
            _observations(compliant=WINDOW.min_samples, non_compliant=0),
            window=WINDOW,
            now=NOW,
        )
        assert result.measured
        assert result.rate == 1.0

    def test_unmeasured_never_meets_the_bar(self) -> None:
        """D5's bootstrap: unmeasured is fail-safe, never a pass."""
        result = classify(
            MODEL, _observations(compliant=1, non_compliant=0), window=WINDOW, now=NOW
        )
        assert not result.meets_bar


class TestAC6PreRegisteredBars:
    """AC-6 — the bars are committed config, and they reject a broken baseline."""

    def test_defaults_are_the_pre_registered_values(self) -> None:
        """Changing a default must break this test.

        The commit history is the artifact AC-6 asks for; this assertion is what makes a
        silent later edit visible as a change to a pre-registered bar rather than a tweak.
        """
        settings = AppConfig()
        assert settings.grounding_compliance_window_size == 100
        assert settings.grounding_compliance_min_samples == 30
        assert settings.grounding_compliance_max_window_age_hours == 336
        assert settings.grounding_compliance_bar == 0.95

    def test_configured_window_reads_those_defaults(self) -> None:
        window = configured_window()
        assert window.size == 100
        assert window.min_samples == 30
        assert window.max_age == timedelta(hours=336)
        assert window.bar == 0.95

    def test_broken_baseline_is_rejected_under_the_committed_bar(self) -> None:
        """A seeded always-non-compliant model must not clear the bar.

        'A bar that a known-broken implementation would pass is not a bar' (ADR-0138).
        """
        window = configured_window()
        broken = [
            ComplianceObservation(
                model_key="always-non-compliant",
                observed_at=NOW - timedelta(minutes=index),
                compliant=False,
            )
            for index in range(window.min_samples)
        ]
        result = classify("always-non-compliant", broken, window=window, now=NOW)
        assert result.measured, "the baseline must be measured, or the bar is untested"
        assert result.rate == 0.0
        assert not result.meets_bar

    def test_a_compliant_model_clears_the_committed_bar(self) -> None:
        """The bar is reachable — a bar nothing can pass is equally useless."""
        window = configured_window()
        good = [
            ComplianceObservation(
                model_key=MODEL, observed_at=NOW - timedelta(minutes=index), compliant=True
            )
            for index in range(window.min_samples)
        ]
        result = classify(MODEL, good, window=window, now=NOW)
        assert result.meets_bar
