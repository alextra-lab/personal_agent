"""Convenience wrapper for invoking the joinability probe from the scheduler.

Opens its own short-lived Postgres / Neo4j / Redis connections (the brainstem
scheduler already owns an ES client, which we accept as a parameter), runs the
walk with ``SystemTraceContext.new("joinability_probe")``, writes the result
to ES, and releases all resources. A failure anywhere is logged and swallowed
— the scheduler must not crash because a probe tick failed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from personal_agent.config.settings import get_settings
from personal_agent.observability.joinability.result import (
    ResultDoc,
    substrate_docs_from_result,
)
from personal_agent.observability.joinability.sampling import (
    pick_session,
    seed_for,
)
from personal_agent.observability.joinability.sink import (
    write_result,
    write_substrate_results,
)
from personal_agent.observability.joinability.walk import JoinabilityWalk
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.trace import SystemTraceContext

if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch

log = get_logger(__name__)


async def run_scheduled_probe(*, es_client: "AsyncElasticsearch | None") -> ResultDoc | None:
    """Run one joinability probe tick from the brainstem scheduler.

    Args:
        es_client: Already-open AsyncElasticsearch client owned by the
            scheduler's :class:`DataLifecycleManager`. When ``None`` the
            probe still walks Postgres/Neo4j and emits a result doc to
            stdout via the structured log, but does not persist to ES.

    Returns:
        The :class:`ResultDoc` for the run, or ``None`` if the probe could
        not be launched (e.g. settings disable it). Any errors raised by
        substrate walks are absorbed by :class:`JoinabilityWalk` itself.
    """
    settings = get_settings()
    if not getattr(settings, "joinability_probe_enabled", True):
        return None

    ctx = SystemTraceContext.new("joinability_probe")
    started_at = datetime.now(timezone.utc)
    seed = seed_for(started_at)
    window_hours = settings.joinability_probe_window_hours

    pg_pool = await _open_pg_pool()
    neo4j_driver = _open_neo4j_driver()
    redis = await _open_redis()
    try:
        session_id = await _pick_session(pg_pool, window_hours=window_hours, seed=seed)
        # Substrate clients are typed as Any | None at open time (lazy imports);
        # the walk's __init__ tightens them per-substrate. Pass directly.
        walk_pool: Any = pg_pool
        walk_neo4j: Any = neo4j_driver
        walk_redis: Any = redis
        walk = JoinabilityWalk(
            pg_pool=walk_pool,
            es=es_client,
            neo4j_driver=walk_neo4j,
            redis=walk_redis,
            ctx=ctx,
            logs_prefix=settings.elasticsearch_index_prefix,
            captures_prefix=settings.captains_log_index_prefix,
        )
        if session_id is None:
            doc = ResultDoc(
                run_id=str(uuid.uuid4()),
                started_at=started_at,
                duration_ms=0.0,
                source="scheduler",
                window_hours=window_hours,
                random_seed=seed,
                sampled_session_id=None,
                outcome="skipped",
                trace_id=ctx.trace_id,
            )
        else:
            doc = await walk.run(
                session_id,
                source="scheduler",
                window_hours=window_hours,
                random_seed=seed,
            )

        log.info(
            "joinability_probe_completed",
            run_id=doc.run_id,
            outcome=doc.outcome,
            sampled_session_id=doc.sampled_session_id,
            seed=seed,
            trace_id=ctx.trace_id,
        )
        if es_client is not None:
            try:
                await write_result(
                    es_client,
                    doc,
                    prefix=settings.joinability_probe_index_prefix,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "joinability_probe_es_write_failed",
                    error=str(exc),
                    trace_id=ctx.trace_id,
                )
            # Flat per-substrate projection (FRE-550) for the legacy-aggs
            # dashboard panels — the run doc's nested arrays can't be aggregated.
            sub_docs = substrate_docs_from_result(doc)
            if sub_docs:
                try:
                    await write_substrate_results(
                        es_client,
                        sub_docs,
                        prefix=settings.joinability_probe_index_prefix,
                        trace_id=ctx.trace_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "joinability_probe_substrate_es_write_failed",
                        error=str(exc),
                        trace_id=ctx.trace_id,
                    )
        return doc
    finally:
        await _close(pg_pool, neo4j_driver, redis)


# ---------------------------------------------------------------------------
# Private helpers — substrate open / close
# ---------------------------------------------------------------------------


async def _open_pg_pool() -> Any | None:
    settings = get_settings()
    try:
        import asyncpg  # type: ignore[import-untyped]

        dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
        pool: Any = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        return pool
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "joinability_probe_pg_open_failed",
            error=str(exc),
            trace_id="joinability-probe",
        )
        return None


def _open_neo4j_driver() -> object | None:
    settings = get_settings()
    try:
        from neo4j import AsyncGraphDatabase

        return AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "joinability_probe_neo4j_open_failed",
            error=str(exc),
            trace_id="joinability-probe",
        )
        return None


async def _open_redis() -> Any | None:
    settings = get_settings()
    if not settings.event_bus_enabled:
        return None
    try:
        import redis.asyncio as aioredis_mod

        client = aioredis_mod.from_url(settings.event_bus_redis_url, decode_responses=True)
        await client.ping()  # type: ignore[misc]
        return client
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "joinability_probe_redis_open_failed",
            error=str(exc),
            trace_id="joinability-probe",
        )
        return None


async def _close(
    pg_pool: object | None,
    neo4j_driver: object | None,
    redis: object | None,
) -> None:
    if pg_pool is not None:
        try:
            await pg_pool.close()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "joinability_probe_pg_close_failed",
                error=str(exc),
                trace_id="joinability-probe",
            )
    if neo4j_driver is not None:
        try:
            await neo4j_driver.close()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "joinability_probe_neo4j_close_failed",
                error=str(exc),
                trace_id="joinability-probe",
            )
    if redis is not None:
        try:
            await redis.close()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "joinability_probe_redis_close_failed",
                error=str(exc),
                trace_id="joinability-probe",
            )


async def _pick_session(
    pg_pool: object | None,
    *,
    window_hours: int,
    seed: int,
) -> str | None:
    if pg_pool is None:
        return None
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=window_hours)
    end = now - timedelta(minutes=5)
    async with pg_pool.acquire() as conn:  # type: ignore[attr-defined]
        rows = await conn.fetch(
            """
            SELECT session_id::text AS sid
            FROM sessions
            WHERE created_at BETWEEN $1 AND $2
            ORDER BY session_id
            """,
            start,
            end,
        )
    return pick_session([r["sid"] for r in rows], seed=seed)
