"""One-time backfill of ``first_accessed_at`` for cold-start mitigation (ADR-0042 step 8).

Sets ``first_accessed_at`` from creation-time fields only; does not touch
``last_accessed_at`` or ``access_count`` (real access data only).
"""

from __future__ import annotations

from personal_agent.config.settings import AppConfig, get_settings
from personal_agent.memory.service import MemoryService
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

_ENTITY_BACKFILL_CYPHER = """
MATCH (e:Entity)
WHERE e.first_accessed_at IS NULL AND e.first_seen IS NOT NULL
WITH e LIMIT $limit
SET e.first_accessed_at = e.first_seen
RETURN count(e) AS n
"""

_REL_BACKFILL_CYPHER = """
MATCH ()-[r]->()
WHERE r.first_accessed_at IS NULL AND r.created_at IS NOT NULL
WITH r LIMIT $limit
SET r.first_accessed_at = r.created_at
RETURN count(r) AS n
"""


async def run_freshness_first_accessed_backfill(
    memory: MemoryService,
    *,
    dry_run: bool,
    batch_size: int = 100,
    settings: AppConfig | None = None,
) -> tuple[int, int]:
    """Backfill ``first_accessed_at`` on entities and relationships in batches.

    Args:
        memory: Connected memory service.
        dry_run: When True, log pending counts via MATCH … RETURN count(*); no writes.
        batch_size: Max nodes/relationships per transaction.
        settings: Optional config override.

    Returns:
        ``(entities_touched, relationships_touched)``. On ``dry_run``, returns pending
        counts (no writes). Otherwise returns cumulative updated row counts.
    """
    cfg = settings or get_settings()
    log.info(
        "freshness_backfill_start",
        dry_run=dry_run,
        batch_size=batch_size,
        environment=str(cfg.environment),
    )
    if not memory.connected or memory.driver is None:
        log.warning("freshness_backfill_skipped_not_connected")
        return (0, 0)

    driver = memory.driver
    ent_total = 0
    rel_total = 0

    if dry_run:
        ent_pending, rel_pending = await count_freshness_backfill_pending(memory)
        log.info(
            "freshness_backfill_dry_run",
            entities_pending=ent_pending,
            relationships_pending=rel_pending,
            batch_size=batch_size,
        )
        return (ent_pending, rel_pending)

    while True:
        async with driver.session() as session:
            result = await session.run(_ENTITY_BACKFILL_CYPHER, limit=batch_size)
            rec = await result.single()
            n = int(rec["n"]) if rec else 0
        if n <= 0:
            break
        ent_total += n
        log.info("freshness_backfill_entity_batch", batch=n, cumulative=ent_total)

    while True:
        async with driver.session() as session:
            result = await session.run(_REL_BACKFILL_CYPHER, limit=batch_size)
            rec = await result.single()
            n = int(rec["n"]) if rec else 0
        if n <= 0:
            break
        rel_total += n
        log.info("freshness_backfill_relationship_batch", batch=n, cumulative=rel_total)

    log.info(
        "freshness_backfill_completed",
        entities_updated=ent_total,
        relationships_updated=rel_total,
    )
    return (ent_total, rel_total)


async def count_freshness_backfill_pending(memory: MemoryService) -> tuple[int, int]:
    """Return pending entity and relationship counts for backfill."""
    if not memory.connected or memory.driver is None:
        return (0, 0)
    driver = memory.driver
    async with driver.session() as session:
        qe = await session.run(
            """
            MATCH (e:Entity)
            WHERE e.first_accessed_at IS NULL AND e.first_seen IS NOT NULL
            RETURN count(e) AS n
            """
        )
        re = await qe.single()
        ent_pending = int(re["n"]) if re else 0
        qr = await session.run(
            """
            MATCH ()-[r]->()
            WHERE r.first_accessed_at IS NULL AND r.created_at IS NOT NULL
            RETURN count(r) AS n
            """
        )
        rr = await qr.single()
        rel_pending = int(rr["n"]) if rr else 0
    return (ent_pending, rel_pending)
