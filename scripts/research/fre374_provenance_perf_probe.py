#!/usr/bin/env python3
"""FRE-374 D4 gate: measure write latency for descriptions[] append vs. single-field set.

Compares two MERGE patterns on a test Neo4j graph:
  A. Current: SET e.description = CASE WHEN ... THEN $desc ELSE e.description END
  B. Proposed: SET e.descriptions = COALESCE(e.descriptions, []) + [{text: $desc, ts: datetime()}]

Runs 500 entity writes for each pattern and reports p50/p95/p99 latency.

Decision rule: if median overhead of B vs A is < 25%, proceed with D4 description
provenance schema migration. If >= 25%, evaluate server-side alternatives first.

Record results in docs/research/fre374-provenance-perf-probe-results.md before
moving ADR-0073 D4 from Deferred to Accepted.

Usage:
    make test-infra-up   # start test Neo4j on :7688
    APP_ENV=test uv run python scripts/research/fre374_provenance_perf_probe.py
"""

from __future__ import annotations

import asyncio
import statistics
import time
from typing import Any
from uuid import uuid4

import structlog

log = structlog.get_logger(__name__)

ITERATIONS = 500
_TEST_ENTITY_PREFIX = f"fre374-perf-{uuid4().hex[:8]}"

PATTERN_A = """
MERGE (e:Entity {name: $name})
SET e.description = CASE WHEN e.description IS NULL OR trim(e.description) = ''
                    THEN $description ELSE e.description END,
    e.last_seen = datetime()
"""

PATTERN_B = """
MERGE (e:Entity {name: $name})
SET e.descriptions = COALESCE(e.descriptions, []) + [{text: $description, ts: datetime()}],
    e.last_seen = datetime()
"""


async def _run_benchmark(driver: Any, pattern: str, desc: str) -> list[float]:
    """Run ITERATIONS writes of the given pattern.

    Args:
        driver: Neo4j async driver.
        pattern: Cypher pattern string with $name and $description params.
        desc: Base description string (iteration number appended).

    Returns:
        List of per-write latencies in milliseconds.
    """
    latencies: list[float] = []
    for i in range(ITERATIONS):
        name = f"{_TEST_ENTITY_PREFIX}-{i}"
        start = time.perf_counter()
        async with driver.session() as session:
            await session.run(pattern, name=name, description=f"{desc} {i}")
        latencies.append((time.perf_counter() - start) * 1000)
    return latencies


async def _cleanup(driver: Any) -> None:
    """Remove all test entities created by this probe run."""
    async with driver.session() as session:
        await session.run(
            "MATCH (e:Entity) WHERE e.name STARTS WITH $prefix DETACH DELETE e",
            prefix=_TEST_ENTITY_PREFIX,
        )


def _percentile(sorted_data: list[float], pct: float) -> float:
    """Calculate the percentile of a sorted list.

    Args:
        sorted_data: Pre-sorted list of values.
        pct: Percentile (0.0 to 1.0).

    Returns:
        The percentile value.
    """
    idx = int(pct * len(sorted_data))
    return sorted_data[min(idx, len(sorted_data) - 1)]


async def main() -> None:
    """Run the benchmark and print results."""
    from neo4j import AsyncGraphDatabase
    from personal_agent.config import get_settings

    settings = get_settings()
    driver = AsyncGraphDatabase.driver(  # fre-375-allow: benchmark probe, reads settings URI, creates test-prefixed entities only
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    await driver.verify_connectivity()
    log.info("probe_connected", uri=settings.neo4j_uri)

    try:
        log.info("running_pattern_a", iterations=ITERATIONS)
        lat_a = await _run_benchmark(driver, PATTERN_A, "Description for perf test")
        log.info("running_pattern_b", iterations=ITERATIONS)
        lat_b = await _run_benchmark(driver, PATTERN_B, "Description for perf test")
    finally:
        await _cleanup(driver)
        await driver.close()

    lat_a_sorted = sorted(lat_a)
    lat_b_sorted = sorted(lat_b)

    def _fmt(lats: list[float], label: str) -> str:
        return (
            f"{label}: "
            f"p50={statistics.median(lats):.1f}ms "
            f"p95={_percentile(lats, 0.95):.1f}ms "
            f"p99={_percentile(lats, 0.99):.1f}ms "
            f"mean={statistics.mean(lats):.1f}ms"
        )

    median_a = statistics.median(lat_a_sorted)
    median_b = statistics.median(lat_b_sorted)
    overhead_pct = ((median_b - median_a) / median_a * 100) if median_a > 0 else float("inf")

    print(f"\n=== FRE-374 provenance-on-write benchmark ({ITERATIONS} iterations each) ===")
    print(_fmt(lat_a_sorted, "Pattern A (current CASE WHEN)   "))
    print(_fmt(lat_b_sorted, "Pattern B (append to descriptions[])"))
    print(f"\nMedian overhead of B vs A: {overhead_pct:+.1f}%")
    if overhead_pct < 25:
        print("DECISION: Overhead < 25% — proceed with D4 schema migration in follow-up issue.")
    else:
        print("DECISION: Overhead >= 25% — evaluate alternatives before committing to D4.")
    print("\nRecord these numbers in docs/research/fre374-provenance-perf-probe-results.md")


if __name__ == "__main__":
    asyncio.run(main())
