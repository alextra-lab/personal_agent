"""Aggregate knowledge-graph staleness tiers for review job and insights (FRE-166 / FRE-167).

Scans Neo4j Entity nodes and relationships, classifies each row with
:class:`~personal_agent.memory.freshness.classify_staleness`, and returns
counts plus auxiliary metrics for telemetry and Captain's Log proposals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from personal_agent.config.settings import AppConfig, get_settings
from personal_agent.memory.freshness import StalenessTier, classify_staleness
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

_DEFAULT_BATCH = 500


def freshness_tier_snapshot_path(cfg: AppConfig) -> Path:
    """Filesystem path for the weekly tier-count JSON snapshot."""
    return cfg.log_dir.parent / "freshness_tier_snapshot.json"


def _neo4j_datetime(value: Any) -> datetime | None:
    """Convert Neo4j driver datetime or ISO string to timezone-aware UTC."""
    if value is None:
        return None
    if hasattr(value, "to_native"):
        value = value.to_native()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            d = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


@dataclass
class StalenessTierCounts:
    """Count of graph elements per staleness tier."""

    warm: int = 0
    cooling: int = 0
    cold: int = 0
    dormant: int = 0

    def to_dict(self) -> dict[str, int]:
        """Serialize tier counts for JSON snapshots."""
        return {
            "warm": self.warm,
            "cooling": self.cooling,
            "cold": self.cold,
            "dormant": self.dormant,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StalenessTierCounts:
        """Restore from snapshot JSON."""
        return cls(
            warm=int(data.get("warm", 0)),
            cooling=int(data.get("cooling", 0)),
            cold=int(data.get("cold", 0)),
            dormant=int(data.get("dormant", 0)),
        )


@dataclass
class GraphStalenessSummary:
    """Full staleness aggregation over entities and relationships."""

    entities: StalenessTierCounts = field(default_factory=StalenessTierCounts)
    relationships: StalenessTierCounts = field(default_factory=StalenessTierCounts)
    top_accessed_entities: list[tuple[str, int]] = field(default_factory=list)
    never_accessed_old_entity_count: int = 0
    # name, last_accessed_at, access_count, first_seen (for proposal detail)
    dormant_entity_samples: list[tuple[str, datetime | None, int, datetime | None]] = field(
        default_factory=list
    )
    dormant_relationships_by_type: dict[str, int] = field(default_factory=dict)


def tier_counts_delta(
    previous: StalenessTierCounts | None, current: StalenessTierCounts
) -> dict[str, int]:
    """Return per-tier count change (current - previous), or current if no previous."""
    if previous is None:
        return current.to_dict()
    return {
        "warm": current.warm - previous.warm,
        "cooling": current.cooling - previous.cooling,
        "cold": current.cold - previous.cold,
        "dormant": current.dormant - previous.dormant,
    }


async def aggregate_graph_staleness(
    driver: Any,
    settings: AppConfig | None = None,
    *,
    batch_size: int = _DEFAULT_BATCH,
) -> GraphStalenessSummary:
    """Scan all entities and relationships and aggregate staleness metrics.

    Args:
        driver: Connected Neo4j async driver.
        settings: App config; defaults to ``get_settings()``.
        batch_size: Rows per paged Neo4j read.

    Returns:
        Aggregated summary for telemetry, snapshots, and insights.
    """
    cfg = settings or get_settings()
    summary = GraphStalenessSummary()
    now = datetime.now(timezone.utc)
    noise_cutoff = now - timedelta(days=float(cfg.freshness_never_accessed_noise_days))

    # --- Entities (paged) ---
    skip = 0
    dormant_candidates: list[tuple[str, datetime | None, int, datetime | None]] = []
    while True:
        query = """
            MATCH (e:Entity)
            RETURN e.name AS name,
                   e.first_seen AS first_seen,
                   e.last_accessed_at AS last_accessed_at,
                   coalesce(e.access_count, 0) AS access_count
            ORDER BY name
            SKIP $skip LIMIT $limit
        """
        async with driver.session() as session:
            result = await session.run(query, skip=skip, limit=batch_size)
            rows = [record async for record in result]
        if not rows:
            break
        for row in rows:
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            first_seen = _neo4j_datetime(row.get("first_seen"))
            last_acc = _neo4j_datetime(row.get("last_accessed_at"))
            access_count = int(row.get("access_count") or 0)
            tier = classify_staleness(last_acc, access_count, first_seen, cfg)
            if tier == StalenessTier.WARM:
                summary.entities.warm += 1
            elif tier == StalenessTier.COOLING:
                summary.entities.cooling += 1
            elif tier == StalenessTier.COLD:
                summary.entities.cold += 1
            else:
                summary.entities.dormant += 1
                dormant_candidates.append((name, last_acc, access_count, first_seen))
            if access_count == 0 and first_seen is not None and first_seen < noise_cutoff:
                summary.never_accessed_old_entity_count += 1
        skip += len(rows)
        if len(rows) < batch_size:
            break

    # Oldest staleness first: use last_accessed_at, else first_seen as proxy
    def _dormant_key(t: tuple[str, datetime | None, int, datetime | None]) -> datetime:
        _n, last_acc, _c, first_seen = t
        ref = last_acc if last_acc is not None else first_seen
        return ref or datetime.min.replace(tzinfo=timezone.utc)

    dormant_candidates.sort(key=_dormant_key)
    summary.dormant_entity_samples = dormant_candidates[:5]

    # --- Top accessed entities ---
    top_q = """
        MATCH (e:Entity)
        WHERE coalesce(e.access_count, 0) > 0
        RETURN e.name AS name, coalesce(e.access_count, 0) AS cnt
        ORDER BY cnt DESC
        LIMIT 5
    """
    async with driver.session() as session:
        result = await session.run(top_q)
        async for row in result:
            n = str(row.get("name") or "").strip()
            if n:
                summary.top_accessed_entities.append((n, int(row.get("cnt") or 0)))

    # --- Relationships (paged) ---
    skip = 0
    while True:
        rel_q = """
            MATCH ()-[r]->()
            RETURN type(r) AS rel_type,
                   r.created_at AS created_at,
                   r.last_accessed_at AS last_accessed_at,
                   coalesce(r.access_count, 0) AS access_count
            SKIP $skip LIMIT $limit
        """
        async with driver.session() as session:
            result = await session.run(rel_q, skip=skip, limit=batch_size)
            rows = [record async for record in result]
        if not rows:
            break
        for row in rows:
            rel_type = str(row.get("rel_type") or "RELATED")
            created_at = _neo4j_datetime(row.get("created_at"))
            last_acc = _neo4j_datetime(row.get("last_accessed_at"))
            access_count = int(row.get("access_count") or 0)
            tier = classify_staleness(last_acc, access_count, created_at, cfg)
            if tier == StalenessTier.WARM:
                summary.relationships.warm += 1
            elif tier == StalenessTier.COOLING:
                summary.relationships.cooling += 1
            elif tier == StalenessTier.COLD:
                summary.relationships.cold += 1
            else:
                summary.relationships.dormant += 1
                summary.dormant_relationships_by_type[rel_type] = (
                    summary.dormant_relationships_by_type.get(rel_type, 0) + 1
                )
        skip += len(rows)
        if len(rows) < batch_size:
            break

    log.info(
        "freshness_aggregate_completed",
        entity_total=sum(summary.entities.to_dict().values()),
        relationship_total=sum(summary.relationships.to_dict().values()),
        never_accessed_old_entities=summary.never_accessed_old_entity_count,
    )
    return summary
