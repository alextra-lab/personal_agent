"""Entity extraction pipeline using local SLM or Claude (Phase 2.2).

This module provides structured entity and relationship extraction from
conversation text using local reasoning models (Qwen 8B, LFM 1.2B) or Claude 4.5.
"""

from typing import Any

import orjson

from personal_agent.config.settings import get_settings
from personal_agent.llm_client import LocalLLMClient, ModelRole
from personal_agent.llm_client.claude import ClaudeClient
from personal_agent.telemetry import get_logger

log = get_logger(__name__)
settings = get_settings()


async def extract_entities_and_relationships(
    user_message: str,
    assistant_response: str,
    claude_client: ClaudeClient | None = None,
) -> dict[str, Any]:
    """Extract entities and relationships from conversation using SLM or Claude.

    Args:
        user_message: User's message
        assistant_response: Assistant's response
        claude_client: Optional Claude API client (uses local SLM if None)

    Returns:
        Dict with entities, relationships, entity_names, and summary
    """
    # Determine which model to use
    use_claude = settings.entity_extraction_model == "claude" and claude_client is not None

    # Build extraction prompt
    prompt = f"""Analyze the following conversation and extract key entities, relationships, and a summary.

Conversation:
User: {user_message}
Assistant: {assistant_response}

Return ONLY valid JSON in this exact format (no explanation, no thinking, just JSON):
{{
  "summary": "Brief summary of the conversation",
  "entities": [
    {{
      "name": "Entity Name",
      "type": "Person|Place|Topic|Concept|Other",
      "description": "Brief description",
      "properties": {{}}
    }}
  ],
  "relationships": [
    {{
      "source": "Entity Name 1",
      "target": "Entity Name 2",
      "type": "DISCUSSES|PART_OF|SIMILAR_TO|RELATED_TO",
      "weight": 0.0-1.0,
      "properties": {{}}
    }}
  ]
}}"""

    log.info(
        "entity_extraction_started",
        model=settings.entity_extraction_model,
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
                system="You are an expert at extracting structured information from conversations. Always return valid JSON.",
            )
            content = claude_response.get("content", "")
            model_used = "claude"
        else:
            # Use local SLM (Qwen 8B reasoning or LFM 1.2B fast)
            local_client = LocalLLMClient()

            # Determine model role based on config
            if settings.entity_extraction_model == "lfm2.5-1.2b":
                model_role = ModelRole.ROUTER  # LFM 1.2B (fast)
            else:
                model_role = ModelRole.REASONING  # Qwen 8B (default)

            log.debug(
                "entity_extraction_calling_local_llm",
                model=settings.entity_extraction_model,
                role=model_role.value,
                max_tokens=2000,
            )

            # Add system prompt to messages
            messages = [
                {
                    "role": "system",
                    "content": "You are an expert at extracting structured information from conversations. Always return valid JSON.",
                },
                {"role": "user", "content": prompt},
            ]

            llm_response = await local_client.respond(
                role=model_role,
                messages=messages,
                system_prompt=None,  # Already in messages
                tools=None,
                max_tokens=2000,  # Limit response length to prevent timeouts
            )
            # LLMResponse is a TypedDict - use dict access
            content = llm_response["content"]
            model_used = settings.entity_extraction_model

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

        # Parse JSON response
        # Handle markdown code fences if present
        if "```json" in content:
            start = content.find("```json") + 7
            end = content.find("```", start)
            content = content[start:end].strip()
        elif "```" in content:
            start = content.find("```") + 3
            end = content.find("```", start)
            content = content[start:end].strip()

        try:
            result = orjson.loads(content)
        except orjson.JSONDecodeError as e:
            log.error(
                "entity_extraction_json_parse_failed",
                error=str(e),
                content_preview=content[:200],
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
