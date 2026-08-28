"""D5 enforcement selection (ADR-0138 D5, FRE-1285).

The half of D5 FRE-1284 deliberately left out: what the measured rate *means*. These tests
assert the **outcome** — which level a turn runs under — rather than the wiring that
delivers it, because the wiring is not what oscillates.

Two tests here exist because a plan review found the code path they cover missing rather
than wrong: ``test_staleness_demotion_stamps_cooldown`` and its companion close the
``LIGHT → stale → HEAVY → measured`` path, on which an ordinary below-band cooldown test
passes while a model promotes having earned nothing.
"""

from __future__ import annotations

import inspect
import random
from datetime import datetime, timedelta, timezone

import pytest

from personal_agent.grounding.compliance import (
    ComplianceObservation,
    ComplianceWindow,
    classify,
)
from personal_agent.grounding.enforcement_selection import (
    EnforcementBand,
    EnforcementLevel,
    EnforcementState,
    SelectionReason,
    build_forced_retrieval_directive,
    configured_band,
    initial_state,
    select_enforcement,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

BAND = EnforcementBand(
    promote_at=0.95,
    demote_below=0.90,
    cooldown=timedelta(hours=24),
    probation_rate=0.10,
)

LIGHT_STATE = EnforcementState(level=EnforcementLevel.LIGHT, demoted_at=None)


def _never_probation() -> random.Random:
    """A generator whose draws never fall under any sane probation rate."""

    class _Fixed(random.Random):
        def random(self) -> float:
            return 1.0

    return _Fixed()


def _always_probation() -> random.Random:
    class _Fixed(random.Random):
        def random(self) -> float:
            return 0.0

    return _Fixed()


def _select(rate: float | None, standing: EnforcementState, *, now: datetime = NOW, rng=None):
    return select_enforcement(
        rate=rate, standing=standing, band=BAND, now=now, rng=rng or _never_probation()
    )


# ── AC-3 — unmeasured is heavy ───────────────────────────────────────────────


def test_unmeasured_is_heavy() -> None:
    """A model nobody has observed pays the strict path (AC-3)."""
    selection = _select(None, initial_state())
    assert selection.applied is EnforcementLevel.HEAVY
    assert selection.standing.level is EnforcementLevel.HEAVY
    assert selection.reason is SelectionReason.UNMEASURED


def test_a_brand_new_model_has_no_cooldown_to_serve() -> None:
    """Never-light is not the same as demoted.

    ADR-0138 D5 gives the cooldown to a *demoted* model. A model that has never been
    light has not been demoted, so it is promotable the moment it is measured — the
    bootstrap would otherwise punish every new model for a demotion that never happened.
    """
    assert initial_state().demoted_at is None
    promoted = _select(0.99, initial_state())
    assert promoted.applied is EnforcementLevel.LIGHT
    assert promoted.reason is SelectionReason.PROMOTED


@pytest.mark.parametrize("observations", [[], [(True, 0)], [(True, 0), (False, 1)]])
def test_every_unmeasured_reason_yields_heavy(observations: list[tuple[bool, int]]) -> None:
    """Whichever way the reading comes back unmeasured, the answer is heavy (AC-3)."""
    window = ComplianceWindow(size=100, min_samples=30, max_age=timedelta(hours=336), bar=0.95)
    rows = [
        ComplianceObservation(
            model_key="m", observed_at=NOW - timedelta(minutes=offset), compliant=ok
        )
        for ok, offset in observations
    ]
    reading = classify("m", rows, window=window, now=NOW)
    assert not reading.measured
    assert _select(reading.rate, initial_state()).applied is EnforcementLevel.HEAVY


# ── AC-1 — the band, and the cooldown ────────────────────────────────────────


def test_demotes_on_first_reading_below_band() -> None:
    """Demotion is immediate: one reading below the lower threshold (AC-1)."""
    selection = _select(0.80, LIGHT_STATE)
    assert selection.applied is EnforcementLevel.HEAVY
    assert selection.reason is SelectionReason.DEMOTED
    assert selection.standing.demoted_at == NOW
    assert selection.changed


def test_in_band_holds_whichever_level_is_standing() -> None:
    """The hysteresis band: between the thresholds nothing moves (AC-1)."""
    held_light = _select(0.92, LIGHT_STATE)
    assert held_light.applied is EnforcementLevel.LIGHT
    assert held_light.reason is SelectionReason.BAND_HOLD
    assert not held_light.changed

    heavy = EnforcementState(level=EnforcementLevel.HEAVY, demoted_at=NOW - timedelta(days=9))
    held_heavy = _select(0.92, heavy)
    assert held_heavy.applied is EnforcementLevel.HEAVY
    assert held_heavy.reason is SelectionReason.BAND_HOLD


def test_promotion_blocked_until_cooldown_elapses() -> None:
    """Sustained recovery does not promote inside the cooldown (AC-1)."""
    demoted = EnforcementState(level=EnforcementLevel.HEAVY, demoted_at=NOW - timedelta(hours=1))
    held = _select(0.99, demoted)
    assert held.applied is EnforcementLevel.HEAVY
    assert held.reason is SelectionReason.COOLDOWN_HOLD
    assert not held.changed

    elapsed = _select(0.99, demoted, now=NOW + timedelta(hours=24))
    assert elapsed.applied is EnforcementLevel.LIGHT
    assert elapsed.reason is SelectionReason.PROMOTED
    assert elapsed.standing.demoted_at is None


def test_promotion_requires_a_measured_window() -> None:
    """An unmeasured reading cannot promote however long the cooldown has run (AC-1)."""
    demoted = EnforcementState(level=EnforcementLevel.HEAVY, demoted_at=NOW - timedelta(days=30))
    assert _select(None, demoted).applied is EnforcementLevel.HEAVY


def test_staying_below_band_does_not_restart_the_cooldown() -> None:
    """One demotion, one cooldown.

    Re-stamping every turn a heavy model reads below the band would make the cooldown
    unserveable — it would restart faster than it elapses.
    """
    stamped = NOW - timedelta(hours=10)
    heavy = EnforcementState(level=EnforcementLevel.HEAVY, demoted_at=stamped)
    selection = _select(0.10, heavy)
    assert selection.standing.demoted_at == stamped
    assert selection.reason is SelectionReason.BELOW_BAND_HOLD
    assert not selection.changed


# ── AC-1b — the staleness path (codex plan-review finding 1) ─────────────────


def test_staleness_demotion_stamps_cooldown() -> None:
    """Going unmeasured *from light* is a demotion, and stamps the cooldown (AC-1b).

    Without the stamp a model that simply stopped producing recognized spans re-promotes
    the instant it rebuilds a window, having served no cooldown — promotion without
    earning it, arriving through the one transition an ordinary below-band test never
    exercises.
    """
    selection = _select(None, LIGHT_STATE)
    assert selection.applied is EnforcementLevel.HEAVY
    assert selection.reason is SelectionReason.UNMEASURED
    assert selection.standing.demoted_at == NOW
    assert selection.changed


def test_stale_demoted_model_cannot_promote_immediately() -> None:
    """The whole path, end to end: light → stale → heavy → measured high (AC-1b)."""
    went_stale = _select(None, LIGHT_STATE)

    recovered = _select(0.99, went_stale.standing, now=NOW + timedelta(hours=1))
    assert recovered.applied is EnforcementLevel.HEAVY
    assert recovered.reason is SelectionReason.COOLDOWN_HOLD

    later = _select(0.99, went_stale.standing, now=NOW + timedelta(hours=25))
    assert later.applied is EnforcementLevel.LIGHT


def test_unmeasured_while_already_heavy_preserves_the_original_stamp() -> None:
    """A heavy model going stale does not refresh its own cooldown."""
    stamped = NOW - timedelta(hours=10)
    heavy = EnforcementState(level=EnforcementLevel.HEAVY, demoted_at=stamped)
    assert _select(None, heavy).standing.demoted_at == stamped


# ── AC-2 — selection never reads model identity ──────────────────────────────


def test_selection_signature_carries_no_model_identity() -> None:
    """AC-2 made structural: identity is absent from the input, not merely unused.

    "Rename a model and assert its enforcement is unchanged" is guaranteed by a decision
    whose inputs contain no name to read — which is stronger than a test that a name
    present in the input happens not to be branched on today.
    """
    params = set(inspect.signature(select_enforcement).parameters)
    assert params == {"rate", "standing", "band", "now", "rng"}

    for model in (EnforcementBand, EnforcementState):
        fields = set(model.model_fields)
        assert not {f for f in fields if "model" in f or "key" in f or "provider" in f}


def test_renaming_the_model_changes_nothing() -> None:
    """Identical histories under different keys select identically (AC-2)."""
    window = ComplianceWindow(size=100, min_samples=30, max_age=timedelta(hours=336), bar=0.95)
    history = [(index % 10 != 0, index) for index in range(40)]

    selections = []
    for key in ("gemma-3-27b", "renamed-yesterday", "provider/some-other-name"):
        rows = [
            ComplianceObservation(
                model_key=key, observed_at=NOW - timedelta(minutes=offset), compliant=ok
            )
            for ok, offset in history
        ]
        reading = classify(key, rows, window=window, now=NOW)
        selections.append(_select(reading.rate, LIGHT_STATE))

    assert len({(s.applied, s.reason) for s in selections}) == 1


# ── AC-4 — probation ─────────────────────────────────────────────────────────


def test_probation_routes_a_heavy_turn_light_without_promoting_it() -> None:
    """Only the pre-generation forcing is withheld; the standing level does not move."""
    heavy = EnforcementState(level=EnforcementLevel.HEAVY, demoted_at=NOW)
    selection = _select(0.10, heavy, rng=_always_probation())
    assert selection.probation
    assert selection.applied is EnforcementLevel.LIGHT
    assert selection.standing.level is EnforcementLevel.HEAVY


def test_probation_turn_is_an_unconfounded_observation() -> None:
    """AC-4b: the probation turn enters the denominator.

    This is the property that makes probation break the bootstrap deadlock rather than
    merely appear to. ``retrieval_forced`` is what FRE-1284's metric excludes on, so a
    probation turn that reported itself forced would be discarded and the model could
    never accrue the observations promotion requires.
    """
    heavy = EnforcementState(level=EnforcementLevel.HEAVY, demoted_at=NOW)
    probation = _select(0.10, heavy, rng=_always_probation())
    assert not probation.retrieval_forced

    ordinary = _select(0.10, heavy, rng=_never_probation())
    assert ordinary.retrieval_forced


def test_probation_fraction_over_many_turns() -> None:
    """The configured fraction actually occurs (AC-4).

    Asserted as a band around the rate over a seeded run rather than an exact count: the
    draw is Bernoulli per turn, and a test demanding an exact count would be asserting the
    generator's stream rather than the sampler's rate.
    """
    heavy = EnforcementState(level=EnforcementLevel.HEAVY, demoted_at=NOW)
    rng = random.Random(20260828)
    routed_light = sum(
        1 for _ in range(4000) if _select(0.10, heavy, rng=rng).applied is EnforcementLevel.LIGHT
    )
    assert 0.08 < routed_light / 4000 < 0.12


def test_a_light_model_is_never_on_probation() -> None:
    """Probation samples heavy turns; a light model is already on the light path."""
    selection = _select(0.99, LIGHT_STATE, rng=_always_probation())
    assert not selection.probation
    assert selection.applied is EnforcementLevel.LIGHT


# ── AC-5 — the model that complies only when pre-forced ──────────────────────


def test_pre_forced_only_model_settles_on_heavy() -> None:
    """It must never promote, and must not flap (AC-5).

    Simulated as the real loop: heavy turns produce no observation at all (they are
    confounded and never written), probation turns produce a *failing* one, because the
    model complies only when sources were put in its hands. The rate can therefore only
    fall, and the model settles on heavy rather than cycling.
    """
    window = ComplianceWindow(size=100, min_samples=30, max_age=timedelta(hours=336), bar=0.95)
    rng = random.Random(11)
    standing = initial_state()
    observations: list[ComplianceObservation] = []
    standing_levels: list[EnforcementLevel] = []
    unexplained_light = 0

    for turn in range(3000):
        moment = NOW + timedelta(minutes=turn)
        reading = classify("m", observations, window=window, now=moment)
        selection = select_enforcement(
            rate=reading.rate, standing=standing, band=BAND, now=moment, rng=rng
        )
        standing = selection.standing
        standing_levels.append(standing.level)
        if selection.applied is EnforcementLevel.LIGHT and not selection.probation:
            unexplained_light += 1

        if selection.retrieval_forced:
            continue  # confounded — never written, so it cannot inflate the rate
        observations.append(
            ComplianceObservation(model_key="m", observed_at=moment, compliant=False)
        )

    # Never promoted, and never flapped: the standing level held heavy for every one of
    # 3000 turns, so there is no promote/demote cycle to find.
    assert set(standing_levels) == {EnforcementLevel.HEAVY}
    # Every light turn was a probation draw. A light turn with no probation flag would be
    # the model serving an unforced turn it had not earned.
    assert unexplained_light == 0
    assert len(observations) > window.min_samples, "probation must supply enough to be measured"


def test_a_genuinely_compliant_model_does_promote() -> None:
    """The mirror of AC-5 — the mechanism must not simply pin everything heavy."""
    window = ComplianceWindow(size=100, min_samples=30, max_age=timedelta(hours=336), bar=0.95)
    rng = random.Random(12)
    standing = initial_state()
    observations: list[ComplianceObservation] = []

    for turn in range(3000):
        moment = NOW + timedelta(minutes=turn)
        reading = classify("m", observations, window=window, now=moment)
        selection = select_enforcement(
            rate=reading.rate, standing=standing, band=BAND, now=moment, rng=rng
        )
        standing = selection.standing
        if standing.level is EnforcementLevel.LIGHT:
            break
        if not selection.retrieval_forced:
            observations.append(
                ComplianceObservation(model_key="m", observed_at=moment, compliant=True)
            )

    assert standing.level is EnforcementLevel.LIGHT


# ── Band configuration ───────────────────────────────────────────────────────


def test_band_rejects_a_collapsed_or_inverted_band() -> None:
    """Separate thresholds, never one value — ADR-0138 D5, enforced by the type."""
    for promote_at, demote_below in ((0.90, 0.90), (0.80, 0.90)):
        with pytest.raises(ValueError, match="promote_at"):
            EnforcementBand(
                promote_at=promote_at,
                demote_below=demote_below,
                cooldown=timedelta(hours=24),
                probation_rate=0.1,
            )


def test_configured_band_reads_the_pre_registered_settings() -> None:
    """The promote edge is the contract bar itself, not a second copy of it.

    ADR-0138 D5 requires promote ≠ demote, not promote ≠ the contract bar. Reusing
    ``grounding_compliance_bar`` is what stops the promote line drifting away from the
    contract it is supposed to represent.
    """
    from personal_agent.config import settings

    band = configured_band()
    assert band.promote_at == settings.grounding_compliance_bar
    assert band.demote_below == settings.grounding_enforcement_demote_below
    assert band.probation_rate == settings.grounding_enforcement_probation_rate
    assert band.cooldown == timedelta(hours=settings.grounding_enforcement_cooldown_hours)
    assert band.promote_at > band.demote_below


def test_probation_rate_is_high_enough_to_ever_measure_a_model() -> None:
    """The bootstrap must terminate.

    At probation rate p, an unmeasured model needs about ``min_samples / p`` turns
    carrying a non-exempt span before a rate exists at all. A rate low enough to make
    that unreachable is the deadlock D5's probation sampling exists to break.
    """
    from personal_agent.config import settings

    turns_to_first_reading = settings.grounding_compliance_min_samples / (
        settings.grounding_enforcement_probation_rate
    )
    assert turns_to_first_reading <= 500


def test_forced_retrieval_directive_does_not_hand_back_a_claim() -> None:
    """The directive says retrieve first, and asserts nothing about the world.

    It precedes generation, so unlike D4's retry directive it has no blocked claim to
    name — and it must not invent one.
    """
    directive = build_forced_retrieval_directive()
    assert "retriev" in directive.lower()
    assert directive.strip()
