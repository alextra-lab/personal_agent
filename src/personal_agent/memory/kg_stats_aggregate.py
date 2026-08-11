"""Daily Neo4j -> Postgres projection of KG health/freshness metrics (FRE-1210 T6.1).

Feeds the ``kg_stats`` table (migration ``0026_kg_stats.sql``), which backs the
"KG Health" Grafana dashboard. Kept separate from :mod:`memory.freshness_aggregate`
(staleness-tier scanning only) because this computes a materially wider set of
KG-health metrics, several unrelated to staleness tiers (embedding reachability,
duplicate names, turn coverage, an entity-level heat ranking).

Every entity-level query excludes the owner node (``:Person:Entity``,
``user_id`` set -- FRE-632 / ADR-0052 amendment): it is the owner's identity
anchor, not an extracted entity, and every existing graph-health metric in
:mod:`second_brain.quality_monitor` excludes it for the same reason.

Two related-but-distinct notions of "recency" are used here, deliberately:

- The ``recency_bucket`` metric and ``top_heat_entity`` ranking mean *recency
  of a genuine read*: they only consider entities with ``access_count > 0``.
  ``create_entity`` sets ``last_accessed_at`` to the creation timestamp at
  write time, with ``access_count = 0`` (``memory/service.py``'s
  ``create_entity``) -- so a plain ``last_accessed_at IS NOT NULL`` check
  would misclassify a never-read entity as "recently accessed."
- The two heatmap cross-tab cells (``recency_frequency_cell``,
  ``type_recency_cell``) intentionally do NOT apply that gate: their purpose
  (design doc T6.2) is to make the cold mass visible as a block in the grid,
  which requires cold (``access_count = 0``) entities to appear in some
  recency bucket -- using their ``last_accessed_at`` (creation time, for a
  never-read entity) as a purely descriptive "last touched" axis, not a
  claim about having been read.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from personal_agent.config.settings import AppConfig, get_settings
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

_BATCH = 500
_PG_CONNECT_TIMEOUT_SECONDS = 10.0
_TOP_HEAT_LIMIT = 10

# Semantic KG edges only -- mirrors second_brain/quality_monitor.py's
# check_graph_health convention. Excludes infrastructure/provenance edges
# (CONTAINS, NEXT, PARTICIPATED_IN, OPERATED_BY, description-history), which
# are not "knowledge" edges and would silently change what "edge count"
# means to a dashboard reader.
_SEMANTIC_REL_TYPES = ["RELATIONSHIP", "DISCUSSES"]

_ACCESS_COUNT_BUCKET_EDGES = [(0, "0"), (2, "1-2"), (5, "3-5"), (10, "6-10"), (25, "11-25")]
_RECENCY_BUCKET_EDGES = [
    (1, "0-1d"),
    (7, "2-7d"),
    (14, "8-14d"),
    (30, "15-30d"),
    (60, "31-60d"),
]


@dataclass(frozen=True)
class KgStatRow:
    """One row to write to ``kg_stats``."""

    metric_name: str
    dimension: str | None
    metric_value: float


@dataclass(frozen=True)
class _EntityRow:
    name: str
    entity_type: str
    access_count: int | None
    last_accessed_at: datetime | None


def _neo4j_datetime(value: Any) -> datetime | None:
    """Convert Neo4j driver datetime or ISO string to timezone-aware UTC.

    Duplicated from :func:`memory.freshness_aggregate._neo4j_datetime` (a
    private helper, not meant for cross-module import) rather than exported
    for one small function.
    """
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


def _bucket(value: float, edges: Sequence[tuple[float, str]], overflow_label: str) -> str:
    for threshold, label in edges:
        if value <= threshold:
            return label
    return overflow_label


async def _scan_entities(driver: Any, batch_size: int = _BATCH) -> list[_EntityRow]:
    """Paged scan of every non-owner Entity, one round trip per page.

    Feeds entity_count, access_count_bucket, recency_bucket, cold_mass_ratio,
    unmeasured_ratio, both heatmap cross-tabs, and top_heat_entity -- one scan
    for all of them rather than a query per metric.
    """
    rows: list[_EntityRow] = []
    skip = 0
    while True:
        query = """
            MATCH (e:Entity)
            WHERE e.user_id IS NULL
            RETURN e.name AS name,
                   coalesce(e.entity_type, 'UNKNOWN') AS entity_type,
                   e.access_count AS access_count,
                   e.last_accessed_at AS last_accessed_at
            ORDER BY name
            SKIP $skip LIMIT $limit
        """
        async with driver.session() as session:
            result = await session.run(query, skip=skip, limit=batch_size)
            page = [record async for record in result]
        if not page:
            break
        for record in page:
            name = str(record.get("name") or "").strip()
            if not name:
                continue
            ac = record.get("access_count")
            rows.append(
                _EntityRow(
                    name=name,
                    entity_type=str(record.get("entity_type") or "UNKNOWN"),
                    access_count=int(ac) if ac is not None else None,
                    last_accessed_at=_neo4j_datetime(record.get("last_accessed_at")),
                )
            )
        skip += len(page)
        if len(page) < batch_size:
            break
    return rows


async def _relationship_count_by_type(driver: Any) -> list[KgStatRow]:
    query = """
        MATCH ()-[r]->()
        WHERE type(r) IN $rel_types
        RETURN type(r) AS t, count(r) AS n
    """
    async with driver.session() as session:
        result = await session.run(query, rel_types=_SEMANTIC_REL_TYPES)
        records = [record async for record in result]
    return [KgStatRow("relationship_count", str(r["t"]), float(r["n"])) for r in records]


async def _embedding_missing(driver: Any) -> KgStatRow:
    """Entities unreachable by vector recall (missing or zero-vectored embedding).

    Full non-owner Entity population -- deliberately NOT restricted to
    ``_backfill_entity_embeddings``'s narrower backfill-eligible population
    (which additionally requires a non-empty ``description``,
    ``memory/service.py``). A description-less, embedding-less entity is
    still unreachable by vector search; that's the more important case to
    surface, not one to exclude.
    """
    query = """
        MATCH (e:Entity)
        WHERE e.user_id IS NULL
          AND (e.embedding IS NULL OR none(x IN e.embedding WHERE x <> 0.0))
        RETURN count(e) AS n
    """
    async with driver.session() as session:
        result = await session.run(query)
        record = await result.single()
    return KgStatRow("embedding_missing", None, float(record["n"] if record else 0))


async def _duplicate_groups(driver: Any) -> tuple[KgStatRow, KgStatRow]:
    """Case-insensitive name-collision groups, and how many disagree on entity_type.

    Normalization matches second_brain/quality_monitor.py's established
    duplicate query: ``toLower(trim(name))``, blank names rejected.
    """
    query = """
        MATCH (e:Entity) WHERE e.user_id IS NULL
        WITH toLower(trim(e.name)) AS lname, collect(DISTINCT e.entity_type) AS types, count(e) AS n
        WHERE lname <> "" AND n > 1
        RETURN count(*) AS duplicate_groups,
               sum(CASE WHEN size(types) > 1 THEN 1 ELSE 0 END) AS type_disagreements
    """
    async with driver.session() as session:
        result = await session.run(query)
        record = await result.single()
    dup = float(record["duplicate_groups"] if record else 0)
    disagree = float(record["type_disagreements"] if record else 0)
    return (
        KgStatRow("duplicate_group_count", None, dup),
        KgStatRow("type_disagreement_count", None, disagree),
    )


async def _turns_without_entities_ratio(driver: Any) -> KgStatRow:
    """``DISCUSSES`` is the sole production Turn->Entity edge (service.py, consolidator.py)."""
    query = """
        MATCH (t:Turn)
        WITH t, EXISTS((t)-[:DISCUSSES]->(:Entity)) AS has_entity
        RETURN count(t) AS total, sum(CASE WHEN has_entity THEN 0 ELSE 1 END) AS without_entities
    """
    async with driver.session() as session:
        result = await session.run(query)
        record = await result.single()
    if not record or not record["total"]:
        return KgStatRow("turns_without_entities_ratio", None, 0.0)
    ratio = float(record["without_entities"]) / float(record["total"])
    return KgStatRow("turns_without_entities_ratio", None, ratio)


def _entity_count_by_type(entities: list[_EntityRow]) -> list[KgStatRow]:
    counts: dict[str, int] = defaultdict(int)
    for e in entities:
        counts[e.entity_type] += 1
    return [KgStatRow("entity_count", t, float(n)) for t, n in counts.items()]


def _access_count_buckets(entities: list[_EntityRow]) -> list[KgStatRow]:
    """Heat histogram.

    Entities with no ``access_count`` property are excluded here (they're
    counted by ``unmeasured_ratio`` instead) -- a bare ``0`` bucket should
    mean "measured, never read," not "never measured."
    """
    counts: dict[str, int] = defaultdict(int)
    for e in entities:
        if e.access_count is None:
            continue
        counts[_bucket(e.access_count, _ACCESS_COUNT_BUCKET_EDGES, "26+")] += 1
    return [KgStatRow("access_count_bucket", b, float(n)) for b, n in counts.items()]


def _recency_buckets(entities: list[_EntityRow], now: datetime) -> list[KgStatRow]:
    """Recency of a genuine read -- excludes access_count in (None, 0)."""
    counts: dict[str, int] = defaultdict(int)
    for e in entities:
        if not e.access_count or e.last_accessed_at is None:
            continue
        age_days = (now - e.last_accessed_at).total_seconds() / 86400.0
        counts[_bucket(age_days, _RECENCY_BUCKET_EDGES, "60d+")] += 1
    return [KgStatRow("recency_bucket", b, float(n)) for b, n in counts.items()]


def _cold_mass_and_unmeasured_ratio(entities: list[_EntityRow]) -> list[KgStatRow]:
    total = len(entities)
    if total == 0:
        return [KgStatRow("cold_mass_ratio", None, 0.0), KgStatRow("unmeasured_ratio", None, 0.0)]
    never_read = sum(1 for e in entities if e.access_count == 0)
    unmeasured = sum(1 for e in entities if e.access_count is None)
    return [
        KgStatRow("cold_mass_ratio", None, never_read / total),
        KgStatRow("unmeasured_ratio", None, unmeasured / total),
    ]


def _heatmap_cells(entities: list[_EntityRow], now: datetime) -> list[KgStatRow]:
    """The two cross-tab panels (design doc T6.2 panels 3-4).

    Uses ``last_accessed_at`` (populated at creation even for never-read
    entities) with NO access_count gate, so cold-mass entities appear as a
    real cell rather than being invisible -- see module docstring.
    """
    rf_counts: dict[tuple[str, str], int] = defaultdict(int)
    tr_counts: dict[tuple[str, str], int] = defaultdict(int)
    for e in entities:
        if e.access_count is None or e.last_accessed_at is None:
            continue
        age_days = (now - e.last_accessed_at).total_seconds() / 86400.0
        recency = _bucket(age_days, _RECENCY_BUCKET_EDGES, "60d+")
        access = _bucket(e.access_count, _ACCESS_COUNT_BUCKET_EDGES, "26+")
        rf_counts[(recency, access)] += 1
        tr_counts[(e.entity_type, recency)] += 1

    rows = [
        KgStatRow("recency_frequency_cell", f"{recency}|{access}", float(n))
        for (recency, access), n in rf_counts.items()
    ]
    rows += [
        KgStatRow("type_recency_cell", f"{etype}|{recency}", float(n))
        for (etype, recency), n in tr_counts.items()
    ]
    return rows


def _top_heat_entities(
    entities: list[_EntityRow], cfg: AppConfig, now: datetime
) -> list[KgStatRow]:
    """Top-10 by ``access_count * e^(-lambda * age_days)`` (AC-2).

    lambda = ln(2) / freshness_half_life_days -- the same decay constant
    already governing the WARM tier boundary (memory/freshness.py), not a
    new free parameter. Entities with access_count in (None, 0) are excluded
    by the guard below, not merely scored zero -- this is what makes AC-2(a)
    ("no access_count=0 entity ever appears in the top-10") true by
    construction.
    """
    half_life = cfg.freshness_half_life_days
    if half_life <= 0:
        return []
    lam = math.log(2) / half_life
    scored: list[tuple[str, float]] = []
    for e in entities:
        if not e.access_count or e.last_accessed_at is None:
            continue
        age_days = (now - e.last_accessed_at).total_seconds() / 86400.0
        score = e.access_count * math.exp(-lam * max(age_days, 0.0))
        scored.append((e.name, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [KgStatRow("top_heat_entity", name, score) for name, score in scored[:_TOP_HEAT_LIMIT]]


async def aggregate_kg_stats(driver: Any, settings: AppConfig | None = None) -> list[KgStatRow]:
    """Compute every kg_stats metric for one projection run.

    Args:
        driver: Connected Neo4j async driver.
        settings: App config; defaults to ``get_settings()``.

    Returns:
        Every :class:`KgStatRow` for this run, ready for :func:`write_kg_stats`.
    """
    cfg = settings or get_settings()
    now = datetime.now(timezone.utc)

    entities = await _scan_entities(driver)

    rows: list[KgStatRow] = []
    rows.extend(_entity_count_by_type(entities))
    rows.extend(await _relationship_count_by_type(driver))
    rows.extend(_access_count_buckets(entities))
    rows.extend(_recency_buckets(entities, now))
    rows.extend(_cold_mass_and_unmeasured_ratio(entities))
    rows.append(await _embedding_missing(driver))
    dup_row, disagree_row = await _duplicate_groups(driver)
    rows.append(dup_row)
    rows.append(disagree_row)
    rows.append(await _turns_without_entities_ratio(driver))
    rows.extend(_heatmap_cells(entities, now))
    rows.extend(_top_heat_entities(entities, cfg, now))

    log.info("kg_stats_aggregate_completed", entity_total=len(entities), row_count=len(rows))
    return rows


async def _open_pg_conn() -> Any | None:
    """Open a short-lived asyncpg connection against ``settings.database_url``.

    Mirrors observability/delivery_ratio/scheduler_runner.py's pattern: a
    daily cadence, one sequential write -- a pool buys no reuse/concurrency
    here.
    """
    settings = get_settings()
    try:
        import asyncpg  # type: ignore[import-untyped]

        dsn = _normalize_asyncpg_dsn(settings.database_url)
        conn: Any = await asyncpg.connect(dsn, timeout=_PG_CONNECT_TIMEOUT_SECONDS)
        return conn
    except Exception as exc:  # noqa: BLE001
        log.warning("kg_stats_pg_connect_failed", error=str(exc))
        return None


async def _close_pg_conn(conn: Any) -> None:
    try:
        await conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("kg_stats_pg_close_failed", error=str(exc))


async def write_kg_stats(rows: list[KgStatRow], trace_id: str) -> int:
    """Write one projection run's rows to kg_stats.

    All rows share one ``observed_at`` timestamp (captured once), forming one
    queryable snapshot per run.

    Args:
        rows: Rows from :func:`aggregate_kg_stats`.
        trace_id: Correlation id for structured logs.

    Returns:
        Number of rows written, or ``0`` on any failure (connection, or the
        write itself) -- never raises.
    """
    if not rows:
        return 0
    conn = await _open_pg_conn()
    if conn is None:
        log.warning("kg_stats_write_skipped_no_pg", trace_id=trace_id)
        return 0
    observed_at = datetime.now(timezone.utc)
    try:
        await conn.executemany(
            """
            INSERT INTO kg_stats (observed_at, metric_name, dimension, metric_value)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (observed_at, metric_name, dimension) DO NOTHING
            """,
            [(observed_at, r.metric_name, r.dimension, r.metric_value) for r in rows],
        )
        return len(rows)
    except Exception as exc:  # noqa: BLE001
        log.warning("kg_stats_write_failed", trace_id=trace_id, error=str(exc), exc_info=True)
        return 0
    finally:
        await _close_pg_conn(conn)
