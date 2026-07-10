"""ADR-0114 AC-1 / mechanism-AC-2 Cypher aggregation + report (FRE-839).

AC-1 (population-scale multi-parent accretion — mechanism gate): the
eligible set E = concepts mentioned in >=2 conversations. Pass = median
`MEMBER_OF` degree over E is >=2 AND >=60% of E carry >=2
provenance-distinct memberships (backed by assertions from a different
source conversation).

**Degree and provenance-distinctness are computed as two INDEPENDENT
conditions, never one inferred from the other** — a correction from this
ticket's plan-review (codex): the categorizer proposes 1-3 categories per
concept in a SINGLE call, so one episode alone can produce `MEMBER_OF`
degree 2 or 3 for a concept no other conversation ever mentions. A concept
whose degree came entirely from one chatty conversation must NOT count
toward the >=60% bar. See `compute_ac1_report`'s query for the conjunction.

Mechanism-AC-2 (alias resolution — not the full hard-negative pairwise P/R
bar, that's FRE-841/843's job): spot-checks that the ADR's own named
case-variant examples resolve to one Concept hub post-ingest.

Runnable standalone against the real populated sandbox:
    uv run python -m scripts.study.ac_proof
"""

from __future__ import annotations

import asyncio
import json
import statistics
from typing import Any

from scripts.study.neo4j_types import Neo4jDriver

# The ADR's own named case-variant examples (D2's forensic read of prod) —
# the mechanism-AC-2 spot-check, not the full hard-negative eval set
# (FRE-841/843's job).
_KNOWN_CASE_VARIANT_PAIRS: tuple[tuple[str, str], ...] = (
    ("Arterial calcification", "Arterial Calcification"),
    ("halitosis", "Halitosis"),
)

_AC1_QUERY = (
    "MATCH (c:Concept)-[:MENTIONED_IN]->(ep:Episode) "
    "WITH c, count(DISTINCT ep) AS episode_count "
    "WHERE episode_count >= 2 "
    "OPTIONAL MATCH (c)-[m:MEMBER_OF]->(:Category) "
    "WITH c, count(m) AS degree "
    "OPTIONAL MATCH (c)<-[:ABOUT]-(a:MembershipAssertion)<-[:PRODUCED]-(:Mention)"
    "<-[:HAS_MENTION]-(ep2:Episode) "
    "WITH c, degree, count(DISTINCT ep2) AS backing_episode_count "
    "RETURN c.id AS concept_id, degree AS degree, backing_episode_count AS backing_episode_count"
)


async def compute_ac1_report(driver: Neo4jDriver) -> dict[str, Any]:
    """Compute the AC-1 report over the real populated sandbox.

    Args:
        driver: Connected async Neo4j driver pointed at the study sandbox.

    Returns:
        `{eligible_set_size, median_degree, pct_meeting_bar}` — a null
        result (0/0/0.0) is reported honestly, not reframed (a clean null
        is a valid, budgeted ADR-0114 outcome).
    """
    async with driver.session() as session:
        result = await session.run(_AC1_QUERY)
        rows = [r async for r in result]

    if not rows:
        return {"eligible_set_size": 0, "median_degree": 0, "pct_meeting_bar": 0.0}

    degrees = [int(r["degree"]) for r in rows]
    meets_bar = [int(r["degree"]) >= 2 and int(r["backing_episode_count"]) >= 2 for r in rows]

    return {
        "eligible_set_size": len(rows),
        "median_degree": statistics.median(degrees),
        "pct_meeting_bar": sum(meets_bar) / len(rows),
    }


async def compute_mechanism_ac2_spot_check(
    driver: Neo4jDriver,
    *,
    pairs: tuple[tuple[str, str], ...] | list[tuple[str, str]] = _KNOWN_CASE_VARIANT_PAIRS,
) -> list[dict[str, Any]]:
    """Spot-check that each named case-variant pair resolves to one Concept hub.

    Args:
        driver: Connected async Neo4j driver pointed at the study sandbox.
        pairs: Surface-name pairs to check (default: the ADR's own named
            examples).

    Returns:
        One `{pair, same_hub}` dict per pair.
    """
    results = []
    async with driver.session() as session:
        for pair in pairs:
            surface_a, surface_b = pair
            result = await session.run(
                "MATCH (s1:Surface {normalized_name: toLower(trim($a))})-[:ALIAS_OF]->(c1:Concept), "
                "(s2:Surface {normalized_name: toLower(trim($b))})-[:ALIAS_OF]->(c2:Concept) "
                "RETURN c1.id = c2.id AS same_hub",
                {"a": surface_a, "b": surface_b},
            )
            record = await result.single()
            results.append(
                {"pair": pair, "same_hub": bool(record["same_hub"]) if record else False}
            )
    return results


async def _amain() -> dict[str, Any]:
    from neo4j import AsyncGraphDatabase

    from scripts.study.config import StudySettings

    settings = StudySettings()
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    try:
        ac1_report = await compute_ac1_report(driver)
        ac2_spot_check = await compute_mechanism_ac2_spot_check(driver)
    finally:
        await driver.close()

    return {"ac1": ac1_report, "mechanism_ac2_spot_check": ac2_spot_check}


def main() -> None:
    """CLI entrypoint."""
    report = asyncio.run(_amain())
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
