"""Entity extraction pipeline using local SLM or Claude (Phase 2.2).

This module provides structured entity and relationship extraction from
conversation text using local reasoning models (Qwen 8B, LFM 1.2B) or Claude 4.5.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import orjson

from personal_agent.config import load_model_config, settings
from personal_agent.cost_gate import BudgetDenied
from personal_agent.llm_client import InferenceSlotTimeout, LLMTimeout, LocalLLMClient, ModelRole
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

_EXTRACTION_SYSTEM_PROMPT = """\
You are a knowledge graph extraction expert building a personal memory system.
Reason carefully about which entities and relationships have lasting knowledge value.
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

EXTRACTION RULES (follow strictly):
1. NEVER extract "User" or "Assistant" as entities — they are conversation participants, not knowledge
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
8. If the conversation is a system test, ping, or empty exchange with no real content,
   return empty entities and relationships arrays
9. Write summaries as one concrete sentence about what was accomplished or learned
10. Descriptions should add context beyond the name — what makes this entity notable here?

GOOD EXAMPLES:
  ✓ {{"name": "Paris", "type": "Location", "description": "Capital of France, subject of weather inquiry"}}
  ✓ {{"name": "Qwen3.5", "type": "Technology", "description": "Local reasoning LLM used for entity extraction"}}
  ✓ {{"name": "Neo4j", "type": "Technology", "description": "Graph database storing the personal memory graph"}}
  ✓ {{"name": "GraphRAG", "type": "Concept", "description": "Technique combining knowledge graphs with RAG retrieval"}}

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
      "description": "One sentence with useful context beyond the name",
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


async def extract_entities_and_relationships(
    user_message: str,
    assistant_response: str,
    *,
    trace_id: UUID | str | None = None,
    attempt_number: int | None = None,
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
        attempt_number: Sequential retry counter per trace_id (FRE-307);
            the consolidator computes this and passes it through so log
            lines are aggregable in Kibana ("median attempts to success").

    Returns:
        Dict with entities, relationships, entity_names, and summary.

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

    log.info(
        "entity_extraction_started",
        entity_extraction_role=entity_extraction_role,
        provider=provider,
        model_id=model_def.id if model_def else None,
        user_msg_len=len(user_message),
        assistant_msg_len=len(assistant_response),
    )

    try:
        # Call appropriate LLM and extract content
        if provider is not None:
            # Cloud path: any provider via LiteLLM
            from personal_agent.llm_client.factory import get_llm_client

            cloud_client = get_llm_client(role_name=entity_extraction_role)
            log.debug(
                "entity_extraction_using_cloud",
                model_id=model_def.id if model_def else None,
                provider=provider,
            )

            cloud_response = await cloud_client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": prompt}],
                system_prompt=_EXTRACTION_SYSTEM_PROMPT,
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
                )
            except (LLMTimeout, InferenceSlotTimeout) as e:
                log.warning(
                    "entity_extraction_timeout",
                    error=str(e),
                    error_type=type(e).__name__,
                    timeout_seconds=settings.entity_extraction_timeout_seconds,
                    message="Returning empty entities to avoid blocking consolidation.",
                )
                return _default_extraction_result(user_message)

            # LLMResponse is a TypedDict - use dict access
            content = llm_response["content"]
            model_used = entity_extraction_role

            log.debug(
                "entity_extraction_llm_response_received",
                model=model_used,
                response_len=len(content),
                prompt_tokens=llm_response.get("usage", {}).get("prompt_tokens"),
                completion_tokens=llm_response.get("usage", {}).get("completion_tokens"),
            )

        if not content:
            log.warning("extraction_empty_response", model=model_used)
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
            )
            return _default_extraction_result(user_message)

        entities = _supplement_person_entities_from_user_message(
            user_message,
            list(result.get("entities", [])),
        )
        result["entities"] = entities

        # Extract entity names for convenience
        entity_names = [e.get("name", "") for e in entities if e.get("name")]

        log.info(
            "entity_extraction_completed",
            entities_found=len(entity_names),
            relationships_found=len(result.get("relationships", [])),
            model_used=model_used,
        )

        return {
            "summary": result.get("summary", ""),
            "entities": entities,
            "relationships": result.get("relationships", []),
            "entity_names": entity_names,
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
    }
