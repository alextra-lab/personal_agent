"""Seshat API Gateway — FastAPI application factory.

Two entry points:

- :func:`create_gateway_router` — returns an ``APIRouter`` suitable for
  mounting on the main execution service (local dev, ``settings.gateway_mount_local``).
- :func:`create_gateway_app` — returns a standalone ``FastAPI`` instance with
  a minimal lifespan (no LLM client, no orchestrator, no brainstem scheduler).

The gateway connects only to storage backends: Neo4j, PostgreSQL, and
Elasticsearch.  In local mode these connections are shared with the main
service through ``app.state`` (set by the execution service lifespan before
mounting the router).

Example (standalone production)::

    uvicorn personal_agent.gateway.app:gateway_app --port 9001
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from fastapi import APIRouter, FastAPI, Request

from personal_agent.config.settings import get_settings
from personal_agent.gateway.chat_api import router as chat_router
from personal_agent.gateway.knowledge_api import router as knowledge_router
from personal_agent.gateway.observation_api import router as observation_router
from personal_agent.gateway.session_api import router as session_router
from personal_agent.telemetry import get_logger
from personal_agent.transport.agui.endpoint import router as transport_router

log = get_logger(__name__)


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
# Router factory (shared between local-mount and standalone modes)
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
    root.include_router(observation_router)
    root.include_router(_health_router)
    return root


# ---------------------------------------------------------------------------
# Standalone lifespan (minimal — storage backends only)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _gateway_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Minimal lifespan for standalone gateway deployment.

    Connects to Neo4j, PostgreSQL, and Elasticsearch.
    Does NOT start the orchestrator, LLM client, brainstem scheduler, or MCP
    gateway — those belong to the execution service only.

    Args:
        app: FastAPI application instance.

    Yields:
        Control to FastAPI (endpoints active while inside the context).
    """
    settings = get_settings()
    log.info("gateway_starting_standalone")

    # -----------------------------------------------------------------------
    # PostgreSQL — session factory
    # -----------------------------------------------------------------------
    from personal_agent.service.database import AsyncSessionLocal, init_db

    await init_db()
    app.state.db_session_factory = AsyncSessionLocal
    log.info("gateway_database_initialized")

    # -----------------------------------------------------------------------
    # Elasticsearch — observation queries
    # -----------------------------------------------------------------------
    app.state.es_client = None
    try:
        from personal_agent.telemetry.es_handler import ElasticsearchHandler

        es_handler = ElasticsearchHandler(settings.elasticsearch_url)
        if await es_handler.connect() and es_handler.es_logger.client is not None:
            app.state.es_client = es_handler.es_logger.client
            log.info("gateway_elasticsearch_connected")
        else:
            log.warning("gateway_elasticsearch_unavailable")
    except Exception as exc:
        log.warning("gateway_elasticsearch_connect_failed", error=str(exc))

    # -----------------------------------------------------------------------
    # Neo4j — knowledge graph
    # -----------------------------------------------------------------------
    app.state.knowledge_graph = None
    if settings.enable_memory_graph:
        try:
            from personal_agent.memory.service import MemoryService

            memory_service = MemoryService()
            if await memory_service.connect():
                app.state.knowledge_graph = _KnowledgeGraphAdapter(memory_service)
                log.info("gateway_neo4j_connected")
            else:
                log.warning("gateway_neo4j_unavailable")
        except Exception as exc:
            log.warning("gateway_neo4j_connect_failed", error=str(exc))

    log.info("gateway_ready_standalone")
    yield

    # -----------------------------------------------------------------------
    # Shutdown
    # -----------------------------------------------------------------------
    log.info("gateway_shutting_down")
    if app.state.knowledge_graph is not None:
        try:
            await app.state.knowledge_graph._service.disconnect()
        except Exception:
            pass
    if app.state.es_client is not None:
        try:
            await app.state.es_client.close()
        except Exception:
            pass
    log.info("gateway_stopped")


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
        a dedicated full-text search method exists.

        Args:
            query: Free-text search string.
            limit: Maximum results.
            ctx: Trace context (unused here; passed for interface compatibility).

        Returns:
            List of :class:`~personal_agent.memory.models.EntityNode` instances.
        """
        from personal_agent.memory.models import EntityNode, MemoryQuery

        mq = MemoryQuery(entity_names=[query], limit=limit)
        result = await self._service.query_memory(mq)
        entities: list[EntityNode] = list(result.entities)
        if not entities:
            # Fallback: fetch interests and filter by name
            all_entities: list[EntityNode] = await self._service.get_user_interests(limit=200)
            q_lower = query.lower()
            entities = [
                e
                for e in all_entities
                if q_lower in e.name.lower() or q_lower in (e.description or "").lower()
            ][:limit]
        return list(entities[:limit])

    async def get_entity(self, entity_id: str) -> Any | None:
        """Retrieve a single entity by name/ID.

        Args:
            entity_id: Entity name used as identifier in Neo4j.

        Returns:
            :class:`~personal_agent.memory.models.EntityNode` or ``None``.
        """
        from personal_agent.memory.models import MemoryQuery

        mq = MemoryQuery(entity_names=[entity_id], limit=1)
        result = await self._service.query_memory(mq)
        if result.entities:
            return result.entities[0]
        return None

    async def store_fact(self, fact: Any, ctx: Any) -> str:
        """Persist an entity to the knowledge graph.

        Args:
            fact: :class:`~personal_agent.memory.models.Entity` to store.
            ctx: Trace context.

        Returns:
            Entity identifier string.
        """
        entity_id: str = await self._service.create_entity(fact)
        return entity_id

    async def get_relationships(self, entity_id: str) -> list[Any]:
        """Retrieve all direct relationships for an entity.

        Args:
            entity_id: Entity name/ID.

        Returns:
            List of :class:`~personal_agent.memory.models.Relationship` objects.
        """
        from personal_agent.memory.models import MemoryQuery

        mq = MemoryQuery(entity_names=[entity_id], limit=50)
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


# ---------------------------------------------------------------------------
# Standalone app factory
# ---------------------------------------------------------------------------


def create_gateway_app() -> FastAPI:
    """Create a standalone FastAPI application for the Seshat API Gateway.

    Suitable for a dedicated uvicorn process (port 9001 or behind a reverse
    proxy).  The lifespan connects only to storage backends — no LLM client,
    no orchestrator, no brainstem.

    Returns:
        Configured :class:`fastapi.FastAPI` instance.
    """
    from personal_agent.gateway.errors import add_error_handlers

    app = FastAPI(
        title="Seshat API Gateway",
        description="Versioned REST API for knowledge graph, sessions, and observations",
        version="1.0.0",
        lifespan=_gateway_lifespan,
    )
    add_error_handlers(app)
    app.include_router(create_gateway_router())  # /api/v1/* — storage APIs
    app.include_router(chat_router)              # /chat   — Anthropic streaming
    app.include_router(transport_router)          # /stream/* — AG-UI SSE
    return app


# Standalone ASGI entry-point for uvicorn
gateway_app = create_gateway_app()
