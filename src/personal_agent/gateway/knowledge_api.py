"""Knowledge graph REST endpoints for the Seshat API Gateway.

Exposes the Neo4j knowledge graph over HTTP under ``/knowledge/*``.  All
endpoints require the ``knowledge:read`` or ``knowledge:write`` scope **and**
a verified ``Cf-Access-Authenticated-User-Email`` header (FRE-379). User
identity flows into the ``TraceContext`` passed to the KG backend, where
the FRE-229 visibility filter scopes per-user private entries while public
KG entries (the shared substrate) flow freely.

The router delegates to whatever ``KnowledgeGraphProtocol`` implementation is
attached to ``request.app.state.knowledge_graph``.  In local dev the
``MemoryService`` is wired directly; in remote deployments a
``GatewayKnowledgeGraphClient`` can be used instead.
"""

from collections.abc import AsyncGenerator, Sequence
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.gateway.auth import TokenInfo, require_scope
from personal_agent.gateway.errors import not_found, service_unavailable
from personal_agent.gateway.rate_limiting import get_rate_limiter
from personal_agent.memory.models import Entity, EntityNode, Relationship
from personal_agent.service.auth import _CF_EMAIL_HEADER, _get_user_with_display_name
from personal_agent.telemetry.trace import SystemTraceContext

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class StoreEntityRequest(BaseModel):
    """Request body for POST /knowledge/entities.

    Attributes:
        entity: Entity name (e.g. ``"Paris"``).
        entity_type: Type label (e.g. ``"Place"``).
        metadata: Optional free-form key-value metadata.
    """

    model_config = ConfigDict(frozen=True)

    entity: str
    entity_type: str
    metadata: dict[str, Any] = {}


class StoreEntityResponse(BaseModel):
    """Response for POST /knowledge/entities.

    Attributes:
        id: Entity identifier returned by the knowledge graph.
        created: Always True in the current implementation.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    created: bool


# ---------------------------------------------------------------------------
# Dependency: resolve KnowledgeGraphProtocol from app state
# ---------------------------------------------------------------------------


def _get_kg(request: Request) -> Any:
    """Resolve the knowledge-graph backend from app state.

    Args:
        request: Incoming FastAPI request.

    Returns:
        Object satisfying ``KnowledgeGraphProtocol`` (typically
        :class:`~personal_agent.memory.service.MemoryService`).

    Raises:
        HTTPException(503): When no backend is wired.
    """
    kg = getattr(request.app.state, "knowledge_graph", None)
    if kg is None:
        raise service_unavailable("Knowledge graph backend is not available")
    return kg


async def _get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Provide an async DB session from app state (mirrors session_api._get_db).

    Used by the user-resolution helper below; no other code path in this
    file currently needs the DB.
    """
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise service_unavailable("Database session factory is not available")
    async with factory() as session:
        yield session


async def _require_request_user_id(request: Request, db: AsyncSession) -> Any:
    """Resolve the authenticated user's UUID from the CF Access header.

    Mirrors the same-named helper in :mod:`personal_agent.gateway.session_api`
    (which can't be imported here without creating a cross-module cycle).
    FRE-379 closes the gap where bearer-token holders could read private KG
    entries belonging to other users — adding this dependency makes the
    endpoint reject token-only callers and pins each request to one user.

    Args:
        request: Incoming FastAPI request.
        db: Active async SQLAlchemy session for the users-table lookup.

    Returns:
        Stable ``user_id`` UUID for the requester.

    Raises:
        HTTPException(401): When the ``Cf-Access-Authenticated-User-Email``
            header is absent.
    """
    email = request.headers.get(_CF_EMAIL_HEADER)
    if not email:
        raise HTTPException(
            status_code=401,
            detail="Authentication required (missing CF Access user header)",
        )
    user_id, _ = await _get_user_with_display_name(db, email)
    return user_id


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/search")
async def search_knowledge(
    request: Request,
    q: str,
    limit: int = 10,
    token: TokenInfo = Depends(require_scope("knowledge:read")),  # noqa: B008
    db: AsyncSession = Depends(_get_db),  # noqa: B008
) -> list[dict[str, Any]]:
    """Search the knowledge graph by free-text query, scoped by caller (FRE-379).

    Args:
        request: FastAPI request (injected).
        q: Free-text search string.
        limit: Maximum number of results (1–100, default 10).
        token: Validated bearer token with ``knowledge:read`` scope.
        db: Database session for CF-Access → user_id resolution.

    Returns:
        List of entity result dicts visible to the calling user.

    Raises:
        HTTPException(401): When the CF Access header is absent.
    """
    get_rate_limiter().check(token)
    user_id = await _require_request_user_id(request, db)
    kg = _get_kg(request)
    ctx = SystemTraceContext.new("knowledge_api", user_id=user_id)

    log.info(
        "gateway_knowledge_search",
        query=q,
        limit=limit,
        token_name=token.name,
        user_id=str(user_id),
        trace_id=ctx.trace_id,
    )

    results: Sequence[EntityNode] = await kg.search(q, limit, ctx)
    return [r.model_dump() for r in results]


@router.get("/entities/{entity_id}")
async def get_entity(
    request: Request,
    entity_id: str,
    token: TokenInfo = Depends(require_scope("knowledge:read")),  # noqa: B008
    db: AsyncSession = Depends(_get_db),  # noqa: B008
) -> dict[str, Any]:
    """Retrieve a single entity by identifier, scoped by caller (FRE-379).

    Args:
        request: FastAPI request (injected).
        entity_id: Unique entity identifier.
        token: Validated bearer token with ``knowledge:read`` scope.
        db: Database session for CF-Access → user_id resolution.

    Returns:
        Entity dict, only if visible to the calling user.

    Raises:
        HTTPException(401): When the CF Access header is absent.
        HTTPException(404): When the entity is not found OR not visible
            (cross-user private entries return 404, not 403, to avoid
            confirming existence — mirrors PR #76 discipline).
    """
    get_rate_limiter().check(token)
    user_id = await _require_request_user_id(request, db)
    kg = _get_kg(request)
    ctx = SystemTraceContext.new("knowledge_api", user_id=user_id)

    log.info(
        "gateway_knowledge_get_entity",
        entity_id=entity_id,
        token_name=token.name,
        user_id=str(user_id),
        trace_id=ctx.trace_id,
    )

    entity: EntityNode | None = await kg.get_entity(entity_id, ctx)
    if entity is None:
        raise not_found("entity")
    return entity.model_dump()


@router.post("/entities", status_code=201)
async def store_entity(
    request: Request,
    body: StoreEntityRequest,
    token: TokenInfo = Depends(require_scope("knowledge:write")),  # noqa: B008
    db: AsyncSession = Depends(_get_db),  # noqa: B008
) -> StoreEntityResponse:
    """Persist a new entity to the knowledge graph.

    FRE-379: requires CF Access identity; the trace_id and originating
    user are recorded on the new node per ADR-0074 §I5. Stored entity's
    visibility is decided by the KG backend (defaults to ``"public"``);
    user-tagged private entries should land via the higher-level
    ``store_fact`` path that knows the calling user.

    Args:
        request: FastAPI request (injected).
        body: Entity creation payload.
        token: Validated bearer token with ``knowledge:write`` scope.
        db: Database session for CF-Access → user_id resolution.

    Returns:
        StoreEntityResponse with the assigned entity identifier.

    Raises:
        HTTPException(401): When the CF Access header is absent.
    """
    get_rate_limiter().check(token)
    user_id = await _require_request_user_id(request, db)
    kg = _get_kg(request)
    ctx = SystemTraceContext.new("knowledge_api", user_id=user_id)

    log.info(
        "gateway_knowledge_store_entity",
        entity=body.entity,
        entity_type=body.entity_type,
        token_name=token.name,
        user_id=str(user_id),
        trace_id=ctx.trace_id,
    )

    fact = Entity(
        name=body.entity,
        entity_type=body.entity_type,
        properties=dict(body.metadata),
    )
    entity_id: str = await kg.store_fact(fact, ctx)
    return StoreEntityResponse(id=entity_id, created=True)


@router.get("/entities/{entity_id}/relationships")
async def get_entity_relationships(
    request: Request,
    entity_id: str,
    token: TokenInfo = Depends(require_scope("knowledge:read")),  # noqa: B008
    db: AsyncSession = Depends(_get_db),  # noqa: B008
) -> list[dict[str, Any]]:
    """Retrieve direct relationships for an entity, scoped by caller (FRE-379).

    Args:
        request: FastAPI request (injected).
        entity_id: Unique entity identifier.
        token: Validated bearer token with ``knowledge:read`` scope.
        db: Database session for CF-Access → user_id resolution.

    Returns:
        List of relationship dicts visible to the calling user.

    Raises:
        HTTPException(401): When the CF Access header is absent.
    """
    get_rate_limiter().check(token)
    user_id = await _require_request_user_id(request, db)
    kg = _get_kg(request)
    ctx = SystemTraceContext.new("knowledge_api", user_id=user_id)

    log.info(
        "gateway_knowledge_get_relationships",
        entity_id=entity_id,
        token_name=token.name,
        user_id=str(user_id),
        trace_id=ctx.trace_id,
    )

    relationships: Sequence[Relationship] = await kg.get_relationships(entity_id, ctx)
    return [r.model_dump() for r in relationships]
