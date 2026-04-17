# ADR-0052: Seshat Owner Identity Primitive

**Status**: Proposed (Needs Approval)
**Date**: 2026-04-17
**Deciders**: Project owner
**Related**: ADR-0026 (Search Memory Native Tool), ADR-0035 (Seshat Embeddings + Reranker), ADR-0039 (Proactive Memory), ADR-0041 (Event Bus Promotion), ADR-0042 (Knowledge Graph Freshness)

---

## Context

Seshat, the personal memory system, is the project's flagship value proposition: an agent that learns and remembers who the operator is and what they care about, across sessions. In practice, the agent currently cannot reliably answer *"who am I talking to?"* even on a local install that has processed 50+ past turns. A representative failing exchange:

> **User:** Who am I?
> **Agent (after full-text search + memory scan):** I found 0 `Person` entities in my memory graph matching you. I can see `Priya Sharma`, `Alice`, `Leslie Lamport`… but your name is not stored anywhere in my memory.

### Two concepts were being conflated

Early diagnostic work suggested the extraction pipeline was *by design* refusing to record the user. Re-reading the prompt shows this is incorrect:

```text
ENTITY TYPES:
  Person — a real named individual (never extract "User" or "Assistant")

EXTRACTION RULES:
1. NEVER extract "User" or "Assistant" as entities — they are conversation
   participants, not knowledge
```

The forbidden terms `User` / `Assistant` / `System` / `Reasoning` are **chat-template role labels** (LLM API scaffolding). The rule is *defensive prompting against a naming collision* — without it, the extractor sometimes created junk `:Person {name: "User"}` nodes from the literal message prefix. The rule is correct and should stay.

The **human operator** (a real named individual who occupies the `User` role slot) is an ordinary Person and can be extracted normally. The rule does not forbid that.

### The actual missing primitive

Even when `:Person {name: "Alex"}` *is* extracted and persisted, nothing in the graph schema binds **that Person** to **the speaker in the `User` role of this agent instance**. The graph contains many `:Person` nodes with no distinction between "the operator" and "anyone mentioned in passing." As a result:

- `search_memory("who am I")` returns all persons, or none, but cannot identify the operator.
- The orchestrator cannot inject owner facts (name, pronouns, location, preferences) into its system prompt on each turn, because there is no anchor to read from.
- Identity resolution burns tool-call iterations (`list_issues`, `get_issue` × N, repeated `search_memory` calls) trying to infer what a one-line config value could provide instantly.

Independent contributing factors observed in logs (*not* the root cause, but which amplify the gap):

- `entity_extraction_role: gpt-5.4-nano` is a cloud model. If `AGENT_OPENAI_API_KEY` was unset or invalid on earlier runs, extraction silently degraded to no-op and those turns never wrote entities. The local profile has no extraction fallback.
- The extraction pipeline is invoked during post-turn consolidation. Turns from sessions predating Phase 2.2 have no entities at all.

---

## Decision

Introduce a **first-class operator identity primitive** in the memory graph, seeded declaratively from config and enriched by extraction over time. Three coordinated pieces:

### 1. Schema: an owner binding edge

A single `:Agent` node per deployment, bound to exactly one `:Person`:

```cypher
MERGE (agent:Agent {id: $agent_id})
MERGE (person:Person {name: $owner_name})
  ON CREATE SET person.is_owner = true,
                person.created_at = datetime(),
                person.source = "config_bootstrap"
MERGE (agent)-[:OPERATED_BY]->(person)
```

Properties on the owner `:Person` are additive — extraction over future turns enriches (location, pronouns, role, preferences) without overwriting the bootstrapped name.

The `is_owner` boolean is redundant with the `OPERATED_BY` edge but is kept for cheap filtering in broad-recall queries.

### 2. Bootstrap: declarative seed from config

A new settings field, read on first connect and re-asserted on every startup via `MERGE`:

```python
# personal_agent/config/settings.py
class AppSettings(BaseSettings):
    ...
    owner_name: str = Field(
        default="",
        description="Full name of the human operator. Written to Seshat on "
                    "startup as :Person {is_owner: true} and injected into "
                    "the orchestrator system prompt as {{owner}}.",
    )
    agent_id: str = Field(
        default="seshat-local",
        description="Stable identifier for this agent deployment. Used to "
                    "bind the :Agent node to the owner :Person.",
    )
```

Operator sets once in `.env`:

```bash
AGENT_OWNER_NAME=Alex
AGENT_AGENT_ID=seshat-local
```

The bootstrap runs in `MemoryService.connect()` after vector-index setup. Idempotent via `MERGE`; safe across restarts and safe when `owner_name` is empty (no-op).

### 3. Prompt injection: owner facts on every turn

The orchestrator's system-prompt assembler reads the owner `:Person` on each request and injects a compact stanza:

```text
## Operator
You are assisting {{owner.name}}. Known facts (from memory):
- Based in {{owner.location}}
- {{owner.pronouns}}
- Role: {{owner.role}}
Reference these naturally. Do not tool-call to look up who the user is.
```

Facts are whatever properties exist on the owner node at prompt time. Missing fields are omitted rather than templated as empty strings. This eliminates the "who am I?" iteration burn loop entirely.

### 4. Extraction prompt clarification (no behaviour change)

Update the extraction prompt to make the role-label vs. operator distinction explicit, so future prompt edits don't accidentally re-introduce the conflation:

```text
1. NEVER extract the protocol labels "User" / "Assistant" / "System" /
   "Reasoning" as Person entities — these are chat-template role slots,
   not people.
   This does NOT preclude extracting the human operator (the person
   who speaks through the User slot) when named. If a turn contains
   "my name is Alex" or similar self-reference, extract Alex as a
   normal Person entity.
```

No code change beyond the prompt string.

---

## Alternatives Considered

### Alternative 1: System-prompt-only (no graph primitive)

Just stuff `AGENT_OWNER_NAME` into the system prompt and skip the graph work.

**Rejected**: Treats the symptom, not the cause. The whole point of Seshat is that memory is the source of truth for long-lived facts. A system-prompt constant can't be enriched by extraction, can't be queried by `search_memory`, and can't answer "what do you know about me?" beyond the one string. We'd be patching around the missing primitive instead of adding it.

### Alternative 2: Rely on extraction alone (no bootstrap)

Trust that the operator will eventually say their name in a turn, and let extraction do the rest.

**Rejected**: (a) Creates a chicken-and-egg — the agent can't personalise its behaviour until the operator has already had a conversation that triggers extraction. (b) Extraction is probabilistic; on a given turn the cloud model might not produce a Person entity. (c) Leaves no way to distinguish the operator from any other Person in the graph. The bootstrap + binding edge gives a deterministic guarantee.

### Alternative 3: Store owner identity in a side table (PostgreSQL `user` row)

Model operator identity as a relational row, separate from the knowledge graph.

**Rejected**: Splits the ontology. Other things the agent learns about the operator (preferences, locations, projects) are Person-Person and Person-Topic relationships that naturally live in Neo4j. A separate table forces a second query on every turn and creates a reconciliation problem when extraction *does* produce a Person named "Alex."

### Alternative 4: Multiple `is_owner` persons (multi-user agent)

Allow N persons to have `is_owner = true` to support shared family/team usage.

**Deferred**: Single-operator is the Phase-2 assumption (ADR-0044, ADR-0046). Multi-operator would require per-session operator resolution, ACL on memory reads, and a different prompt-injection strategy. Keep the schema single-operator for now but do not rule out extending `OPERATED_BY` to a cardinality of N later; the edge shape supports it.

---

## Consequences

### Positive

- **Identity is deterministic**: `.env` → bootstrap → graph → prompt. Reliable from first startup, before any conversation happens.
- **Kills the iteration-burn loop**: "Who am I?" is answered from the system prompt, not via tool calls. Directly fixes the `tool_iteration_limit_reached` class of failures observed on simple identity questions.
- **Memory becomes the source of truth**: Future enrichment (location, preferences, role) lands on the same node the system prompt reads from. One anchor, one story.
- **`search_memory` improvement**: Broad-recall path can filter `is_owner = true` to surface operator facts cheaply; entity-match path still works for third-person queries.
- **Extensible to proactive memory (ADR-0039)**: Proactive retrieval can prioritise edges emanating from the owner node.

### Negative / Risks

- **Schema migration**: Existing graphs have no `:Agent` node and no `OPERATED_BY` edge. On first post-change connect, the bootstrap must (a) create the `:Agent` node, (b) detect whether a pre-existing `:Person {name: $owner_name}` should be adopted vs. a new node created. Heuristic: if a `:Person` with an exact-match (case-insensitive) name already exists, adopt it by adding `is_owner = true` and the edge; otherwise create a fresh node.
- **Privacy surface**: `owner_name` in `.env` is low-sensitivity but non-zero. Ensure `.env.example` documents it and the field is never logged in plain structlog output (redact in `telemetry/`).
- **Prompt-injection failure mode**: If the owner node exists but has garbage properties (from an extraction glitch), the injected stanza could degrade the system prompt. Mitigation: cap the stanza to a known allowlist of fields (`name`, `location`, `pronouns`, `role`) and truncate each to ≤120 chars.
- **Local-profile extraction gap remains**: Even with this ADR, turns on the `local` profile that can't reach the cloud extractor will still not enrich the owner node. That is ADR-scope for a separate owner-agnostic fix (local fallback extraction). Out of scope here.

### Neutral

- No change to the extraction prompt's behaviour — only the clarifying comment. The rule already (correctly) permitted extracting the operator-as-person.
- No change to `search_memory`'s external interface. Internal query path gains an `is_owner` filter for broad recall.

---

## Implementation Notes

1. **Config**
   - Add `owner_name: str` and `agent_id: str` to `AppSettings` with safe defaults.
   - Document both in `.env.example` under a new `# ── Operator Identity ──` section.

2. **Memory bootstrap**
   - Extend `MemoryService.connect()` with a `_bootstrap_owner_identity()` step after `ensure_vector_index()`.
   - Idempotent `MERGE` Cypher (see schema block above).
   - No-op when `owner_name == ""`.
   - Log `owner_bootstrap_ran` / `owner_bootstrap_skipped` with redacted name.

3. **Prompt assembly**
   - New helper `get_owner_stanza()` in `personal_agent/orchestrator/prompts.py`.
   - Queries the owner `:Person` and returns a Markdown stanza or empty string.
   - Invoked once per turn in the system-prompt builder.
   - Whitelist fields: `name`, `location`, `pronouns`, `role`, `languages`.

4. **Extraction prompt**
   - Update `_EXTRACTION_PROMPT_TEMPLATE` in `src/personal_agent/second_brain/entity_extraction.py` with the clarified rule text.
   - No behavioural tests should change; add one regression test: given input `"my name is Alex"`, extractor produces `Alex` as a `Person`.

5. **Tests**
   - Unit: bootstrap idempotency (run twice, same graph state).
   - Unit: bootstrap with empty `owner_name` (no-op).
   - Unit: owner stanza rendering with partial properties.
   - Integration: end-to-end on a throwaway Neo4j — set `AGENT_OWNER_NAME=Alex`, start the service, verify `(:Agent)-[:OPERATED_BY]->(:Person {name: "Alex", is_owner: true})` exists.

6. **Rollout**
   - Ship behind no flag; the feature is inert when `owner_name == ""`.
   - Document in `docs/guides/USAGE_GUIDE.md`: "First-time setup — set `AGENT_OWNER_NAME` in `.env`."
