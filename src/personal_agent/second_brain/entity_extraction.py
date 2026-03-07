"""Entity extraction pipeline using local SLM or Claude (Phase 2.2).

This module provides structured entity and relationship extraction from
conversation text using local reasoning models (Qwen 8B, LFM 1.2B) or Claude 4.5.
"""

from typing import TYPE_CHECKING, Any

import orjson

from personal_agent.config import load_model_config, settings
from personal_agent.llm_client import LLMTimeout, LocalLLMClient, ModelRole
from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from personal_agent.llm_client.claude import ClaudeClient

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


async def extract_entities_and_relationships(
    user_message: str,
    assistant_response: str,
    claude_client: "ClaudeClient | None" = None,
) -> dict[str, Any]:
    """Extract entities and relationships from conversation using SLM or Claude.

    Args:
        user_message: User's message
        assistant_response: Assistant's response
        claude_client: Optional Claude API client (uses local SLM if None)

    Returns:
        Dict with entities, relationships, entity_names, and summary
    """
    model_config = load_model_config()
    entity_extraction_role = model_config.entity_extraction_role
    use_claude = entity_extraction_role == "claude" and claude_client is not None

    prompt = _EXTRACTION_PROMPT_TEMPLATE.format(
        user_message=user_message,
        assistant_response=assistant_response,
    )

    log.info(
        "entity_extraction_started",
        entity_extraction_role=entity_extraction_role,
        user_msg_len=len(user_message),
        assistant_msg_len=len(assistant_response),
    )

    try:
        # Call appropriate LLM and extract content
        if use_claude:
            # Use Claude API (guaranteed non-None by use_claude check)
            assert claude_client is not None, "Claude client required when use_claude=True"
            log.debug("entity_extraction_using_claude")

            claude_response = await claude_client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                system=_EXTRACTION_SYSTEM_PROMPT,
            )
            content = claude_response.get("content", "")
            model_used = "claude"
        else:
            # Use local SLM: role from config/models.yaml entity_extraction_role
            local_client = LocalLLMClient()
            model_role = ModelRole.from_str(entity_extraction_role) or ModelRole.REASONING

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
                llm_response = await local_client.respond(
                    role=model_role,
                    messages=messages,
                    system_prompt=None,  # Already in messages
                    tools=None,
                    max_tokens=10000,  # thinking_budget_tokens (≤3000) + JSON response headroom
                    max_retries=0,  # No retries: a timeout means the model is overloaded;
                                    # retrying queues more work and blocks consolidation for ~27min
                    timeout_s=float(settings.entity_extraction_timeout_seconds),
                )
            except LLMTimeout as e:
                log.warning(
                    "entity_extraction_timeout",
                    error=str(e),
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

        # Extract entity names for convenience
        entity_names = [e.get("name", "") for e in result.get("entities", []) if e.get("name")]

        log.info(
            "entity_extraction_completed",
            entities_found=len(entity_names),
            relationships_found=len(result.get("relationships", [])),
            model_used=model_used,
        )

        return {
            "summary": result.get("summary", ""),
            "entities": result.get("entities", []),
            "relationships": result.get("relationships", []),
            "entity_names": entity_names,
        }

    except Exception as e:
        log.error(
            "entity_extraction_failed",
            error=str(e),
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
