# ADR-0026: `search_memory` Native Tool

**Status**: Accepted  
**Date**: 2026-03-07  
**Deciders**: Project owner  

---

## Context

ADR-0025 solves the entry-point problem: memory context is injected *before* the LLM first sees a recall query. This ADR solves a complementary problem: **mid-conversation recall**, where the agent discovers mid-task that it needs to consult history — for example:

> "Actually, before you continue, check if I've discussed Santorini before."

In this scenario, the agent should be able to call a tool to query memory graph directly rather than relying on what was pre-injected in `step_init`. Additionally, this tool can be invoked by the LLM during multi-step reasoning when it autonomously decides that consulting memory would improve its answer.

### Current tool landscape

Built-in in-process Python tools registered in `src/personal_agent/tools/`:

| Tool | Description |
|---|---|
| `read_file` | Read file contents from filesystem |
| `list_directory` | List directory entries |
| `system_metrics_snapshot` | Current CPU/memory/disk/network |

MCP-proxied tools are registered with an `mcp_` prefix via `src/personal_agent/mcp/gateway.py`. There is currently no tool that queries the agent's own memory graph.

### Why a native in-process tool (not an MCP tool)

MCP tools are discovered at runtime from the Docker gateway, require a subprocess handshake, and are prefixed `mcp_`. A `search_memory` tool needs to:
1. Call `MemoryService` directly (in-process, same Python runtime)
2. Work even when the MCP gateway is offline
3. Not pollute the memory graph with its own tool call as an entity (`mcp_search_memory` was explicitly added to the extraction exclusion list in ADR-0024)

An in-process native tool is the correct pattern, matching the existing `system_metrics_snapshot_tool`.

---

## Decision

Add a new native in-process tool `search_memory` to `src/personal_agent/tools/memory_search.py`, register it as an MVP tool alongside `read_file` and `system_metrics_snapshot`, and expose it to the LLM via the standard tool definition mechanism.

---

## Implementation

### New file: `src/personal_agent/tools/memory_search.py`

```python
"""In-process tool for querying the memory graph.

Allows the LLM to call ``search_memory`` during multi-step reasoning
to retrieve past conversations, entities, or session history.
"""

from __future__ import annotations

from typing import Any

from personal_agent.telemetry import get_logger
from personal_agent.tools.types import ToolDefinition, ToolParameter, ToolResult

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
        ),
        ToolParameter(
            name="entity_names",
            type="array",
            description=(
                "Optional: specific entity names to match, e.g. ['Santorini', 'Athens']. "
                "Combines with query_text if both provided."
            ),
            required=False,
        ),
        ToolParameter(
            name="recency_days",
            type="integer",
            description=(
                "Only return results from the past N days. Default is 90. "
                "Use 0 to search all history."
            ),
            required=False,
        ),
        ToolParameter(
            name="limit",
            type="integer",
            description="Maximum number of results to return (1–50, default 10).",
            required=False,
        ),
    ],
    requires_approval=False,
    requires_network=False,
    estimated_duration_ms=300,
)


async def search_memory_executor(
    args: dict[str, Any],
    ctx: Any | None = None,  # TraceContext, typed Any to avoid circular import
) -> ToolResult:
    """Execute a memory graph query and return structured results.

    Args:
        args: Tool arguments matching search_memory_tool parameters.
        ctx: Optional trace context for logging.

    Returns:
        ToolResult with success/failure and memory results as output.
    """
    query_text: str = args.get("query_text", "")
    entity_types: list[str] = args.get("entity_types") or []
    entity_names: list[str] = args.get("entity_names") or []
    recency_days: int = int(args.get("recency_days") or 90)
    limit: int = min(max(int(args.get("limit") or 10), 1), 50)

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
        from personal_agent.memory.models import MemoryQuery
        from personal_agent.memory.service import MemoryService

        # Use service singleton from app context if available
        memory_service: MemoryService | None = None
        try:
            from personal_agent.service.app import memory_service as global_memory_service  # type: ignore[attr-defined]
            if global_memory_service and global_memory_service.connected:
                memory_service = global_memory_service
        except (ImportError, AttributeError):
            pass

        if not memory_service or not memory_service.connected:
            return ToolResult(
                success=False,
                output=None,
                error="Memory service unavailable or not connected.",
            )

        # Determine query path
        if entity_names or not _looks_like_broad_query(query_text, entity_types):
            # Entity-name match path
            query = MemoryQuery(
                entity_names=entity_names or _extract_keywords(query_text),
                entity_types=entity_types,
                limit=limit,
                recency_days=recency_days if recency_days > 0 else None,
            )
            result = await memory_service.query_memory(
                query, query_text=query_text
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
            # Broad recall path for open-ended queries
            broad = await memory_service.query_memory_broad(
                entity_types=entity_types or None,
                recency_days=recency_days if recency_days > 0 else 3650,
                limit=limit,
            )
            output = {
                "entities": broad.get("entities", []),
                "sessions": broad.get("sessions", []),
                "recent_turns": broad.get("turns_summary", []),
                "query_path": "broad_recall",
            }

        log.info(
            "search_memory_tool_completed",
            trace_id=trace_id,
            query_path=output.get("query_path"),
            result_count=output.get("total_turns") or len(output.get("entities", [])),
        )

        return ToolResult(success=True, output=output)

    except Exception as e:
        log.error("search_memory_tool_failed", error=str(e), trace_id=trace_id, exc_info=True)
        return ToolResult(success=False, output=None, error=str(e))


def _looks_like_broad_query(query_text: str, entity_types: list[str]) -> bool:
    """Heuristic: is this an open-ended 'what have I discussed?' query?"""
    broad_keywords = {
        "everything", "anything", "topics", "subjects", "history",
        "all", "previous", "past", "before", "discussed", "mentioned",
        "talked about", "asked about",
    }
    words = set(query_text.lower().split())
    return bool(words & broad_keywords) and not entity_types


def _extract_keywords(query_text: str) -> list[str]:
    """Extract candidate entity names from free-text query (capitalised words)."""
    words = query_text.split()
    return [
        w.strip('",.:;!?()')
        for w in words
        if len(w) > 2 and w[0].isupper()
    ][:5]
```

### Register in `src/personal_agent/tools/__init__.py`

Modify `register_mvp_tools`:

```python
from personal_agent.tools.memory_search import search_memory_executor, search_memory_tool

def register_mvp_tools(registry: ToolRegistry) -> None:
    registry.register(read_file_tool, read_file_executor)
    registry.register(list_directory_tool, list_directory_executor)
    registry.register(system_metrics_snapshot_tool, system_metrics_snapshot_executor)
    registry.register(search_memory_tool, search_memory_executor)  # Phase 2.2
```

### Wire `ctx` into the executor call

File: `src/personal_agent/tools/executor.py`

The `ToolExecutionLayer.execute` method passes `args` to the executor function. Check if the executor is async and is `search_memory_executor` — or better, make all executors accept optional `ctx`:

```python
# In ToolExecutionLayer.execute (or _execute_tool_with_governance):
if asyncio.iscoroutinefunction(executor):
    raw = await executor(args, ctx=trace_ctx)
else:
    raw = await asyncio.to_thread(executor, args)
```

The existing executor signature `(args: dict[str, Any]) -> ToolResult` is compatible if `ctx` is optional with a default of `None`.

---

## LLM Tool Definition (OpenAI format)

`get_tool_definitions_for_llm` in `src/personal_agent/tools/registry.py` auto-converts `ToolDefinition` objects to OpenAI JSON. The resulting definition will look like:

```json
{
  "type": "function",
  "function": {
    "name": "search_memory",
    "description": "Search the personal memory graph for past conversations, entities, and topics the user has previously discussed...",
    "parameters": {
      "type": "object",
      "properties": {
        "query_text": {"type": "string", "description": "..."},
        "entity_types": {"type": "array", "description": "..."},
        "entity_names": {"type": "array", "description": "..."},
        "recency_days": {"type": "integer", "description": "..."},
        "limit": {"type": "integer", "description": "..."}
      },
      "required": ["query_text"]
    }
  }
}
```

---

## Interaction with ADR-0025

| Concern | ADR-0025 | ADR-0026 |
|---|---|---|
| Trigger | User message detected at `step_init` | LLM decides mid-reasoning |
| Injection | System prompt context block | Tool result returned to LLM |
| Best for | "What Greek locations have I asked about?" (direct recall query) | "Check if I asked about Athens before telling me the cost" (task with memory sub-step) |
| Fallback if disconnected | Memory context empty, agent still answers | `ToolResult(success=False, error="Memory unavailable")` |

Both mechanisms complement each other. The intent detector (ADR-0025) removes the need to call the tool for simple recall questions. The tool remains available for nuanced in-context use.

---

## Alternatives Considered

**A. Expose `search_memory` as an MCP server.**
Rejected: MCP requires Docker, adds startup latency, and introduces the `mcp_` prefix — which was specifically excluded from entity extraction in ADR-0024 to reduce noise. An in-process tool has zero overhead and no dependency.

**B. Implement via a Cypher query endpoint in the service API.**
Rejected: would require an HTTP round-trip within the same Python process; adds network overhead and a new API surface to maintain.

**C. Store query results as a Turn node ("I searched my memory").**
Rejected: a tool call to query history is not a user interaction. Storing it would create graph noise. The entity extraction prompt already excludes tool call artefacts.

---

## Consequences

**Positive:**
- LLM can query memory during multi-step reasoning, making it genuinely agentic
- Zero overhead — no subprocess, no HTTP, no Docker dependency
- Tool result is structured JSON; LLM can reference specific turn IDs, entity names, and timestamps in its response
- Follows existing tool registration pattern — no new infrastructure

**Negative:**
- The `ctx` threading to the executor requires a small refactor of `ToolExecutionLayer` to pass `trace_ctx` to async executors
- `_extract_keywords` heuristic for free-text → entity names is basic; a more sophisticated approach (NER/embedding lookup) can be added later

---

## Acceptance Criteria

- [ ] `search_memory` appears in the LLM tool list returned by `get_tool_definitions_for_llm`
- [ ] Agent correctly calls `search_memory` when asked "Before answering, check if I've discussed Athens before"
- [ ] `ToolResult.output` contains `matched_turns` or `entities` depending on query path
- [ ] Tool gracefully returns `success=False` when `MemoryService` is not connected
- [ ] `search_memory` tool calls are NOT stored as entities in the graph (verify via entity extraction exclusion rules)
- [ ] Unit test for `search_memory_executor` with a mock `MemoryService`
