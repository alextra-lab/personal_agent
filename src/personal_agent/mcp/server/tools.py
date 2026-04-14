"""MCP server tool definitions for Seshat.

These tools expose Seshat's Knowledge Layer to external agents via the MCP
protocol. The tool set mirrors the Seshat API Gateway (ADR-0045) endpoints so
both the SKILL.md (Tier 2) and MCP server (Tier 3) integration paths hit the
same backend (ADR-0050 D1, D2).

Tools:
    seshat_search_knowledge    -- semantic search over entities and relationships
    seshat_get_entity          -- retrieve a specific entity by ID
    seshat_store_fact          -- write a new entity to the knowledge graph
    seshat_get_session_context -- read conversation history from a session
    seshat_query_observations  -- query execution traces and performance data
    seshat_delegate            -- delegate a sub-task back to Seshat (reverse)

See: docs/architecture_decisions/ADR-0050-remote-agent-harness-integration.md D2
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MCPToolDefinition:
    """An MCP tool definition exposed by the Seshat server.

    Attributes:
        name: Tool name used in MCP protocol tool-call messages.
        description: Human-readable description shown to the external agent.
        input_schema: JSON Schema object describing the tool's input parameters.
    """

    name: str
    description: str
    input_schema: dict[str, Any]


SESHAT_TOOLS: tuple[MCPToolDefinition, ...] = (
    MCPToolDefinition(
        name="seshat_search_knowledge",
        description=(
            "Search Seshat's knowledge graph for entities and relationships. "
            "Returns ranked results with freshness and confidence metadata."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free-text search query.",
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "description": "Maximum number of results to return.",
                },
            },
            "required": ["query"],
        },
    ),
    MCPToolDefinition(
        name="seshat_get_entity",
        description=(
            "Retrieve a specific entity from the knowledge graph by its ID, "
            "including relationships, access history, and confidence scores."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "Unique entity identifier.",
                },
            },
            "required": ["entity_id"],
        },
    ),
    MCPToolDefinition(
        name="seshat_store_fact",
        description=(
            "Store a new entity or relationship in the knowledge graph. "
            "The source is tagged as 'external_agent' for provenance tracking."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": "Entity name or fact description.",
                },
                "entity_type": {
                    "type": "string",
                    "description": "Entity type (e.g. 'concept', 'person', 'decision').",
                },
                "metadata": {
                    "type": "object",
                    "description": "Additional structured metadata for the entity.",
                },
            },
            "required": ["entity", "entity_type"],
        },
    ),
    MCPToolDefinition(
        name="seshat_get_session_context",
        description=(
            "Read conversation history from a Seshat session. "
            "Useful for delegation context -- understand what was discussed before."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Unique session identifier.",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "Maximum number of messages to return (most recent).",
                },
            },
            "required": ["session_id"],
        },
    ),
    MCPToolDefinition(
        name="seshat_query_observations",
        description=(
            "Query Seshat's execution traces, cost data, and performance metrics. "
            "Filter by trace ID or return the most recent observations."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "Maximum number of observations to return.",
                },
                "trace_id": {
                    "type": "string",
                    "description": "Optional trace ID to filter observations.",
                },
            },
        },
    ),
    MCPToolDefinition(
        name="seshat_delegate",
        description=(
            "Delegate a sub-task back to Seshat for processing (reverse delegation). "
            "Requires the 'delegate' scope in the caller's access token. "
            "Delegated tasks run through Seshat's normal governance pipeline."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Description of the task to delegate.",
                },
                "type": {
                    "type": "string",
                    "enum": ["linear_issue", "knowledge_query", "decomposition"],
                    "description": "Task type controlling which pipeline handles it.",
                },
                "details": {
                    "type": "object",
                    "description": "Additional structured details for the task.",
                },
            },
            "required": ["task", "type"],
        },
    ),
)

# Index by name for O(1) lookup in the server router.
SESHAT_TOOLS_BY_NAME: dict[str, MCPToolDefinition] = {t.name: t for t in SESHAT_TOOLS}
