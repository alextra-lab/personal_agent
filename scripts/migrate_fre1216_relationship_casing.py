#!/usr/bin/env python3
"""One-time Neo4j backfill: canonicalize relationship-type casing variants (FRE-1216 #1).

`create_relationship` now normalizes to uppercase before every write (memory/service.py), so no
*new* casing variant can be introduced from that path. This script repairs relationships that
predate that fix (the live-graph `USEs` vs `USES` defect — 1 edge, alongside 2,939+ `USES` edges).

For each relationship whose type is not already its own uppercase form:
  - If no canonical-cased relationship already connects the same (source, target) pair: rename in
    place (`apoc.refactor.setType`) — the properties travel with the relationship unchanged.
  - If a canonical-cased relationship already connects the same pair (the live case: `USEs` and a
    parallel `USES` both connect the same two nodes): merge the variant's properties onto the
    canonical edge — prefer non-null; `access_count` takes the max (not a sum: both edges' counts
    already reflect real historical access, so summing would double-count); `created_at`/
    `first_accessed_at` take the earliest; `last_accessed_at` takes the latest — then delete the
    variant edge.

Idempotent: the candidate predicate is `type(r) <> toUpper(type(r))`, so a converted relationship
is excluded from all future runs.

Usage:
    uv run python scripts/migrate_fre1216_relationship_casing.py
    uv run python scripts/migrate_fre1216_relationship_casing.py --confirm-prod
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass
from typing import Any

try:
    from neo4j import AsyncGraphDatabase as Neo4jAsyncGraphDatabase
except ModuleNotFoundError:
    print("neo4j package not installed — run 'uv sync' first.", file=sys.stderr)
    sys.exit(1)

from personal_agent.config.settings import get_settings

settings = get_settings()


@dataclass(frozen=True)
class VariantEdge:
    """A relationship whose type is not its own canonical (uppercase) form."""

    element_id: str
    rel_type: str
    canonical_type: str
    source_element_id: str
    target_element_id: str
    properties: dict[str, Any]


async def _fetch_variant_edges(driver: object) -> list[VariantEdge]:
    async with driver.session() as session:  # type: ignore[attr-defined]
        result = await session.run(
            """
            MATCH (a)-[r]->(b)
            WHERE type(r) <> toUpper(type(r))
            RETURN elementId(r) AS rel_id, type(r) AS rel_type,
                   elementId(a) AS source_id, elementId(b) AS target_id,
                   properties(r) AS props
            """
        )
        rows = await result.data()
    return [
        VariantEdge(
            element_id=row["rel_id"],
            rel_type=row["rel_type"],
            canonical_type=row["rel_type"].upper(),
            source_element_id=row["source_id"],
            target_element_id=row["target_id"],
            properties=row["props"] or {},
        )
        for row in rows
    ]


async def _canonical_edge_element_id(
    driver: object, source_element_id: str, target_element_id: str, canonical_type: str
) -> str | None:
    async with driver.session() as session:  # type: ignore[attr-defined]
        # Match on any relationship type and filter with type(canon) = $canonical_type — not an
        # f-string-interpolated MATCH pattern. canonical_type originates from a live type(r) read
        # (relationship_type is unconstrained free text upstream, from LLM extraction — see
        # models.py's Relationship.relationship_type), so it must never be embedded in Cypher
        # syntax; a bound parameter is the only safe way to compare against it (self-review,
        # security-review).
        result = await session.run(
            """
            MATCH (a)-[canon]->(b)
            WHERE elementId(a) = $source_id AND elementId(b) = $target_id
              AND type(canon) = $canonical_type
            RETURN elementId(canon) AS canon_id
            LIMIT 1
            """,
            source_id=source_element_id,
            target_id=target_element_id,
            canonical_type=canonical_type,
        )
        rec = await result.single()
    return rec["canon_id"] if rec else None


def _merge_properties(canonical: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    """Reconcile a variant edge's properties onto the canonical edge's, preferring non-null.

    ``access_count`` takes the max (both already reflect real historical access — summing would
    double-count). Timestamp-shaped keys ending in ``_at`` take the earliest for ``created_at``/
    ``first_accessed_at`` and the latest for ``last_accessed_at``; everything else prefers the
    canonical edge's existing (non-null) value, falling back to the variant's.
    """
    merged = dict(canonical)
    for key, variant_value in variant.items():
        if variant_value is None:
            continue
        canonical_value = merged.get(key)
        if canonical_value is None:
            merged[key] = variant_value
        elif key == "access_count":
            merged[key] = max(canonical_value, variant_value)
        elif key in ("created_at", "first_accessed_at"):
            merged[key] = min(canonical_value, variant_value)
        elif key == "last_accessed_at":
            merged[key] = max(canonical_value, variant_value)
        # else: canonical's existing non-null value wins (already in `merged`).
    return merged


async def run_backfill(driver: object) -> dict[str, int]:
    """Canonicalize every relationship-type casing variant. Returns a summary count.

    Args:
        driver: A connected async Neo4j driver.

    Returns:
        ``{"renamed": n, "merged": n}``.
    """
    edges = await _fetch_variant_edges(driver)
    renamed = 0
    merged = 0

    for edge in edges:
        canon_id = await _canonical_edge_element_id(
            driver, edge.source_element_id, edge.target_element_id, edge.canonical_type
        )
        async with driver.session() as session:  # type: ignore[attr-defined]
            if canon_id is None:
                await session.run(
                    "MATCH ()-[r]->() WHERE elementId(r) = $rel_id "
                    "CALL apoc.refactor.setType(r, $canonical_type) YIELD input "
                    "RETURN input",
                    rel_id=edge.element_id,
                    canonical_type=edge.canonical_type,
                )
                renamed += 1
            else:
                canon_result = await session.run(
                    "MATCH ()-[canon]->() WHERE elementId(canon) = $canon_id "
                    "RETURN properties(canon) AS props",
                    canon_id=canon_id,
                )
                canon_rec = await canon_result.single()
                canon_props = dict(canon_rec["props"] or {}) if canon_rec else {}
                merged_props = _merge_properties(canon_props, edge.properties)

                await session.run(
                    "MATCH ()-[canon]->() WHERE elementId(canon) = $canon_id SET canon = $props",
                    canon_id=canon_id,
                    props=merged_props,
                )
                await session.run(
                    "MATCH ()-[r]->() WHERE elementId(r) = $rel_id DELETE r",
                    rel_id=edge.element_id,
                )
                merged += 1

    return {"renamed": renamed, "merged": merged}


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

    summary = await run_backfill(driver)
    print(f"✓ Renamed (no conflict): {summary['renamed']}")
    print(f"✓ Merged onto an existing canonical edge: {summary['merged']}")

    await driver.close()
    print("\nBackfill complete.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "One-time Neo4j backfill: canonicalize relationship-type casing "
            "variants, e.g. USEs -> USES (FRE-1216 #1)."
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
