"""Unit tests for the FRE-1036 monthly-index migration's pure logic and its mocked-ES reindex/cleanup verification behaviour.

Covers the legacy-index pattern matching, destination-name computation, and
month-end origination-date computation — no Elasticsearch involved for those.
The critical property under test there is that anchored regexes exclude
sibling families (agent-captains-captures-subagents-*, agent-monitors-
joinability-substrate-*) from their parent's pattern, since a wildcard-
pattern reindex source would otherwise sweep sibling documents into the
wrong destination.

The mocked-ES tests cover ``reindex_family``/``cleanup_family``'s
verification logic directly: multiple source indices feed the same monthly
destination, so a per-source count check that compares one source's count
against the destination's already-cumulative total is a rubber stamp once a
few sources have landed — a later source's silent reindex failure goes
undetected because earlier sources' documents already put the destination
count above that one source's own count. Verification must be aggregate
(sum of all a destination's sources vs. that destination's live count,
checked once before any of its sources are deleted), not per-source.
"""

from __future__ import annotations

import argparse
from datetime import timezone
from typing import Any

import pytest
from scripts.migrate_fre1036_monthly_indices import (
    EXCLUDED_PREFIXES,
    FamilyConfig,
    FamilyPlan,
    IncompleteClusterError,
    IncompleteFamilyError,
    SourceMapping,
    _dest_index,
    _list_all_indices,
    _month_end,
    _run,
    assert_cluster_complete,
    assert_family_complete,
    cleanup_family,
    cluster_unaccounted_indices,
    families,
    plan_family,
    reindex_family,
)
from scripts.migrate_fre1036_monthly_indices import _daily_pattern as daily_pattern
from scripts.migrate_fre1036_monthly_indices import _match_legacy as _match


def _cfg(name: str):
    for cfg in families():
        if cfg.name == name:
            return cfg
    raise AssertionError(f"no family config named {name!r}")


class _FakeES:
    """Minimal fake AsyncElasticsearch client for reindex_family/cleanup_family tests.

    ``counts`` is a mutable index-name -> doc-count map the test configures
    up front and the fake's ``reindex``/``indices.delete`` calls mutate, so
    each test controls exactly what a real cluster would report at each step.
    ``reindex_responses`` optionally overrides the auto-derived reindex
    response for a given (source, dest) pair, for simulating a source whose
    reindex reports success but under-counts what actually landed.
    """

    def __init__(
        self,
        counts: dict[str, int],
        reindex_responses: dict[tuple[str, str], dict[str, Any]] | None = None,
    ) -> None:
        self.counts = counts
        self.reindex_responses = reindex_responses or {}
        self.deleted: list[str] = []
        self.origination_dates: dict[str, int] = {}
        self.indices = self._Indices(self)

    async def count(self, index: str) -> dict[str, int]:
        if index not in self.counts:
            raise LookupError(f"no such index: {index}")
        return {"count": self.counts[index]}

    async def reindex(
        self, body: dict[str, Any], wait_for_completion: bool = True, refresh: bool = True
    ) -> dict[str, Any]:
        source = body["source"]["index"]
        dest = body["dest"]["index"]
        override = self.reindex_responses.get((source, dest))
        if override is not None:
            return override
        n = self.counts.get(source, 0)
        self.counts[dest] = self.counts.get(dest, 0) + n
        return {"total": n, "created": n, "updated": 0, "noops": 0, "failures": []}

    class _Indices:
        def __init__(self, parent: "_FakeES") -> None:
            self._parent = parent

        async def put_settings(self, index: str, body: dict[str, Any]) -> None:
            self._parent.origination_dates[index] = body["index.lifecycle.origination_date"]

        async def delete(self, index: str) -> None:
            self._parent.deleted.append(index)
            self._parent.counts.pop(index, None)


class _FakeCatES:
    """Minimal fake AsyncElasticsearch client for cat.indices calls.

    ``live_indices`` is the full set of index names a real cluster would
    report — including sibling-family indices that share a prefix substring,
    already-migrated destinations, and anything genuinely unaccounted for.
    Handles both plan_family's per-family glob (``f"{prefix}-*"``) and
    _list_all_indices' cluster-wide glob (``"*"``), like a real cat.indices
    call would for either pattern.
    """

    def __init__(self, live_indices: list[str]) -> None:
        self._live_indices = live_indices
        self.cat = self._Cat(self)

    class _Cat:
        def __init__(self, parent: "_FakeCatES") -> None:
            self._parent = parent

        async def indices(self, index: str, format: str) -> list[dict[str, str]]:
            if index == "*":
                return [{"index": name} for name in self._parent._live_indices]
            assert index.endswith("-*")
            prefix = index[:-2]
            return [
                {"index": name}
                for name in self._parent._live_indices
                if name.startswith(f"{prefix}-")
            ]


class _FakeFullES(_FakeCatES, _FakeES):
    """Both fakes combined, needed to drive _run() end-to-end.

    _FakeCatES (cat.indices, for plan_family) and _FakeES (count/reindex/
    delete, for reindex_family/cleanup_family) — _run() calls plan_family
    before the mutating calls.
    """

    def __init__(self, live_indices: list[str], counts: dict[str, int]) -> None:
        _FakeCatES.__init__(self, live_indices)
        _FakeES.__init__(self, counts)

    async def close(self) -> None:
        pass


def _plan(cfg_name: str, source_names: list[str]) -> FamilyPlan:
    cfg = _cfg(cfg_name)
    mappings = []
    for name in source_names:
        m = _match(cfg, name)
        assert m is not None, f"{name!r} does not match {cfg_name}'s legacy patterns"
        mappings.append(SourceMapping(source=name, dest=_dest_index(cfg, m), match=m))
    return FamilyPlan(family=cfg_name, mappings=mappings)


@pytest.mark.asyncio
async def test_reindex_family_happy_path_sets_origination_date_once() -> None:
    """Two sources reindex cleanly into one destination; origination_date set once."""
    plan = _plan("agent-logs", ["agent-logs-2026.07.01", "agent-logs-2026.07.02"])
    es = _FakeES(counts={"agent-logs-2026.07.01": 100, "agent-logs-2026.07.02": 50})
    cfg = _cfg("agent-logs")

    ok = await reindex_family(es, cfg, plan)

    assert ok is True
    assert es.counts["agent-logs-2026-07"] == 150
    assert es.origination_dates.keys() == {"agent-logs-2026-07"}


@pytest.mark.asyncio
async def test_reindex_family_flags_reported_failures() -> None:
    """A reindex response carrying failures fails the whole family, not silently."""
    plan = _plan("agent-logs", ["agent-logs-2026.07.01"])
    es = _FakeES(
        counts={"agent-logs-2026.07.01": 100},
        reindex_responses={
            ("agent-logs-2026.07.01", "agent-logs-2026-07"): {
                "total": 100,
                "created": 90,
                "updated": 0,
                "noops": 0,
                "failures": [{"index": "agent-logs-2026-07", "shard": 0, "reason": "boom"}],
            }
        },
    )
    cfg = _cfg("agent-logs")

    ok = await reindex_family(es, cfg, plan)

    assert ok is False


@pytest.mark.asyncio
async def test_reindex_family_aggregate_check_catches_a_later_source_undercount() -> None:
    """The aggregate per-destination check catches a shortfall a per-source check would miss.

    Source B's reindex response reports itself as clean (no failures, landed
    == total_read) but — simulating a real-world class of ES-side
    inconsistency this script cannot see from the response alone — the
    documents never actually land in the shared destination. A per-source
    check comparing B's own count (50) against the destination's cumulative
    total (which already has A's 100 docs, so 100 >= 50) would wrongly pass.
    The aggregate check (100 < 150) must catch it.
    """
    plan = _plan("agent-logs", ["agent-logs-2026.07.01", "agent-logs-2026.07.02"])  # A=100, B=50
    es = _FakeES(
        counts={"agent-logs-2026.07.01": 100, "agent-logs-2026.07.02": 50},
        reindex_responses={
            # B's response claims success but is not reflected in es.counts —
            # i.e. it never actually incremented the shared destination.
            ("agent-logs-2026.07.02", "agent-logs-2026-07"): {
                "total": 50,
                "created": 50,
                "updated": 0,
                "noops": 0,
                "failures": [],
            }
        },
    )
    cfg = _cfg("agent-logs")

    ok = await reindex_family(es, cfg, plan)

    assert ok is False  # aggregate check: dest has only A's 100, expected 150


@pytest.mark.asyncio
async def test_cleanup_family_deletes_when_aggregate_count_sufficient() -> None:
    """All of a destination's sources are deleted once its aggregate count checks out."""
    plan = _plan("agent-logs", ["agent-logs-2026.07.01", "agent-logs-2026.07.02"])
    es = _FakeES(
        counts={
            "agent-logs-2026.07.01": 100,
            "agent-logs-2026.07.02": 50,
            "agent-logs-2026-07": 150,
        }
    )
    cfg = _cfg("agent-logs")

    ok, deleted = await cleanup_family(es, cfg, plan)

    assert ok is True
    assert set(deleted) == {"agent-logs-2026.07.01", "agent-logs-2026.07.02"}


@pytest.mark.asyncio
async def test_cleanup_family_refuses_to_delete_when_aggregate_count_short() -> None:
    """A destination short of its sources' combined count blocks deletion of ALL its sources.

    This is the case a per-source check would miss: neither individual
    source's count exceeds the destination's total, but neither should be
    deleted since the destination is short overall.
    """
    plan = _plan("agent-logs", ["agent-logs-2026.07.01", "agent-logs-2026.07.02"])
    es = _FakeES(
        counts={
            "agent-logs-2026.07.01": 100,
            "agent-logs-2026.07.02": 50,
            "agent-logs-2026-07": 100,  # short by B's 50 — B's data never landed
        }
    )
    cfg = _cfg("agent-logs")

    ok, deleted = await cleanup_family(es, cfg, plan)

    assert ok is False
    assert deleted == []
    assert es.counts["agent-logs-2026.07.01"] == 100  # untouched
    assert es.counts["agent-logs-2026.07.02"] == 50  # untouched


@pytest.mark.asyncio
async def test_cleanup_family_destinations_are_verified_independently() -> None:
    """One destination's shortfall does not block deletion of a separate, healthy destination."""
    cfg = _cfg("agent-logs")
    good = SourceMapping(
        source="agent-logs-2026.07.01",
        dest="agent-logs-2026-07",
        match=_match(cfg, "agent-logs-2026.07.01"),  # type: ignore[arg-type]
    )
    bad = SourceMapping(
        source="agent-logs-2026.08.01",
        dest="agent-logs-2026-08",
        match=_match(cfg, "agent-logs-2026.08.01"),  # type: ignore[arg-type]
    )
    plan = FamilyPlan(family="agent-logs", mappings=[good, bad])
    es = _FakeES(
        counts={
            "agent-logs-2026.07.01": 10,
            "agent-logs-2026-07": 10,  # healthy
            "agent-logs-2026.08.01": 10,
            "agent-logs-2026-08": 0,  # short — never landed
        }
    )

    ok, deleted = await cleanup_family(es, cfg, plan)

    assert ok is False  # the family as a whole is not fully clean
    assert deleted == ["agent-logs-2026.07.01"]  # but the healthy destination still proceeded


@pytest.mark.asyncio
async def test_cleanup_family_deletes_known_empty_deletion_when_verified_empty() -> None:
    """agent-logs-000001 (FRE-1105) deletes cleanly once its live count re-verifies as 0.

    This step is independent of the per-destination reindex flow above — the
    index carries no data to reindex, so there is no source/dest relationship
    for it at all.
    """
    cfg = _cfg("agent-logs")
    plan = FamilyPlan(family="agent-logs", mappings=[], pending_deletions=["agent-logs-000001"])
    es = _FakeES(counts={"agent-logs-000001": 0})

    ok, deleted = await cleanup_family(es, cfg, plan)

    assert ok is True
    assert deleted == ["agent-logs-000001"]
    assert "agent-logs-000001" in es.deleted


@pytest.mark.asyncio
async def test_cleanup_family_handles_mappings_and_pending_deletions_together() -> None:
    """agent-logs' real shape: daily legacy stragglers AND agent-logs-000001 at once.

    The two deletion paths (per-destination reindex-verified sources, and the
    independent known-empty-deletion loop) must both run and neither interferes
    with the other when a family actually has both kinds of work.
    """
    cfg = _cfg("agent-logs")
    plan = _plan("agent-logs", ["agent-logs-2026.07.01"])
    plan.pending_deletions = ["agent-logs-000001"]
    es = _FakeES(
        counts={
            "agent-logs-2026.07.01": 100,
            "agent-logs-2026-07": 100,
            "agent-logs-000001": 0,
        }
    )

    ok, deleted = await cleanup_family(es, cfg, plan)

    assert ok is True
    assert set(deleted) == {"agent-logs-2026.07.01", "agent-logs-000001"}


@pytest.mark.asyncio
async def test_cleanup_family_refuses_known_empty_deletion_when_not_actually_empty() -> None:
    """Config-drift guard: the known-empty label is never trusted without a fresh count."""
    cfg = _cfg("agent-logs")
    plan = FamilyPlan(family="agent-logs", mappings=[], pending_deletions=["agent-logs-000001"])
    es = _FakeES(counts={"agent-logs-000001": 5})

    ok, deleted = await cleanup_family(es, cfg, plan)

    assert ok is False
    assert deleted == []
    assert es.counts["agent-logs-000001"] == 5  # untouched


def test_agent_logs_daily_dotted_matches_and_maps_to_monthly_dash() -> None:
    """A daily dotted agent-logs index maps to its monthly dash destination."""
    cfg = _cfg("agent-logs")
    m = _match(cfg, "agent-logs-2026.02.22")
    assert m is not None
    assert _dest_index(cfg, m) == "agent-logs-2026-02"


def test_agent_logs_v2_suffix_matches_same_destination() -> None:
    """A -v2 reindex-artifact index consolidates into the same monthly bucket."""
    cfg = _cfg("agent-logs")
    m = _match(cfg, "agent-logs-2026.04.15-v2")
    assert m is not None
    assert _dest_index(cfg, m) == "agent-logs-2026-04"


def test_agent_logs_pattern_does_not_match_already_monthly() -> None:
    """An already-migrated monthly index is not re-matched as a legacy source."""
    cfg = _cfg("agent-logs")
    assert _match(cfg, "agent-logs-2026-02") is None


def test_captains_captures_pattern_excludes_subagents_sibling() -> None:
    """The anchored parent pattern must not match the subagents family's indices."""
    cfg = _cfg("agent-captains-captures")
    assert _match(cfg, "agent-captains-captures-2026-02-22") is not None
    assert _match(cfg, "agent-captains-captures-subagents-2026-02-22") is None


def test_captains_captures_subagents_matches_its_own_pattern() -> None:
    """The subagents family's own pattern matches its own daily indices."""
    cfg = _cfg("agent-captains-captures-subagents")
    m = _match(cfg, "agent-captains-captures-subagents-2026-02-22")
    assert m is not None
    assert _dest_index(cfg, m) == "agent-captains-captures-subagents-2026-02"


def test_joinability_pattern_excludes_substrate_sibling() -> None:
    """The anchored parent pattern must not match the substrate family's indices."""
    cfg = _cfg("agent-monitors-joinability")
    assert _match(cfg, "agent-monitors-joinability-2026.07.31") is not None
    assert _match(cfg, "agent-monitors-joinability-substrate-2026.07.31") is None


def test_joinability_substrate_matches_its_own_pattern() -> None:
    """The substrate family's own pattern matches its own daily indices."""
    cfg = _cfg("agent-monitors-joinability-substrate")
    m = _match(cfg, "agent-monitors-joinability-substrate-2026.07.31")
    assert m is not None
    assert _dest_index(cfg, m) == "agent-monitors-joinability-substrate-2026-07"


def test_slm_health_matches_both_monthly_and_daily_patterns() -> None:
    """FRE-1105: slm-health carries dotted monthly AND dotted daily legacy indices.

    Before FRE-1105 this family carried only the monthly pattern, so its daily
    stragglers matched nothing and were silently orphaned — this is the exact
    defect the ticket reports (10 orphaned daily indices found on the live
    cluster). A daily-suffixed name must now match, via the daily pattern.
    """
    cfg = _cfg("agent-monitors-slm-health")
    m_monthly = _match(cfg, "agent-monitors-slm-health-2026.06")
    assert m_monthly is not None
    assert _dest_index(cfg, m_monthly) == "agent-monitors-slm-health-2026-06"

    m_daily = _match(cfg, "agent-monitors-slm-health-2026.06.15")
    assert m_daily is not None
    assert _dest_index(cfg, m_daily) == "agent-monitors-slm-health-2026-06"


def test_user_turn_ratings_matches_both_monthly_and_daily_patterns() -> None:
    """FRE-1105: user-turn-ratings carries dotted monthly AND dotted daily legacy indices.

    Same defect class as slm-health (7 orphaned daily indices found on the
    live cluster) — see test_slm_health_matches_both_monthly_and_daily_patterns.
    """
    cfg = _cfg("user-turn-ratings")
    m_monthly = _match(cfg, "user-turn-ratings-2026.06")
    assert m_monthly is not None
    assert _dest_index(cfg, m_monthly) == "user-turn-ratings-2026-06"

    m_daily = _match(cfg, "user-turn-ratings-2026.05.31")
    assert m_daily is not None
    assert _dest_index(cfg, m_daily) == "user-turn-ratings-2026-05"


def test_insights_pattern_only_matches_pre_cutover_daily_stragglers() -> None:
    """agent-insights already writes monthly-dash; only the daily orphans match."""
    cfg = _cfg("agent-insights")
    m = _match(cfg, "agent-insights-2026-04-17")
    assert m is not None
    assert _dest_index(cfg, m) == "agent-insights-2026-04"
    # The current, correct monthly format must not match (nothing to migrate).
    assert _match(cfg, "agent-insights-2026-04") is None


# Completeness assertion (FRE-1105) — every live index under a family's prefix
# must land in mappings, pending_deletions, an existing destination, or a
# registered sibling family; anything left over is the silent-orphan defect.


@pytest.mark.asyncio
async def test_plan_family_flags_a_genuinely_unaccounted_index() -> None:
    """A stray index matching no pattern, no destination shape, no exclusion, is unaccounted."""
    cfg = _cfg("agent-logs")
    es = _FakeCatES(
        [
            "agent-logs-2026.07.01",  # matched legacy source
            "agent-logs-2026-07",  # existing destination
            "agent-logs-mystery-index",  # genuinely unaccounted
        ]
    )

    plan = await plan_family(es, cfg)

    assert plan.unaccounted == ["agent-logs-mystery-index"]
    with pytest.raises(IncompleteFamilyError):
        assert_family_complete(plan)


@pytest.mark.asyncio
async def test_plan_family_excludes_sibling_family_indices_from_unaccounted() -> None:
    """A registered sibling family's own indices must not count as the parent's orphans.

    cat.indices(index="agent-captains-captures-*") also returns
    agent-captains-captures-subagents-* indices on a real cluster — those
    belong to, and are accounted for by, the subagents family's own
    plan_family call, not the parent's.
    """
    cfg = _cfg("agent-captains-captures")
    es = _FakeCatES(
        [
            "agent-captains-captures-2026-02-22",
            "agent-captains-captures-subagents-2026-02-22",
        ]
    )

    plan = await plan_family(es, cfg)

    assert plan.unaccounted == []
    assert_family_complete(plan)  # must not raise


@pytest.mark.asyncio
async def test_plan_family_puts_known_empty_deletion_in_pending_not_unaccounted() -> None:
    """agent-logs-000001 is accounted for as a pending deletion, not an orphan."""
    cfg = _cfg("agent-logs")
    es = _FakeCatES(["agent-logs-000001"])

    plan = await plan_family(es, cfg)

    assert plan.pending_deletions == ["agent-logs-000001"]
    assert plan.unaccounted == []
    assert_family_complete(plan)  # must not raise


def test_assert_family_complete_is_a_noop_when_nothing_unaccounted() -> None:
    """A plan with no unaccounted indices never raises."""
    plan = FamilyPlan(family="agent-logs", mappings=[], unaccounted=[])
    assert_family_complete(plan)  # does not raise


@pytest.mark.asyncio
async def test_run_one_broken_family_does_not_abort_family_all(monkeypatch) -> None:
    """FRE-1105: an IncompleteFamilyError in one family must not abort --family all.

    Two families: one has a genuinely unaccounted index (broken), one is clean
    (healthy). A `delta` run over "all" must still reindex the healthy family's
    source even though the broken family fails.
    """
    broken = FamilyConfig("broken-family", "broken-family", (daily_pattern("broken-family", "-"),))
    healthy = FamilyConfig(
        "healthy-family", "healthy-family", (daily_pattern("healthy-family", "-"),)
    )
    monkeypatch.setattr(
        "scripts.migrate_fre1036_monthly_indices.families", lambda: [broken, healthy]
    )
    es = _FakeFullES(
        live_indices=[
            "broken-family-2026-02-22",  # matched
            "broken-family-mystery",  # unaccounted -> IncompleteFamilyError
            "healthy-family-2026-02-22",  # matched, clean
        ],
        counts={"broken-family-2026-02-22": 5, "healthy-family-2026-02-22": 5},
    )
    monkeypatch.setattr("elasticsearch.AsyncElasticsearch", lambda *a, **k: es)
    args = argparse.Namespace(command="delta", family="all", confirm_prod=True)

    exit_code = await _run(args)

    assert exit_code == 3  # the broken family failed
    # the healthy family was still processed, not skipped:
    assert es.counts.get("healthy-family-2026-02") == 5


# Real-cluster-shape regression tests (FRE-1105) — the exact index names found
# live on the cluster on 2026-08-06 via `plan --confirm-prod`, not a synthetic
# minimal case. Before the fix, every daily-dotted name below matched nothing
# (both families carried only their monthly pattern) and was silently
# unaccounted — this reproduces the ticket's own reported defect directly.


@pytest.mark.asyncio
async def test_slm_health_real_cluster_shape_has_zero_unaccounted_after_fix() -> None:
    """agent-monitors-slm-health: 12 daily orphans + 3 already-migrated monthly destinations."""
    cfg = _cfg("agent-monitors-slm-health")
    es = _FakeCatES(
        [
            "agent-monitors-slm-health-2026.06.02",
            "agent-monitors-slm-health-2026.06.03",
            "agent-monitors-slm-health-2026.06.04",
            "agent-monitors-slm-health-2026.06.05",
            "agent-monitors-slm-health-2026.06.06",
            "agent-monitors-slm-health-2026.06.07",
            "agent-monitors-slm-health-2026.06.08",
            "agent-monitors-slm-health-2026.06.09",
            "agent-monitors-slm-health-2026.06.10",
            "agent-monitors-slm-health-2026.06.11",
            "agent-monitors-slm-health-2026.06.12",
            "agent-monitors-slm-health-2026.06.13",
            "agent-monitors-slm-health-2026-06",
            "agent-monitors-slm-health-2026-07",
            "agent-monitors-slm-health-2026-08",
        ]
    )

    plan = await plan_family(es, cfg)

    assert len(plan.mappings) == 12
    assert plan.unaccounted == []
    assert_family_complete(plan)  # must not raise


@pytest.mark.asyncio
async def test_user_turn_ratings_real_cluster_shape_has_zero_unaccounted_after_fix() -> None:
    """user-turn-ratings: 4 not-yet-migrated monthly sources + 7 daily orphans + 2 destinations."""
    cfg = _cfg("user-turn-ratings")
    es = _FakeCatES(
        [
            "user-turn-ratings-2026.04",
            "user-turn-ratings-2026.05",
            "user-turn-ratings-2026.06",
            "user-turn-ratings-2026.07",
            "user-turn-ratings-2026.05.31",
            "user-turn-ratings-2026.06.01",
            "user-turn-ratings-2026.06.02",
            "user-turn-ratings-2026.06.04",
            "user-turn-ratings-2026.06.05",
            "user-turn-ratings-2026.06.06",
            "user-turn-ratings-2026.06.07",
            "user-turn-ratings-2026-07",
            "user-turn-ratings-2026-08",
        ]
    )

    plan = await plan_family(es, cfg)

    assert len(plan.mappings) == 11  # 4 monthly + 7 daily
    assert plan.unaccounted == []
    assert_family_complete(plan)  # must not raise


# Cluster-level completeness (FRE-1105 master-gate finding, PR #848): the
# per-family check only ever sees families() already knows about. This is
# the same check one level up — over the registry itself, not within one
# family — catching a family whose "nothing to migrate" exclusion has gone
# silently stale (agent-topology and agent-monitors-projector-health both
# held zero live indices at authoring time and now hold dozens, confirmed
# live on the real cluster 2026-08-06).


def test_cluster_unaccounted_indices_flags_a_prefix_with_no_configured_family() -> None:
    """agent-topology has no FamilyConfig and no EXCLUDED_PREFIXES entry — it must surface."""
    live = [
        "agent-logs-2026-07",  # configured family's own destination
        "agent-topology-2026-07-07",  # genuinely unaccounted
    ]

    unaccounted = cluster_unaccounted_indices(live)

    assert unaccounted == ["agent-topology-2026-07-07"]
    with pytest.raises(IncompleteClusterError):
        assert_cluster_complete(unaccounted)


def test_cluster_unaccounted_indices_respects_excluded_prefixes() -> None:
    """caddy-access and slm-requests are declared out of scope — must not surface."""
    live = ["caddy-access-2026-08", "slm-requests-2026.07.20"]

    unaccounted = cluster_unaccounted_indices(live)

    assert unaccounted == []
    assert_cluster_complete(unaccounted)  # must not raise


def test_cluster_unaccounted_indices_recognizes_every_configured_family() -> None:
    """One representative live index per configured family — none are unaccounted."""
    live = [f"{cfg.dest_prefix}-2026-07" for cfg in families()]

    unaccounted = cluster_unaccounted_indices(live)

    assert unaccounted == []


def test_excluded_prefixes_do_not_shadow_a_configured_family() -> None:
    """EXCLUDED_PREFIXES and families() must not name overlapping prefixes."""
    family_prefixes = {cfg.dest_prefix for cfg in families()}
    assert family_prefixes.isdisjoint(EXCLUDED_PREFIXES)


@pytest.mark.asyncio
async def test_list_all_indices_excludes_dot_prefixed_system_indices() -> None:
    """ES system indices (.kibana, .security-7, etc.) are never cluster-unaccounted candidates."""
    es = _FakeCatES([".kibana", ".security-7", "agent-logs-2026-07"])

    live = await _list_all_indices(es)

    assert live == ["agent-logs-2026-07"]


def test_assert_cluster_complete_is_a_noop_when_nothing_unaccounted() -> None:
    """An empty unaccounted list never raises."""
    assert_cluster_complete([])  # does not raise


# Real-cluster-shape regression test (FRE-1105 master-gate finding) — one
# representative index per prefix actually found live on the cluster
# 2026-08-06 (13 prefixes total: 9 configured families + 2 excluded +
# agent-topology + agent-monitors-projector-health), not a synthetic case.


def test_cluster_real_shape_flags_exactly_the_two_drifted_prefixes() -> None:
    """One representative index per real live prefix; only the two drifted ones surface."""
    live = [
        "agent-captains-captures-2026-07-01",
        "agent-captains-captures-subagents-2026-02-22",
        "agent-captains-reflections-2026-07",
        "agent-insights-2026-04",
        "agent-logs-2026-07",
        "agent-monitors-joinability-2026-07",
        "agent-monitors-joinability-substrate-2026-07",
        "agent-monitors-slm-health-2026-08",
        "user-turn-ratings-2026-08",
        "caddy-access-2026-08",
        "slm-requests-2026.08.06",
        "agent-topology-2026-08",
        "agent-monitors-projector-health-2026-08",
    ]

    unaccounted = cluster_unaccounted_indices(live)

    assert unaccounted == [
        "agent-monitors-projector-health-2026-08",
        "agent-topology-2026-08",
    ]


def test_month_end_mid_year() -> None:
    """Month-end lands on the last calendar day at 23:59:59.999 UTC."""
    end = _month_end("2026", "02")
    assert end.year == 2026
    assert end.month == 2
    assert end.day == 28  # 2026 is not a leap year
    assert end.tzinfo is timezone.utc


def test_month_end_december_rolls_into_next_year() -> None:
    """December's month-end correctly stays in December, not January."""
    end = _month_end("2026", "12")
    assert end.year == 2026
    assert end.month == 12
    assert end.day == 31
    assert end.hour == 23
    assert end.minute == 59
