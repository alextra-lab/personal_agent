"""FRE-343 one-shot backfill — populate (:Person)-[:PARTICIPATED_IN]->(:Turn) edges.

Idempotent. Algorithm:
  1. Resolve OWNER_UUID from settings.agent_owner_email.
  2. Stream all Sessions from Postgres.
  3. For each Session: target_uid = session.user_id OR OWNER_UUID (NULL fallback).
  4. MERGE the edge in Neo4j for every Turn in that Session.

Run once after the FRE-343 PR merges:
    uv run python -m scripts.backfill_participated_in

Re-runs are safe (MERGE + ON CREATE SET).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import UUID

from neo4j import AsyncGraphDatabase
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from personal_agent.config.settings import get_settings
from personal_agent.service.auth import get_or_create_user_by_email
from personal_agent.telemetry import get_logger

log = get_logger(__name__)
settings = get_settings()


async def _resolve_owner_uuid() -> UUID:
    """Look up the owner's UUID from Postgres.

    Returns:
        The owner's stable UUID from the users table.

    Raises:
        ValueError: If agent_owner_email is not configured.
    """
    if not settings.agent_owner_email:
        raise ValueError(
            "AGENT_OWNER_EMAIL is not set. Cannot resolve owner UUID for backfill."
        )
    engine = create_async_engine(settings.database_url)
    try:
        async with AsyncSession(engine) as db:
            uid = await get_or_create_user_by_email(db, settings.agent_owner_email)
            await db.commit()
            return uid
    finally:
        await engine.dispose()


async def _stream_sessions() -> list[tuple[str, UUID | None]]:
    """Stream (session_id, user_id) tuples from Postgres.

    Returns:
        List of (session_id_str, user_uuid_or_None) for all sessions.
    """
    engine = create_async_engine(settings.database_url)
    try:
        async with AsyncSession(engine) as db:
            result = await db.execute(
                text("SELECT session_id, user_id FROM sessions ORDER BY created_at")
            )
            rows = result.fetchall()
    finally:
        await engine.dispose()

    return [(str(r[0]), UUID(str(r[1])) if r[1] else None) for r in rows]


async def _backfill_session(
    neo4j_session,
    session_id: str,
    target_uid: UUID,
    source: str,
) -> dict[str, int]:
    """MERGE the PARTICIPATED_IN edges for one Session's Turns.

    Args:
        neo4j_session: Open Neo4j async session.
        session_id: The session UUID string whose Turns to backfill.
        target_uid: The user UUID to link as the :Person participant.
        source: Label for logging — "session" or "owner_fallback".

    Returns:
        Dict with keys 'turns', 'created', 'existed'.
    """
    result = await neo4j_session.run(
        """
        MATCH (t:Turn {session_id: $session_id})
        WITH t
        MATCH (p:Person {user_id: $target_uid})
        MERGE (p)-[r:PARTICIPATED_IN]->(t)
          ON CREATE SET r.created_at = t.timestamp,
                        r.backfilled = true
        RETURN
          count(t) AS turns,
          sum(CASE WHEN r.backfilled = true THEN 1 ELSE 0 END) AS backfilled_count
        """,
        session_id=session_id,
        target_uid=str(target_uid),
    )
    record = await result.single()
    if record is None:
        return {"turns": 0, "created": 0, "existed": 0}

    turns = record["turns"] or 0
    backfilled_count = record["backfilled_count"] or 0
    existed = max(turns - backfilled_count, 0)
    created = backfilled_count

    log.info(
        "backfill_participated_in_edges",
        session_id=session_id,
        user_id=str(target_uid),
        user_id_source=source,
        edges_created=created,
        edges_existed=existed,
    )
    return {"turns": turns, "created": created, "existed": existed}


async def _verify_owner_person(neo4j_driver, owner_uuid: UUID) -> None:
    """Fail loud if the owner's :Person node doesn't exist.

    Args:
        neo4j_driver: Connected Neo4j async driver.
        owner_uuid: The owner's UUID to verify.

    Raises:
        RuntimeError: If the :Person node is absent in Neo4j.
    """
    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (p:Person {user_id: $uid}) RETURN p LIMIT 1",
            uid=str(owner_uuid),
        )
        record = await result.single()
    if record is None:
        raise RuntimeError(
            f"Owner :Person {{user_id: {owner_uuid}}} not found in Neo4j. "
            "Has the FRE-213 bootstrap (get_or_provision_user_person) run? "
            "This script cannot continue without an owner anchor."
        )


async def run_backfill() -> dict[str, int]:
    """Main entrypoint — streams sessions and backfills edges.

    Returns:
        Aggregate counts: sessions_processed, edges_created, edges_existed.
    """
    owner_uuid = await _resolve_owner_uuid()
    log.info("backfill_owner_resolved", owner_uuid=str(owner_uuid))

    sessions = await _stream_sessions()
    log.info("backfill_sessions_loaded", session_count=len(sessions))

    neo4j_driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    try:
        await _verify_owner_person(neo4j_driver, owner_uuid)

        totals: dict[str, int] = {
            "sessions_processed": 0,
            "edges_created": 0,
            "edges_existed": 0,
        }
        async with neo4j_driver.session() as neo4j_session:
            for session_id, sess_user_id in sessions:
                target_uid = sess_user_id if sess_user_id else owner_uuid
                source = "session" if sess_user_id else "owner_fallback"
                counts = await _backfill_session(
                    neo4j_session, session_id, target_uid, source
                )
                totals["sessions_processed"] += 1
                totals["edges_created"] += counts["created"]
                totals["edges_existed"] += counts["existed"]

        log.info("backfill_summary", **totals)
        return totals
    finally:
        await neo4j_driver.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FRE-343 one-shot backfill — populate (:Person)-[:PARTICIPATED_IN]->(:Turn) edges."
    )
    parser.add_argument(
        "--confirm-prod",
        action="store_true",
        default=False,
        help=(
            "Required when AGENT_ENVIRONMENT is not 'test'. "
            "Confirms intent to write to the production substrate."
        ),
    )
    return parser.parse_args()


def main() -> int:
    """Synchronous CLI entrypoint.

    Returns:
        0 on success, 1 on failure.
    """
    args = _parse_args()
    from personal_agent.config.env_loader import Environment
    if settings.environment != Environment.TEST and not args.confirm_prod:
        print(
            "ERROR: Running against non-TEST environment without --confirm-prod.\n"
            "This script writes to the production substrate.\n"
            "Re-run with --confirm-prod if you intend to modify production data.",
            file=sys.stderr,
        )
        return 2
    try:
        totals = asyncio.run(run_backfill())
    except Exception as e:  # noqa: BLE001
        log.error("backfill_failed", error=str(e), exc_info=True)
        sys.stderr.write(f"BACKFILL FAILED: {e}\n")
        return 1

    sys.stdout.write(
        f"Backfill complete: {totals['sessions_processed']} sessions; "
        f"{totals['edges_created']} edges created, "
        f"{totals['edges_existed']} edges already existed.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
