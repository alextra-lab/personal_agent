"""Knowledge graph REST endpoints for the Seshat API Gateway.

Exposes the Neo4j knowledge graph over HTTP under ``/knowledge/*``.  All
endpoints require the ``knowledge:read`` or ``knowledge:write`` scope.

The router delegates to whatever ``KnowledgeGraphProtocol`` implementation is
attached to ``request.app.state.knowledge_graph``.  In local dev the
``MemoryService`` is wired directly; in remote deployments a
``GatewayKnowledgeGraphClient`` can be used instead.
"""

from collections.abc import Sequence
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from personal_agent.gateway.auth import TokenInfo, require_scope
from personal_agent.gateway.errors import not_found, service_unavailable
from personal_agent.gateway.rate_limiting import get_rate_limiter
from personal_agent.memory.models import Entity, EntityNode, Relationship
from personal_agent.telemetry.trace import TraceContext

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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/search")
async def search_knowledge(
    request: Request,
    q: str,
    limit: int = 10,
    token: TokenInfo = Depends(require_scope("knowledge:read")),  # noqa: B008
) -> list[dict[str, Any]]:
    """Search the knowledge graph by free-text query.

    Args:
        request: FastAPI request (injected).
        q: Free-text search string.
        limit: Maximum number of results (1–100, default 10).
        token: Validated bearer token with ``knowledge:read`` scope.

    Returns:
        List of entity result dicts.
    """
    get_rate_limiter().check(token)
    kg = _get_kg(request)
    ctx = TraceContext.new_trace()

    log.info("gateway_knowledge_search", query=q, limit=limit, token_name=token.name)

    results: Sequence[EntityNode] = await kg.search(q, limit, ctx)
    return [r.model_dump() for r in results]


@router.get("/entities/{entity_id}")
async def get_entity(
    request: Request,
    entity_id: str,
    token: TokenInfo = Depends(require_scope("knowledge:read")),  # noqa: B008
) -> dict[str, Any]:
    """Retrieve a single entity by its identifier.

    Args:
        request: FastAPI request (injected).
        entity_id: Unique entity identifier.
        token: Validated bearer token with ``knowledge:read`` scope.

    Returns:
        Entity dict.

    Raises:
        HTTPException(404): When the entity is not found.
    """
    get_rate_limiter().check(token)
    kg = _get_kg(request)

    log.info("gateway_knowledge_get_entity", entity_id=entity_id, token_name=token.name)

    entity: EntityNode | None = await kg.get_entity(entity_id)
    if entity is None:
        raise not_found("entity")
    return entity.model_dump()


@router.post("/entities", status_code=201)
async def store_entity(
    request: Request,
    body: StoreEntityRequest,
    token: TokenInfo = Depends(require_scope("knowledge:write")),  # noqa: B008
) -> StoreEntityResponse:
    """Persist a new entity to the knowledge graph.

    Args:
        request: FastAPI request (injected).
        body: Entity creation payload.
        token: Validated bearer token with ``knowledge:write`` scope.

    Returns:
        StoreEntityResponse with the assigned entity identifier.
    """
    get_rate_limiter().check(token)
    kg = _get_kg(request)
    ctx = TraceContext.new_trace()

    log.info(
        "gateway_knowledge_store_entity",
        entity=body.entity,
        entity_type=body.entity_type,
        token_name=token.name,
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
) -> list[dict[str, Any]]:
    """Retrieve all direct relationships for an entity.

    Args:
        request: FastAPI request (injected).
        entity_id: Unique entity identifier.
        token: Validated bearer token with ``knowledge:read`` scope.

    Returns:
        List of relationship dicts.
    """
    get_rate_limiter().check(token)
    kg = _get_kg(request)

    log.info(
        "gateway_knowledge_get_relationships",
        entity_id=entity_id,
        token_name=token.name,
    )

    relationships: Sequence[Relationship] = await kg.get_relationships(entity_id)
    return [r.model_dump() for r in relationships]
