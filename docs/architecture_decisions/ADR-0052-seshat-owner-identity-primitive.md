# ADR-0052: Seshat Owner Identity Primitive

**Status**: Accepted (amended 2026-05-09)
**Date**: 2026-04-17
**Amendment date**: 2026-05-09
**Deciders**: Project owner
**Related**: ADR-0026 (Search Memory Native Tool), ADR-0035 (Seshat Embeddings + Reranker), ADR-0039 (Proactive Memory), ADR-0041 (Event Bus Promotion), ADR-0042 (Knowledge Graph Freshness), ADR-0064 (Inbound User Identity)

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

---

## Amendment — 2026-05-09 (FRE-213 implementation)

### Reframing: single-owner over a multi-user CF Access deployment

ADR-0052 was originally written assuming a single operator per deployment. In practice, the harness is already **multi-user via Cloudflare Access** (4 connected users as of 2026-05-09). The amendment reframes "single-operator" as **"single owner / admin, multiple authenticated users"**:

- **Owner** (`is_owner=true`) — one person; the deployment admin; identified by `AGENT_OWNER_NAME` + `AGENT_OWNER_EMAIL`.
- **Connected users** — all CF Access authenticated users; each gets a `:Person {user_id}` node, provisioned lazily on first request.
- **Memory is shared** by default (`visibility=PUBLIC`). Per-user retrieval (e.g. "what did *we* talk about last week") is a separate mechanism (personal-time-window query, tracked in Wave E follow-up ticket).

### Schema clarification

```cypher
# Admin owner — cardinality 1
(:Agent {id})-[:OPERATED_BY]->(:Person {is_owner: true, user_id, email, name, …})

# Every authenticated user — cardinality N, anchored by user_id
(:Person {user_id, email, name, …})

# Extracted entities (third parties) — no user_id
(:Person {name, …})   ← never has user_id; never has is_owner
```

### Dropped: case-insensitive name-match adoption heuristic

The original "if a :Person with the same name exists, adopt it" heuristic is **removed**. Bootstrap and per-request provision always anchor by `user_id` (Postgres UUID), never by name. Reason: same-named third-party entities extracted from conversation (e.g. a friend named "Alex") must never be merged into the harness-user's Person node. Two `:Person {name: "Alex"}` nodes with and without `user_id` are distinct by design.

### New: auto-provision for non-owner users

`MemoryService.get_or_provision_user_person()` is called on every authenticated request to ensure a `:Person {user_id}` exists. The `name` field is seeded from `users.display_name` (nullable) falling back to the email local-part. Properties are additive — extraction enrichment overwrites nothing.

### Stanza is per-connected-user, not per-owner

`get_owner_stanza()` in `orchestrator/prompts.py` accepts `user_id`, `email`, `display_name` and calls `get_or_provision_user_person()` each turn. This means every authenticated user is greeted by their own name — not just the admin owner. Falls back to empty string when `user_id` is None (CLI/unauthenticated paths).

### Research grounding

Patterns validated against: Mem0g (anchors user identity by `speaker_id`, explicitly documents "do not confuse character names with users"), Letta (shared blocks vs. per-user blocks), Neo4j multi-tenant provenance-edge pattern, W3C PROV. See `docs/research/2026-05-09-graph-identity-multi-user-patterns.md`.

### `:Person(user_id)` uniqueness constraint

A `CREATE CONSTRAINT person_user_id_unique` is ensured at startup to guarantee one `:Person` per `user_id`.

### Known follow-up: dedup hardening (HIGH)

`memory/dedup.py:_find_similar_entities` currently matches on exact lowercase name across all `:Person` nodes regardless of `user_id`. A same-named extracted third party could collide into the owner Person on a future extraction turn. Fix: add `WHERE node.user_id IS NULL` to the similarity search candidates. Tracked as a Wave E follow-up ticket.

---

## Update 2026-05-14 — Personal-history retrieval (FRE-343)

### Decision

Adopt the W3C-PROV provenance edge pattern:

```cypher
(:Person {user_id})-[:PARTICIPATED_IN {created_at, backfilled?}]->(:Turn {turn_id})
```

Written at Turn-save time inside `MemoryService.create_conversation` (atomic with the Turn MERGE, same Neo4j session). Read by a new native tool `recall_personal_history`. Default `search_memory` behavior — and the shared-knowledge-graph design — is **unchanged**.

### `user_id` invariant tightening

`TaskCapture.user_id` is now `UUID` (non-optional). This is the correct invariant because `service.auth.get_request_user` always resolves a `user_id` from one of three sources:

1. `Cf-Access-Authenticated-User-Email` header (CF Access, production path).
2. `settings.agent_owner_email` fallback (dev/CLI path).
3. HTTP 401 — request rejected.

The previous `user_id: UUID | None = None` was a defensive holdover, not a real production state. `MemoryService.create_conversation` keeps `user_id: UUID | None = None` (optional) so that the unused `store_episode` adapter path doesn't break; the PARTICIPATED_IN MERGE block is guarded on `if user_id is not None`. The production consolidator always provides `user_id`, so the edge always lands.

### Rationale

`(:Person)-[:PARTICIPATED_IN]->(:Turn)` is the Neo4j-community-recommended pattern for "multi-tenant shared-entity graphs with per-tenant interaction history" and aligns with W3C PROV's `(:Activity)-[:wasAssociatedWith]->(:Agent)`. Shared entities stay shared; provenance is modelled as edges, not as per-user copies of nodes. Forward-compatible with FRE-230 (Location) — `(:Turn)-[:OCCURRED_AT]->(:Location)` is a parallel additive edge that needs no rework here.

### MATCH-not-MERGE on `:Person`

The PARTICIPATED_IN Cypher uses `MATCH (p:Person {user_id: $user_id})` — not MERGE. MERGE would silently create a name-less `:Person` on every Turn for any unknown `user_id`, polluting the graph. With MATCH, a missing `:Person` results in no edge written (the Turn itself is still created). That outcome is a logic bug worth investigating, not a fallback.

### Backfill

One-shot script `scripts/backfill_participated_in.py` (run via `make backfill-participated-in`) backfills the edge for existing Turns. For each Session in Postgres: `target_uid = session.user_id` if set, else owner UUID. The current Postgres schema enforces `sessions.user_id NOT NULL` (FRE-268 migration), so the owner-fallback branch is defensive only. Idempotent (MERGE + `ON CREATE SET r.backfilled = true`).

### See also

- Spec: `docs/superpowers/specs/2026-05-14-fre-343-personal-time-window-retrieval-design.md`
- Plan: `docs/superpowers/plans/2026-05-14-fre-343-personal-time-window-retrieval.md`
- Research: `docs/research/2026-05-09-graph-identity-multi-user-patterns.md` §6
- Skill doc: `docs/skills/personal-history-recall.md` (XML-pilot in body)
- Linear: [FRE-343](https://linear.app/frenchforest/issue/FRE-343)
