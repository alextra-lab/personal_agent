"""Migrate old-format entity types to canonical taxonomy (ADR-0024).

The entity extraction prompt was updated to use 7 canonical types:
  Person, Organization, Location, Technology, Concept, Event, Topic

Pre-cloud entities may have old types like PROGRAMMING_LANGUAGE, FRAMEWORK,
LOCATION (all-caps), TOPIC (all-caps), etc. This script normalizes them.

Usage:
    uv run python tests/manual/migrate_entity_types.py            # dry run
    uv run python tests/manual/migrate_entity_types.py --execute  # apply
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from neo4j import AsyncGraphDatabase

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "neo4j_dev_password"

# Old entity_type -> canonical type mapping
TYPE_MIGRATION_MAP: dict[str, str] = {
    # Language/framework types -> Technology
    "PROGRAMMING_LANGUAGE": "Technology",
    "FRAMEWORK": "Technology",
    "TOOL": "Technology",
    "SOFTWARE": "Technology",
    "LIBRARY": "Technology",
    "API": "Technology",
    "MODEL": "Technology",
    # All-caps versions of canonical types
    "LOCATION": "Location",
    "TOPIC": "Topic",
    "PERSON": "Person",
    "ORGANIZATION": "Organization",
    "CONCEPT": "Concept",
    "EVENT": "Event",
    "TECHNOLOGY": "Technology",
    # Other observed malformed types
    "Place": "Location",
    "PLACE": "Location",
    "Other": "Topic",
    "OTHER": "Topic",
}


async def run_migration(dry_run: bool = True) -> None:
    """Connect to Neo4j and migrate entity types."""
    driver = AsyncGraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    await driver.verify_connectivity()
    print(f"Connected to Neo4j at {NEO4J_URI}\n")

    async with driver.session() as session:
        # Show current entity type distribution
        r = await session.run(
            """
            MATCH (e:Entity)
            RETURN e.entity_type AS type, count(e) AS cnt
            ORDER BY cnt DESC
            """
        )
        records = await r.values()
        print("=== CURRENT ENTITY TYPE DISTRIBUTION ===")
        for row in records:
            marker = " <-- WILL MIGRATE" if row[0] in TYPE_MIGRATION_MAP else ""
            print(f"  {row[0]}: {row[1]}{marker}")

        # Show what will be migrated
        old_types = list(TYPE_MIGRATION_MAP.keys())
        r = await session.run(
            """
            MATCH (e:Entity)
            WHERE e.entity_type IN $old_types
            RETURN e.entity_type AS old_type, e.name AS name
            ORDER BY e.entity_type, e.name
            """,
            old_types=old_types,
        )
        records = await r.values()
        print(f"\n=== ENTITIES TO MIGRATE ({len(records)} total) ===")
        for row in records:
            new_type = TYPE_MIGRATION_MAP[row[0]]
            print(f"  [{row[0]} -> {new_type}] {row[1]}")

        if not records:
            print("\n  No entities need migration. Graph is already normalized.")
            await driver.close()
            return

        if dry_run:
            print("\n[DRY RUN] No changes made. Re-run with --execute to apply.")
        else:
            print("\n[EXECUTING] Migrating entity types...")
            total_updated = 0
            for old_type, new_type in TYPE_MIGRATION_MAP.items():
                r = await session.run(
                    """
                    MATCH (e:Entity {entity_type: $old_type})
                    SET e.entity_type = $new_type
                    RETURN count(e) AS updated
                    """,
                    old_type=old_type,
                    new_type=new_type,
                )
                record = await r.single()
                count = record["updated"] if record else 0
                if count > 0:
                    print(f"  {old_type} -> {new_type}: {count} entities updated")
                    total_updated += count

            print(f"\n  Total entities migrated: {total_updated}")

            # Show post-migration distribution
            r = await session.run(
                """
                MATCH (e:Entity)
                RETURN e.entity_type AS type, count(e) AS cnt
                ORDER BY cnt DESC
                """
            )
            records = await r.values()
            print("\n=== POST-MIGRATION ENTITY TYPE DISTRIBUTION ===")
            for row in records:
                print(f"  {row[0]}: {row[1]}")

    await driver.close()


if __name__ == "__main__":
    execute = "--execute" in sys.argv
    asyncio.run(run_migration(dry_run=not execute))
