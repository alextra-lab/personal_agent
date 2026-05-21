"""Probe 2: Redundant relationships between entity pairs.

Hypothesis: Because the consolidator creates relationships without checking
whether an existing edge already expresses the same semantic relation
(consolidator.py:506-521 has no dedup), the same entity pair can accumulate
multiple edges of overlapping meaning (e.g. RELATED_TO + SIMILAR_TO + USES).

That's not integration — an integrating layer would either pick one relation,
or refine the typing, or attach evidence to a single canonical edge.

Method:
  1. Find all unordered pairs (a, b) with >= 2 distinct relationship types.
  2. Rank by total edge count.
  3. Emit a markdown report with top 30 pairs and the relation types between
     them, plus an aggregate count.
"""

from __future__ import annotations

import asyncio

from _common import ensure_output_dir, neo4j_session

TOP_PAIRS = 30


async def main() -> None:
    out_dir = ensure_output_dir()
    out_path = out_dir / "probe_2_redundant_edges.md"

    async with neo4j_session() as session:
        # Direction-agnostic pair discovery. We order names to dedupe a<->b.
        query = """
            MATCH (a:Entity)-[r]->(b:Entity)
            WHERE a <> b AND a.name < b.name
            WITH a.name AS a_name, b.name AS b_name,
                 collect(DISTINCT type(r)) AS forward_types,
                 count(r) AS forward_count
            OPTIONAL MATCH (b2:Entity {name: b_name})-[r2]->(a2:Entity {name: a_name})
            WITH a_name, b_name, forward_types, forward_count,
                 collect(DISTINCT type(r2)) AS backward_types,
                 count(r2) AS backward_count
            WITH a_name, b_name,
                 [t IN forward_types + backward_types WHERE t IS NOT NULL] AS all_types,
                 forward_count + backward_count AS total_edges
            WITH a_name, b_name, total_edges,
                 [t IN all_types | t] AS types,
                 size([t IN all_types | t]) AS type_count
            WHERE type_count >= 2
            RETURN a_name, b_name, types, total_edges, type_count
            ORDER BY type_count DESC, total_edges DESC
            LIMIT $limit
        """
        result = await session.run(query, limit=TOP_PAIRS)
        pairs = [dict(row) async for row in result]

        count_q = """
            MATCH (a:Entity)-[r]->(b:Entity)
            WHERE a <> b AND a.name < b.name
            WITH a.name AS a_name, b.name AS b_name,
                 collect(DISTINCT type(r)) AS forward_types
            OPTIONAL MATCH (b2:Entity {name: b_name})-[r2]->(a2:Entity {name: a_name})
            WITH a_name, b_name, forward_types,
                 collect(DISTINCT type(r2)) AS backward_types
            WITH a_name, b_name,
                 [t IN forward_types + backward_types WHERE t IS NOT NULL] AS all_types
            WITH size(all_types) AS type_count
            WHERE type_count >= 2
            RETURN count(*) AS pairs_with_redundancy
        """
        cr = await session.run(count_q)
        total_redundant = (await cr.single())["pairs_with_redundancy"]

        total_pair_q = """
            MATCH (a:Entity)-[r]->(b:Entity)
            WHERE a <> b AND a.name < b.name
            WITH DISTINCT a.name AS an, b.name AS bn
            RETURN count(*) AS total_pairs
        """
        tp = await session.run(total_pair_q)
        total_pairs = (await tp.single())["total_pairs"]

    rate = (100.0 * total_redundant / total_pairs) if total_pairs else 0.0

    lines: list[str] = [
        "# Probe 2 — Redundant Relationships Between Entity Pairs",
        "",
        f"**Total ordered entity pairs with at least one edge:** {total_pairs}",
        f"**Pairs with >=2 distinct relationship types:** {total_redundant}"
        f" ({rate:.1f}%)",
        "",
        "Multiple edge types between the same pair often express the same"
        " semantic relation under different names (RELATED_TO + SIMILAR_TO +"
        " USES). A genuine integration layer would either pick one type or"
        " consolidate evidence; the current schema accumulates.",
        "",
        f"## Top {TOP_PAIRS} pairs by distinct-type count",
        "",
        "| a | b | distinct types | total edges | types |",
        "|---|---|---|---|---|",
    ]
    for pair in pairs:
        types_str = ", ".join(sorted(pair["types"]))
        lines.append(
            f"| `{pair['a_name']}` | `{pair['b_name']}` |"
            f" {pair['type_count']} | {pair['total_edges']} | {types_str} |"
        )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("**Interpretation:** rows where the distinct types include"
                 " pairs like (RELATED_TO, SIMILAR_TO), (USES, PART_OF), or"
                 " (RELATED_TO, USES) are strong candidates for redundant"
                 " semantic relations.")

    out_path.write_text("\n".join(lines))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
