"""In-process tool for querying the memory graph.

Allows the LLM to call ``search_memory`` during multi-step reasoning
to retrieve past conversations, entities, or session history.
"""

from __future__ import annotations

from typing import Any

from personal_agent.telemetry import get_logger
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)


search_memory_tool = ToolDefinition(
    name="search_memory",
    description=(
        "Search the personal memory graph for past conversations, entities, "
        "and topics the user has previously discussed. "
        "Use this when you need to recall specific history from earlier sessions "
        "or when the user asks what they have discussed before. "
        "Returns matching entities, turn summaries, and session context."
    ),
    category="memory",
    parameters=[
        ToolParameter(
            name="query_text",
            type="string",
            description=(
                "Free-text query describing what to find. "
                "Examples: 'Greek islands', 'Python async patterns', "
                "'conversations about travel planning'"
            ),
            required=True,
        ),
        ToolParameter(
            name="entity_types",
            type="array",
            description=(
                "Optional filter by entity type. "
                "Valid values: Location, Person, Organization, Technology, "
                "Topic, Concept, Event. Leave empty to search all types."
            ),
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="entity_names",
            type="array",
            description=(
                "Optional: specific entity names to match, e.g. ['Santorini', 'Athens']. "
                "Combines with query_text if both provided."
            ),
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="recency_days",
            type="number",
            description=(
                "Only return results from the past N days. Default is 90. "
                "Use 0 to search all history."
            ),
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="limit",
            type="number",
            description="Maximum number of results to return (1–50, default 10).",
            required=False,
            default=None,
            json_schema=None,
        ),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "LOCKDOWN", "RECOVERY"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=30,
    rate_limit_per_hour=None,
)


async def search_memory_executor(
    query_text: str = "",
    entity_types: list[str] | None = None,
    entity_names: list[str] | None = None,
    recency_days: int = 90,
    limit: int = 10,
    ctx: Any = None,
) -> dict[str, Any]:
    """Execute a memory graph query and return structured results.

    Args:
        query_text: Free-text query describing what to find.
        entity_types: Optional filter by entity type.
        entity_names: Optional specific entity names to match.
        recency_days: Only return results from the past N days (0 = all history).
        limit: Maximum number of results (1–50).
        ctx: Optional trace context for logging.

    Returns:
        Dict with matched_turns (entity path) or entities/sessions/recent_turns (broad path).
        Raises ToolExecutionError when memory service is unavailable.
    """
    entity_types = entity_types or []
    entity_names = entity_names or []
    recency_days = int(recency_days or 90)
    limit = min(max(int(limit or 10), 1), 50)

    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"

    log.info(
        "search_memory_tool_called",
        trace_id=trace_id,
        query_text=query_text[:80],
        entity_types=entity_types,
        entity_names=entity_names,
        recency_days=recency_days,
        limit=limit,
    )

    try:
        from personal_agent.events import AccessContext
        from personal_agent.memory.models import MemoryQuery
        from personal_agent.memory.service import MemoryService

        memory_service: MemoryService | None = None
        try:
            from personal_agent.service.app import (  # type: ignore[attr-defined]
                memory_service as global_memory_service,
            )

            if global_memory_service and global_memory_service.connected:
                memory_service = global_memory_service
        except (ImportError, AttributeError):
            pass

        if not memory_service or not memory_service.connected:
            raise ToolExecutionError("Memory service unavailable or not connected.")

        if entity_names or not _looks_like_broad_query(query_text, entity_types):
            query = MemoryQuery(
                entity_names=entity_names or _extract_keywords(query_text),
                entity_types=entity_types,
                limit=limit,
                recency_days=recency_days if recency_days > 0 else None,
            )
            result = await memory_service.query_memory(
                query,
                query_text=query_text,
                access_context=AccessContext.TOOL_CALL,
                trace_id=trace_id,
            )
            output = {
                "matched_turns": [
                    {
                        "turn_id": t.turn_id,
                        "timestamp": t.timestamp.isoformat(),
                        "user_message": t.user_message[:300],
                        "summary": t.summary or "",
                        "key_entities": t.key_entities,
                    }
                    for t in result.conversations
                ],
                "entities_found": len(result.entities),
                "total_turns": len(result.conversations),
                "query_path": "entity_match",
            }
        else:
            broad = await memory_service.query_memory_broad(
                entity_types=entity_types or None,
                recency_days=recency_days if recency_days > 0 else 3650,
                limit=limit,
                access_context=AccessContext.TOOL_CALL,
                trace_id=trace_id,
            )
            output = {
                "entities": broad.get("entities", []),
                "sessions": broad.get("sessions", []),
                "recent_turns": broad.get("turns_summary", []),
                "query_path": "broad_recall",
            }

        total = output.get("total_turns")
        if total is None:
            entities: list[Any] = output.get("entities", []) or []
            total = len(entities) if isinstance(entities, list) else 0
        log.info(
            "search_memory_tool_completed",
            trace_id=trace_id,
            query_path=output.get("query_path"),
            result_count=total,
        )

        return output

    except ToolExecutionError:
        raise
    except Exception as e:
        log.error(
            "search_memory_tool_failed",
            error=str(e),
            trace_id=trace_id,
            exc_info=True,
        )
        raise ToolExecutionError(str(e)) from e


def _looks_like_broad_query(query_text: str, entity_types: list[str]) -> bool:
    """Heuristic: is this an open-ended 'what have I discussed?' query?"""
    broad_keywords = {
        "everything",
        "anything",
        "topics",
        "subjects",
        "history",
        "all",
        "previous",
        "past",
        "before",
        "discussed",
        "mentioned",
        "talked about",
        "asked about",
    }
    words = set(query_text.lower().split())
    return bool(words & broad_keywords) and not entity_types


def _extract_keywords(query_text: str) -> list[str]:
    """Extract candidate entity names from free-text query (capitalised words)."""
    words = query_text.split()
    return [w.strip('",.:;!?()') for w in words if len(w) > 2 and w[0].isupper()][:5]
