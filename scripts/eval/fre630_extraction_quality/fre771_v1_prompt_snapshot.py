"""FRE-771 — the frozen pre-swap (V1) extraction prompt + the context manager to activate it.

A byte-verbatim snapshot of the retired 7-type template, and the monkeypatch that
activates it against the live `entity_extraction` module for the powered A/B's "current"
comparison arm.

Isolated into its own module (no `harness`/`bench` import) so it is unit-testable without
live cloud credentials — `harness.py` pins `AGENT_MODEL_CONFIG_PATH` to the cloud profile
at import time (by design, for benchmark realism), which requires real API keys; this
module only needs `personal_agent.second_brain.entity_extraction`, which does not.

Never updated again after FRE-771 ships — this is not a live rollback path, it exists
solely to reproduce the retired prompt for the one-time powered A/B (mirrors
`relabel_v2_types.py`'s own frozen-copy convention for a research script's point-in-time
reference material).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from personal_agent.second_brain import entity_extraction

#: FRE-771 — a frozen, byte-verbatim snapshot of the pre-swap (V1, 7-type) extraction
#: prompt template, kept ONLY to reproduce the "current" comparison arm of this ticket's
#: powered A/B. Never updated again after this ticket ships — it is not a live rollback
#: path (mirrors `relabel_v2_types.py`'s own frozen-copy convention for a research
#: script's point-in-time reference material).
_V1_PROMPT_TEMPLATE_SNAPSHOT = """\
Analyze this conversation and extract knowledge graph elements.

ENTITY TYPES — use EXACTLY one of these values, no others:
  Person        — a real named individual (never extract "User" or "Assistant")
  Organization  — a company, team, project group, or institution
  Location      — a geographic place, city, country, or region
  Technology    — a software tool, framework, language, model, or API
  Concept       — an abstract idea, methodology, or domain principle
  Event         — a specific named occurrence or milestone
  Topic         — a well-defined subject area being discussed

RELATIONSHIP TYPES — use EXACTLY one of these UPPER_SNAKE_CASE values, no others:
  PART_OF       — entity is a component or subset of another
  USES          — entity uses or depends on another
  RELATED_TO    — general semantic relationship
  SIMILAR_TO    — entities are comparable or equivalent
  CREATED_BY    — entity was created or authored by another
  LOCATED_IN    — entity is geographically within another

KNOWLEDGE CLASS — every entity MUST carry a "class", EXACTLY one of these three:
  World     — reusable, impersonal know-how: facts, concepts, products, techniques, and
              named people/orgs/places discussed as general knowledge. This is the default
              for real subject-matter content.
  Personal  — a named thing belonging to the USER'S OWN life: their doctor, employer, city of
              residence, their own project. (A first-person SITUATIONAL FACT such as
              "my lease ends in March" is NOT an entity — put it under "claims", below.)
  System    — the agent's OWN machinery: infrastructure, tooling, telemetry, operations.
              Examples: a database discussed as infra ("Postgres is healthy"), healthchecks,
              log/telemetry review (sensor_poll, cost_gate_reaper_swept, DEBUG counts),
              harness internals (executor.py, ToolLoopGate, the consolidation job),
              connectivity pings. This is NOT user knowledge — label it System so it can be
              kept out of the tutor. Judge by the SUBJECT of the turn, not the word alone:
              "our graph store is up" → Neo4j is System; "graph databases store nodes and
              edges" → Neo4j is World.
  (There is NO "Stance" entity. A stance is a RELATION — emit it under "stances", below.)

STANCES — the user's explicit first-person affect toward, or mastery of, a World concept
  ("I love X", "I prefer X over Y", "I'm learning X", "I've basically mastered X").
  A stance is NOT an entity and must NEVER be written into an entity's description.
  Each stance object:
    subject — always the literal string "owner"
    target  — the World concept it is about (this concept should ALSO appear in "entities")
    affect  — a SHORT phrase for the sentiment/preference ("loves it", "prefers over CR-V",
              "wants to learn"); use "" if the stance is purely a mastery/skill level
    mastery — a number 0.0-1.0 if a skill/learning level is stated or clearly implied,
              otherwise null (a pure preference like "I love it" has mastery = null)
    description — one sentence of context

CLAIMS — first-person SITUATIONAL FACTS about the user's own life/relationships/events that
  are assertions, not named entities ("my lease ends in March", "I saw the cardiologist
  today", "I'm actively shopping for a car"). These have no entity slot and are silently
  DROPPED today — you MUST emit them here instead.
  Each claim object:
    subject — always the literal string "owner"
    content — the fact as ONE self-contained declarative sentence
    facet   — a SHORT stable slot key naming WHAT the fact is about, as lower_snake_case
              (e.g. "lease_end_date", "employer", "current_city", "car_shopping_status").
              Two claims about the SAME underlying thing must share the SAME facet so a
              later value replaces the earlier one. Use "" if you cannot name a clear slot.
    update_kind — one of "new" | "correction" | "evolution":
              "correction" when the user is FIXING an earlier mistake ("actually…", "I was
              wrong", "not X, it's Y"); "evolution" when the fact CHANGED ("now…", "as of…",
              "we moved to…", "extended to…"); "new" otherwise (the default).
    description — optional one sentence of context

Do NOT put any provenance, timestamp, id, or record-date on stances or claims — the system
adds those, not you.

EXTRACTION RULES (follow strictly):
1. NEVER extract the protocol role labels "User" / "Assistant" / "System" / "Reasoning" as Person
   entities — these are chat-template role slots, not people.
   This does NOT preclude extracting the human operator (the person who speaks through the User
   slot) when they are named. If a turn contains "my name is Alex" or similar self-reference,
   extract Alex as a normal Person entity.
2. NEVER extract generic message artifacts: "Test message", "Another message", "Original message",
   "Quick test", "Test query", or any placeholder/test text
3. NEVER extract internal tool binding names that start with "mcp_" (e.g. mcp_perplexity_ask,
   mcp_docker, mcp_search, mcp_fetch_content) — extract the underlying service instead
   (e.g. "Perplexity" not "mcp_perplexity_ask", "Docker" not "mcp_docker")
   NEVER extract the native tool name "search_memory" as an entity — it is an internal
   capability, not user-discussed content (ADR-0026).
4. NEVER extract ephemeral data values as entities: temperatures ("7°C", "53°F"), dates used
   only as context ("March 6, 2026"), sky conditions ("Partly sunny"), generic time references
5. ONLY extract entities with knowledge recall value: would knowing this be useful in a future conversation?
6. Normalize entity names to canonical form with consistent diacritics and casing:
   - "Python" not "python", "Neo4j" not "neo4j", "LM Studio" not "lm studio"
   - "Météo France" not "Meteo France" or "Météo-France" (use the official accented form, no hyphen)
   - "Forcalquier" not "Forcqlquier" (correct spelling, not typos)
7. Deduplicate: if two names clearly refer to the same entity, use one canonical name only
8. If the exchange is an empty/placeholder/test artifact with no real content (e.g. "test
   message", a bare ping with no substance), return empty entities and relationships arrays.
   BUT a real operational turn the user actually engaged in — a healthcheck they ran, a
   telemetry/log review, a harness explainer — is NOT empty: emit its subjects as entities
   with class=System (see rule 11), do not drop them.
9. Write summaries as one concrete sentence about what was accomplished or learned
10. Descriptions should add context beyond the name — what makes this entity notable here?
11. Assign every entity a "class" (World | Personal | System) per the definitions above. When
    the turn's SUBJECT is the agent's own infra/tooling/telemetry/healthcheck, the entity is
    System even if it names a general technology.
12. NEVER flatten a user stance into an entity description (no "a car the user loves",
    "central to the user's preference"). Emit it as a structured object in "stances".
13. NEVER drop a first-person situational fact. If it is an assertion about the user's life
    rather than a named entity, emit it in "claims".
14. Set each entity's "description_update_kind" from THIS TURN's intent (you are NOT comparing to
    any stored description — you cannot see one; judge only the conversation in front of you):
      "correction" — the turn EXPLICITLY corrects/contradicts an earlier statement about this
                     entity ("actually X is Y, not Z"; "I was wrong, it's …").
      "enrichment" — the turn SUBSTANTIVELY defines or explains the entity (a real definition or
                     characterization), not just a passing mention.
      "new"        — the default: a passing mention with no correction or defining intent.
    Emit "new" whenever unsure. This signal is about the entity's DESCRIPTION only — never a
    stance or a claim.

GOOD EXAMPLES:
  ✓ {{"name": "Paris", "type": "Location", "class": "World", "description": "Capital of France, subject of weather inquiry"}}
  ✓ {{"name": "Qwen3.5", "type": "Technology", "class": "World", "description": "Local reasoning LLM used for entity extraction"}}
  ✓ {{"name": "Postgres", "type": "Technology", "class": "System", "description": "The agent's own database, referenced in a healthcheck"}}
  ✓ {{"name": "GraphRAG", "type": "Concept", "class": "World", "description": "Technique combining knowledge graphs with RAG retrieval"}}
  ✓ {{"name": "Neo4j", "type": "Technology", "class": "World", "description": "A graph database management system storing data as nodes and typed relationships", "description_update_kind": "enrichment"}}  ← the turn substantively defines it
  ✓ stance: {{"subject": "owner", "target": "Toyota RAV4 Hybrid", "affect": "loves the hybrid powertrain", "mastery": null, "description": "User strongly prefers the RAV4 Hybrid's drivetrain"}}
  ✓ claim:  {{"subject": "owner", "content": "The user's current car lease ends in March.", "description": "Situational constraint driving purchase timing"}}

BAD EXAMPLES (never produce these):
  ✗ {{"name": "User", "type": "Person", ...}}
  ✗ {{"name": "Assistant", "type": "Person", ...}}
  ✗ {{"name": "mcp_perplexity_ask", "type": "Technology", ...}}  ← use "Perplexity" instead
  ✗ {{"name": "mcp_docker", "type": "Technology", ...}}           ← use "Docker" instead
  ✗ {{"name": "search_memory", "type": "Technology", ...}}        ← internal tool, not an entity
  ✗ {{"name": "7°C", "type": "Concept", ...}}                    ← ephemeral data value
  ✗ {{"name": "Météo-France", ...}}                               ← use "Météo France" (no hyphen)
  ✗ {{"name": "Test message", "type": "Message", ...}}
  ✗ {{"name": "Topic", "type": "Topic", ...}}
  ✗ {{"name": "Toyota RAV4 Hybrid", ..., "description": "a car the user loves"}}   ← stance flattened; emit a stance
  ✗ (silently omitting "my lease ends in March")                                   ← emit it as a claim
  ✗ {{"name": "my lease ends in March", "type": "Event", ...}}                     ← situational fact is a claim, not an entity
  ✗ {{"subject": "owner", "target": "...", "provenance": {{...}}}}                 ← never emit provenance; the system adds it

{fewshot_exemplars}Conversation:
User: {user_message}
Assistant: {assistant_response}

Return ONLY valid JSON (no markdown fences, no explanation):
{{
  "summary": "One concrete sentence about what was accomplished or discussed",
  "entities": [
    {{
      "name": "Canonical Entity Name",
      "type": "Person|Organization|Location|Technology|Concept|Event|Topic",
      "class": "World|Personal|System",
      "description": "One sentence with useful context beyond the name",
      "description_update_kind": "new|enrichment|correction",
      "properties": {{}}
    }}
  ],
  "relationships": [
    {{
      "source": "Entity Name 1",
      "target": "Entity Name 2",
      "type": "PART_OF|USES|RELATED_TO|SIMILAR_TO|CREATED_BY|LOCATED_IN",
      "weight": 0.1-1.0,
      "properties": {{}}
    }}
  ],
  "stances": [
    {{
      "subject": "owner",
      "target": "World Concept Name",
      "affect": "short phrase or empty string",
      "mastery": null,
      "description": "One sentence of context"
    }}
  ],
  "claims": [
    {{
      "subject": "owner",
      "content": "One self-contained declarative sentence about the user's situation",
      "facet": "lower_snake_case slot key, or empty string",
      "update_kind": "new|correction|evolution",
      "description": "Optional one sentence of context"
    }}
  ]
}}\
"""


@contextmanager
def v1_prompt_template_active() -> Iterator[None]:
    """Monkeypatch the live extractor's prompt template to the frozen V1 snapshot.

    Exception-safe: the original (production V2) template is restored in a
    ``finally`` even if the patched block raises. Callers MUST NOT run V1-phase and
    V2-phase extraction calls concurrently while this is active — see the module
    docstring's concurrency note.
    """
    original = entity_extraction._EXTRACTION_PROMPT_TEMPLATE
    entity_extraction._EXTRACTION_PROMPT_TEMPLATE = _V1_PROMPT_TEMPLATE_SNAPSHOT
    try:
        yield
    finally:
        entity_extraction._EXTRACTION_PROMPT_TEMPLATE = original
