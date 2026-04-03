"""Filter MCP gateway tools by configured server allowlist.

The gateway exposes tools from many servers. ``AGENT_MCP_GATEWAY_ENABLED_SERVERS``
selects which servers' tools to register. Matching uses, in order:

1. **Substring**: ``server_token`` appears in the MCP tool name (e.g. ``perplexity`` in
   ``perplexity_ask``).
2. **Metadata**: Hosted Linear tools include ``meta`` with ``linear.app`` in allowed
   domains (tool names like ``save_issue`` do not contain ``linear``).
3. **Aliases**: Some servers expose tools whose names omit the server id (e.g.
   Elasticsearch ``esql``, DuckDuckGo ``search``). Those are listed explicitly.

This module lives outside ``personal_agent.mcp`` so it can be imported in tests without
loading the MCP SDK or the gateway package (which pulls in a heavy import graph).
"""

from __future__ import annotations

from typing import Any

# Tools whose names do not contain the server token and have no distinguishing meta
# (see Docker MCP gateway ``docker mcp tools list --format json``).
_MCP_SERVER_TOOL_NAME_ALIASES: dict[str, frozenset[str]] = {
    "context7": frozenset({"get-library-docs", "resolve-library-id"}),
    "duckduckgo": frozenset({"search"}),
    "elasticsearch": frozenset({"esql", "list_indices", "get_mappings"}),
}


def _meta_contains_linear_app(meta: Any) -> bool:
    """Return True if MCP tool metadata indicates Linear's hosted MCP server."""
    if meta is None:
        return False
    if isinstance(meta, dict):
        return any(_meta_contains_linear_app(v) for v in meta.values())
    if isinstance(meta, list):
        return any(_meta_contains_linear_app(item) for item in meta)
    if isinstance(meta, str):
        return "linear.app" in meta
    return False


def mcp_tool_matches_enabled_server(tool: dict[str, Any], server_token: str) -> bool:
    """Return whether ``tool`` should be kept when ``server_token`` is in the allowlist.

    Args:
        tool: Tool dict from MCP ``list_tools()`` (e.g. ``model_dump()``), including
            optional ``name`` and ``meta``.
        server_token: One entry from ``settings.mcp_gateway_enabled_servers`` (e.g.
            ``linear``, ``elasticsearch``).

    Returns:
        True if this tool should be registered for that token.
    """
    token = server_token.strip()
    if not token:
        return False

    name = tool.get("name") or ""
    if token in name:
        return True

    lowered = token.lower()
    if lowered == "linear":
        meta = tool.get("meta") or tool.get("_meta")
        if _meta_contains_linear_app(meta):
            return True

    aliases = _MCP_SERVER_TOOL_NAME_ALIASES.get(lowered)
    if aliases is not None and name in aliases:
        return True

    return False
