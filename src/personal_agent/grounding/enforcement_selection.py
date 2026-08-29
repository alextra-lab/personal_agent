"""D5's enforcement selection — light or heavy, keyed on the measured rate (FRE-1285).

:mod:`personal_agent.grounding.compliance` computes a reading and decides nothing. This is
the other half: what follows from the reading. The contract itself does **not** vary — no
uncited world-fact assertion, identical at 27B and at the frontier — and neither does
verification, which runs inline and blocking on every turn at every level. What varies is
whether retrieval is forced *before* generation.

**Selection reads the rate and nothing else.** Not a model name, not a provider, not a
hand-maintained tier list — that is Option 2 from ADR-0138's rejected alternatives arriving
through the back door, and it would make correctness a function of routing. The guarantee
here is structural rather than disciplinary: :func:`select_enforcement` takes a ``float |
None``, so there is no identity in its input to read.

**Three rules stop it misbehaving, and each closes a specific failure.**

- *Unmeasured means heavy.* Fail-safe. A model nobody has observed pays the strict path
  rather than being trusted on a guess.
- *A hysteresis band, never one value.* A model sitting on the line does not flap.
- *A cooldown after demotion.* Recovery has to be sustained, not instantaneous.

**Every LIGHT → HEAVY transition stamps the cooldown, including the one caused by going
unmeasured.** A model whose window goes stale stopped producing recognized spans; if that
transition left the stamp empty, the model would re-promote the moment it rebuilt a window,
having served no cooldown at all — a promotion nothing earned, arriving through the one
path an ordinary below-band test never exercises. A model that has *never* been light keeps
an empty stamp and is promotable as soon as it is measured, which is the honest reading of
ADR-0138 D5's "a **demoted** model serves a cooldown".

**Probation is what stops the bootstrap deadlocking.** Only unforced turns are measurable,
and an unmeasured model is heavy, so without probation an unmeasured model would never
accrue the observations promotion requires. A configured fraction of a heavy model's turns
therefore run the light path — fully verified like any other, with only the pre-generation
forcing withheld, so a bad output on a probation turn is blocked and never served.

This module is **pure**. Where the level is applied, what heavy does to a turn, and how the
standing level is persisted belong to the executor.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

_RNG = random.Random()
"""The sampler's own generator, so seeding it in a test cannot perturb anything else."""


class EnforcementLevel(StrEnum):
    """Whether retrieval is forced before generation."""

    LIGHT = "light"
    HEAVY = "heavy"


class SelectionReason(StrEnum):
    """Why this turn runs at the level it does.

    Carried so a level is legible from the turn record without re-deriving it from the
    rate — the difference between "held heavy because the rate is bad" and "held heavy
    because the cooldown has not elapsed" is the difference between a model problem and a
    schedule, and a single ``heavy`` cannot tell them apart.
    """

    UNMEASURED = "unmeasured"
    DEMOTED = "demoted"
    BELOW_BAND_HOLD = "below_band_hold"
    BAND_HOLD = "band_hold"
    COOLDOWN_HOLD = "cooldown_hold"
    PROMOTED = "promoted"
    ABOVE_BAND_HOLD = "above_band_hold"


class EnforcementBand(BaseModel):
    """The pre-registered thresholds a selection is made under.

    Attributes:
        promote_at: The rate at or above which a model may be promoted. This is the
            contract bar itself rather than a second copy of it.
        demote_below: The rate under which a model is demoted immediately.
        cooldown: How long a demoted model waits before promotion is eligible again.
        probation_rate: The fraction of a heavy model's turns routed to the light path to
            generate unconfounded observations.
    """

    model_config = ConfigDict(frozen=True)

    promote_at: float = Field(ge=0.0, le=1.0)
    demote_below: float = Field(ge=0.0, le=1.0)
    cooldown: timedelta
    probation_rate: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _band_is_open(self) -> EnforcementBand:
        """Reject a collapsed or inverted band.

        ADR-0138 D5 requires separate promote and demote thresholds, never one value.
        A collapsed band is not a conservative configuration — it is the flapping the
        hysteresis exists to prevent, and it is not detectable from the outside once a
        model starts oscillating across it.

        Returns:
            The validated band.

        Raises:
            ValueError: If ``promote_at`` does not sit strictly above ``demote_below``.
        """
        if self.promote_at <= self.demote_below:
            raise ValueError(
                f"promote_at ({self.promote_at}) must sit strictly above demote_below "
                f"({self.demote_below}): a collapsed band is what flaps."
            )
        return self


class EnforcementState(BaseModel):
    """A model's standing level, and when it was last demoted.

    The two things a rate cannot express. The level, because an in-band reading has to
    hold whatever was already in force; and the demotion instant, because it is the only
    piece of state no later turn can reconstruct from the observations alone.

    Attributes:
        level: The standing level — what applies when this turn is not on probation.
        demoted_at: When the model last went from light to heavy, or ``None`` if it never
            has. A model that has never been light has never been demoted and serves no
            cooldown.
    """

    model_config = ConfigDict(frozen=True)

    level: EnforcementLevel
    demoted_at: datetime | None = None


class EnforcementSelection(BaseModel):
    """What one turn runs under, and what to persist afterwards.

    Attributes:
        applied: The level this turn actually runs at. Differs from ``standing.level``
            exactly when the turn is on probation.
        standing: The post-transition state, to persist when ``changed``.
        reason: Why.
        probation: Whether this heavy turn was routed light to be measurable.
        changed: Whether ``standing`` differs from the state selection was given.
    """

    model_config = ConfigDict(frozen=True)

    applied: EnforcementLevel
    standing: EnforcementState
    reason: SelectionReason
    probation: bool = False
    changed: bool = False

    @property
    def retrieval_forced(self) -> bool:
        """Whether this turn's generation had sources forced on it beforehand.

        The field FRE-1284's metric excludes on, answered from the level that was
        **applied** rather than the one standing. A probation turn is heavy-standing and
        light-applied; reporting it forced would discard the very observation probation
        exists to produce, and the bootstrap would deadlock with the machinery all
        apparently working.
        """
        return self.applied is EnforcementLevel.HEAVY


def initial_state() -> EnforcementState:
    """Return the state a model with no stored row is selected under.

    Returns:
        Heavy, with no cooldown owed. Heavy because unmeasured means heavy; no cooldown
        because nothing has demoted this model — it has simply never been seen.
    """
    return EnforcementState(level=EnforcementLevel.HEAVY, demoted_at=None)


def _transition(
    rate: float | None, standing: EnforcementState, band: EnforcementBand, now: datetime
) -> tuple[EnforcementState, SelectionReason]:
    """Apply the band and the cooldown to one reading.

    Args:
        rate: The measured compliance rate, or ``None`` when unmeasured.
        standing: The state currently in force.
        band: The pre-registered thresholds.
        now: The instant the cooldown is measured against.

    Returns:
        The post-transition state and the reason for it.
    """
    was_light = standing.level is EnforcementLevel.LIGHT

    if rate is None:
        # Unmeasured is heavy. From light this is a demotion like any other and stamps
        # the cooldown; from heavy it changes nothing, and must not refresh a stamp the
        # model is already serving.
        if was_light:
            return (
                EnforcementState(level=EnforcementLevel.HEAVY, demoted_at=now),
                SelectionReason.UNMEASURED,
            )
        return standing, SelectionReason.UNMEASURED

    if rate < band.demote_below:
        if was_light:
            return (
                EnforcementState(level=EnforcementLevel.HEAVY, demoted_at=now),
                SelectionReason.DEMOTED,
            )
        # Already heavy: hold, and leave the stamp alone. Re-stamping every turn a heavy
        # model reads badly would make the cooldown restart faster than it elapses, so it
        # could never be served at all.
        return standing, SelectionReason.BELOW_BAND_HOLD

    if rate >= band.promote_at:
        if was_light:
            return standing, SelectionReason.ABOVE_BAND_HOLD
        if standing.demoted_at is not None and now - standing.demoted_at < band.cooldown:
            return standing, SelectionReason.COOLDOWN_HOLD
        return (
            EnforcementState(level=EnforcementLevel.LIGHT, demoted_at=None),
            SelectionReason.PROMOTED,
        )

    return standing, SelectionReason.BAND_HOLD


def select_enforcement(
    *,
    rate: float | None,
    standing: EnforcementState,
    band: EnforcementBand,
    now: datetime,
    rng: random.Random | None = None,
) -> EnforcementSelection:
    """Choose the enforcement level for one turn.

    Note the signature: the decision's inputs are a rate, a state, a band and an instant.
    There is no model key, provider or tier among them, which is ADR-0138 D5's "keyed on
    the computed rate, never on a model name" expressed as something a reviewer can check
    by reading the parameters rather than by auditing the body.

    Probation is drawn against the **post-transition** standing, so a model demoted on
    this very turn is immediately probation-eligible. That is the intended reading: the
    moment a model becomes heavy is the moment it starts needing unconfounded
    observations to earn its way back.

    Args:
        rate: The measured compliance rate, or ``None`` when the model is unmeasured.
        standing: The state currently in force for this model.
        band: The pre-registered thresholds.
        now: The instant the cooldown is measured against.
        rng: Generator for the probation draw, injectable so the fraction is testable.

    Returns:
        The selection: the level to apply, the state to persist, and why.
    """
    post, reason = _transition(rate, standing, band, now)

    generator = rng or _RNG
    probation = post.level is EnforcementLevel.HEAVY and generator.random() < band.probation_rate

    return EnforcementSelection(
        applied=EnforcementLevel.LIGHT if probation else post.level,
        standing=post,
        reason=reason,
        probation=probation,
        changed=post != standing,
    )


def configured_band() -> EnforcementBand:
    """Return the band from committed configuration.

    ``promote_at`` is ``grounding_compliance_bar`` — the contract bar itself, not a second
    setting holding a copy of it. ADR-0138 D5 requires promote to differ from *demote*,
    not from the contract; a separate promote setting would only create a value that can
    drift away from the bar it is supposed to represent.

    Returns:
        The pre-registered band.

    Raises:
        ValueError: If the configured thresholds do not form an open band.
    """
    from personal_agent.config import settings  # noqa: PLC0415

    return EnforcementBand(
        promote_at=settings.grounding_compliance_bar,
        demote_below=settings.grounding_enforcement_demote_below,
        cooldown=timedelta(hours=settings.grounding_enforcement_cooldown_hours),
        probation_rate=settings.grounding_enforcement_probation_rate,
    )


def build_forced_retrieval_directive() -> str:
    """Return heavy's pre-generation directive.

    The companion to the ``tool_choice`` gate rather than a substitute for it: the gate
    makes retrieval *happen*, and this says what to retrieve for. Unlike D4's retry
    directive this one precedes generation, so there is no blocked claim to name — and it
    must not invent one, since a directive that stated the claim would be handing the
    model its own unsourced assertion back as a premise.

    Returns:
        The directive, for the turn's message list.
    """
    return (
        "Before you answer: retrieve first. Use the retrieval tools available to you to "
        "find sources for whatever this turn requires, then write your answer from what "
        "you retrieved, citing each assertion with the identifier of the source that "
        "supports it.\n\n"
        "Your own background knowledge is not a source. If retrieval turns up nothing "
        "that supports a claim, leave the claim out and say what you searched — that is "
        "a correct answer here, not a failed one."
    )


__all__ = [
    "EnforcementBand",
    "EnforcementLevel",
    "EnforcementSelection",
    "EnforcementState",
    "SelectionReason",
    "build_forced_retrieval_directive",
    "configured_band",
    "initial_state",
    "select_enforcement",
]
