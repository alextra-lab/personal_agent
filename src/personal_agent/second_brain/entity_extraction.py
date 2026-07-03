"""Entity extraction pipeline using local SLM or Claude (Phase 2.2).

This module provides structured entity and relationship extraction from
conversation text using local reasoning models (Qwen 8B, LFM 1.2B) or Claude 4.5.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import orjson

from personal_agent.config import load_model_config, settings
from personal_agent.cost_gate import BudgetDenied
from personal_agent.llm_client import InferenceSlotTimeout, LLMTimeout, LocalLLMClient, ModelRole
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.trace import SystemTraceContext

log = get_logger(__name__)

_EXTRACTION_SYSTEM_PROMPT = """\
You are a knowledge graph extraction expert building a personal memory system.
Reason carefully about (a) which entities and relationships have lasting knowledge value,
(b) the KNOWLEDGE CLASS of each entity (World / Personal / System), and (c) the user's
explicit STANCES and personal situational CLAIMS — which you must emit as structured items,
never flatten into an entity's description.
Your final output must be valid JSON only — no markdown fences, no explanation text."""

_EXTRACTION_PROMPT_TEMPLATE = """\
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

Conversation:
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

# High-precision person attribution when the model omits structured Person entities (eval CP-26).
_PROJECT_LEAD_PERSON_RE = re.compile(
    r"(?i)(?:the\s+)?project\s+lead\s+is\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b",
)


def _supplement_person_entities_from_user_message(
    user_message: str,
    entities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Append Person entities from fixed phrases (e.g. project lead is Name Name).

    LLM extraction sometimes misses multi-token person names; this keeps graph
    checks (Neo4j entity by display name) aligned with user-stated roles.

    Args:
        user_message: User turn text.
        entities: Parsed entity dicts from the model.

    Returns:
        Entity list with any supplemented Person rows merged (deduped by name).
    """
    existing_lower = {str(e.get("name", "")).strip().lower() for e in entities if e.get("name")}
    out = list(entities)
    m = _PROJECT_LEAD_PERSON_RE.search(user_message or "")
    if not m:
        return out
    name = m.group(1).strip()
    if len(name.split()) < 2 or name.lower() in existing_lower:
        return out
    out.append(
        {
            "name": name,
            "type": "Person",
            "description": "Named individual (inferred from role attribution in message).",
            "properties": {},
        }
    )
    return out


_VALID_ENTITY_CLASSES = frozenset({"World", "Personal", "System"})

# FRE-712: the extractor's contradiction signal per claim; off-vocabulary → "new".
_VALID_UPDATE_KINDS = frozenset({"new", "correction", "evolution"})

# FRE-725: the extractor's per-entity description signal; off-vocabulary → "new".
_VALID_DESCRIPTION_UPDATE_KINDS = frozenset({"new", "enrichment", "correction"})
_FACET_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _normalize_facet(value: Any) -> str:
    """Normalize a model-emitted facet to a stable lower-snake slot key (FRE-712).

    Lowercasing + snake-casing damps trivial cross-turn drift ("Lease End Date" and
    "lease_end_date" collapse to one slot). Returns "" when absent/empty, which the
    matcher treats as neutral (falls back to embedding similarity).

    Args:
        value: The raw ``facet`` field from the model (may be missing/None).

    Returns:
        A lower-snake slot key, or "" when there is nothing usable.
    """
    text = str(value or "").strip().lower()
    return _FACET_NON_ALNUM_RE.sub("_", text).strip("_")


def _normalize_update_kind(value: Any) -> str:
    """Return a valid update_kind, defaulting off-vocabulary values to "new" (FRE-712).

    Args:
        value: The raw ``update_kind`` field from the model.

    Returns:
        One of "new"/"correction"/"evolution".
    """
    candidate = str(value or "").strip().lower()
    return candidate if candidate in _VALID_UPDATE_KINDS else "new"


def _normalize_description_update_kind(value: Any) -> str:
    """Return a valid per-entity description signal, defaulting off-vocabulary to "new" (FRE-725).

    Mirrors :func:`_normalize_update_kind` for the World-fact description enrichment/correction
    signal. Python owns the defaulting (like ``class``) so the correction gate keys on a stable,
    validated value.

    Args:
        value: The raw ``description_update_kind`` field from the model.

    Returns:
        One of "new"/"enrichment"/"correction".
    """
    candidate = str(value or "").strip().lower()
    return candidate if candidate in _VALID_DESCRIPTION_UPDATE_KINDS else "new"


def _build_provenance(
    *,
    trace_id: UUID | str | None,
    session_id: str | None,
    turn_timestamp: datetime | None,
    extracted_at: datetime,
) -> dict[str, Any]:
    """Build the provenance block stamped onto every Stance and Claim (ADR-0098 D5).

    ``observed_at`` is the *turn* time (when the user asserted the fact), so a Claim
    can later be bitemporally superseded (ADR-0098 D2); it falls back to the
    extraction time only when the caller did not thread a ``turn_timestamp`` (e.g.
    direct/test callers). ``extracted_at`` is pipeline-forensics wall-clock only.

    Args:
        trace_id: Originating capture's trace_id (stamped, not asked of the LLM).
        session_id: Originating capture's session_id.
        turn_timestamp: The turn's timestamp; authoritative ``observed_at``.
        extracted_at: Wall-clock when extraction ran; forensics + fallback.

    Returns:
        Provenance dict with trace_id, session_id, source_type, observed_at, extracted_at.
    """
    observed = turn_timestamp or extracted_at
    return {
        "trace_id": str(trace_id) if trace_id else None,
        "session_id": session_id,
        "source_type": "conversation",
        "observed_at": observed.astimezone(timezone.utc).isoformat(),
        "extracted_at": extracted_at.isoformat(),
    }


def _coerce_mastery(value: Any) -> float | None:
    """Coerce a model-emitted mastery value to a clamped float or None.

    Local SLMs emit ``"0.6"`` (str), ``0.6`` (num), or ``"null"``/None
    inconsistently; normalize so FRE-638's edge write receives a stable type.

    Args:
        value: The raw ``mastery`` field from the model.

    Returns:
        A float clamped to [0.0, 1.0], or None if absent/unparseable.
    """
    if value is None:
        return None
    try:
        mastery = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, mastery))


def _normalize_entity_class(entity: dict[str, Any]) -> str:
    """Return a valid entity class, defaulting to World (fail-open, FRE-637).

    A missing or invalid class fails **open** to ``World`` (visible to the tutor)
    rather than ``System`` so a hedging model never silently starves the tutor.
    System-classification precision is FRE-639's concern.

    Args:
        entity: An extracted entity dict.

    Returns:
        One of ``World`` / ``Personal`` / ``System``.
    """
    candidate = str(entity.get("class", "")).strip().capitalize()
    if candidate in _VALID_ENTITY_CLASSES:
        return candidate
    return "World"


def _finalize_extraction(
    result: dict[str, Any],
    *,
    trace_id: UUID | str | None,
    session_id: str | None,
    turn_timestamp: datetime | None,
) -> None:
    """Normalize entity classes and stamp provenance on stances/claims, in place.

    This is the Python side of the ADR-0098 D5 contract: the LLM emits the
    semantic content of stances/claims; Python owns the ``class`` defaulting and
    the provenance + timestamp (the model cannot know real trace/session identity
    or wall-clock time). Runs *after* Person supplementation so supplemented rows
    also receive a class.

    Args:
        result: The parsed extraction dict (mutated in place).
        trace_id: Originating capture's trace_id.
        session_id: Originating capture's session_id.
        turn_timestamp: The turn's timestamp for ``observed_at``.
    """
    extracted_at = datetime.now(timezone.utc)
    provenance = _build_provenance(
        trace_id=trace_id,
        session_id=session_id,
        turn_timestamp=turn_timestamp,
        extracted_at=extracted_at,
    )

    for entity in result.get("entities", []):
        entity["class"] = _normalize_entity_class(entity)
        # FRE-725: validated per-entity description enrichment/correction signal (Python owns
        # defaulting, like class) so the correction gate keys on a stable, in-vocabulary value.
        entity["description_update_kind"] = _normalize_description_update_kind(
            entity.get("description_update_kind")
        )

    stances = list(result.get("stances", []))
    for stance in stances:
        stance["subject"] = "owner"
        stance["class"] = "Stance"
        stance["mastery"] = _coerce_mastery(stance.get("mastery"))
        stance.setdefault("affect", "")
        stance.setdefault("description", "")
        stance["provenance"] = dict(provenance)
    result["stances"] = stances

    claims = list(result.get("claims", []))
    for claim in claims:
        claim["subject"] = "owner"
        claim["class"] = "Personal"
        claim.setdefault("description", "")
        # FRE-712: normalized slot key + validated contradiction signal (Python owns
        # defaulting, like class) so supersession keys on a stable facet and labels
        # correction-vs-evolution from an explicit signal rather than a heuristic.
        claim["facet"] = _normalize_facet(claim.get("facet"))
        claim["update_kind"] = _normalize_update_kind(claim.get("update_kind"))
        claim["provenance"] = dict(provenance)
    result["claims"] = claims


async def extract_entities_and_relationships(
    user_message: str,
    assistant_response: str,
    *,
    trace_id: UUID | str | None = None,
    session_id: str | None = None,
    attempt_number: int | None = None,
    turn_timestamp: datetime | None = None,
) -> dict[str, Any]:
    """Extract entities and relationships from conversation.

    Dispatches to a cloud provider or a local SLM based on the
    entity_extraction_role defined in config/models.yaml (ADR-0031).
    Cloud dispatch uses LiteLLMClient; local dispatch uses LocalLLMClient.

    Args:
        user_message: User's message.
        assistant_response: Assistant's response.
        trace_id: Originating capture's trace_id, threaded through to
            structured logs for join-with-chat-request (FRE-307 D6).
        session_id: Originating capture's session_id. Threaded into the
            ``SystemTraceContext`` so the cost record carries the same
            session identity as the chat turn that produced the capture
            (ADR-0074 §I4 — avoids ``cost_record_missing_identity``).
        attempt_number: Sequential retry counter per trace_id (FRE-307);
            the consolidator computes this and passes it through so log
            lines are aggregable in Kibana ("median attempts to success").
        turn_timestamp: The originating turn's timestamp (ADR-0098 D5). Threaded
            from ``capture.timestamp`` so a Claim/Stance ``observed_at`` is the
            turn time — not the (lagging) consolidation-run time — which the
            bitemporal supersession in FRE-638 depends on. Falls back to
            extraction wall-clock when omitted.

    Returns:
        Dict with entities, relationships, entity_names, summary, and — new in
        FRE-637 — ``stances`` and ``claims`` (each provenance-stamped). Every
        entity additionally carries a ``class`` (World/Personal/System).

    Raises:
        BudgetDenied: Re-raised so the consolidator can write a
            ``consolidation_attempts`` row with ``outcome=budget_denied``
            and let the next scheduled tick re-pick the trace once the
            budget window rolls. Generic exceptions are still swallowed
            and surfaced as a fallback result.
    """
    model_config = load_model_config()
    entity_extraction_role = model_config.entity_extraction_role
    model_def = model_config.models.get(entity_extraction_role)
    provider = model_def.provider if model_def else None

    prompt = _EXTRACTION_PROMPT_TEMPLATE.format(
        user_message=user_message,
        assistant_response=assistant_response,
    )

    trace_id_str = str(trace_id) if trace_id else None
    log.info(
        "entity_extraction_started",
        entity_extraction_role=entity_extraction_role,
        provider=provider,
        model=model_def.id if model_def else None,
        user_msg_len=len(user_message),
        assistant_msg_len=len(assistant_response),
        trace_id=trace_id_str,
    )

    try:
        # Call appropriate LLM and extract content
        if provider is not None:
            # Cloud path: any provider via LiteLLM
            from personal_agent.llm_client.factory import get_llm_client

            cloud_client = get_llm_client(role_name=entity_extraction_role)
            log.debug(
                "entity_extraction_using_cloud",
                model=model_def.id if model_def else None,
                provider=provider,
                trace_id=trace_id_str,
            )

            cloud_response = await cloud_client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": prompt}],
                system_prompt=_EXTRACTION_SYSTEM_PROMPT,
                trace_ctx=SystemTraceContext.new("entity_extraction", session_id=session_id),
            )
            content = cloud_response["content"]
            model_used = model_def.id if model_def else entity_extraction_role
        else:
            # Local SLM path
            local_client = LocalLLMClient()
            model_role = ModelRole.from_str(entity_extraction_role) or ModelRole.PRIMARY

            log.debug(
                "entity_extraction_calling_local_llm",
                entity_extraction_role=entity_extraction_role,
                role=model_role.value,
                max_tokens=10000,
                trace_id=trace_id_str,
            )

            # Add system prompt to messages
            messages = [
                {
                    "role": "system",
                    "content": _EXTRACTION_SYSTEM_PROMPT,
                },
                {"role": "user", "content": prompt},
            ]

            try:
                from personal_agent.llm_client.concurrency import InferencePriority

                llm_response = await local_client.respond(
                    role=model_role,
                    messages=messages,
                    system_prompt=None,  # Already in messages
                    tools=None,
                    max_tokens=10000,  # thinking_budget_tokens (≤3000) + JSON response headroom
                    max_retries=0,  # No retries: a timeout means the model is overloaded;
                    # retrying queues more work and blocks consolidation for ~27min
                    timeout_s=float(settings.entity_extraction_timeout_seconds),
                    priority=InferencePriority.BACKGROUND,
                    priority_timeout=60.0,
                    trace_ctx=SystemTraceContext.new("entity_extraction", session_id=session_id),
                )
            except (LLMTimeout, InferenceSlotTimeout) as e:
                log.warning(
                    "entity_extraction_timeout",
                    error=str(e),
                    error_type=type(e).__name__,
                    timeout_seconds=settings.entity_extraction_timeout_seconds,
                    message="Returning empty entities to avoid blocking consolidation.",
                    trace_id=trace_id_str,
                )
                return _default_extraction_result(user_message)

            # LLMResponse is a TypedDict - use dict access
            content = llm_response["content"]
            model_used = entity_extraction_role

            log.debug(
                "entity_extraction_llm_response_received",
                model=model_used,
                response_len=len(content),
                input_tokens=llm_response.get("usage", {}).get("prompt_tokens"),
                output_tokens=llm_response.get("usage", {}).get("completion_tokens"),
                trace_id=trace_id_str,
            )

        if not content:
            log.warning("extraction_empty_response", model=model_used, trace_id=trace_id_str)
            return _default_extraction_result(user_message)

        # Parse JSON response.
        # The adapters layer already strips <think>…</think> blocks before this point,
        # so content here is only the model's actual output (possibly with markdown fences).
        content = content.strip()
        if "```json" in content:
            start = content.find("```json") + 7
            end = content.find("```", start)
            content = content[start:end].strip()
        elif "```" in content:
            start = content.find("```") + 3
            end = content.find("```", start)
            content = content[start:end].strip()

        # Last-resort: find the outermost JSON object if there is surrounding text
        if not content.startswith("{"):
            brace_start = content.find("{")
            if brace_start != -1:
                content = content[brace_start:]

        try:
            result = orjson.loads(content)
        except orjson.JSONDecodeError as e:
            truncated = len(content) > 100 and not content.rstrip().endswith("}")
            log.error(
                "entity_extraction_json_parse_failed",
                error=str(e),
                content_len=len(content),
                likely_truncated=truncated,
                content_preview=content[:200],
                content_tail=content[-100:] if truncated else None,
                trace_id=trace_id_str,
            )
            return _default_extraction_result(user_message)

        entities = _supplement_person_entities_from_user_message(
            user_message,
            list(result.get("entities", [])),
        )
        result["entities"] = entities

        # ADR-0098 D5: default entity classes and stamp provenance on stances/claims.
        # Runs after Person supplementation so supplemented rows also get a class.
        _finalize_extraction(
            result,
            trace_id=trace_id,
            session_id=session_id,
            turn_timestamp=turn_timestamp,
        )

        # Extract entity names for convenience
        entity_names = [e.get("name", "") for e in entities if e.get("name")]

        log.info(
            "entity_extraction_completed",
            entities_found=len(entity_names),
            relationships_found=len(result.get("relationships", [])),
            stances_found=len(result.get("stances", [])),
            claims_found=len(result.get("claims", [])),
            model_used=model_used,
            trace_id=trace_id_str,
        )

        return {
            "summary": result.get("summary", ""),
            "entities": entities,
            "relationships": result.get("relationships", []),
            "entity_names": entity_names,
            "stances": result.get("stances", []),
            "claims": result.get("claims", []),
        }

    except BudgetDenied as e:
        # FRE-307: surface budget pressure as a distinct, structured signal so
        # the auto-tuning monitor (FRE-311) and the Extraction Retry Health
        # Kibana panel can aggregate denial counts. Re-raise so the
        # consolidator records the attempt as outcome="budget_denied" rather
        # than the generic "extraction_returned_fallback" path below.
        log.warning(
            "entity_extraction_failed",
            error=str(e),
            error_type="BudgetDenied",
            trace_id=str(trace_id) if trace_id else None,
            attempt_number=attempt_number,
            denial_reason=e.denial_reason,
            role=e.role,
            cap=str(e.cap),
            spend=str(e.current_spend),
        )
        raise
    except Exception as e:
        log.error(
            "entity_extraction_failed",
            error=str(e),
            error_type=type(e).__name__,
            trace_id=str(trace_id) if trace_id else None,
            attempt_number=attempt_number,
            denial_reason=None,
            exc_info=True,
        )
        return _default_extraction_result(user_message)


def _default_extraction_result(user_message: str) -> dict[str, Any]:
    """Return default extraction result when extraction fails.

    Args:
        user_message: User message

    Returns:
        Default extraction result dict
    """
    return {
        "summary": user_message[:200] + "..." if len(user_message) > 200 else user_message,
        "entities": [],
        "relationships": [],
        "entity_names": [],
        "stances": [],
        "claims": [],
    }
