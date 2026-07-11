"""Tests for the ADR-0114 τ_merge sweep driver (FRE-842).

Unit-level: hand-built ledger fixtures with known answers + a mocked Neo4j
session for the two read-only fetch functions. No real infra (see
`test_sweep_integration.py` for the live-sandbox smoke run).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from scripts.study.sweep import (
    AssertionRecord,
    CurvePoint,
    build_checkpoint_snapshots,
    build_snapshot,
    category_count_curve,
    chronological_episode_order,
    curve_from_checkpoints,
    discover_seeds,
    distinctness_check,
    fetch_seeded_ledger,
    non_collapse_check,
    permuted_orders,
    plateau_check,
    stochastic_stability_check,
    top20_and_tail_tables,
)


class _FakeResult:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[dict[str, Any]]:
        for record in self._records:
            yield record


class _FakeSession:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, query: str, parameters: dict[str, Any] | None = None) -> _FakeResult:
        self.calls.append((query, parameters or {}))
        return _FakeResult(self._records)

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakeDriver:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def session(self) -> _FakeSession:
        return self._session


def _record(
    *,
    concept_id: str,
    category: str,
    episode_id: str,
    when: datetime,
    seed: int = 0,
    confidence: float = 0.8,
) -> AssertionRecord:
    return AssertionRecord(
        concept_id=concept_id,
        category_normalized_name=category,
        category_display_name=category,
        proposed_confidence=confidence,
        episode_id=episode_id,
        when=when,
        seed=seed,
    )


_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# fetch_seeded_ledger / discover_seeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_seeded_ledger_parses_rows() -> None:
    session = _FakeSession(
        [
            {
                "concept_id": "c1",
                "category_normalized_name": "x",
                "category_display_name": "X",
                "proposed_confidence": 0.7,
                "episode_id": "ep1",
                "when": _T0.isoformat(),
                "seed": 0,
            }
        ]
    )
    driver = _FakeDriver(session)

    ledger = await fetch_seeded_ledger(driver, seed=0)

    assert len(ledger) == 1
    assert ledger[0].concept_id == "c1"
    assert ledger[0].seed == 0
    assert session.calls[0][1]["seed"] == 0


@pytest.mark.asyncio
async def test_discover_seeds_returns_distinct_sorted_seeds() -> None:
    session = _FakeSession([{"seed": 1}, {"seed": 0}])
    driver = _FakeDriver(session)

    seeds = await discover_seeds(driver)

    assert seeds == [0, 1]


# ---------------------------------------------------------------------------
# chronological_episode_order / permuted_orders
# ---------------------------------------------------------------------------


def test_chronological_order_sorts_by_earliest_assertion() -> None:
    ledger = [
        _record(concept_id="c1", category="x", episode_id="ep2", when=_T0 + timedelta(hours=2)),
        _record(concept_id="c1", category="y", episode_id="ep1", when=_T0),
        _record(concept_id="c2", category="x", episode_id="ep1", when=_T0 + timedelta(minutes=1)),
    ]

    order = chronological_episode_order(ledger)

    assert order == ["ep1", "ep2"]


def test_permuted_orders_are_deterministic_for_same_seed() -> None:
    episode_ids = [f"ep{i}" for i in range(10)]

    first = permuted_orders(episode_ids, n_permutations=2, seed=42)
    second = permuted_orders(episode_ids, n_permutations=2, seed=42)

    assert first == second


def test_permuted_orders_differ_from_each_other() -> None:
    episode_ids = [f"ep{i}" for i in range(10)]

    orders = permuted_orders(episode_ids, n_permutations=2, seed=42)

    assert orders[0] != orders[1]
    assert sorted(orders[0]) == sorted(episode_ids)


def test_permuted_orders_count_matches_n_permutations() -> None:
    episode_ids = [f"ep{i}" for i in range(5)]

    orders = permuted_orders(episode_ids, n_permutations=3, seed=1)

    assert len(orders) == 3


# ---------------------------------------------------------------------------
# build_snapshot
# ---------------------------------------------------------------------------


def test_build_snapshot_groups_by_category_within_included_episodes() -> None:
    ledger = [
        _record(concept_id="c1", category="x", episode_id="ep1", when=_T0),
        _record(concept_id="c2", category="x", episode_id="ep1", when=_T0),
        _record(concept_id="c3", category="x", episode_id="ep2", when=_T0),
    ]

    snapshot = build_snapshot(ledger, {"ep1"})

    assert set(snapshot) == {"x"}
    assert snapshot["x"].concept_ids == frozenset({"c1", "c2"})


# ---------------------------------------------------------------------------
# category_count_curve
# ---------------------------------------------------------------------------


def test_curve_reports_raw_and_canonical_counts_per_checkpoint() -> None:
    ledger = [
        _record(concept_id="c1", category="a", episode_id="ep1", when=_T0),
        _record(concept_id="c1", category="b", episode_id="ep2", when=_T0),  # a,b share c1
        _record(concept_id="c2", category="c", episode_id="ep3", when=_T0),  # c is unrelated
    ]
    order = ["ep1", "ep2", "ep3"]

    curve = category_count_curve(
        ledger, order, tau_merge=0.3, checkpoint_every=1, top_k=10, min_jaccard=0.5
    )

    assert [p.conversations_processed for p in curve] == [1, 2, 3]
    # after ep1+ep2: categories a,b share concept c1 fully -> jaccard 1.0 -> merges at tau_merge 0.3
    assert curve[1].raw_category_count == 2
    assert curve[1].canonical_category_count == 1
    # ep3 adds a disjoint category c -> raw 3, canonical 2 (a/b merged, c distinct)
    assert curve[2].raw_category_count == 3
    assert curve[2].canonical_category_count == 2


def test_build_checkpoint_snapshots_is_reusable_across_a_tau_merge_grid() -> None:
    """Regression for the code-review finding (FRE-842): Stage 1 (snapshot +
    candidate generation) must be computed once and reused across every
    τ_merge in a grid, not recomputed per config — `curve_from_checkpoints`
    only re-runs Stage 2 (canonicalize) per τ_merge.
    """
    ledger = [
        _record(concept_id="c1", category="a", episode_id="ep1", when=_T0),
        _record(concept_id="c1", category="b", episode_id="ep2", when=_T0),
        _record(concept_id="c4", category="b", episode_id="ep2", when=_T0),  # b={c1,c4}
        _record(concept_id="c2", category="c", episode_id="ep3", when=_T0),
    ]
    order = ["ep1", "ep2", "ep3"]

    checkpoints = build_checkpoint_snapshots(
        ledger, order, checkpoint_every=1, top_k=10, min_jaccard=0.1
    )
    curve_low, final_canon_low, final_memberships_low = curve_from_checkpoints(
        checkpoints, tau_merge=0.3
    )
    curve_high, final_canon_high, _ = curve_from_checkpoints(checkpoints, tau_merge=0.99)

    # Same checkpoints object reused for both -- Stage 1 ran exactly once.
    assert len(checkpoints) == 3
    # jaccard(a,b) = |{c1}]/|{c1,c4}| = 0.5: low tau_merge (0.3) merges a/b,
    # high tau_merge (0.99) does not.
    assert curve_low[-1].canonical_category_count == 2
    assert curve_high[-1].canonical_category_count == 3
    assert final_canon_low is not None
    assert final_canon_high is not None
    assert final_memberships_low is not None
    assert final_canon_low.canonical_of["a"] == final_canon_low.canonical_of["b"]
    assert final_canon_high.canonical_of["a"] != final_canon_high.canonical_of["b"]


def test_curve_from_checkpoints_empty_input_returns_none_final_state() -> None:
    curve, final_canon, final_memberships = curve_from_checkpoints([], tau_merge=0.5)

    assert curve == []
    assert final_canon is None
    assert final_memberships is None


# ---------------------------------------------------------------------------
# plateau_check / non_collapse_check
# ---------------------------------------------------------------------------


def test_plateau_check_passes_for_a_curve_that_flattens() -> None:

    # 9 checkpoints; canonical count grows fast early, barely at all late.
    curve = [
        CurvePoint(conversations_processed=n, raw_category_count=n, canonical_category_count=count)
        for n, count in zip(range(1, 10), [1, 2, 3, 4, 4, 4, 4, 4, 4], strict=True)
    ]

    result = plateau_check(curve)

    assert result.passes is True


def test_plateau_check_fails_for_a_curve_that_grows_linearly() -> None:

    curve = [
        CurvePoint(conversations_processed=n, raw_category_count=n, canonical_category_count=n)
        for n in range(1, 10)
    ]

    result = plateau_check(curve)

    assert result.passes is False


def test_plateau_check_refuses_overlapping_tertile_windows_at_n_equals_3() -> None:
    """Regression for the code-review-caught off-by-one (FRE-842): at n=3,
    tertile_size=1 makes the first window curve[:2] and the final window
    curve[-2:] share index 1 unless the guard rejects the curve outright.
    Directly reachable via the README-documented 6-episode seed-1 ledger with
    `--checkpoint-every 2` (3 checkpoints).
    """
    curve = [
        CurvePoint(conversations_processed=2, raw_category_count=2, canonical_category_count=1),
        CurvePoint(conversations_processed=4, raw_category_count=4, canonical_category_count=3),
        CurvePoint(conversations_processed=6, raw_category_count=6, canonical_category_count=3),
    ]

    result = plateau_check(curve)

    assert result.passes is False
    assert result.first_tertile_rate == 0.0
    assert result.final_tertile_rate == 0.0


def test_non_collapse_check_respects_floor() -> None:

    curve = [
        CurvePoint(conversations_processed=10, raw_category_count=10, canonical_category_count=2)
    ]

    assert non_collapse_check(curve, floor=3) is False
    assert non_collapse_check(curve, floor=2) is True


# ---------------------------------------------------------------------------
# distinctness_check
# ---------------------------------------------------------------------------


def test_distinctness_check_flags_overlapping_canonical_groups() -> None:
    from scripts.study.consolidator import CategoryMembers, canonicalize

    memberships = {
        "a": CategoryMembers("a", "a", frozenset({"c1", "c2", "c3"})),
        "b": CategoryMembers(
            "b", "b", frozenset({"c1", "c2", "c4"})
        ),  # high overlap with a, but not merged
        "c": CategoryMembers("c", "c", frozenset({"c9"})),
    }
    result_canon = canonicalize(memberships, [], tau_merge=0.99)  # no candidates -> nothing merges

    result = distinctness_check(memberships, result_canon, overlap_ceiling=0.5)

    assert result.pct_exceeding_ceiling > 0.0
    assert len(result.overlap_histogram) == 3  # 3 pairs among 3 groups


def test_distinctness_check_passes_when_groups_are_disjoint() -> None:
    from scripts.study.consolidator import CategoryMembers, canonicalize

    memberships = {
        "a": CategoryMembers("a", "a", frozenset({"c1"})),
        "b": CategoryMembers("b", "b", frozenset({"c2"})),
    }
    result_canon = canonicalize(memberships, [], tau_merge=0.99)

    result = distinctness_check(memberships, result_canon, overlap_ceiling=0.1)

    assert result.pct_exceeding_ceiling == 0.0


# ---------------------------------------------------------------------------
# stochastic_stability_check
# ---------------------------------------------------------------------------


def test_stability_check_reports_insufficient_seeds_with_one_seed() -> None:

    curves_by_seed = {0: [CurvePoint(1, 1, 1)]}

    result = stochastic_stability_check(curves_by_seed, variance_bound=1.0)

    assert result.passes is None
    assert result.n_seeds == 1


def test_stability_check_passes_within_bound() -> None:

    curves_by_seed = {
        0: [CurvePoint(10, 10, 5)],
        1: [CurvePoint(10, 10, 5)],
        2: [CurvePoint(10, 10, 6)],
    }

    result = stochastic_stability_check(curves_by_seed, variance_bound=1.0)

    assert result.passes is True
    assert result.n_seeds == 3


def test_stability_check_fails_outside_bound() -> None:

    curves_by_seed = {
        0: [CurvePoint(10, 10, 2)],
        1: [CurvePoint(10, 10, 40)],
    }

    result = stochastic_stability_check(curves_by_seed, variance_bound=1.0)

    assert result.passes is False


# ---------------------------------------------------------------------------
# top20_and_tail_tables
# ---------------------------------------------------------------------------


def test_top20_and_tail_tables_are_sorted_and_disjoint() -> None:
    from scripts.study.consolidator import CategoryMembers, canonicalize

    memberships = {
        f"cat{i}": CategoryMembers(f"cat{i}", f"cat{i}", frozenset(f"c{j}" for j in range(i)))
        for i in range(1, 31)
    }
    result_canon = canonicalize(memberships, [], tau_merge=0.99)

    tables = top20_and_tail_tables(memberships, result_canon, tail_sample_size=5, seed=0)

    assert len(tables["top20"]) == 20
    top_names = {row["normalized_name"] for row in tables["top20"]}
    tail_names = {row["normalized_name"] for row in tables["tail_sample"]}
    assert top_names.isdisjoint(tail_names)
    assert len(tables["tail_sample"]) == 5
    # top20 must actually be the 20 largest
    assert min(row["member_count"] for row in tables["top20"]) >= max(
        row["member_count"] for row in tables["tail_sample"]
    )
