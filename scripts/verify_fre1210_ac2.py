#!/usr/bin/env python3
r"""FRE-1210 AC-2 live verification: the heat ranking is discriminating, not decorative.

AC-2 has two parts:
  (a) No `access_count = 0` entity ever appears in the top-10 by
      `access_count * e^(-lambda * age)`.
  (b) The panel's ranking is reproduced independently: recompute the same
      ordering directly in Cypher (via a fresh `aggregate_kg_stats` call, the
      same code path the dashboard's `kg_stats.top_heat_entity` rows came
      from) and show the two top-10 lists agree.

(a) is enforced structurally by `_top_heat_entities`'s own exclusion guard
(unit-tested in `test_kg_stats_aggregate.py`) -- this script re-checks it
against real data as the live half of that proof, by looking up each ranked
entity's actual `access_count` directly.

(b) is checked by comparing the latest `kg_stats.top_heat_entity` rows
(written by the last scheduled projection run) against a freshly-recomputed
ranking run right now. Since `aggregate_kg_stats` is the only correct
implementation of this ranking (there is no independently-derived second
algorithm to cross-check against -- see the implementation plan's discussion
of why), "reproduced independently" means: the same code, invoked separately,
against the live graph, agrees with what was written.

Read-only against Postgres; the Neo4j re-scan is also read-only.

    uv run python scripts/verify_fre1210_ac2.py
"""

from __future__ import annotations

import asyncio
import sys

import orjson

from personal_agent.config.settings import get_settings
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn
from personal_agent.memory.kg_stats_aggregate import aggregate_kg_stats
from personal_agent.memory.service import MemoryService


async def _latest_stored_ranking(dsn: str) -> list[tuple[str, float]]:
    import asyncpg

    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        latest = await conn.fetchval(
            "SELECT max(observed_at) FROM kg_stats WHERE metric_name = 'top_heat_entity'"
        )
        if latest is None:
            return []
        rows = await conn.fetch(
            "SELECT dimension, metric_value FROM kg_stats "
            "WHERE metric_name = 'top_heat_entity' AND observed_at = $1 "
            "ORDER BY metric_value DESC",
            latest,
        )
        return [(r["dimension"], float(r["metric_value"])) for r in rows]
    finally:
        await conn.close()


async def _fresh_ranking(service: MemoryService) -> list[tuple[str, float]]:
    rows = await aggregate_kg_stats(service.driver)  # type: ignore[arg-type]
    top = [r for r in rows if r.metric_name == "top_heat_entity"]
    top.sort(key=lambda r: r.metric_value, reverse=True)
    return [(r.dimension or "", r.metric_value) for r in top]


async def _access_counts(service: MemoryService, names: list[str]) -> dict[str, int | None]:
    if not names:
        return {}
    async with service.driver.session() as session:  # type: ignore[union-attr]
        result = await session.run(
            "UNWIND $names AS n MATCH (e:Entity {name: n}) RETURN e.name AS name, e.access_count AS ac",
            names=names,
        )
        records = [record async for record in result]
    return {str(r["name"]): (int(r["ac"]) if r["ac"] is not None else None) for r in records}


async def _main() -> int:
    cfg = get_settings()
    service = MemoryService()
    if not await service.connect() or service.driver is None:
        print("could not connect to Neo4j", file=sys.stderr)
        return 1

    try:
        stored = await _latest_stored_ranking(_normalize_asyncpg_dsn(cfg.database_url))
        fresh = await _fresh_ranking(service)
        fresh_names = [name for name, _ in fresh]
        access_counts = await _access_counts(service, fresh_names)
    finally:
        await service.disconnect()

    stored_names = [name for name, _ in stored]
    fresh_names_only = [name for name, _ in fresh]

    ac2a_violations = [n for n in fresh_names_only if not access_counts.get(n)]
    ac2b_agrees = stored_names == fresh_names_only

    report = {
        "ac2a_no_never_read_in_top10": len(ac2a_violations) == 0,
        "ac2a_violations": ac2a_violations,
        "ac2b_stored_vs_fresh_agree": ac2b_agrees,
        "stored_ranking": stored_names,
        "fresh_ranking": fresh_names_only,
        "passed": len(ac2a_violations) == 0 and ac2b_agrees,
    }
    print(orjson.dumps(report, option=orjson.OPT_INDENT_2).decode())
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
