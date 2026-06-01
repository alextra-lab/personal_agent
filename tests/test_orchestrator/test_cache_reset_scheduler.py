"""Tests for the cache-aware compaction scheduler (ADR-0081 §D3, FRE-434).

The scheduler decides *when* to pay the single, amortized compaction reset under
the frozen append-only layout. The decision is the closed-form optimum
``L* = sqrt(2·R/c)`` (R = backend reset cost, c = marginal per-turn hold cost),
floored by an anti-thrash min-run and capped by a hard token ceiling.
"""

from __future__ import annotations

import math

from personal_agent.orchestrator.cache_reset_scheduler import (
    compute_optimal_run_length,
    marginal_hold_cost,
    should_reset,
)


def test_optimal_run_length_closed_form() -> None:
    # L* = sqrt(2 R / c)
    assert compute_optimal_run_length(reset_cost_tokens=8000, marginal_hold_cost=500) == math.sqrt(
        2 * 8000 / 500
    )


def test_optimal_run_length_infinite_when_no_hold_cost() -> None:
    assert compute_optimal_run_length(reset_cost_tokens=8000, marginal_hold_cost=0) == math.inf


def test_marginal_hold_cost_combines_growth_and_quality() -> None:
    # c = Δ_turn + w_q · Q_slope
    assert (
        marginal_hold_cost(delta_turn_tokens=500, quality_slope=0.1, quality_token_weight=4000)
        == 500 + 4000 * 0.1
    )


def test_marginal_hold_cost_ignores_negative_quality_slope() -> None:
    # A negative (improving) slope must not push c below the growth term.
    assert (
        marginal_hold_cost(delta_turn_tokens=500, quality_slope=-0.5, quality_token_weight=4000)
        == 500
    )


def test_token_ceiling_forces_reset_even_below_min_run() -> None:
    d = should_reset(
        turns_since_reset=1,
        accumulated_tokens=50_000,
        accum_max_tokens=48_000,
        min_run_turns=12,
        reset_cost_tokens=8000,
        delta_turn_tokens=500,
    )
    assert d.should_reset is True
    assert d.reason == "token_ceiling"


def test_min_run_floor_prevents_thrash() -> None:
    # Past the optimum but below the anti-thrash floor → hold.
    d = should_reset(
        turns_since_reset=3,
        accumulated_tokens=10_000,
        accum_max_tokens=48_000,
        min_run_turns=12,
        reset_cost_tokens=200,  # tiny R → tiny L*
        delta_turn_tokens=500,
    )
    assert d.should_reset is False
    assert d.reason == "below_min_run"


def test_reset_fires_at_optimum() -> None:
    # L* = sqrt(2*8000/500) ≈ 5.66; with min_run 4, a run of 6 turns resets.
    d = should_reset(
        turns_since_reset=6,
        accumulated_tokens=10_000,
        accum_max_tokens=48_000,
        min_run_turns=4,
        reset_cost_tokens=8000,
        delta_turn_tokens=500,
    )
    assert d.should_reset is True
    assert d.reason == "optimum"


def test_holds_before_optimum() -> None:
    d = should_reset(
        turns_since_reset=5,
        accumulated_tokens=10_000,
        accum_max_tokens=48_000,
        min_run_turns=4,
        reset_cost_tokens=8000,
        delta_turn_tokens=500,
    )
    assert d.should_reset is False
    assert d.reason == "holding"


def test_backend_asymmetry_local_runs_longer_than_cloud() -> None:
    # Same growth; larger reset cost (local) yields a longer optimal run.
    local = compute_optimal_run_length(reset_cost_tokens=8000, marginal_hold_cost=500)
    cloud = compute_optimal_run_length(reset_cost_tokens=1000, marginal_hold_cost=500)
    assert local > cloud
