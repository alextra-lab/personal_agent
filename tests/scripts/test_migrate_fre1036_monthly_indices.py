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

from datetime import timezone
from typing import Any

import pytest
from scripts.migrate_fre1036_monthly_indices import (
    FamilyPlan,
    SourceMapping,
    _dest_index,
    _month_end,
    cleanup_family,
    families,
    reindex_family,
)


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


def _plan(cfg_name: str, source_names: list[str]) -> FamilyPlan:
    cfg = _cfg(cfg_name)
    mappings = []
    for name in source_names:
        m = cfg.legacy_pattern.match(name)
        assert m is not None, f"{name!r} does not match {cfg_name}'s legacy pattern"
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
        match=cfg.legacy_pattern.match("agent-logs-2026.07.01"),  # type: ignore[arg-type]
    )
    bad = SourceMapping(
        source="agent-logs-2026.08.01",
        dest="agent-logs-2026-08",
        match=cfg.legacy_pattern.match("agent-logs-2026.08.01"),  # type: ignore[arg-type]
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


def _cfg(name: str):
    for cfg in families():
        if cfg.name == name:
            return cfg
    raise AssertionError(f"no family config named {name!r}")


def test_agent_logs_daily_dotted_matches_and_maps_to_monthly_dash() -> None:
    """A daily dotted agent-logs index maps to its monthly dash destination."""
    cfg = _cfg("agent-logs")
    m = cfg.legacy_pattern.match("agent-logs-2026.02.22")
    assert m is not None
    assert _dest_index(cfg, m) == "agent-logs-2026-02"


def test_agent_logs_v2_suffix_matches_same_destination() -> None:
    """A -v2 reindex-artifact index consolidates into the same monthly bucket."""
    cfg = _cfg("agent-logs")
    m = cfg.legacy_pattern.match("agent-logs-2026.04.15-v2")
    assert m is not None
    assert _dest_index(cfg, m) == "agent-logs-2026-04"


def test_agent_logs_pattern_does_not_match_already_monthly() -> None:
    """An already-migrated monthly index is not re-matched as a legacy source."""
    cfg = _cfg("agent-logs")
    assert cfg.legacy_pattern.match("agent-logs-2026-02") is None


def test_captains_captures_pattern_excludes_subagents_sibling() -> None:
    """The anchored parent pattern must not match the subagents family's indices."""
    cfg = _cfg("agent-captains-captures")
    assert cfg.legacy_pattern.match("agent-captains-captures-2026-02-22") is not None
    assert cfg.legacy_pattern.match("agent-captains-captures-subagents-2026-02-22") is None


def test_captains_captures_subagents_matches_its_own_pattern() -> None:
    """The subagents family's own pattern matches its own daily indices."""
    cfg = _cfg("agent-captains-captures-subagents")
    m = cfg.legacy_pattern.match("agent-captains-captures-subagents-2026-02-22")
    assert m is not None
    assert _dest_index(cfg, m) == "agent-captains-captures-subagents-2026-02"


def test_joinability_pattern_excludes_substrate_sibling() -> None:
    """The anchored parent pattern must not match the substrate family's indices."""
    cfg = _cfg("agent-monitors-joinability")
    assert cfg.legacy_pattern.match("agent-monitors-joinability-2026.07.31") is not None
    assert cfg.legacy_pattern.match("agent-monitors-joinability-substrate-2026.07.31") is None


def test_joinability_substrate_matches_its_own_pattern() -> None:
    """The substrate family's own pattern matches its own daily indices."""
    cfg = _cfg("agent-monitors-joinability-substrate")
    m = cfg.legacy_pattern.match("agent-monitors-joinability-substrate-2026.07.31")
    assert m is not None
    assert _dest_index(cfg, m) == "agent-monitors-joinability-substrate-2026-07"


def test_slm_health_dotted_monthly_maps_to_dash_monthly() -> None:
    """slm-health/user-turn-ratings are already monthly — only the separator moves."""
    cfg = _cfg("agent-monitors-slm-health")
    m = cfg.legacy_pattern.match("agent-monitors-slm-health-2026.06")
    assert m is not None
    assert _dest_index(cfg, m) == "agent-monitors-slm-health-2026-06"
    # A daily-suffixed name (never produced by this family) must not match the
    # monthly-only pattern.
    assert cfg.legacy_pattern.match("agent-monitors-slm-health-2026.06.15") is None


def test_user_turn_ratings_dotted_monthly_maps_to_dash_monthly() -> None:
    """A dotted monthly user-turn-ratings index maps to its dash destination."""
    cfg = _cfg("user-turn-ratings")
    m = cfg.legacy_pattern.match("user-turn-ratings-2026.06")
    assert m is not None
    assert _dest_index(cfg, m) == "user-turn-ratings-2026-06"


def test_insights_pattern_only_matches_pre_cutover_daily_stragglers() -> None:
    """agent-insights already writes monthly-dash; only the daily orphans match."""
    cfg = _cfg("agent-insights")
    m = cfg.legacy_pattern.match("agent-insights-2026-04-17")
    assert m is not None
    assert _dest_index(cfg, m) == "agent-insights-2026-04"
    # The current, correct monthly format must not match (nothing to migrate).
    assert cfg.legacy_pattern.match("agent-insights-2026-04") is None


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
