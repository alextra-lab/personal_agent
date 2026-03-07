# ADR-0024: Session-Centric Graph Model for Behavioral Memory

**Status**: Accepted — Partially Implemented  
**Date**: 2026-03-06  
**Deciders**: Project owner  

---

## Context

The current memory graph uses a single `Conversation` node per captured turn (one user message + one assistant response). Each node stores the full text, a summary, and extracted entities, linked to `Entity` nodes via `DISCUSSES` edges.

This model has two fundamental limitations:

1. **No conversation structure** — turns from the same session are unconnected. There is no way to traverse the arc of a conversation: what was asked first, how the question evolved, what follow-ups emerged.

2. **No behavioral signal** — without session-level grouping and cross-session correlation, the graph cannot answer questions about *how* the user thinks: recurring topics, missing dimensions, question-chaining patterns, or domain distribution over time.

These limitations matter because the project's stated goal is a *self-reflective intelligence* that acts as a *research partner that challenges assumptions*. An agent cannot challenge thinking patterns it cannot observe. The memory graph is the substrate for that observation.

### What we currently have

```
(Conversation {conversation_id=trace_id, session_id, summary, user_message, ...})
    -[:DISCUSSES]->
(Entity {name, entity_type, description, mention_count})
```

- `conversation_id` equals `trace_id` — one node per HTTP request
- `session_id` is stored but never used to create graph structure
- Zero `Conversation → Conversation` links exist
- No session-level aggregation

---

## Decision

Introduce a **Session node** that groups turns, with ordered `NEXT` links between turns within a session. Rename `Conversation` to `Turn` to reflect what it actually represents.

### New graph schema

```
(Session)
  ├─[:CONTAINS {sequence: int}]──► (Turn)
  │                                    └─[:NEXT]──► (Turn)──[:NEXT]──► (Turn)
  └─[:DISCUSSES]──────────────────────────────────────────────────────► (Entity)

(Turn)-[:DISCUSSES]──► (Entity)
```

### Node definitions

**Session**

| Property | Type | Description |
|---|---|---|
| `session_id` | UUID | Primary key — same as Postgres session |
| `started_at` | datetime | Timestamp of first turn |
| `ended_at` | datetime | Timestamp of last turn |
| `turn_count` | int | Number of turns in session |
| `dominant_entities` | list[str] | Top entity names across all turns |
| `session_summary` | str \| None | LLM-generated summary of the full session arc |

**Turn** (renamed from Conversation)

| Property | Unchanged from | Notes |
|---|---|---|
| `turn_id` | `conversation_id` / `trace_id` | No change to value |
| `session_id` | existing | Now used structurally |
| `sequence_number` | new | Position within session (1-indexed) |
| All other fields | unchanged | summary, user_message, assistant_response, key_entities, properties |

### Relationship definitions

| Relationship | From → To | Properties | Semantics |
|---|---|---|---|
| `CONTAINS` | Session → Turn | `{sequence: int}` | Ordered membership |
| `NEXT` | Turn → Turn | — | Sequential flow within session |
| `DISCUSSES` | Turn → Entity | — | Unchanged from current |
| `DISCUSSES` | Session → Entity | `{turn_count: int}` | Aggregated from all turns |
| `FOLLOWED_BY` | Session → Session | `{gap_seconds: int}` | Temporal session ordering (future) |

---

## What this enables

### Immediate (query capability)

- "What sessions involved Python?" → `(Session)-[:DISCUSSES]->(Entity {name: 'Python'})`
- "What did the user ask after asking about X?" → `(Turn {entities: X})-[:NEXT]->(Turn)`
- "Reconstruct this conversation" → `(Session)-[:CONTAINS]->(Turn)` ordered by `sequence`
- "Give me full context for this question" → retrieve the `Session`, not just the `Turn`

### Near-term (pattern analysis via Captain's Log)

The Captain's Log reflection engine can run queries like:

- **Dimension gaps**: "In 47 weather sessions, the user never asked about wind or UV index" → generates a proposal: *"You consistently ask about temperature but rarely about conditions affecting outdoor activities"*
- **Topic distribution**: "85% of sessions in the last 30 days involved tool debugging" → *"You've been in a debugging loop — is this blocking progress on the planned features?"*
- **Question chaining patterns**: "Most sessions terminate after 2 turns; deep sessions (5+ turns) only occur on architecture topics" → *"You tend to drop topics quickly outside architecture discussions"*
- **Cross-session context**: "This question is similar to one asked 3 weeks ago in a session that ended with a different conclusion" → surfaces prior reasoning

### Future (Observation nodes)

```
(Session)-[:GENERATED]->(Observation {type, description, confidence, generated_at})
(Entity)-[:PATTERN]->(Observation)
```

Observations are inferred by the reflection engine — never asserted without evidence — and subject to human review before influencing agent behavior.

---

## Alternatives considered

### Alternative 1: Keep current model, add NEXT links only

Add `(Turn)-[:NEXT]->(Turn)` between turns that share a `session_id`, ordered by timestamp. No Session node.

**Rejected because**: loses session-level aggregation and summary. Cannot answer "what was this session about?" without traversing all turns. Cannot efficiently query sessions by dominant topic.

### Alternative 2: Keep current model as-is

Continue treating each turn as a standalone Conversation node.

**Rejected because**: permanently forecloses behavioral pattern analysis. The project's core goal — a self-reflective partner that challenges assumptions — requires memory of how questions chain and what patterns recur across time.

### Alternative 3: Store session structure only in Postgres

Use the existing `sessions` table in Postgres for sequencing; keep Neo4j for entity relationships only.

**Rejected because**: behavioral pattern queries require graph traversal, not relational queries. Cross-session entity co-occurrence and sequential question analysis are graph problems.

---

## Consequences

### Positive

- Enables the self-reflection and behavioral observation capabilities central to the project vision
- `Session` becomes the natural unit for memory retrieval — richer context for the agent when recalling prior work
- Clean conceptual separation: `Turn` = atomic exchange, `Session` = meaningful unit of work
- `dominant_entities` on Session enables fast topic-based session lookup without full graph traversal

### Negative / risks

- **Migration required** — existing `Conversation` nodes and the `conversation_exists()` deduplication guard must be updated
- **Consolidator complexity** — must now group captures by `session_id` before processing, create Session nodes, and link turns in sequence
- **Session boundary ambiguity** — what constitutes a "session"? Currently determined by Postgres session lifecycle; unclear handling for sessions with gaps or reconnections
- **Session summary cost** — generating a `session_summary` requires an additional LLM call per session; should be optional/deferred

### Open questions

1. Should `session_summary` be generated at consolidation time (expensive) or lazily on first query?
2. What is the canonical session boundary? Postgres session expiry? Time gap threshold (e.g., >30min = new session)?
3. Should `Conversation` nodes be migrated to `Turn` nodes, or should both labels coexist during transition?
4. At what point do `Observation` nodes get created — in the consolidator, or in a separate reflection pass?

---

## Acceptance criteria

- [ ] `Session` nodes exist in the graph, one per unique `session_id` in captures
- [ ] `(Session)-[:CONTAINS {sequence}]->(Turn)` relationships are ordered correctly by timestamp
- [ ] `(Turn)-[:NEXT]->(Turn)` links connect sequential turns within the same session
- [ ] `(Session)-[:DISCUSSES]->(Entity)` aggregates entity mentions across all turns
- [ ] `conversation_exists()` deduplication guard continues to function correctly
- [ ] A Cypher query can reconstruct a full conversation session in turn order
- [ ] A Cypher query can find all sessions that discussed a given entity

---

## Implementation Notes (2026-03-06)

### What was built

The Turn/Session schema was implemented across `memory/models.py`, `memory/service.py`,
and `second_brain/consolidator.py`. The graph now writes `Turn` nodes (replacing `Conversation`)
and creates `Session` nodes at the end of each consolidation batch.

### Findings from first ingest

**Entity quality improvements confirmed:**
- Entity taxonomy is now clean — only the 7 defined types appear
- Summaries are proper sentences, not raw user messages
- Noise entities (User, Assistant, test artifacts) eliminated
- Geographic hierarchy working: `Forcalquier -[LOCATED_IN]-> France`
- 35B reasoning model extracts regional detail (e.g. `Provence-Alpes-Côte d'Azur`) the 4B missed

**Quality gaps identified and fixed:**
- `mcp_*` tool binding names were being extracted as Technology entities — fixed by explicitly
  excluding them in the prompt (extract the underlying service instead)
- `Météo France` / `Météo-France` created two separate nodes — fixed with explicit normalization
  rule in the prompt (canonical form: `Météo France`, no hyphen)
- Ephemeral data values (`7°C`, `March 6, 2026`) were extracted as entities — fixed by adding
  rule 4 to exclude ephemeral data values

**Session structure limitation discovered:**
The current captures are overwhelmingly single-turn sessions — each test interaction was
captured as a standalone session with a unique `session_id`. This means:
- Session nodes are created correctly but each has `turn_count=1`
- `NEXT` chains have nothing to connect (require 2+ turns per session)
- `Session-[:DISCUSSES]->(Entity)` mirrors the single Turn's entities

The Session/NEXT/behavioral-analysis value of ADR-0024 requires real multi-turn conversations.
This will emerge naturally as the agent is used for real work rather than single test queries.

**Empty response rate (~30%):**
The 35B model occasionally returns empty content when its thinking budget exhausts `max_tokens`.
These captures are correctly skipped by the fallback guard and will be retried next run.
`max_tokens` was increased to 6000 to provide headroom after thinking (3000 budget tokens).

### Open question resolution

- **Session boundary**: Determined by Postgres session lifecycle (`session_id` on captures).
  Single-request test sessions result in 1-turn sessions; real multi-turn conversations will
  produce the expected chained structures.
- **`session_summary`**: Deferred — generated lazily in future, not at consolidation time.
- **`Observation` nodes**: Future work, not in scope for initial implementation.

## References

- Current implementation: `src/personal_agent/memory/service.py`, `src/personal_agent/second_brain/consolidator.py`
- Memory models: `src/personal_agent/memory/models.py`
- Project vision (self-reflection, learning): `docs/VISION_DOC.md`
- Captain's Log reflection engine: `src/personal_agent/captains_log/`
