"""Observation (trace event) REST endpoints for the Seshat API Gateway.

Exposes Elasticsearch trace data over HTTP under ``/observations/*``.
All endpoints require the ``observations:read`` scope.

The router delegates to an Elasticsearch ``AsyncElasticsearch`` client
obtained from ``request.app.state.es_client``.  When the client is not
available (ES not connected) the endpoints return 503.
"""

from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from personal_agent.gateway.auth import TokenInfo, require_scope
from personal_agent.gateway.errors import not_found, service_unavailable
from personal_agent.gateway.rate_limiting import get_rate_limiter

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/observations", tags=["observations"])

# Default Elasticsearch index pattern for request traces
_DEFAULT_INDEX = "agent-logs-*"


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ObservationQueryRequest(BaseModel):
    """Body for POST /observations/query.

    Attributes:
        filters: Field-level equality filters (e.g. ``{"level": "error"}``).
        time_range: Elasticsearch range shorthand (e.g. ``"now-1h"`` or
            ``"now-7d"``).  Defaults to last 24 hours.
        limit: Maximum number of results (default 20).
    """

    model_config = ConfigDict(frozen=True)

    filters: dict[str, Any] = {}
    time_range: str = "now-24h"
    limit: int = 20


# ---------------------------------------------------------------------------
# Dependency: resolve ES client
# ---------------------------------------------------------------------------


def _get_es(request: Request) -> Any:
    """Resolve the Elasticsearch client from app state.

    Args:
        request: Incoming FastAPI request.

    Returns:
        ``AsyncElasticsearch`` client.

    Raises:
        HTTPException(503): When no client is attached.
    """
    es = getattr(request.app.state, "es_client", None)
    if es is None:
        raise service_unavailable("Elasticsearch client is not available")
    return es


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/recent")
async def get_recent_observations(
    request: Request,
    limit: int = 20,
    token: TokenInfo = Depends(require_scope("observations:read")),  # noqa: B008
) -> list[dict[str, Any]]:
    """Return the most recent trace events from Elasticsearch.

    Args:
        request: FastAPI request (injected).
        limit: Maximum number of trace events to return (default 20).
        token: Validated bearer token with ``observations:read`` scope.

    Returns:
        List of trace event dicts ordered by timestamp descending.
    """
    get_rate_limiter().check(token)
    es = _get_es(request)

    log.info("gateway_observations_recent", limit=limit, token_name=token.name)

    query: dict[str, Any] = {
        "size": min(limit, 200),
        "sort": [{"@timestamp": {"order": "desc"}}],
        "query": {"match_all": {}},
    }

    try:
        resp = await es.search(index=_DEFAULT_INDEX, body=query)
        hits = resp.get("hits", {}).get("hits", [])
        return [_flatten_hit(h) for h in hits]
    except Exception as exc:
        log.error("gateway_observations_recent_failed", error=str(exc))
        raise service_unavailable("Elasticsearch query failed") from exc


@router.get("/{trace_id}")
async def get_observation(
    request: Request,
    trace_id: str,
    token: TokenInfo = Depends(require_scope("observations:read")),  # noqa: B008
) -> dict[str, Any]:
    """Retrieve all trace events for a specific trace ID.

    Args:
        request: FastAPI request (injected).
        trace_id: The trace identifier to look up.
        token: Validated bearer token with ``observations:read`` scope.

    Returns:
        Dict with ``trace_id`` and ``events`` list.

    Raises:
        HTTPException(404): When no events are found for the trace ID.
    """
    get_rate_limiter().check(token)
    es = _get_es(request)

    log.info("gateway_observations_get", trace_id=trace_id, token_name=token.name)

    query: dict[str, Any] = {
        "query": {"term": {"trace_id": trace_id}},
        "sort": [{"@timestamp": {"order": "asc"}}],
        "size": 500,
    }

    try:
        resp = await es.search(index=_DEFAULT_INDEX, body=query)
        hits = resp.get("hits", {}).get("hits", [])
    except Exception as exc:
        log.error("gateway_observations_get_failed", trace_id=trace_id, error=str(exc))
        raise service_unavailable("Elasticsearch query failed") from exc

    if not hits:
        raise not_found("trace")

    return {"trace_id": trace_id, "events": [_flatten_hit(h) for h in hits]}


@router.post("/query")
async def query_observations(
    request: Request,
    body: ObservationQueryRequest,
    token: TokenInfo = Depends(require_scope("observations:read")),  # noqa: B008
) -> list[dict[str, Any]]:
    """Execute a structured query against Elasticsearch trace indices.

    Args:
        request: FastAPI request (injected).
        body: Query parameters (filters, time range, limit).
        token: Validated bearer token with ``observations:read`` scope.

    Returns:
        List of matching trace event dicts.
    """
    get_rate_limiter().check(token)
    es = _get_es(request)

    log.info(
        "gateway_observations_query",
        filters=body.filters,
        time_range=body.time_range,
        limit=body.limit,
        token_name=token.name,
    )

    must_clauses: list[dict[str, Any]] = [{"range": {"@timestamp": {"gte": body.time_range}}}]
    for field_name, value in body.filters.items():
        must_clauses.append({"term": {field_name: value}})

    query: dict[str, Any] = {
        "size": min(body.limit, 500),
        "sort": [{"@timestamp": {"order": "desc"}}],
        "query": {"bool": {"must": must_clauses}},
    }

    try:
        resp = await es.search(index=_DEFAULT_INDEX, body=query)
        hits = resp.get("hits", {}).get("hits", [])
        return [_flatten_hit(h) for h in hits]
    except Exception as exc:
        log.error("gateway_observations_query_failed", error=str(exc))
        raise service_unavailable("Elasticsearch query failed") from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flatten_hit(hit: dict[str, Any]) -> dict[str, Any]:
    """Flatten an Elasticsearch hit into a flat dict.

    Args:
        hit: Raw Elasticsearch document hit with ``_source``, ``_id``, etc.

    Returns:
        Flat dict with ``_id`` merged into the source document.
    """
    doc: dict[str, Any] = dict(hit.get("_source", {}))
    doc["_id"] = hit.get("_id")
    return doc
