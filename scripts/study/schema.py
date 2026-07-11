"""ADR-0114 D2 evidence-layer schema for the study sandbox (FRE-839).

Applies the forward-compatible schema (evidence layer, preserved ``kind``,
``SUBSUMES`` present-but-unused) to the isolated study Neo4j
(``docker-compose.study.yml``, FRE-838). All constraints are node-key/
uniqueness constraints only — ``neo4j:5.26-community`` (the image this
sandbox runs) does not support relationship property existence constraints
(Enterprise-only).

Schema-only-in-v0 elements, declared here as documentation, never
constrained (there is nothing to constrain — the types don't exist until a
later ticket populates them):

- ``(:Category)-[:SUBSUMES {strength}]->(:Category)`` — populated by the v1
  GDS Leiden proposer (FRE-855, arm F).
- ``RelationAssertion`` — a separate directional-relation-typing arm with
  its own gold set (FRE-840+), not part of the core category hypothesis.
- ``(:Category)-[:CANONICALIZED_AS {tau_merge, decided_at}]->(:Category)`` —
  the offline consolidator's (FRE-842) alias-merge write-back
  (``consolidator.apply_canonicalization_to_graph``): an absorbed category
  points at its canonical representative, kept (never deleted) so evidence
  assertions' ``PROPOSES`` links stay inspectable. No uniqueness constraint
  needed — it is a derived, recomputable edge, not an identity. FRE-842's
  own sweep computes canonicalizations read-only in memory and never writes
  this edge; only a real single-τ_merge* run (FRE-843) does.

Usage:
    uv run python -m scripts.study.schema
"""

from __future__ import annotations

import asyncio

from scripts.study.neo4j_types import Neo4jDriver

CONCEPT_EMBEDDING_DIMENSIONS = 1024  # matches the OVH-managed embedder's live dimension

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    "CREATE CONSTRAINT concept_id_unique IF NOT EXISTS FOR (c:Concept) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT surface_normalized_name_unique IF NOT EXISTS "
    "FOR (s:Surface) REQUIRE s.normalized_name IS UNIQUE",
    "CREATE CONSTRAINT category_normalized_name_unique IF NOT EXISTS "
    "FOR (cat:Category) REQUIRE cat.normalized_name IS UNIQUE",
    "CREATE CONSTRAINT episode_id_unique IF NOT EXISTS FOR (e:Episode) REQUIRE e.id IS UNIQUE",
    "CREATE CONSTRAINT mention_id_unique IF NOT EXISTS FOR (m:Mention) REQUIRE m.id IS UNIQUE",
    "CREATE CONSTRAINT assertion_id_unique IF NOT EXISTS "
    "FOR (a:MembershipAssertion) REQUIRE a.id IS UNIQUE",
    "CREATE VECTOR INDEX concept_embedding IF NOT EXISTS "
    "FOR (c:Concept) ON c.embedding "
    "OPTIONS {indexConfig: {`vector.dimensions`: "
    f"{CONCEPT_EMBEDDING_DIMENSIONS}"
    ", `vector.similarity_function`: 'cosine'}}",
)


async def apply_schema(driver: Neo4jDriver) -> None:
    """Apply every schema constraint/index to the study sandbox.

    Idempotent (every statement carries ``IF NOT EXISTS``) — safe to call on
    every ``run_ingest.py`` invocation, not just once. Neo4j requires schema
    statements to run individually (no ``UNWIND``-batching for DDL), so this
    issues one ``session.run()`` per statement.

    Args:
        driver: Connected async Neo4j driver pointed at the study sandbox.
    """
    async with driver.session() as session:
        for statement in _SCHEMA_STATEMENTS:
            await session.run(statement)


async def _amain() -> None:
    from neo4j import AsyncGraphDatabase  # noqa: PLC0415

    from scripts.study.config import StudySettings  # noqa: PLC0415

    settings = StudySettings()
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    try:
        await apply_schema(driver)
        print("Schema applied.")
    finally:
        await driver.close()


def main() -> None:
    """CLI entrypoint."""
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
