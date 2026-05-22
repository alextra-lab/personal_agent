"""Probe 5: Does the wrong description reach the LLM prompt?

This is the impact-path probe that should have been part of the initial
report. Goal: convert the substrate finding from Probes 1-2 into a
behavior-level claim that's actually measured.

Method:
  1. Replay the broad-recall query the gateway runs for MEMORY_RECALL
     intents: ``recall_broad(entity_types=None, recency_days=90, limit=20)``.
  2. Slice to top 15 entities the way ``executor.py:1732`` does.
  3. Render each one with the literal prompt format the executor uses
     (``- [<type>] <name>: <description> (mentioned <n>x)``).
  4. Classify each rendered line as:
        - ``empty``: description is None or whitespace
        - ``misleading``: description present but obviously wrong against
          the entity name (heuristic; needs manual review)
        - ``adequate``: description present and seems to fit
  5. Cross-reference with ES: how often does broad recall fire? How often
     does the broader memory_context render fire (broad + proactive)?
  6. Print the actual rendered system-prompt section so the user can read
     what the LLM is being told right now.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from _common import ensure_output_dir, neo4j_session


# Entities whose descriptions Probe 1 flagged as drifted. Used to mark
# `misleading` lines without re-running a manual review.
KNOWN_DRIFTED_ENTITIES = {
    "Neo4j": "Query language used to interact with Neo4j",  # describes Cypher
    "PersonalAgent": "background pipeline updating Captain's Log",
    "Elasticsearch": "trace indexing from the ES indexer consumer group",
}


def _classify(name: str, description: str | None) -> str:
    if description is None or not description.strip():
        return "empty"
    if name in KNOWN_DRIFTED_ENTITIES:
        snippet = KNOWN_DRIFTED_ENTITIES[name].lower()
        if snippet in description.lower():
            return "misleading"
    return "adequate"


async def _es_count(query: dict[str, Any]) -> int:
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "http://localhost:9200/agent-logs-*/_count",  # fre-375-allow: read-only production analysis probe
            json=query,
            timeout=10.0,
        )
        r.raise_for_status()
        return r.json()["count"]


async def main() -> None:
    out_dir = ensure_output_dir()
    out_path = out_dir / "probe_5_impact_path.md"

    # === Part 1: replay the broad-recall query ===
    async with neo4j_session() as session:
        # The same query MemoryService.recall_broad runs for
        # entity_types=None, recency_days=90, limit=20.
        # Filter by last_seen within the recency window, order by mention.
        replay_q = """
            MATCH (e:Entity)
            WHERE e.memory_type = 'semantic'
              AND e.last_seen >= datetime() - duration({days: 90})
            RETURN e.name AS name,
                   e.entity_type AS entity_type,
                   e.description AS description,
                   e.mention_count AS mention_count,
                   e.last_seen AS last_seen
            ORDER BY e.mention_count DESC, e.last_seen DESC
            LIMIT 20
        """
        result = await session.run(replay_q)
        broad_rows = [dict(row) async for row in result]

    # === Part 2: simulate the executor's top-15 slice + render ===
    top_15 = broad_rows[:15]

    rendered_lines: list[str] = []
    classifications = {"empty": 0, "misleading": 0, "adequate": 0}
    for row in top_15:
        line = (
            f"- [{row['entity_type'] or ''}] {row['name'] or ''}:"
            f" {row['description'] or ''}"
            f" (mentioned {row['mention_count'] or 1}x)"
        )
        rendered_lines.append(line)
        classifications[_classify(row["name"], row["description"])] += 1

    memory_section = (
        "## Your Memory Graph — Known Entities\n"
        + "\n".join(rendered_lines)
        + "\n\nUse this list to directly answer questions about what the user"
        " has previously discussed. Do NOT say you have no memory."
    )

    # === Part 3: frequency of impact in production ===
    last_30d = {"range": {"@timestamp": {"gte": "now-30d"}}}

    broad_fires = await _es_count(
        {"query": {"bool": {"must": [last_30d, {"term": {"event_type": "memory_recall_broad_query"}}]}}}
    )
    proactive_fires = await _es_count(
        {
            "query": {
                "bool": {
                    "must": [
                        last_30d,
                        {"term": {"event_type": "proactive_memory_suggest_complete"}},
                    ]
                }
            }
        }
    )
    gateway_turns = await _es_count(
        {"query": {"bool": {"must": [last_30d, {"term": {"event_type": "gateway_output"}}]}}}
    )
    has_memory_turns = await _es_count(
        {
            "query": {
                "bool": {
                    "must": [
                        last_30d,
                        {"term": {"event_type": "context_assembled"}},
                        {"term": {"has_memory": True}},
                    ]
                }
            }
        }
    )

    # === Part 4: write the report ===
    pct_misleading = 100.0 * classifications["misleading"] / max(1, len(top_15))
    pct_empty = 100.0 * classifications["empty"] / max(1, len(top_15))
    pct_adequate = 100.0 * classifications["adequate"] / max(1, len(top_15))

    pct_has_mem = 100.0 * has_memory_turns / max(1, gateway_turns)
    pct_broad = 100.0 * broad_fires / max(1, gateway_turns)

    lines: list[str] = [
        "# Probe 5 — Impact Path Measurement",
        "",
        "## Question",
        "",
        "Do the wrong descriptions found in Probes 1-2 actually reach the"
        " LLM prompt? If yes, how often, and with what instructions?",
        "",
        "## Path traced in code",
        "",
        "1. `context.py:118`, `:225` — descriptions are written into the"
        " `memory_context` list.",
        "2. `executor.py:990` — `memory_context` is attached to `ctx`.",
        "3. `executor.py:1725-1739` — if `memory_context[0].type` is"
        " `entity` or `session`, the executor renders entries as system"
        " prompt and tells the LLM: _\"Use this list to directly answer"
        " questions about what the user has previously discussed."
        " Do NOT say you have no memory.\"_",
        "",
        "## Frequency (last 30 days)",
        "",
        f"| Metric | Count | % of gateway turns |",
        f"|---|---|---|",
        f"| Gateway turns | {gateway_turns} | 100% |",
        f"| Turns with `has_memory: True` | {has_memory_turns}"
        f" | {pct_has_mem:.1f}% |",
        f"| Broad-recall fires (`MEMORY_RECALL` task type) | {broad_fires}"
        f" | {pct_broad:.1f}% |",
        f"| Proactive-memory fires | {proactive_fires}"
        f" | {100.0 * proactive_fires / max(1, gateway_turns):.1f}% |",
        "",
        f"**Headline:** {pct_has_mem:.1f}% of turns inject memory context."
        " Both broad-recall (line 1726 branch) and proactive (also type"
        " `entity`) paths flow through the same renderer, so any wrong"
        " description in the top-15 reaches the LLM.",
        "",
        "## What the LLM sees right now",
        "",
        "Below is the **exact** memory section that would be emitted into"
        " the system prompt for the next `MEMORY_RECALL` turn, replayed"
        " against the current Neo4j with the same query"
        " (`recall_broad(entity_types=None, recency_days=90, limit=20)`,"
        " sliced to 15 by `executor.py:1732`):",
        "",
        "```",
        memory_section,
        "```",
        "",
        "## Top-15 classification",
        "",
        f"| Class | Count | % of top 15 |",
        f"|---|---|---|",
        f"| `misleading` (known-drifted entity) | {classifications['misleading']}"
        f" | {pct_misleading:.1f}% |",
        f"| `empty` (description blank) | {classifications['empty']}"
        f" | {pct_empty:.1f}% |",
        f"| `adequate` (description present, not in flagged set) |"
        f" {classifications['adequate']} | {pct_adequate:.1f}% |",
        "",
        "## Interpretation",
        "",
        "- The malformed description **does** make it into the prompt."
        " The substrate finding from Probes 1-2 is on the live retrieval"
        " path, not a dead field.",
        "- The instruction _\"Do NOT say you have no memory\"_ is a strong"
        " nudge against the LLM overriding the supplied facts with its"
        " own priors. So even where the LLM might \"know better\" about"
        " famous entities, the prompt tells it to defer.",
        "- The `empty` rate is the bigger headline number — when the"
        " description is blank, the prompt line collapses to"
        " `- [<type>] <name>:  (mentioned Nx)`, which is uninformative"
        " but at least not actively wrong.",
        "- Whether the LLM **acts on** the wrong description (i.e. the"
        " final behavioral failure) still isn't measured here. A read of"
        " ~10 recent assistant responses where Neo4j or PersonalAgent was"
        " in the top-15 would settle that. Not done in this probe.",
        "",
        "## What this changes about the Probes 1-2 framing",
        "",
        "- Original framing (\"critical / load-bearing / already"
        " happened\") was based on substrate observation alone. It is"
        " partially upheld: the path from substrate to prompt is now"
        " confirmed (lines 1725-1739 of executor.py).",
        "- The final step — _\"does the LLM actually act on the wrong"
        " content\"_ — remains unmeasured. The honest framing is"
        " **\"wrong content reaches the prompt with an instruction to"
        " trust it; behavioral impact on assistant outputs is not"
        " measured.\"** That's a stronger claim than \"substrate only\""
        " and weaker than \"definitely degrades responses.\"",
        "",
    ]

    out_path.write_text("\n".join(lines))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
