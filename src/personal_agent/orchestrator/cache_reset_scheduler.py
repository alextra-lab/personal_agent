"""Cache-aware compaction scheduler (ADR-0081 §D3, FRE-434).

Under the frozen append-only layout (§D2) the prompt grows by a known, bounded
increment per turn, so compaction stops being a per-turn cache-buster and becomes
the single, scheduled cache-**reset** event (the sawtooth). This module computes
*when* to pay that one reset, at the cost/quality optimum, backend-aware.

The model (ADR §D3 Decision 3): treat a run of length ``L`` turns as a renewal
cost — one reset ``R_backend`` plus an accumulating linear per-turn hold cost
``c``. The average cost per turn ``A(L) = R/L + c·L/2`` is minimised at the
closed-form optimal run length::

    L* = sqrt(2·R / c)            c = Δ_turn + w_q · Q_slope

where ``Δ_turn`` is the measured per-turn frozen-token increment and
``w_q · Q_slope`` is the staleness quality penalty expressed in token-equivalents
(``Q_slope`` is fit online from the FRE-407 rating trace; it falls back to ``0``
— growth term only — when ratings are sparse). The optimum is floored by an
anti-thrash ``min_run_turns`` and capped by a hard token ceiling.

Backend asymmetry falls out of the formula: a larger ``R_backend`` (local —
any mid-history change forces a full re-prefill) yields a larger ``L*`` (compact
looser, longer runs), while a small ``R_backend`` (cloud — only the rewritten
span re-creates) yields a small ``L*`` (compact tighter, sooner).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

ResetReason = Literal["token_ceiling", "below_min_run", "optimum", "holding"]


@dataclass(frozen=True)
class ResetDecision:
    """Outcome of a scheduler evaluation.

    Attributes:
        should_reset: Whether to fire a compaction reset this turn.
        reason: Why the decision was made (drives telemetry).
        optimal_run_length: The computed ``L*`` for the current cost terms
            (``inf`` when the marginal hold cost is non-positive).
    """

    should_reset: bool
    reason: ResetReason
    optimal_run_length: float


def marginal_hold_cost(
    delta_turn_tokens: float, quality_slope: float, quality_token_weight: float
) -> float:
    """Return the marginal per-turn hold cost ``c = Δ_turn + w_q · Q_slope``.

    A negative ``quality_slope`` (quality improving with context) is clamped to
    zero so it never drives ``c`` below the deterministic growth term.

    Args:
        delta_turn_tokens: Measured per-turn frozen-token increment.
        quality_slope: Quality cost per stale token (tokens-equivalent), fit from
            the FRE-407 trace; ``0`` when data is sparse.
        quality_token_weight: ``w_q`` — token-equivalent of one quality point.

    Returns:
        The marginal hold cost in tokens/turn.
    """
    return delta_turn_tokens + quality_token_weight * max(quality_slope, 0.0)


def compute_optimal_run_length(reset_cost_tokens: float, marginal_hold_cost: float) -> float:
    """Return ``L* = sqrt(2·R / c)``.

    Args:
        reset_cost_tokens: ``R_backend`` — the one-time reset cost in tokens.
        marginal_hold_cost: ``c`` — the marginal per-turn hold cost.

    Returns:
        The optimal run length in turns, or ``inf`` when ``c <= 0`` (no pressure
        to reset other than the hard ceiling).
    """
    if marginal_hold_cost <= 0:
        return math.inf
    return math.sqrt(2.0 * reset_cost_tokens / marginal_hold_cost)


def should_reset(
    *,
    turns_since_reset: int,
    accumulated_tokens: int,
    accum_max_tokens: int,
    min_run_turns: int,
    reset_cost_tokens: float,
    delta_turn_tokens: float,
    quality_slope: float = 0.0,
    quality_token_weight: float = 4000.0,
) -> ResetDecision:
    """Decide whether to fire a compaction reset this turn (ADR §D3 Decision 3).

    Precedence: the hard token ceiling overrides everything; otherwise the
    anti-thrash floor holds until ``min_run_turns``; otherwise reset once the run
    reaches the cost optimum ``L*``.

    Args:
        turns_since_reset: Turns elapsed in the current run (since the last reset
            or session start).
        accumulated_tokens: Current frozen-context token total.
        accum_max_tokens: Hard token ceiling; ``<= 0`` disables it.
        min_run_turns: Anti-thrash floor — never reset before this many turns.
        reset_cost_tokens: ``R_backend`` (local: full re-prefill; cloud: rewritten
            span only).
        delta_turn_tokens: Measured per-turn frozen increment.
        quality_slope: Staleness quality slope from the FRE-407 trace (``0`` when
            sparse — falls back to the token ceiling + growth term alone).
        quality_token_weight: ``w_q`` token-equivalent of one quality point.

    Returns:
        A :class:`ResetDecision`.
    """
    c = marginal_hold_cost(delta_turn_tokens, quality_slope, quality_token_weight)
    l_star = compute_optimal_run_length(reset_cost_tokens, c)

    if accum_max_tokens > 0 and accumulated_tokens >= accum_max_tokens:
        return ResetDecision(True, "token_ceiling", l_star)
    if turns_since_reset < min_run_turns:
        return ResetDecision(False, "below_min_run", l_star)
    if turns_since_reset >= l_star:
        return ResetDecision(True, "optimum", l_star)
    return ResetDecision(False, "holding", l_star)
