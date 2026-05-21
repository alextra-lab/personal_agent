"""Probe 3: Near-duplicate entities in co-retrieval neighborhoods.

Originally planned as a log-replay against context_assembled traces, but the
production logs only record retrieval *counts*, not the entity names that get
injected (see memory_query_executed: only test-data has populated entity_names
fields). So we approximate the retrieval set with the substrate it draws from:
direct entity co-occurrence in turns.

Hypothesis: When a query targets entity X, retrieval pulls in entities that
co-occur with X in recent turns. If the co-occurrence neighborhood contains
near-duplicate entity names (e.g. ``personal_agent`` and ``Personal Agent``
and ``personal-agent``), the prompt receives redundant context.

Method:
  1. For each of the top 20 entities by activity, fetch the set of co-occurring
     entities (entities discussed in the same Turn).
  2. Within each neighborhood, find near-duplicate name pairs:
        - case-insensitive equality
        - after normalizing punctuation and whitespace
        - or token-set Jaccard >= 0.8
  3. Report per-neighborhood duplicate rate plus the worst examples.

Limitations: this is a structural proxy. A real retrieval audit would replay
the gateway's recall path for a fixed user query; doing that cleanly requires
logging the injected entity list, which we noted as a follow-up.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from _common import ensure_output_dir, neo4j_session

TOP_ENTITIES = 20
NEIGHBORHOOD_LIMIT = 50
JACCARD_THRESHOLD = 0.8


_punct_re = re.compile(r"[^a-z0-9 ]+")
_ws_re = re.compile(r"\s+")


def _normalize(name: str) -> str:
    n = name.lower()
    n = _punct_re.sub(" ", n)
    return _ws_re.sub(" ", n).strip()


def _tokens(name: str) -> frozenset[str]:
    return frozenset(_normalize(name).split())


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _find_dup_pairs(names: list[str]) -> list[tuple[str, str, str]]:
    """Return (a, b, reason) for near-duplicate pairs."""
    pairs: list[tuple[str, str, str]] = []
    norms = [(name, _normalize(name), _tokens(name)) for name in names]
    for i in range(len(norms)):
        for j in range(i + 1, len(norms)):
            a, an, at = norms[i]
            b, bn, bt = norms[j]
            if an == bn:
                pairs.append((a, b, "normalized-equal"))
            elif _jaccard(at, bt) >= JACCARD_THRESHOLD:
                pairs.append((a, b, f"jaccard>={JACCARD_THRESHOLD:.2f}"))
    return pairs


async def main() -> None:
    out_dir = ensure_output_dir()
    out_path = out_dir / "probe_3_co_retrieval_duplicates.md"

    async with neo4j_session() as session:
        top_q = """
            MATCH (e:Entity)
            WHERE e.memory_type = 'semantic'
            WITH e, COUNT { (e)<-[:DISCUSSES]-() } AS discusses
            ORDER BY discusses DESC
            LIMIT $top_k
            RETURN e.name AS name, discusses
        """
        tr = await session.run(top_q, top_k=TOP_ENTITIES)
        top_entities = [dict(row) async for row in tr]

        per_entity: list[dict[str, Any]] = []
        for ent in top_entities:
            name = ent["name"]
            neighborhood_q = """
                MATCH (e:Entity {name: $name})<-[:DISCUSSES]-(t:Turn)
                      -[:DISCUSSES]->(other:Entity)
                WHERE other.name <> $name
                WITH other, count(DISTINCT t) AS co_turns
                ORDER BY co_turns DESC
                LIMIT $limit
                RETURN other.name AS name, co_turns
            """
            nr = await session.run(
                neighborhood_q, name=name, limit=NEIGHBORHOOD_LIMIT
            )
            neighbors = [dict(row) async for row in nr]
            neighbor_names = [n["name"] for n in neighbors]
            dups = _find_dup_pairs(neighbor_names)
            per_entity.append(
                {
                    "entity": name,
                    "neighborhood_size": len(neighbors),
                    "duplicate_pairs": dups,
                    "duplicate_rate": (
                        len(dups) / max(1, len(neighbors))
                    ),
                }
            )

    total_pairs = sum(len(e["duplicate_pairs"]) for e in per_entity)
    avg_rate = (
        sum(e["duplicate_rate"] for e in per_entity) / len(per_entity)
        if per_entity
        else 0.0
    )

    lines: list[str] = [
        "# Probe 3 — Near-Duplicate Entities in Co-Retrieval Neighborhoods",
        "",
        f"**Top entities analyzed:** {len(per_entity)}",
        f"**Total duplicate pairs found:** {total_pairs}",
        f"**Mean duplicate-pair rate per neighborhood:** {avg_rate * 100:.1f}%",
        "",
        f"Each neighborhood = up to {NEIGHBORHOOD_LIMIT} entities that"
        f" co-occur in turns with the seed entity. Duplicate detection uses"
        f" normalized-name equality and token Jaccard >= {JACCARD_THRESHOLD}.",
        "",
        "## Per-entity neighborhoods",
        "",
    ]
    for entry in per_entity:
        lines.append(f"### `{entry['entity']}`")
        lines.append(f"- neighborhood size: {entry['neighborhood_size']}")
        lines.append(f"- duplicate pairs: {len(entry['duplicate_pairs'])}")
        lines.append(
            f"- rate: {entry['duplicate_rate'] * 100:.1f}%"
        )
        if entry["duplicate_pairs"]:
            lines.append("- pairs:")
            for a, b, reason in entry["duplicate_pairs"][:10]:
                lines.append(f"  - `{a}` ⇔ `{b}`  ({reason})")
        lines.append("")

    out_path.write_text("\n".join(lines))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
