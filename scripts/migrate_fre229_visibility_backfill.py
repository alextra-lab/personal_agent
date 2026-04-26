#!/usr/bin/env python3
"""One-time Neo4j backfill: set visibility='public' on all existing nodes (FRE-229).

Sets the visibility property to 'public' on every :Turn, :Entity, :Session node,
and on every relationship, where visibility is currently NULL. This preserves
the existing shared-graph behaviour: all historical data stays visible to
everyone until future classification (private/group tagging) is applied.

Run order: run this script BEFORE deploying the FRE-229 build, or immediately
after deployment. The read filter in memory/service.py includes an IS NULL
grace clause so NULL nodes remain visible during the rollout window, but
running this script removes that dependency and makes the data model canonical.

Usage:
    uv run python scripts/migrate_fre229_visibility_backfill.py

The script is idempotent — safe to run more than once.
"""

import asyncio
import sys

try:
    from neo4j import AsyncGraphDatabase as Neo4jAsyncGraphDatabase
except ModuleNotFoundError:
    print("neo4j package not installed — run 'uv sync' first.", file=sys.stderr)
    sys.exit(1)

from personal_agent.config.settings import get_settings

settings = get_settings()


async def run_backfill() -> None:
    """Execute the Neo4j visibility backfill."""
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

    async with driver.session() as session:
        # Backfill nodes
        for label in ("Turn", "Entity", "Session"):
            result = await session.run(
                f"MATCH (n:{label}) WHERE n.visibility IS NULL "
                f"SET n.visibility = 'public' "
                f"RETURN count(n) AS n"
            )
            rec = await result.single()
            count = rec["n"] if rec else 0
            print(f"✓ :{label} nodes backfilled: {count}")

        # Backfill relationships (any type)
        result = await session.run(
            "MATCH ()-[r]->() WHERE r.visibility IS NULL "
            "SET r.visibility = 'public' "
            "RETURN count(r) AS n"
        )
        rec = await result.single()
        count = rec["n"] if rec else 0
        print(f"✓ Relationships backfilled: {count}")

    await driver.close()
    print("\nBackfill complete — all existing nodes tagged visibility='public'.")


if __name__ == "__main__":
    asyncio.run(run_backfill())
