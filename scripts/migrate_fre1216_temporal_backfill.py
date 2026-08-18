#!/usr/bin/env python3
"""One-time Neo4j backfill: convert STRING-typed first_seen/last_seen to DATE_TIME (FRE-1216 #2).

Historical `:Entity` nodes carry `first_seen`/`last_seen` as plain ISO strings — a range query
over either compares lexicographically, not chronologically (`"2026-9-1" > "2026-10-1"` is a
wrong-but-true string comparison). Every *current* write site (memory/service.py's mention path
and `create_entity`) already writes native Neo4j `datetime()` values; only historical rows, written
before those call sites used `datetime()`, need repair.

Idempotent: `apoc.meta.cypher.type(...) = 'STRING'` is the candidate predicate, so a converted node
is excluded from all future runs.

Usage:
    uv run python scripts/migrate_fre1216_temporal_backfill.py
    uv run python scripts/migrate_fre1216_temporal_backfill.py --confirm-prod
"""

import argparse
import asyncio
import sys

try:
    from neo4j import AsyncGraphDatabase as Neo4jAsyncGraphDatabase
except ModuleNotFoundError:
    print("neo4j package not installed — run 'uv sync' first.", file=sys.stderr)
    sys.exit(1)

from personal_agent.config.settings import get_settings

settings = get_settings()


async def run_backfill(driver: object) -> dict[str, int]:
    """Convert STRING-typed first_seen/last_seen on :Entity nodes to native DATE_TIME.

    Args:
        driver: A connected async Neo4j driver.

    Returns:
        Count of nodes touched per field (``{"first_seen": n, "last_seen": n}``).
    """
    counts: dict[str, int] = {}
    async with driver.session() as session:  # type: ignore[attr-defined]
        for field in ("first_seen", "last_seen"):
            result = await session.run(
                f"""
                MATCH (e:Entity)
                WHERE apoc.meta.cypher.type(e.{field}) = 'STRING'
                SET e.{field} = datetime(e.{field})
                RETURN count(e) AS n
                """
            )
            rec = await result.single()
            counts[field] = rec["n"] if rec else 0
    return counts


async def _amain() -> None:
    uri = settings.neo4j_uri
    user = settings.neo4j_user
    password = settings.neo4j_password

    driver = Neo4jAsyncGraphDatabase.driver(uri, auth=(user, password))
    try:
        await driver.verify_connectivity()
        print(f"✓ Connected to Neo4j at {uri}")
    except Exception as e:
        print(f"✗ Cannot connect to Neo4j: {e}", file=sys.stderr)
        await driver.close()
        sys.exit(1)

    counts = await run_backfill(driver)
    for field, n in counts.items():
        print(f"✓ {field} converted STRING → DATE_TIME: {n}")

    await driver.close()
    print("\nBackfill complete.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "One-time Neo4j backfill: convert STRING-typed first_seen/last_seen "
            "on :Entity nodes to native DATE_TIME (FRE-1216 #2)."
        )
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


if __name__ == "__main__":
    args = _parse_args()
    from personal_agent.config.env_loader import Environment

    if settings.environment != Environment.TEST and not args.confirm_prod:
        print(
            "ERROR: Running against non-TEST environment without --confirm-prod.\n"
            "This script writes to the production substrate.\n"
            "Re-run with --confirm-prod if you intend to modify production data.",
            file=sys.stderr,
        )
        sys.exit(2)
    asyncio.run(_amain())
