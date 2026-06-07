"""Sub-agent captures REST read surface for the Seshat API Gateway (FRE-519).

Exposes the per-sub-agent ``SubAgentCapture`` records (written by FRE-505 to the
Elasticsearch index family ``{captains_log_index_prefix}-captures-subagents-*``,
doc_id ``{trace_id}:{task_id}``) over HTTP under ``/observations/sub-agents/*`` so a
decomposition turn's sub-agents — what each was fed (input-context breakdown +
memory presence), task/mode/tools granted-vs-used, full output + injected digest +
truncation ratio — are reconstructable in one call without raw ES queries.

Three endpoints, all requiring the ``observations:read`` scope (these are system
observability rows, not user content — consistent with the sibling ES
``/observations/*`` and ``/observations/route-traces/*`` surfaces):

- ``GET /recent`` — recent-N captures, newest first; optional ``failed_only`` restricts
  to sub-agents that errored or produced an empty digest.
- ``GET /session/{session_id}`` — a session's captures, newest first.
- ``GET /{trace_id}`` — the captures for one turn. A non-decomposed turn legitimately
  has none, so this returns ``200`` with ``count: 0`` (not 404).

Read-only; FRE-505 owns the write path. The index pattern is settings-driven (shared
with the FRE-505 writer) so test/prod substrate isolation (FRE-375) holds.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from personal_agent.captains_log.capture import SUBAGENT_CAPTURES_INDEX_PREFIX
from personal_agent.gateway.auth import TokenInfo, require_scope
from personal_agent.gateway.errors import service_unavailable
from personal_agent.gateway.rate_limiting import get_rate_limiter
from personal_agent.telemetry.trace import SystemTraceContext

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/observations/sub-agents", tags=["observations"])

# Index pattern for the FRE-505 sub-agent captures (settings-driven; FRE-375).
_INDEX = f"{SUBAGENT_CAPTURES_INDEX_PREFIX}-*"
# Server-side cap on result-set size — callers cannot drive an unbounded ES scan.
_MAX_LIMIT = 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_es(request: Request) -> Any:
    """Resolve the Elasticsearch client from app state.

    Args:
        request: Incoming FastAPI request.

    Returns:
        The ``AsyncElasticsearch`` client.

    Raises:
        HTTPException(503): When no client is attached (ES not connected).
    """
    es = getattr(request.app.state, "es_client", None)
    if es is None:
        raise service_unavailable("Elasticsearch client is not available")
    return es


def _clamp_limit(limit: int) -> int:
    """Validate and clamp a caller-supplied ``limit`` to ``[1, _MAX_LIMIT]``.

    Args:
        limit: Raw limit from the query string.

    Returns:
        The clamped limit.

    Raises:
        HTTPException(400): When ``limit`` is less than 1.
    """
    if limit < 1:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_parameter",
                "message": "limit must be a positive integer",
                "status": 400,
            },
        )
    return min(limit, _MAX_LIMIT)


def _flatten_hit(hit: dict[str, Any]) -> dict[str, Any]:
    """Flatten an Elasticsearch hit into the source doc with its ``_id`` merged in.

    Args:
        hit: Raw Elasticsearch document hit.

    Returns:
        The ``_source`` dict with ``_id`` added.
    """
    doc: dict[str, Any] = dict(hit.get("_source", {}))
    doc["_id"] = hit.get("_id")
    return doc


async def _search(es: Any, body: dict[str, Any], trace_id: str) -> list[dict[str, Any]]:
    """Run an ES search against the sub-agent captures index, mapping errors to 503.

    Args:
        es: The Elasticsearch client.
        body: The ES query body.
        trace_id: Request trace id for error-log correlation.

    Returns:
        Flattened hit dicts.

    Raises:
        HTTPException(503): When the ES query fails.
    """
    try:
        resp = await es.search(index=_INDEX, body=body)
        hits = resp.get("hits", {}).get("hits", [])
        return [_flatten_hit(h) for h in hits]
    except Exception as exc:
        log.error("gateway_sub_agent_captures_failed", error=str(exc), trace_id=trace_id)
        raise service_unavailable("Elasticsearch query failed") from exc


# ---------------------------------------------------------------------------
# Endpoints — fixed paths declared before the ``/{trace_id}`` parametrised route
# ---------------------------------------------------------------------------


@router.get("/recent")
async def list_recent_sub_agent_captures(
    request: Request,
    limit: int = 50,
    failed_only: bool = False,
    token: TokenInfo = Depends(require_scope("observations:read")),  # noqa: B008
) -> list[dict[str, Any]]:
    """Return the most recent sub-agent captures, newest first.

    Args:
        request: FastAPI request (injected).
        limit: Maximum captures to return (clamped to ``_MAX_LIMIT``; must be >= 1).
        failed_only: Restrict to sub-agents that failed (errored / empty digest).
        token: Validated bearer token with ``observations:read`` scope.

    Returns:
        List of serialized sub-agent capture docs, newest first.
    """
    get_rate_limiter().check(token)
    es = _get_es(request)
    ctx = SystemTraceContext.new("sub_agent_capture_api")
    bounded = _clamp_limit(limit)

    must: list[dict[str, Any]] = [{"match_all": {}}]
    if failed_only:
        must.append({"term": {"success": False}})

    log.info(
        "gateway_sub_agent_captures_recent",
        limit=bounded,
        failed_only=failed_only,
        token_name=token.name,
        trace_id=ctx.trace_id,
    )
    body: dict[str, Any] = {
        "size": bounded,
        "sort": [{"timestamp": {"order": "desc"}}, {"task_id": {"order": "asc"}}],
        "query": {"bool": {"must": must}},
    }
    return await _search(es, body, ctx.trace_id)


@router.get("/session/{session_id}")
async def list_sub_agent_captures_by_session(
    request: Request,
    session_id: str,
    limit: int = 50,
    token: TokenInfo = Depends(require_scope("observations:read")),  # noqa: B008
) -> list[dict[str, Any]]:
    """Return a session's sub-agent captures, newest first.

    Args:
        request: FastAPI request (injected).
        session_id: The owning session identifier (opaque string).
        limit: Maximum captures to return (clamped to ``_MAX_LIMIT``; must be >= 1).
        token: Validated bearer token with ``observations:read`` scope.

    Returns:
        List of serialized sub-agent capture docs for the session, newest first.
    """
    get_rate_limiter().check(token)
    es = _get_es(request)
    ctx = SystemTraceContext.new("sub_agent_capture_api")
    bounded = _clamp_limit(limit)

    log.info(
        "gateway_sub_agent_captures_by_session",
        session_id=session_id,
        limit=bounded,
        token_name=token.name,
        trace_id=ctx.trace_id,
    )
    body: dict[str, Any] = {
        "size": bounded,
        "sort": [{"timestamp": {"order": "desc"}}, {"task_id": {"order": "asc"}}],
        "query": {"term": {"session_id": session_id}},
    }
    return await _search(es, body, ctx.trace_id)


@router.get("/{trace_id}")
async def get_sub_agent_captures(
    request: Request,
    trace_id: str,
    token: TokenInfo = Depends(require_scope("observations:read")),  # noqa: B008
) -> dict[str, Any]:
    """Return the sub-agent captures for a single decomposition turn.

    A non-decomposed turn legitimately has no sub-agents, so this returns ``200``
    with ``count: 0`` rather than 404 — a definitive "this turn did not decompose".

    Args:
        request: FastAPI request (injected).
        trace_id: The turn trace identifier (opaque string).
        token: Validated bearer token with ``observations:read`` scope.

    Returns:
        Dict with ``trace_id``, ``count``, and the ``sub_agents`` list (dispatch order).
    """
    get_rate_limiter().check(token)
    es = _get_es(request)
    ctx = SystemTraceContext.new("sub_agent_capture_api")

    body: dict[str, Any] = {
        "size": _MAX_LIMIT,
        "sort": [{"timestamp": {"order": "asc"}}, {"task_id": {"order": "asc"}}],
        "query": {"term": {"trace_id": trace_id}},
    }
    sub_agents = await _search(es, body, ctx.trace_id)
    log.info(
        "gateway_sub_agent_captures_get",
        trace_id=trace_id,
        count=len(sub_agents),
        token_name=token.name,
        request_trace_id=ctx.trace_id,
    )
    return {"trace_id": trace_id, "count": len(sub_agents), "sub_agents": sub_agents}
