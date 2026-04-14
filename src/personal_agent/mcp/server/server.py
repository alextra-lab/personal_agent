"""Seshat MCP Server -- exposes Knowledge Layer to external agents.

This is Seshat-as-server (not Seshat-as-client). External agent harnesses
connect to this server to access Seshat's knowledge graph, session history,
and observation data via the MCP protocol (ADR-0050 D2).

The handlers are stub implementations — they will be wired to the Seshat API
Gateway (Phase C2, ADR-0045) once the gateway is deployed. The tool definitions
and routing framework are the deliverable for this issue (FRE-208).

Architecture:
    External agent  -->  SeshatMCPServer.handle_tool_call()
                              |
                              v
                    _handle_<tool_name>()   (stub -> will delegate to gateway)
                              |
                              v
                    Seshat API Gateway (ADR-0045)
                              |
                        Knowledge Layer
                    (Neo4j / PostgreSQL / ES)

See: docs/architecture_decisions/ADR-0050-remote-agent-harness-integration.md D2
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any, cast

import structlog

from personal_agent.mcp.server.tools import (
    SESHAT_TOOLS,
    SESHAT_TOOLS_BY_NAME,
    MCPToolDefinition,
)

log = structlog.get_logger(__name__)

# Sentinel returned when a handler is not yet wired to the gateway.
_STUB_RESPONSE: dict[str, str] = {
    "status": "stub",
    "message": "Handler will be wired to Seshat API Gateway after Phase C2 deployment.",
}


class SeshatMCPServer:
    """MCP server exposing Seshat's capabilities to external agents.

    Tool calls are routed to per-tool handler methods. Current handlers are
    stubs that log the call and return a status message. They will be wired to
    the Gateway API (Phase C2) once deployed.

    Attributes:
        _gateway_url: Optional URL of the Seshat API Gateway. When set, handlers
            will forward requests via HTTP rather than returning stubs.
    """

    def __init__(self, gateway_url: str | None = None) -> None:
        """Initialise the MCP server.

        Args:
            gateway_url: Optional URL of the Seshat API Gateway
                (e.g. ``https://seshat.example.com``). When None, all handlers
                return stub responses.
        """
        self._gateway_url = gateway_url

    @property
    def tools(self) -> tuple[MCPToolDefinition, ...]:
        """Return all tool definitions exposed by this server.

        Returns:
            Tuple of MCPToolDefinition objects, one per tool.
        """
        return SESHAT_TOOLS

    async def handle_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        caller_id: str = "unknown",
    ) -> dict[str, Any]:
        """Route an MCP tool call to the appropriate handler.

        Args:
            tool_name: MCP tool name (must match one of SESHAT_TOOLS names).
            arguments: Tool arguments per the tool's input schema.
            caller_id: Optional caller identity for audit logging.

        Returns:
            Dict containing the tool result or an error description.
        """
        if tool_name not in SESHAT_TOOLS_BY_NAME:
            log.warning(
                "seshat_mcp_server.unknown_tool",
                tool=tool_name,
                caller_id=caller_id,
                known_tools=list(SESHAT_TOOLS_BY_NAME.keys()),
            )
            return {"error": f"Unknown tool: {tool_name}"}

        handler_name = f"_handle_{tool_name}"
        handler = getattr(self, handler_name, None)
        if handler is None:
            log.error(
                "seshat_mcp_server.handler_missing",
                tool=tool_name,
                handler=handler_name,
            )
            return {"error": f"Handler not implemented: {tool_name}"}

        log.info(
            "seshat_mcp_server.tool_call",
            tool=tool_name,
            caller_id=caller_id,
            has_gateway=self._gateway_url is not None,
        )
        typed_handler = cast(
            Callable[..., Coroutine[Any, Any, dict[str, Any]]],
            handler,
        )
        return await typed_handler(arguments, caller_id=caller_id)

    # ------------------------------------------------------------------
    # Per-tool handlers (stubs -- wire to gateway after Phase C2)
    # ------------------------------------------------------------------

    async def _handle_seshat_search_knowledge(
        self,
        args: dict[str, Any],
        caller_id: str = "unknown",
    ) -> dict[str, Any]:
        """Handle seshat_search_knowledge tool call.

        Args:
            args: Tool arguments (query, limit).
            caller_id: Caller identity for audit logging.

        Returns:
            Search results or stub response.
        """
        log.info(
            "seshat_mcp_server.search_knowledge",
            query=args.get("query"),
            limit=args.get("limit", 10),
            caller_id=caller_id,
        )
        return _STUB_RESPONSE

    async def _handle_seshat_get_entity(
        self,
        args: dict[str, Any],
        caller_id: str = "unknown",
    ) -> dict[str, Any]:
        """Handle seshat_get_entity tool call.

        Args:
            args: Tool arguments (entity_id).
            caller_id: Caller identity for audit logging.

        Returns:
            Entity details or stub response.
        """
        log.info(
            "seshat_mcp_server.get_entity",
            entity_id=args.get("entity_id"),
            caller_id=caller_id,
        )
        return _STUB_RESPONSE

    async def _handle_seshat_store_fact(
        self,
        args: dict[str, Any],
        caller_id: str = "unknown",
    ) -> dict[str, Any]:
        """Handle seshat_store_fact tool call.

        Args:
            args: Tool arguments (entity, entity_type, metadata).
            caller_id: Caller identity for audit logging.

        Returns:
            Store confirmation or stub response.
        """
        log.info(
            "seshat_mcp_server.store_fact",
            entity=args.get("entity"),
            entity_type=args.get("entity_type"),
            caller_id=caller_id,
        )
        return _STUB_RESPONSE

    async def _handle_seshat_get_session_context(
        self,
        args: dict[str, Any],
        caller_id: str = "unknown",
    ) -> dict[str, Any]:
        """Handle seshat_get_session_context tool call.

        Args:
            args: Tool arguments (session_id, limit).
            caller_id: Caller identity for audit logging.

        Returns:
            Session messages or stub response.
        """
        log.info(
            "seshat_mcp_server.get_session_context",
            session_id=args.get("session_id"),
            limit=args.get("limit", 20),
            caller_id=caller_id,
        )
        return _STUB_RESPONSE

    async def _handle_seshat_query_observations(
        self,
        args: dict[str, Any],
        caller_id: str = "unknown",
    ) -> dict[str, Any]:
        """Handle seshat_query_observations tool call.

        Args:
            args: Tool arguments (limit, trace_id).
            caller_id: Caller identity for audit logging.

        Returns:
            Observation records or stub response.
        """
        log.info(
            "seshat_mcp_server.query_observations",
            limit=args.get("limit", 20),
            trace_id=args.get("trace_id"),
            caller_id=caller_id,
        )
        return _STUB_RESPONSE

    async def _handle_seshat_delegate(
        self,
        args: dict[str, Any],
        caller_id: str = "unknown",
    ) -> dict[str, Any]:
        """Handle seshat_delegate tool call (reverse delegation).

        Args:
            args: Tool arguments (task, type, details).
            caller_id: Caller identity for audit logging.

        Returns:
            Delegation result or stub response.
        """
        log.info(
            "seshat_mcp_server.delegate",
            task=args.get("task"),
            task_type=args.get("type"),
            caller_id=caller_id,
        )
        return _STUB_RESPONSE
