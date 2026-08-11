"""FRE-1210 T6.1 -- pure-function unit tests for kg_stats_aggregate.

These target the bucketing/ratio/ranking logic directly via ``_EntityRow``
fixtures, not through a live Neo4j scan -- ``_scan_entities`` (the only I/O in
the module) already enforces owner exclusion via its Cypher WHERE clause, so
every function under test here only ever sees non-owner rows by construction.
Live-Neo4j coverage (owner exclusion itself, relationship counts, embedding
reachability, duplicate-name normalization, turn coverage) is in
``test_kg_stats_aggregate_integration.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from personal_agent.config.settings import get_settings
from personal_agent.memory.kg_stats_aggregate import (
    _access_count_buckets,
    _cold_mass_and_unmeasured_ratio,
    _entity_count_by_type,
    _EntityRow,
    _heatmap_cells,
    _recency_buckets,
    _top_heat_entities,
)

_NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)


def _entity(
    name: str,
    entity_type: str = "MethodOrConcept",
    access_count: int | None = 0,
    days_ago: float | None = None,
) -> _EntityRow:
    """Build an ``_EntityRow`` fixture; ``days_ago=None`` means no last_accessed_at."""
    last_accessed_at = _NOW - timedelta(days=days_ago) if days_ago is not None else None
    return _EntityRow(
        name=name,
        entity_type=entity_type,
        access_count=access_count,
        last_accessed_at=last_accessed_at,
    )


class TestEntityCountByType:
    """entity_count metric, one row per entity_type."""

    def test_groups_by_type(self) -> None:
        """Counts partition correctly across two entity types."""
        entities = [
            _entity("a", entity_type="Person"),
            _entity("b", entity_type="Person"),
            _entity("c", entity_type="KnowledgeArtifact"),
        ]
        rows = {(r.dimension): r.metric_value for r in _entity_count_by_type(entities)}
        assert rows == {"Person": 2.0, "KnowledgeArtifact": 1.0}


class TestColdMassAndUnmeasuredRatio:
    """cold_mass_ratio and unmeasured_ratio -- the codex-caught conflation bug."""

    def test_disjoint_populations(self) -> None:
        """access_count=0 and access_count IS NULL land in different metrics.

        This is the case the original draft's ``coalesce(access_count, 0) = 0``
        predicate would have failed: it folded "never measured" into "never
        read," duplicating unmeasured_ratio's population instead of
        partitioning against it.
        """
        entities = [
            _entity("never-read", access_count=0, days_ago=5),
            _entity("unmeasured", access_count=None),
            _entity("warm", access_count=3, days_ago=1),
            _entity("also-unmeasured", access_count=None),
        ]
        rows = {r.metric_name: r.metric_value for r in _cold_mass_and_unmeasured_ratio(entities)}
        assert rows["cold_mass_ratio"] == 1 / 4  # only "never-read"
        assert rows["unmeasured_ratio"] == 2 / 4  # the two None-access entities

    def test_empty_graph_does_not_divide_by_zero(self) -> None:
        """An empty entity population yields 0.0 ratios, not a ZeroDivisionError."""
        rows = {r.metric_name: r.metric_value for r in _cold_mass_and_unmeasured_ratio([])}
        assert rows["cold_mass_ratio"] == 0.0
        assert rows["unmeasured_ratio"] == 0.0


class TestAccessCountBuckets:
    """access_count_bucket heat histogram."""

    def test_unmeasured_excluded_from_histogram(self) -> None:
        """An entity with no access_count property appears in no bucket."""
        entities = [
            _entity("a", access_count=0),
            _entity("b", access_count=None),  # must not appear in any bucket
            _entity("c", access_count=30),
        ]
        buckets = {r.dimension: r.metric_value for r in _access_count_buckets(entities)}
        assert buckets == {"0": 1.0, "26+": 1.0}

    def test_bucket_edges(self) -> None:
        """Every bucket boundary from the ticket's own table is honored."""
        entities = [
            _entity("a0", access_count=0),
            _entity("a1", access_count=1),
            _entity("a2", access_count=2),
            _entity("a3", access_count=5),
            _entity("a4", access_count=10),
            _entity("a5", access_count=25),
            _entity("a6", access_count=26),
        ]
        buckets = {r.dimension: r.metric_value for r in _access_count_buckets(entities)}
        assert buckets == {"0": 1.0, "1-2": 2.0, "3-5": 1.0, "6-10": 1.0, "11-25": 1.0, "26+": 1.0}


class TestRecencyBuckets:
    """recency_bucket -- recency of a genuine read only."""

    def test_never_read_excluded_despite_having_last_accessed_at(self) -> None:
        """A never-read entity's creation-time last_accessed_at doesn't count.

        create_entity sets last_accessed_at at creation time even when
        access_count=0 -- the recency histogram must not misread that as a
        genuine recent access.
        """
        entities = [
            _entity("cold-but-touched", access_count=0, days_ago=0.1),
            _entity("unmeasured", access_count=None, days_ago=0.1),
            _entity("read-recently", access_count=1, days_ago=0.5),
        ]
        buckets = _recency_buckets(entities, now=_NOW)
        assert len(buckets) == 1
        assert buckets[0].dimension == "0-1d"
        assert buckets[0].metric_value == 1.0


class TestHeatmapCells:
    """recency_frequency_cell / type_recency_cell -- the two cross-tab panels."""

    def test_cold_entities_appear_in_cross_tabs(self) -> None:
        """The cold mass is a real, visible cell here, unlike recency_bucket.

        Design doc T6.2: "the cold mass becomes a visible block in the
        corner" -- so, unlike recency_bucket, this must NOT exclude
        access_count=0 entities.
        """
        entities = [
            _entity("cold", entity_type="MethodOrConcept", access_count=0, days_ago=40),
            _entity("hot", entity_type="Person", access_count=15, days_ago=1),
        ]
        cells = _heatmap_cells(entities, now=_NOW)
        rf_dims = {r.dimension for r in cells if r.metric_name == "recency_frequency_cell"}
        tr_dims = {r.dimension for r in cells if r.metric_name == "type_recency_cell"}
        assert "31-60d|0" in rf_dims
        assert "0-1d|11-25" in rf_dims
        assert "MethodOrConcept|31-60d" in tr_dims
        assert "Person|0-1d" in tr_dims

    def test_entities_missing_last_accessed_at_excluded(self) -> None:
        """An entity with neither access data nor a timestamp yields no cell."""
        entities = [_entity("no-timestamp", access_count=None, days_ago=None)]
        assert _heatmap_cells(entities, now=_NOW) == []


class TestTopHeatEntities:
    """top_heat_entity ranking -- AC-2's discrimination requirement."""

    def test_never_read_entities_never_appear(self) -> None:
        """AC-2(a): excluded by construction, not merely scored to zero."""
        cfg = get_settings()
        entities = [
            _entity("cold-1", access_count=0, days_ago=1),
            _entity("unmeasured-1", access_count=None),
            _entity("warm-1", access_count=5, days_ago=1),
        ]
        top = _top_heat_entities(entities, cfg, now=_NOW)
        names = [r.dimension for r in top]
        assert "cold-1" not in names
        assert "unmeasured-1" not in names
        assert "warm-1" in names

    def test_more_recent_access_scores_higher_at_equal_count(self) -> None:
        """The exponential decay term breaks ties by recency, not just count."""
        cfg = get_settings()
        entities = [
            _entity("stale", access_count=10, days_ago=30),
            _entity("fresh", access_count=10, days_ago=1),
        ]
        top = _top_heat_entities(entities, cfg, now=_NOW)
        assert [r.dimension for r in top] == ["fresh", "stale"]

    def test_limited_to_ten(self) -> None:
        """More than 10 eligible entities still yields exactly 10 rows."""
        cfg = get_settings()
        entities = [_entity(f"e{i}", access_count=i + 1, days_ago=1) for i in range(15)]
        top = _top_heat_entities(entities, cfg, now=_NOW)
        assert len(top) == 10
