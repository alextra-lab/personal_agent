"""D5's per-model citation-compliance metric (ADR-0138 D5, FRE-1284).

The reading D5's whole design rests on, and the one that turns *"is a 27B primary good
enough?"* from an argument into a number.

**Numerator:** turns in which every non-exempt span carried a citation passing D3(a)(b)(c)
on first generation, with no D4 retry. **Denominator:** turns containing at least one
non-exempt span. Evaluated over a rolling window, with a minimum sample count and a maximum
window age.

**The metric counts only turns where retrieval was not pre-forced**, and that exclusion is
the single most important property here — a blocking finding in ADR review round 2. Heavy
enforcement supplies sources *before* generation, so first-generation compliance measured
under heavy enforcement is largely a measurement of the enforcement, not of the model.
Score those turns and a model that only complies when spoon-fed earns promotion on inflated
numbers, fails under light enforcement, is demoted, recovers under heavy, and oscillates
forever.

**Staleness closes the frozen-denominator hole.** Turns with no non-exempt span never enter
the denominator, so a model that stops producing recognized spans would otherwise coast
indefinitely on a stale favourable window while emitting uncited claims. A window that ages
past its configured maximum without sufficient new observations reverts the model to
*unmeasured* — which under D5 means heavy. Compliance is re-earned, never banked.

This module is **pure**: it computes a reading and decides nothing. What follows from the
reading — light versus heavy, probation, the hysteresis band, cooldown — is D5's other half
and belongs to FRE-1285.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from personal_agent.captains_log.turn_evidence import GroundingRecord


class UnmeasuredReason(StrEnum):
    """Why a model has no rate.

    Kept distinct because the remedies are entirely different: a model that was never
    measured needs turns, while a model whose window went stale *had* turns and stopped
    producing them — which under D5 is itself the signal.
    """

    NO_OBSERVATIONS = "no_observations"
    INSUFFICIENT_SAMPLES = "insufficient_samples"
    STALE_WINDOW = "stale_window"


class ComplianceObservation(BaseModel):
    """One unconfounded turn, reduced to what the metric needs.

    Attributes:
        model_key: The catalog key of the model that actually answered — the deployment
            key, never the role name, since an attachment-routed turn resolves to a
            different model than its role and crediting one model's turns to another is
            how a promotion gets bought with someone else's compliance.
        observed_at: When the turn was verified, timezone-aware. The verification instant,
            not the insertion instant: the write is backgrounded, and a lagged timestamp
            would weaken exactly the staleness guarantee it feeds.
        compliant: Whether every non-exempt span passed on first generation.
    """

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    model_key: str
    observed_at: datetime
    compliant: bool


class ComplianceWindow(BaseModel):
    """The pre-registered parameters a reading is computed under (AC-6).

    Attributes:
        size: Rolling window, in observations.
        min_samples: Below this many *fresh* observations the model is unmeasured.
        max_age: How old an observation may be and still count.
        bar: The rate the contract is read against.
    """

    model_config = ConfigDict(frozen=True)

    size: int
    min_samples: int
    max_age: timedelta
    bar: float


class ModelCompliance(BaseModel):
    """One model's reading.

    Attributes:
        model_key: Which model this describes.
        measured: Whether a rate could be computed at all.
        reason: Why not, when it could not.
        rate: The compliance rate over the fresh window, or None when unmeasured.
        bar: The bar the rate was read against, carried so a stored reading stays
            interpretable after the configured bar changes.
        sample_count: How many observations the rate was computed over — the fresh
            count, which is the population that actually decided it.
        oldest_observed_at: Oldest observation in that population.
        newest_observed_at: Newest observation in that population.
    """

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    model_key: str
    measured: bool
    reason: UnmeasuredReason | None = None
    rate: float | None = None
    bar: float
    sample_count: int = 0
    oldest_observed_at: datetime | None = None
    newest_observed_at: datetime | None = None

    @property
    def meets_bar(self) -> bool:
        """Whether the model's measured rate clears the bar.

        **False when unmeasured**, which is D5's bootstrap stated in code: an unmeasured
        model is heavy, so the fail-safe answer and the "no reading" answer must be the
        same value. Returning None here would push that decision onto every caller, and
        FRE-1285 is the caller that must not get it wrong.
        """
        return self.rate is not None and self.rate >= self.bar


def is_unconfounded_observation(record: GroundingRecord, *, citable: bool) -> bool:
    """Whether one turn's grounding record may enter the metric.

    Four conditions, each of which is a clause of D5 (or, for the fourth, of ADR-0139
    D1 AC-5) rather than a defensive check:

    - **Verification ran.** A denied budget reservation or a broken extractor is a fact
      about Seshat's accounting, not evidence about the model's claim. There is no verdict
      to count. Callers log the skip so a per-model exclusion rate stays observable — an
      exclusion nobody can see is an exclusion nobody can audit.
    - **At least one non-exempt span.** D5's denominator, literally.
    - **Retrieval was not forced.** The round-2 finding. Today the field means "this
      generation followed a D4 retry"; FRE-1285 widens the same field to heavy
      enforcement's pre-generation forcing. Both are confounded and both are excluded.
    - **The turn was citable.** An ``uncitable`` turn — every tool result this turn
      offered was refused — is the system offering nothing to cite from, not the model
      declining to cite. Counting it as a compliance failure is the same confound as
      counting a pre-forced turn as a success, and this is the single place both
      FRE-1284's per-model metric and FRE-1285's enforcement selection would otherwise
      absorb it, since both read the window this predicate gates.

    A **degraded** extraction is deliberately *not* excluded. Degradation fails safe, so it
    can only depress the rate; dropping those turns would be the choice that inflates, and
    an inflated rate is the failure that promotes a model which has not earned it.

    Args:
        record: The turn's grounding record.
        citable: Whether ADR-0139 D1 classified this turn's evidence as ``citable``
            (:func:`~personal_agent.grounding.verification.classify_turn_evidence`).

    Returns:
        Whether this turn is an unconfounded observation of the model.
    """
    return (
        record.available
        and record.non_exempt_count >= 1
        and not record.retrieval_forced
        and citable
    )


def configured_window() -> ComplianceWindow:
    """Return the window parameters from committed configuration.

    Returns:
        The pre-registered window. The values live in ``settings`` — and therefore in git
        history — rather than being passed in at the call site, which is what makes AC-6's
        "recorded before results were seen" checkable by reading the log rather than by
        trusting a claim.
    """
    from personal_agent.config import settings  # noqa: PLC0415

    return ComplianceWindow(
        size=settings.grounding_compliance_window_size,
        min_samples=settings.grounding_compliance_min_samples,
        max_age=timedelta(hours=settings.grounding_compliance_max_window_age_hours),
        bar=settings.grounding_compliance_bar,
    )


def classify(
    model_key: str,
    observations: Sequence[ComplianceObservation],
    *,
    window: ComplianceWindow,
    now: datetime | None = None,
) -> ModelCompliance:
    """Compute one model's compliance reading over its rolling window.

    The order is window, then freshness, then minimum — and the rate is computed over the
    **fresh** population rather than the whole window, because an observation past the
    maximum age must not contribute to a rate any more than it may sustain one.

    Args:
        model_key: The model being read.
        observations: That model's observations, in any order. Sorted here rather than
            trusted from the caller: an index does not define SQL result order, and a
            window silently assembled from the wrong end of the table would be a reading
            nobody could tell was wrong.
        window: The pre-registered parameters.
        now: The instant the window is aged against. Defaults to the current UTC time.

    Returns:
        The reading — a rate, or ``unmeasured`` with the reason.
    """
    moment = now or datetime.now(timezone.utc)

    recent = sorted(observations, key=lambda row: row.observed_at, reverse=True)[: window.size]
    if not recent:
        return ModelCompliance(
            model_key=model_key,
            measured=False,
            reason=UnmeasuredReason.NO_OBSERVATIONS,
            bar=window.bar,
        )

    cutoff = moment - window.max_age
    fresh = [row for row in recent if row.observed_at >= cutoff]

    if len(fresh) < window.min_samples:
        # Which of the two it is turns on whether the window *had* a sample and lost it to
        # age, or never had one. Both are unmeasured; only one says the model went quiet.
        aged_out = len(recent) >= window.min_samples
        return ModelCompliance(
            model_key=model_key,
            measured=False,
            reason=UnmeasuredReason.STALE_WINDOW
            if aged_out
            else UnmeasuredReason.INSUFFICIENT_SAMPLES,
            bar=window.bar,
            sample_count=len(fresh),
            oldest_observed_at=fresh[-1].observed_at if fresh else None,
            newest_observed_at=fresh[0].observed_at if fresh else None,
        )

    compliant = sum(1 for row in fresh if row.compliant)
    return ModelCompliance(
        model_key=model_key,
        measured=True,
        rate=compliant / len(fresh),
        bar=window.bar,
        sample_count=len(fresh),
        oldest_observed_at=fresh[-1].observed_at,
        newest_observed_at=fresh[0].observed_at,
    )


__all__ = [
    "ComplianceObservation",
    "ComplianceWindow",
    "ModelCompliance",
    "UnmeasuredReason",
    "classify",
    "configured_window",
    "is_unconfounded_observation",
]
