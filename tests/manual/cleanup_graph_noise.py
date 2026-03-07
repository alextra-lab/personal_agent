"""Graph noise cleanup: remove low-value entities from Neo4j.

Removes entities that pollute the knowledge graph with no recall value:
- Generic conversation participants ("User", "Assistant")
- Test/placeholder artifacts ("Test message", "Another message", etc.)
- Entities whose entire description of knowledge is "None" or empty AND whose
  name is clearly a meta-artifact (not a real-world concept)

Run dry-run first to preview what will be removed, then --execute to apply.

Usage:
    uv run python tests/manual/cleanup_graph_noise.py            # dry run
    uv run python tests/manual/cleanup_graph_noise.py --execute  # apply
    uv run python tests/manual/cleanup_graph_noise.py --stats    # stats only
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from neo4j import AsyncGraphDatabase

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "neo4j_dev_password"

# Entities that are always noise — generic conversation participants and test artifacts.
# These names match what the old extraction prompt consistently generated.
NOISE_ENTITY_NAMES: list[str] = [
    # Conversation participants (never meaningful as graph entities)
    "User",
    "Assistant",
    # Generic message placeholders
    "Test message",
    "Test",
    "Quick test",
    "Another message",
    "Original message",
    "Test query",
    "Message",
    # Generic meta-conversation concepts
    "Invalid routing",
    "Invalid Routing",
    "Invalid Target Model",
    "Ambiguous query",
    "Nonexistent Tool",
    "Topic",
    "Response",
    # Exact duplicates / near-duplicates with typos (keep canonical)
    "Forcqlquier",  # typo dup of Forcalquier
]

# Entity types that indicate systematic extraction errors.
# These will be reviewed (not deleted) and reported.
SUSPICIOUS_ENTITY_TYPES: list[str] = [
    "Message",
    "Response",
    "Entity",
    "Entity Name",
    "System Reply",
    "Query",
    "None",
    "Person|Concept",  # malformed compound type
    "Time/Place",       # malformed compound type
]


async def run_cleanup(dry_run: bool = True, stats_only: bool = False) -> None:
    """Connect to Neo4j and clean up noise entities."""
    driver = AsyncGraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    await driver.verify_connectivity()
    print(f"Connected to Neo4j at {NEO4J_URI}\n")

    async with driver.session() as session:
        # --- Stats ---
        r = await session.run("MATCH (n) RETURN labels(n)[0] as label, count(n) as cnt ORDER BY cnt DESC")
        records = await r.values()
        print("=== CURRENT NODE COUNTS ===")
        for row in records:
            print(f"  {row[0]}: {row[1]}")

        r = await session.run("MATCH ()-[r]->() RETURN type(r) as rel_type, count(r) as cnt ORDER BY cnt DESC")
        records = await r.values()
        print("\n=== CURRENT RELATIONSHIP COUNTS ===")
        for row in records:
            print(f"  {row[0]}: {row[1]}")

        r = await session.run(
            "MATCH (e:Entity) WHERE e.name IN $names "
            "RETURN e.name, e.mention_count ORDER BY e.mention_count DESC",
            names=NOISE_ENTITY_NAMES,
        )

        records = await r.values()
        total_noise_mentions = sum(r[1] or 0 for r in records)
        print(f"\n=== NOISE ENTITIES TO REMOVE ({len(records)} found, {total_noise_mentions} total noise mentions) ===")
        for row in records:
            print(f"  '{row[0]}': {row[1]} mentions")

        r = await session.run(
            "MATCH (e:Entity) WHERE e.entity_type IN $types "
            "RETURN e.entity_type, e.name LIMIT 30",
            types=SUSPICIOUS_ENTITY_TYPES,
        )
        records = await r.values()
        print(f"\n=== SUSPICIOUS ENTITY TYPES (review, not deleted: {len(records)} shown) ===")
        for row in records:
            print(f"  [{row[0]}] {row[1]}")

        if stats_only:
            await driver.close()
            return

        # --- Cleanup ---
        if dry_run:
            print("\n[DRY RUN] No changes made. Re-run with --execute to apply.")
        else:
            print("\n[EXECUTING] Removing noise entities and their relationships...")

            # Delete noise entities and all their relationships
            result = await session.run(
                """
                MATCH (e:Entity)
                WHERE e.name IN $names
                DETACH DELETE e
                """,
                names=NOISE_ENTITY_NAMES,
            )
            summary = await result.consume()
            print(f"  Deleted {summary.counters.nodes_deleted} Entity nodes")
            print(f"  Deleted {summary.counters.relationships_deleted} relationships")

            # Remove dangling DISCUSSES relationships pointing to now-gone entities
            # (already handled by DETACH DELETE above, but verify)
            r = await session.run(
                "MATCH (c:Conversation)-[:DISCUSSES]->(e:Entity) "
                "WHERE e.name IN $names "
                "RETURN count(*) as remaining",
                names=NOISE_ENTITY_NAMES,
            )
            remaining = (await r.single() or {}).get("remaining", 0)
            print(f"  Remaining dangling DISCUSSES edges to noise: {remaining}")

            # Clean up Turn.key_entities lists that reference noise entity names
            r = await session.run(
                """
                MATCH (t:Turn)
                WHERE any(e IN t.key_entities WHERE e IN $names)
                SET t.key_entities = [e IN t.key_entities WHERE NOT e IN $names]
                RETURN count(t) as updated
                """,
                names=NOISE_ENTITY_NAMES,
            )
            updated_count = (await r.single() or {}).get("updated", 0)
            print(f"  Cleaned key_entities lists on {updated_count} Turn nodes")

            # Final stats
            r = await session.run("MATCH (n) RETURN labels(n)[0] as label, count(n) as cnt ORDER BY cnt DESC")
            records = await r.values()
            print("\n=== POST-CLEANUP NODE COUNTS ===")
            for row in records:
                print(f"  {row[0]}: {row[1]}")

            r = await session.run("MATCH (e:Entity) RETURN e.name, e.mention_count ORDER BY e.mention_count DESC LIMIT 15")
            records = await r.values()
            print("\n=== TOP ENTITIES AFTER CLEANUP ===")
            for row in records:
                print(f"  {row[0]} (x{row[1]})")

    await driver.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    execute = "--execute" in args
    stats = "--stats" in args
    asyncio.run(run_cleanup(dry_run=not execute, stats_only=stats))
