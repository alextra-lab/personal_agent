"""Native Elasticsearch tool — Phase 1 of CLI-First Tool Migration (ADR-0028).

Replaces four MCP tools (mcp_esql, mcp_list_indices, mcp_get_mappings,
mcp_get_shards) with a single in-process Python tool using httpx.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx

from personal_agent.config import settings
from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

ESAction = Literal["esql", "list_indices", "get_mappings", "get_shards"]

query_elasticsearch_tool = ToolDefinition(
    name="query_elasticsearch",
    description=(
        "Query the local Elasticsearch cluster for logs, traces, metrics, and index metadata. "
        "Supports four actions:\n"
        "- 'esql': Run an ES|QL query (requires 'query' param). "
        "Example: FROM agent-logs-* | WHERE level='ERROR' | LIMIT 20\n"
        "- 'list_indices': List all indices (optional 'index' glob filter).\n"
        "- 'get_mappings': Get field mappings for an index (requires 'index' param).\n"
        "- 'get_shards': Get shard info for all or one index (optional 'index' param).\n"
        "Use for observability queries, log analysis, and index introspection. "
        "Prefer this over any disabled MCP Elasticsearch tools."
    ),
    category="network",
    parameters=[
        ToolParameter(
            name="action",
            type="string",
            description=(
                "Operation to perform: 'esql', 'list_indices', 'get_mappings', or 'get_shards'."
            ),
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="query",
            type="string",
            description="ES|QL query string. Required when action='esql'.",
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="index",
            type="string",
            description=(
                "Index name or glob pattern. "
                "Required for get_mappings; optional for list_indices and get_shards."
            ),
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="limit",
            type="number",
            description="Maximum rows to return for esql action (1-1000, default 100).",
            required=False,
            default=None,
            json_schema=None,
        ),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=30,
    rate_limit_per_hour=200,
)


async def query_elasticsearch_executor(
    action: str = "",
    query: str | None = None,
    index: str | None = None,
    limit: int | None = None,
    ctx: TraceContext | None = None,
) -> dict[str, Any]:
    """Execute an Elasticsearch operation via the REST API.

    Dispatches to one of four sub-operations based on ``action``.

    Args:
        action: One of 'esql', 'list_indices', 'get_mappings', 'get_shards'.
        query: ES|QL query string (required for action='esql').
        index: Index name or glob (required for get_mappings, optional otherwise).
        limit: Max ES|QL result rows (default 100, capped at 1000).
        ctx: Optional trace context for structured logging.

    Returns:
        Dict whose structure depends on the action:
        - esql: ``{"columns": [...], "values": [...], "row_count": N}``
        - list_indices: ``{"indices": [...], "index_count": N}``
        - get_mappings: ``{"index": str, "mappings": {...}}``
        - get_shards: ``{"shards": [...], "shard_count": N}``

    Raises:
        ToolExecutionError: On invalid action, missing required params,
            connection failure, timeout, or non-2xx response.
    """
    action = (action or "").strip().lower()
    valid_actions: set[ESAction] = {"esql", "list_indices", "get_mappings", "get_shards"}
    if action not in valid_actions:
        raise ToolExecutionError(
            f"Invalid action '{action}'. Must be one of: {', '.join(sorted(valid_actions))}."
        )

    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"
    base_url = settings.elasticsearch_url.rstrip("/")

    log.info(
        "query_elasticsearch_started",
        trace_id=trace_id,
        action=action,
        index=index,
    )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            result = await _dispatch(client, action, base_url, query, index, limit, trace_id)
    except ToolExecutionError:
        raise
    except httpx.ConnectError as exc:
        msg = (
            f"Cannot connect to Elasticsearch at {base_url}. "
            "Is the elasticsearch Docker service running?"
        )
        log.error("query_elasticsearch_connect_failed", trace_id=trace_id, error=msg)
        raise ToolExecutionError(msg) from exc
    except httpx.TimeoutException as exc:
        msg = "Elasticsearch request timed out after 30s."
        log.error("query_elasticsearch_timeout", trace_id=trace_id, error=msg)
        raise ToolExecutionError(msg) from exc
    except Exception as exc:
        log.error(
            "query_elasticsearch_failed",
            trace_id=trace_id,
            action=action,
            error=str(exc),
            exc_info=True,
        )
        raise ToolExecutionError(str(exc)) from exc

    log.info(
        "query_elasticsearch_completed",
        trace_id=trace_id,
        action=action,
    )
    return result


async def _dispatch(
    client: httpx.AsyncClient,
    action: str,
    base_url: str,
    query: str | None,
    index: str | None,
    limit: int | None,
    trace_id: str,
) -> dict[str, Any]:
    """Route to the appropriate ES REST call."""
    if action == "esql":
        return await _run_esql(client, base_url, query, limit, trace_id)
    if action == "list_indices":
        return await _list_indices(client, base_url, index, trace_id)
    if action == "get_mappings":
        return await _get_mappings(client, base_url, index, trace_id)
    # action == "get_shards"
    return await _get_shards(client, base_url, index, trace_id)


async def _run_esql(
    client: httpx.AsyncClient,
    base_url: str,
    query: str | None,
    limit: int | None,
    trace_id: str,
) -> dict[str, Any]:
    if not query or not query.strip():
        raise ToolExecutionError("'query' parameter is required for action='esql'.")

    row_limit = max(1, min(int(limit or 100), 1000))
    # Inject LIMIT only if query doesn't already have one (case-insensitive)
    esql_query = query.strip()
    if "| limit" not in esql_query.lower():
        esql_query = f"{esql_query} | LIMIT {row_limit}"

    resp = await client.post(
        f"{base_url}/_query",
        json={"query": esql_query, "format": "json"},
        headers={"Content-Type": "application/json"},
    )
    _raise_for_status(resp, trace_id)
    data = resp.json()

    columns = [col.get("name") for col in (data.get("columns") or [])]
    values = data.get("values") or []
    return {
        "columns": columns,
        "values": values,
        "row_count": len(values),
        "query_used": esql_query,
    }


async def _list_indices(
    client: httpx.AsyncClient,
    base_url: str,
    index: str | None,
    trace_id: str,
) -> dict[str, Any]:
    path = f"{base_url}/_cat/indices"
    if index:
        path = f"{base_url}/_cat/indices/{index}"
    resp = await client.get(
        path, params={"format": "json", "h": "index,status,health,docs.count,store.size"}
    )
    _raise_for_status(resp, trace_id)
    indices = resp.json()
    return {"indices": indices, "index_count": len(indices)}


async def _get_mappings(
    client: httpx.AsyncClient,
    base_url: str,
    index: str | None,
    trace_id: str,
) -> dict[str, Any]:
    if not index or not index.strip():
        raise ToolExecutionError("'index' parameter is required for action='get_mappings'.")
    resp = await client.get(f"{base_url}/{index.strip()}/_mapping")
    _raise_for_status(resp, trace_id)
    data = resp.json()
    # Return first index's mappings (or all if wildcard returned multiple)
    keys = list(data.keys())
    if len(keys) == 1:
        return {"index": keys[0], "mappings": data[keys[0]].get("mappings", {})}
    return {"indices": {k: v.get("mappings", {}) for k, v in data.items()}}


async def _get_shards(
    client: httpx.AsyncClient,
    base_url: str,
    index: str | None,
    trace_id: str,
) -> dict[str, Any]:
    path = f"{base_url}/_cat/shards"
    if index:
        path = f"{base_url}/_cat/shards/{index.strip()}"
    resp = await client.get(path, params={"format": "json"})
    _raise_for_status(resp, trace_id)
    shards = resp.json()
    return {"shards": shards, "shard_count": len(shards)}


def _raise_for_status(resp: httpx.Response, trace_id: str) -> None:
    """Raise ToolExecutionError with ES error body on non-2xx."""
    if resp.is_error:
        try:
            body = resp.json()
            reason = body.get("error", {}).get("reason") or str(body)
        except Exception:
            reason = resp.text[:500]
        msg = f"Elasticsearch returned HTTP {resp.status_code}: {reason}"
        log.error(
            "query_elasticsearch_http_error",
            trace_id=trace_id,
            status=resp.status_code,
            reason=reason,
        )
        raise ToolExecutionError(msg)
