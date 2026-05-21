"""Probe 1: Entity attribute drift across turns.

Hypothesis: ``MERGE (e:Entity) SET e.description = $description`` (service.py:605)
overwrites the entity description on every promotion. If the same entity is
mentioned across many turns with substantively different framings, the stored
description reflects only the *last* extraction — the earlier framings are
lost. That is concatenation, not integration.

Method:
  1. Pick the top 20 entities by source_turn_ids list length.
  2. For each, fetch the current description plus up to 10 sample turns that
     DISCUSS the entity (sorted by recency).
  3. Emit a markdown report so a human can scan whether the description
     captures the variety of turn framings or has been narrowed to one.
"""

from __future__ import annotations

import asyncio

from _common import ensure_output_dir, neo4j_session

TOP_K_ENTITIES = 20
SAMPLE_TURNS_PER_ENTITY = 10


async def main() -> None:
    out_dir = ensure_output_dir()
    out_path = out_dir / "probe_1_entity_drift.md"

    async with neo4j_session() as session:
        top_query = """
            MATCH (e:Entity)
            WHERE e.source_turn_ids IS NOT NULL
              AND size(e.source_turn_ids) >= 3
              AND e.memory_type = 'semantic'
            RETURN e.name AS name,
                   e.entity_type AS entity_type,
                   e.description AS description,
                   e.mention_count AS mention_count,
                   size(e.source_turn_ids) AS turn_count,
                   e.first_seen AS first_seen,
                   e.last_seen AS last_seen
            ORDER BY turn_count DESC, e.mention_count DESC
            LIMIT $top_k
        """
        result = await session.run(top_query, top_k=TOP_K_ENTITIES)
        entities = [dict(row) async for row in result]

        lines: list[str] = [
            "# Probe 1 — Entity Attribute Drift",
            "",
            f"Top {TOP_K_ENTITIES} semantic entities by source-turn count, with"
            f" sample turn texts that mention them.",
            "",
            "**What to look for:** entities whose stored `description` reflects"
            " only one of the turn framings, or has been narrowed to a single"
            " incident while the turns show varied uses.",
            "",
        ]

        drift_candidates: list[str] = []
        for ent in entities:
            name = ent["name"]
            turn_q = """
                MATCH (t:Turn)-[:DISCUSSES]->(e:Entity {name: $name})
                RETURN t.user_message AS user_message,
                       t.assistant_response AS assistant_response,
                       t.summary AS summary,
                       t.timestamp AS timestamp,
                       t.turn_id AS turn_id
                ORDER BY t.timestamp DESC
                LIMIT $limit
            """
            tr = await session.run(turn_q, name=name, limit=SAMPLE_TURNS_PER_ENTITY)
            turns = [dict(row) async for row in tr]

            lines.append(f"## {name}")
            lines.append(f"- **type:** {ent['entity_type']}")
            lines.append(f"- **mention_count:** {ent['mention_count']}")
            lines.append(f"- **turn_count:** {ent['turn_count']}")
            lines.append(f"- **first_seen:** {ent['first_seen']}")
            lines.append(f"- **last_seen:** {ent['last_seen']}")
            desc = (ent["description"] or "").strip()
            lines.append(f"- **stored description:** {desc or '_(empty)_'}")
            lines.append("")
            lines.append("**Sample turns (most recent first):**")
            lines.append("")
            for turn in turns:
                # Prefer summary, then user_message, then assistant_response.
                txt = (
                    turn.get("summary")
                    or turn.get("user_message")
                    or turn.get("assistant_response")
                    or ""
                ).strip().replace("\n", " ")
                if len(txt) > 240:
                    txt = txt[:240] + "..."
                ts = turn.get("timestamp") or ""
                lines.append(f"- _[{ts}]_ {txt}")
            lines.append("")

            # Heuristic flag: description is shorter than 30 chars but
            # entity has many turns => likely truncated / overwritten gist.
            if desc and len(desc) < 30 and ent["turn_count"] >= 10:
                drift_candidates.append(name)
            # Heuristic flag: stored description doesn't include any token
            # appearing in the most recent turn's summary/user_message.
            if desc and turns:
                latest = turns[0]
                latest_text = (
                    (latest.get("summary") or "")
                    + " "
                    + (latest.get("user_message") or "")
                    + " "
                    + (latest.get("assistant_response") or "")
                ).lower()
                desc_tokens = {t for t in desc.lower().split() if len(t) > 4}
                if desc_tokens and not any(t in latest_text for t in desc_tokens):
                    drift_candidates.append(name)

        lines.append("---")
        lines.append("")
        lines.append("## Heuristic flags")
        lines.append("")
        if drift_candidates:
            lines.append(
                f"Entities flagged as candidate drift cases ({len(set(drift_candidates))}):"
            )
            for name in sorted(set(drift_candidates)):
                lines.append(f"- `{name}`")
        else:
            lines.append("_No entities flagged by heuristics._")
        lines.append("")
        lines.append(
            "**Note:** heuristics are intentionally cheap. Manual review of the"
            " sample turns above is the real signal."
        )

        out_path.write_text("\n".join(lines))

    print(f"wrote {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
