"""Route-trace ledger REST read surface for the Seshat API Gateway (FRE-514).

Exposes the FRE-452 route-trace ledger (Postgres ``route_traces``) over HTTP under
``/observations/route-traces/*`` so a turn's stimulus → model-path → result-type record is
fetchable without direct SQL. The row shape is the seam-neutral
:class:`~personal_agent.observability.route_trace.types.RouteTraceRow` DTO, serialized
as-is (ADR-0088 keeps this row stable across the future ``observe_topology`` seam).

Three endpoints, all requiring the ``observations:read`` scope (these are system
observability rows, not user content — consistent with the sibling ES ``/observations/*``
endpoints; the table has no ``user_id`` so there is no per-user ownership check):

- ``GET /{trace_id}`` — a single row by trace id.
- ``GET /session/{session_id}`` — a session's rows, newest first.
- ``GET /recent`` — recent-N with three optional deterministic-shell boundary filters:
  ``label_lie`` (gateway-declared expansion vs actual orchestration — a *candidate*
  heuristic), ``fallback_triggered``, and ``not_reconciled`` (live/authoritative cost
  disagreed). Filters compose with ``AND``.

The ledger is resolved via the process-wide singleton
(:func:`~personal_agent.observability.route_trace.ledger.get_route_trace_ledger`); an
unconnected pool yields 503.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, cast
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder

from personal_agent.gateway.auth import TokenInfo, require_scope
from personal_agent.gateway.errors import not_found, service_unavailable
from personal_agent.observability.route_trace.ledger import (
    RouteTraceLedger,
    get_route_trace_ledger,
)
from personal_agent.observability.route_trace.types import RouteTraceRow
from personal_agent.telemetry.trace import SystemTraceContext

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/observations/route-traces", tags=["observations"])

# Server-side cap on result-set size (FRE-514, Codex review): callers cannot drive an
# unbounded scan straight through to Postgres.
_MAX_LIMIT = 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_ledger() -> RouteTraceLedger:
    """Return the connected route-trace ledger singleton.

    Returns:
        The process-wide :class:`RouteTraceLedger`.

    Raises:
        HTTPException(503): When the ledger's connection pool is not available.
    """
    ledger = get_route_trace_ledger()
    if ledger.pool is None:
        raise service_unavailable("Route-trace ledger is not available")
    return ledger


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


def _serialize_row(row: RouteTraceRow) -> dict[str, Any]:
    """Serialize a :class:`RouteTraceRow` to a JSON-safe dict (seam-neutral shape).

    Uses the DTO itself as the single source of truth (no duplicated field mirror);
    ``jsonable_encoder`` handles the ``UUID``/``datetime`` fields.

    Args:
        row: The route-trace row to serialize.

    Returns:
        A JSON-serialisable dict mirroring the DTO fields.
    """
    return cast(dict[str, Any], jsonable_encoder(asdict(row)))


# ---------------------------------------------------------------------------
# Endpoints — fixed paths declared before the ``/{trace_id}`` parametrised route
# ---------------------------------------------------------------------------


@router.get("/recent")
async def list_recent_route_traces(
    limit: int = 50,
    label_lie: bool = False,
    fallback_triggered: bool = False,
    not_reconciled: bool = False,
    token: TokenInfo = Depends(require_scope("observations:read")),  # noqa: B008
) -> list[dict[str, Any]]:
    """Return the most recent route-trace rows, with optional boundary filters.

    Args:
        limit: Maximum rows to return (clamped to ``_MAX_LIMIT``; must be >= 1).
        label_lie: Restrict to label-lie candidates (declared expansion vs actual event).
        fallback_triggered: Restrict to turns that escalated to the primary.
        not_reconciled: Restrict to turns whose live/authoritative cost disagreed.
        token: Validated bearer token with ``observations:read`` scope.

    Returns:
        List of serialized route-trace rows, newest first.
    """
    ledger = _get_ledger()
    ctx = SystemTraceContext.new("route_trace_api")
    bounded = _clamp_limit(limit)
    log.info(
        "gateway_route_traces_recent",
        limit=bounded,
        label_lie=label_lie,
        fallback_triggered=fallback_triggered,
        not_reconciled=not_reconciled,
        token_name=token.name,
        trace_id=ctx.trace_id,
    )
    rows = await ledger.list_recent(
        limit=bounded,
        label_lie=label_lie,
        fallback_triggered=fallback_triggered,
        not_reconciled=not_reconciled,
    )
    return [_serialize_row(r) for r in rows]


@router.get("/session/{session_id}")
async def list_route_traces_by_session(
    session_id: str,
    limit: int = 50,
    token: TokenInfo = Depends(require_scope("observations:read")),  # noqa: B008
) -> list[dict[str, Any]]:
    """Return a session's route-trace rows, newest first.

    Args:
        session_id: The owning session identifier (UUID string).
        limit: Maximum rows to return (clamped to ``_MAX_LIMIT``; must be >= 1).
        token: Validated bearer token with ``observations:read`` scope.

    Returns:
        List of serialized route-trace rows for the session, newest first.

    Raises:
        HTTPException(400): When ``session_id`` is not a valid UUID.
    """
    ledger = _get_ledger()
    bounded = _clamp_limit(limit)
    try:
        sid = UUID(session_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_parameter",
                "message": "session_id must be a valid UUID",
                "status": 400,
            },
        ) from None
    ctx = SystemTraceContext.new("route_trace_api")
    log.info(
        "gateway_route_traces_by_session",
        session_id=str(sid),
        limit=bounded,
        token_name=token.name,
        trace_id=ctx.trace_id,
    )
    rows = await ledger.list_by_session_id(sid, limit=bounded)
    return [_serialize_row(r) for r in rows]


@router.get("/{trace_id}")
async def get_route_trace(
    trace_id: str,
    token: TokenInfo = Depends(require_scope("observations:read")),  # noqa: B008
) -> dict[str, Any]:
    """Return a single route-trace row by trace id.

    Args:
        trace_id: The turn trace identifier (UUID string).
        token: Validated bearer token with ``observations:read`` scope.

    Returns:
        The serialized route-trace row.

    Raises:
        HTTPException(404): When ``trace_id`` is malformed or no row exists (existence is
            not leaked — both map to 404).
    """
    ledger = _get_ledger()
    try:
        tid = UUID(trace_id)
    except ValueError:
        raise not_found("route_trace") from None
    ctx = SystemTraceContext.new("route_trace_api")
    log.info(
        "gateway_route_traces_get",
        trace_id=str(tid),
        token_name=token.name,
        request_trace_id=ctx.trace_id,
    )
    row = await ledger.get_by_trace_id(tid)
    if row is None:
        raise not_found("route_trace")
    return _serialize_row(row)
