"""CLI for the joinability probe (ADR-0074 Phase 5).

Picks one random session from the last ``--window-hours`` of traffic, walks
every substrate, asserts identity tuples, and (by default) writes a single
result document to ES index ``agent-monitors-joinability-YYYY.MM.DD``.

Exit codes:
    0   green   — all asserted checks passed
    1   yellow  — partial coverage (network blip, etc.)
    2   red     — at least one orphan or substrate-check failure
    3   skipped — no eligible session found (not a failure)
    64  usage   — bad CLI args

Usage:
    python -m scripts.monitors.joinability_probe
    python -m scripts.monitors.joinability_probe --dry-run --no-write-es
    python -m scripts.monitors.joinability_probe --session-id <uuid>
    python -m scripts.monitors.joinability_probe --seed 1748016000
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

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

EXIT_GREEN = 0
EXIT_YELLOW = 1
EXIT_RED = 2
EXIT_SKIPPED = 3
EXIT_USAGE = 64

log = get_logger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="joinability_probe",
        description="Cross-substrate identity walker (ADR-0074 Phase 5).",
    )
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Override the sampling seed (default: derived from start time).",
    )
    parser.add_argument(
        "--session-id",
        type=str,
        default=None,
        help="Bypass sampling and walk this exact session id.",
    )
    parser.add_argument(
        "--write-es",
        dest="write_es",
        action="store_true",
        default=True,
        help="Write the result doc to Elasticsearch (default).",
    )
    parser.add_argument(
        "--no-write-es",
        dest="write_es",
        action="store_false",
        help="Skip writing the result doc (still walks).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Walk + emit JSON to stdout; does not write ES.",
    )
    parser.add_argument(
        "--source",
        choices=["scheduler", "cli", "ci", "manual"],
        default="cli",
    )
    parser.add_argument(
        "--fail-on",
        choices=["yellow", "red", "never"],
        default="red",
        help="Exit code policy: 'red' (default) returns 0 on yellow.",
    )
    return parser.parse_args(argv)


async def _pick_eligible_session(
    pg_pool: object | None,
    *,
    window_hours: int,
    seed: int,
) -> str | None:
    """Query Postgres for eligible sessions and pick one deterministically.

    The 5-minute trailing window (``now - 5 minutes``) gives ES eventual-
    consistency time to settle before the walk inspects it.
    """
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
    pool = [r["sid"] for r in rows]
    return pick_session(pool, seed=seed)


async def _open_clients() -> tuple[
    object | None, "AsyncElasticsearch | None", object | None, object | None
]:
    """Open substrate clients honoring AppConfig — None on failure (not raise)."""
    settings = get_settings()
    pg_pool: object | None = None
    es: AsyncElasticsearch | None = None
    neo4j_driver: object | None = None
    redis: object | None = None

    try:
        import asyncpg

        # Convert SQLAlchemy DSN (postgresql+asyncpg://...) to bare DSN.
        dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
        pg_pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "joinability_probe_pg_open_failed", error=str(exc), trace_id="joinability-probe"
        )

    try:
        from elasticsearch import AsyncElasticsearch as ESClient

        es = ESClient([settings.elasticsearch_url], request_timeout=30)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "joinability_probe_es_open_failed", error=str(exc), trace_id="joinability-probe"
        )

    try:
        from neo4j import AsyncGraphDatabase

        neo4j_driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "joinability_probe_neo4j_open_failed", error=str(exc), trace_id="joinability-probe"
        )

    if settings.event_bus_enabled:
        try:
            import redis.asyncio as aioredis

            redis = aioredis.from_url(settings.event_bus_redis_url, decode_responses=True)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "joinability_probe_redis_open_failed",
                error=str(exc),
                trace_id="joinability-probe",
            )

    return pg_pool, es, neo4j_driver, redis


async def _close_clients(
    pg_pool: object | None, es: object | None, neo4j_driver: object | None, redis: object | None
) -> None:
    if pg_pool is not None:
        try:
            await pg_pool.close()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "joinability_probe_pg_close_failed", error=str(exc), trace_id="joinability-probe"
            )
    if es is not None:
        try:
            await es.close()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "joinability_probe_es_close_failed", error=str(exc), trace_id="joinability-probe"
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


def _exit_code(outcome: str, fail_on: str) -> int:
    if outcome == "green":
        return EXIT_GREEN
    if outcome == "skipped":
        return EXIT_SKIPPED
    if outcome == "yellow":
        return EXIT_YELLOW if fail_on in ("yellow", "never") else EXIT_GREEN
    if outcome == "red":
        return EXIT_RED if fail_on != "never" else EXIT_GREEN
    return EXIT_USAGE


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    started_at = datetime.now(timezone.utc)
    seed = args.seed if args.seed is not None else seed_for(started_at)

    ctx = SystemTraceContext.new("joinability_probe")
    log.info(
        "joinability_probe_started",
        source=args.source,
        window_hours=args.window_hours,
        seed=seed,
        session_id_override=args.session_id,
        trace_id=ctx.trace_id,
    )

    pg_pool, es, neo4j_driver, redis = await _open_clients()

    try:
        session_id: str | None = args.session_id
        if session_id is None:
            session_id = await _pick_eligible_session(
                pg_pool, window_hours=args.window_hours, seed=seed
            )

        walk = JoinabilityWalk(
            pg_pool=pg_pool,
            es=es,
            neo4j_driver=neo4j_driver,
            redis=redis,
            ctx=ctx,
            logs_prefix=settings.elasticsearch_index_prefix,
            captures_prefix=settings.captains_log_index_prefix,
        )
        if session_id is None:
            # No eligible session — emit a skipped result without walking.
            doc = ResultDoc(
                run_id=str(uuid.uuid4()),
                started_at=started_at,
                duration_ms=0.0,
                source=args.source,
                window_hours=args.window_hours,
                random_seed=seed,
                sampled_session_id=None,
                outcome="skipped",
                trace_id=ctx.trace_id,
            )
        else:
            doc = await walk.run(
                session_id,
                source=args.source,
                window_hours=args.window_hours,
                random_seed=seed,
            )

        # Emit a reproduce hint so a failed run can be re-investigated.
        log.info(
            "joinability_probe_completed",
            run_id=doc.run_id,
            outcome=doc.outcome,
            sampled_session_id=doc.sampled_session_id,
            seed=seed,
            reproduce_cmd=(
                f"python -m scripts.monitors.joinability_probe "
                f"--seed {seed} --window-hours {args.window_hours}"
                + (f" --session-id {doc.sampled_session_id}" if doc.sampled_session_id else "")
            ),
            trace_id=ctx.trace_id,
        )

        write_es = args.write_es and not args.dry_run
        if write_es and es is not None:
            try:
                await write_result(es, doc, prefix=settings.joinability_probe_index_prefix)
            except Exception as exc:  # noqa: BLE001 — sink failure is logged, not fatal
                log.warning(
                    "joinability_probe_es_write_failed",
                    error=str(exc),
                    trace_id=ctx.trace_id,
                )
            # Flat per-substrate projection (FRE-550) so legacy Kibana aggs can
            # break joinability detail down by substrate / status / orphan
            # severity (the run doc's nested arrays can't be aggregated).
            sub_docs = substrate_docs_from_result(doc)
            if sub_docs:
                try:
                    await write_substrate_results(
                        es,
                        sub_docs,
                        prefix=settings.joinability_probe_index_prefix,
                        trace_id=ctx.trace_id,
                    )
                except Exception as exc:  # noqa: BLE001 — sink failure is logged, not fatal
                    log.warning(
                        "joinability_probe_substrate_es_write_failed",
                        error=str(exc),
                        trace_id=ctx.trace_id,
                    )
        if args.dry_run:
            sys.stdout.write(doc.model_dump_json(indent=2) + "\n")

        outcome: Literal["green", "yellow", "red", "skipped"] = doc.outcome
        return _exit_code(outcome, args.fail_on)
    finally:
        await _close_clients(pg_pool, es, neo4j_driver, redis)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
