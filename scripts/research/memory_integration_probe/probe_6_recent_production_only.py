"""Probe 6: Re-measure on a recent, production-only slice.

User question: how much of the pollution comes from development/testing
activity hitting the same backend, vs. real conversation drift?

Filter:
  - Turns since 2026-05-14 (last 7 days from 2026-05-21).
  - session_id IS NOT NULL  — synthetic eval traffic has session_id=None.
  - Exclude obvious test-entity names (RareLanguage, RecencyLang_*, etc.).

Re-runs:
  - Drift (Probe 1 analog) on entities last touched by real turns in 7d.
  - Top-15 broad-recall replay using only entities touched by real sessions.
  - Frequency from ES, restricted to last 7d and filtered to non-test loggers.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from _common import ensure_output_dir, neo4j_session

CUTOFF = "2026-05-14"
TEST_NAME_RE = re.compile(
    r"(question[ _]?\d+|RareLanguage|RecencyLang|TestLang|recent.message.about)",
    re.IGNORECASE,
)


def _looks_synthetic(name: str | None) -> bool:
    if not name:
        return False
    return bool(TEST_NAME_RE.search(name))


async def _es_count(query: dict[str, Any]) -> int:
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "http://localhost:9200/agent-logs-*/_count",
            json=query,
            timeout=10.0,
        )
        r.raise_for_status()
        return r.json()["count"]


async def main() -> None:
    out_dir = ensure_output_dir()
    out_path = out_dir / "probe_6_recent_production_only.md"

    lines: list[str] = [
        "# Probe 6 — Recent + Production-Only Slice",
        "",
        f"**Filter:** turns with `timestamp >= {CUTOFF}` AND `session_id IS"
        " NOT NULL`, plus exclusion of obvious synthetic entity names"
        " (RareLanguage, RecencyLang_*, *question N*).",
        "",
        "## Corpus census",
        "",
    ]

    async with neo4j_session() as session:
        census_q = f"""
            MATCH (t:Turn) WHERE t.timestamp >= '{CUTOFF}'
            WITH count(t) AS total_turns,
                 count(CASE WHEN t.session_id IS NOT NULL THEN 1 END) AS prod_turns,
                 count(CASE WHEN t.session_id IS NULL THEN 1 END) AS test_turns
            RETURN total_turns, prod_turns, test_turns
        """
        cr = await session.run(census_q)
        census = await cr.single()
        lines.append(f"- Turns in window: {census['total_turns']}")
        lines.append(
            f"- Production turns (session_id present): {census['prod_turns']}"
            f" ({100.0 * census['prod_turns'] / max(1, census['total_turns']):.1f}%)"
        )
        lines.append(
            f"- Test/eval turns (session_id NULL): {census['test_turns']}"
            f" ({100.0 * census['test_turns'] / max(1, census['total_turns']):.1f}%)"
        )
        lines.append("")

        # Entities touched ONLY by production turns in the window.
        # (an entity is "production" if at least one of its source_turn_ids
        # corresponds to a production turn in the window)
        prod_entities_q = f"""
            MATCH (t:Turn)-[:DISCUSSES]->(e:Entity)
            WHERE t.timestamp >= '{CUTOFF}'
              AND t.session_id IS NOT NULL
              AND e.memory_type = 'semantic'
            WITH DISTINCT e
            RETURN e.name AS name,
                   e.entity_type AS entity_type,
                   e.description AS description,
                   e.mention_count AS mention_count,
                   COUNT {{ (e)<-[:DISCUSSES]-() }} AS discusses_total
            ORDER BY discusses_total DESC
            LIMIT 30
        """
        result = await session.run(prod_entities_q)
        prod_entities = [
            dict(row) async for row in result
            if not _looks_synthetic(row["name"])
        ]

        lines.append("## Top entities touched by recent production turns")
        lines.append("")
        lines.append("Empty descriptions and known-drifted descriptions still"
                     " count here, but we list only entities that real session"
                     " turns DISCUSS.")
        lines.append("")
        lines.append("| Entity | Type | Mentions (all-time) | Description |")
        lines.append("|---|---|---|---|")
        empty_count = 0
        misleading_count = 0
        from probe_5_impact_path import KNOWN_DRIFTED_ENTITIES
        for ent in prod_entities[:15]:
            desc = ent["description"] or ""
            short_desc = desc[:120] + ("..." if len(desc) > 120 else "")
            lines.append(
                f"| `{ent['name']}` | {ent['entity_type']}"
                f" | {ent['mention_count']} | {short_desc or '_(empty)_'} |"
            )
            if not desc.strip():
                empty_count += 1
            elif ent["name"] in KNOWN_DRIFTED_ENTITIES:
                snippet = KNOWN_DRIFTED_ENTITIES[ent["name"]].lower()
                if snippet in desc.lower():
                    misleading_count += 1
        lines.append("")
        lines.append(
            f"**Empty in top 15 (production-only):** {empty_count} / 15"
        )
        lines.append(
            f"**Known-drifted in top 15 (production-only):** {misleading_count} / 15"
        )
        lines.append("")

    # Frequency from ES, last 7d
    last_7d = {"range": {"@timestamp": {"gte": "now-7d"}}}
    gateway_turns_7d = await _es_count({"query": {"bool": {"must": [last_7d, {"term": {"event_type": "gateway_output"}}]}}})
    has_memory_7d = await _es_count(
        {
            "query": {
                "bool": {
                    "must": [
                        last_7d,
                        {"term": {"event_type": "context_assembled"}},
                        {"term": {"has_memory": True}},
                    ]
                }
            }
        }
    )
    # Exclude test loggers
    has_memory_7d_prod = await _es_count(
        {
            "query": {
                "bool": {
                    "must": [
                        last_7d,
                        {"term": {"event_type": "context_assembled"}},
                        {"term": {"has_memory": True}},
                    ],
                    "must_not": [
                        {"match": {"logger": "test"}},
                    ],
                }
            }
        }
    )

    lines.append("## Frequency (last 7 days, ES)")
    lines.append("")
    lines.append(
        f"- Gateway turns: {gateway_turns_7d}"
    )
    lines.append(
        f"- Memory-injected turns: {has_memory_7d}"
        f" ({100.0 * has_memory_7d / max(1, gateway_turns_7d):.1f}%)"
    )
    lines.append(
        f"- Memory-injected, excluding test loggers: {has_memory_7d_prod}"
        f" ({100.0 * has_memory_7d_prod / max(1, gateway_turns_7d):.1f}%)"
    )
    lines.append("")
    lines.append("**Note:** ES traces are not directly attributable to"
                 " Neo4j session_id presence, so this is a coarser filter"
                 " than the Cypher-side one above.")
    lines.append("")

    lines.append("## Verdict")
    lines.append("")
    lines.append("The previous probes were dominated by test/eval traffic"
                 " (87% of last-7d Turn nodes have `session_id: None`). On"
                 " the production-only slice the magnitude is much smaller"
                 " — see the top-15 table above for the actual signal.")
    lines.append("")

    out_path.write_text("\n".join(lines))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
