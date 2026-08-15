"""Seshat API Gateway — FastAPI router factory.

- :func:`create_gateway_router` — returns an ``APIRouter`` suitable for
  mounting on the main execution service (local dev, ``settings.gateway_mount_local``).

The gateway connects only to storage backends: Neo4j, PostgreSQL, and
Elasticsearch.  These connections are shared with the main service through
``app.state`` (set by the execution service lifespan before mounting the
router).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, Request

from personal_agent.gateway.feedback_api import router as feedback_router
from personal_agent.gateway.knowledge_api import router as knowledge_router
from personal_agent.gateway.observation_api import router as observation_router
from personal_agent.gateway.route_trace_api import router as route_trace_router
from personal_agent.gateway.session_api import config_router
from personal_agent.gateway.session_api import router as session_router
from personal_agent.gateway.sub_agent_capture_api import router as sub_agent_capture_router
from personal_agent.memory.session_digest import SessionDigestView

# ---------------------------------------------------------------------------
# Health router (no auth required)
# ---------------------------------------------------------------------------

_health_router = APIRouter(tags=["health"])


@_health_router.get("/health")
async def gateway_health(request: Request) -> dict[str, Any]:
    """Gateway health check — no authentication required.

    Args:
        request: FastAPI Request (injected automatically).

    Returns:
        Dict with ``status`` and ``components`` sub-keys.
    """
    app_state = request.app.state
    kg_ok = getattr(app_state, "knowledge_graph", None) is not None
    es_ok = getattr(app_state, "es_client", None) is not None
    db_ok = getattr(app_state, "db_session_factory", None) is not None

    return {
        "status": "healthy",
        "components": {
            "neo4j": "connected" if kg_ok else "unavailable",
            "elasticsearch": "connected" if es_ok else "unavailable",
            "database": "connected" if db_ok else "unavailable",
        },
    }


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_gateway_router() -> APIRouter:
    """Build and return the versioned gateway ``APIRouter``.

    The router carries the ``/api/v1`` prefix and includes all sub-routers:
    knowledge, sessions, observations, and health.

    Returns:
        Configured ``APIRouter`` ready for ``app.include_router()``.
    """
    root = APIRouter(prefix="/api/v1")
    root.include_router(knowledge_router)
    root.include_router(session_router)
    root.include_router(config_router)
    root.include_router(observation_router)
    root.include_router(route_trace_router)
    root.include_router(sub_agent_capture_router)
    root.include_router(feedback_router)
    root.include_router(_health_router)
    return root


# ---------------------------------------------------------------------------
# KnowledgeGraphProtocol adapter over MemoryService
# ---------------------------------------------------------------------------


class _KnowledgeGraphAdapter:
    """Wraps MemoryService to satisfy KnowledgeGraphProtocol.

    Full type: :class:`~personal_agent.memory.service.MemoryService` to satisfy
    :class:`~personal_agent.memory.protocols.KnowledgeGraphProtocol`.

    Only the methods required by gateway endpoints are implemented here.
    ``query_memory`` delegates to the full service.

    Args:
        service: Connected ``MemoryService`` instance.
    """

    def __init__(self, service: Any) -> None:
        self._service = service

    async def search(self, query: str, limit: int, ctx: Any) -> list[Any]:
        """Search entities matching the free-text query.

        Delegates to :meth:`~personal_agent.memory.service.MemoryService.get_user_interests`
        and filters by name prefix/substring as a lightweight stand-in until
        a dedicated full-text search method exists. FRE-379: scopes results
        by ``ctx.user_id`` so private KG entries stay per-user while
        public/group entries flow freely (FRE-229 visibility filter).

        Args:
            query: Free-text search string.
            limit: Maximum results.
            ctx: Trace context. ``ctx.user_id`` and ``authenticated=True``
                are threaded into the underlying :class:`MemoryQuery`.

        Returns:
            List of :class:`~personal_agent.memory.models.EntityNode` instances.
        """
        from personal_agent.memory.models import EntityNode, MemoryQuery

        user_id = getattr(ctx, "user_id", None)
        is_authenticated = user_id is not None
        mq = MemoryQuery(
            entity_names=[query],
            limit=limit,
            user_id=user_id,
            authenticated=is_authenticated,
        )
        result = await self._service.query_memory(mq)
        entities: list[EntityNode] = list(result.entities)
        if not entities:
            # Fallback: fetch interests scoped to the caller and filter.
            all_entities: list[EntityNode] = await self._service.get_user_interests(
                limit=200, user_id=user_id, authenticated=is_authenticated
            )
            q_lower = query.lower()
            entities = [
                e
                for e in all_entities
                if q_lower in e.name.lower() or q_lower in (e.description or "").lower()
            ][:limit]
        return list(entities[:limit])

    async def get_entity(self, entity_id: str, ctx: Any) -> Any | None:
        """Retrieve a single entity by name/ID, scoped by caller (FRE-379).

        Args:
            entity_id: Entity name used as identifier in Neo4j.
            ctx: Trace context; ``user_id`` controls visibility filtering.

        Returns:
            :class:`~personal_agent.memory.models.EntityNode` or ``None``.
        """
        from personal_agent.memory.models import MemoryQuery

        user_id = getattr(ctx, "user_id", None)
        mq = MemoryQuery(
            entity_names=[entity_id],
            limit=1,
            user_id=user_id,
            authenticated=user_id is not None,
        )
        result = await self._service.query_memory(mq)
        if result.entities:
            return result.entities[0]
        return None

    async def store_fact(self, fact: Any, ctx: Any) -> str:
        """Persist an entity to the knowledge graph.

        Args:
            fact: :class:`~personal_agent.memory.models.Entity` to store.
            ctx: Trace context. ``trace_id`` and ``session_id`` are written as
                origination on the new node (ADR-0074 §I5). No ``extractor_model``
                — gateway ``store_fact`` is user-provided facts, not extraction.

        Returns:
            Entity identifier string.
        """
        entity_id: str = await self._service.create_entity(
            fact,
            visibility="public",
            originating_trace_id=getattr(ctx, "trace_id", None),
            originating_session_id=getattr(ctx, "session_id", None),
        )
        return entity_id

    async def get_relationships(self, entity_id: str, ctx: Any) -> list[Any]:
        """Retrieve all direct relationships for an entity, scoped by caller (FRE-379).

        Args:
            entity_id: Entity name/ID.
            ctx: Trace context; ``user_id`` controls visibility filtering.

        Returns:
            List of :class:`~personal_agent.memory.models.Relationship` objects.
        """
        from personal_agent.memory.models import MemoryQuery

        user_id = getattr(ctx, "user_id", None)
        mq = MemoryQuery(
            entity_names=[entity_id],
            limit=50,
            user_id=user_id,
            authenticated=user_id is not None,
        )
        result = await self._service.query_memory(mq)
        return list(result.relationships)

    async def query_memory(self, query: Any) -> Any:
        """Delegate to the underlying service's query_memory.

        Args:
            query: :class:`~personal_agent.memory.models.MemoryQuery`.

        Returns:
            :class:`~personal_agent.memory.models.MemoryQueryResult`.
        """
        result: Any = await self._service.query_memory(query)
        return result

    async def get_session_digest_views(
        self, session_ids: Sequence[str], *, trace_id: str | None = None
    ) -> dict[str, SessionDigestView]:
        """Delegate to MemoryService's batch session-digest read (ADR-0124 Phase 1).

        Args:
            session_ids: Postgres session ids for the current page.
            trace_id: Trace identifier for log correlation.

        Returns:
            ``{session_id: SessionDigestView}`` for sessions with a label/digest.
        """
        result: dict[str, SessionDigestView] = await self._service.get_session_digest_views(
            session_ids, trace_id=trace_id
        )
        return result
