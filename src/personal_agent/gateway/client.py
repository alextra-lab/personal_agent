"""HTTP client for the Seshat API Gateway — remote KnowledgeGraphProtocol.

:class:`GatewayKnowledgeGraphClient` implements
:class:`~personal_agent.memory.protocols.KnowledgeGraphProtocol` by calling
the gateway over HTTP using ``httpx``.  It is used by remote execution
profiles (cloud, external agents) that cannot access Neo4j directly.

Usage::

    client = GatewayKnowledgeGraphClient(
        base_url="https://gateway.example.com",
        token="my-bearer-token",
    )
    entities = await client.search("Paris", limit=5, ctx=ctx)
    await client.aclose()

Or as an async context manager::

    async with GatewayKnowledgeGraphClient(...) as client:
        entity = await client.get_entity("Paris")
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog

from personal_agent.memory.models import (
    Entity,
    EntityNode,
    MemoryQuery,
    MemoryQueryResult,
    Relationship,
)
from personal_agent.telemetry.trace import TraceContext

log = structlog.get_logger(__name__)


class GatewayKnowledgeGraphClient:
    """KnowledgeGraphProtocol implementation that calls the Gateway HTTP API.

    Used by remote execution profiles (cloud, external agents) that cannot
    access Neo4j directly.  All methods convert between Pydantic models and
    the JSON serialisation format used by the gateway endpoints.

    Args:
        base_url: Base URL of the gateway (e.g. ``"http://localhost:9000"``).
        token: Bearer token for authentication.
        timeout: Request timeout in seconds (default 30).
    """

    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        """Initialise the HTTP client.

        Args:
            base_url: Gateway base URL.
            token: Bearer token.
            timeout: Per-request timeout in seconds.
        """
        try:
            import httpx
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise ImportError(
                "httpx is required for GatewayKnowledgeGraphClient. "
                "Install it with: pip install httpx"
            ) from exc

        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        self._base_url = base_url

    async def search(
        self,
        query: str,
        limit: int,
        ctx: TraceContext,
    ) -> Sequence[EntityNode]:
        """Search the knowledge graph by free-text query.

        Args:
            query: Free-text search string.
            limit: Maximum number of results.
            ctx: Trace context (passed as ``X-Trace-Id`` header).

        Returns:
            Sequence of ``EntityNode`` results.

        Raises:
            httpx.HTTPStatusError: On non-2xx responses.
        """
        log.debug("gateway_client_search", query=query, limit=limit, trace_id=ctx.trace_id)
        resp = await self._client.get(
            "/api/v1/knowledge/search",
            params={"q": query, "limit": limit},
            headers={"X-Trace-Id": ctx.trace_id},
        )
        resp.raise_for_status()
        return [EntityNode(**item) for item in resp.json()]

    async def get_entity(self, entity_id: str) -> EntityNode | None:
        """Retrieve a single entity by its identifier.

        Args:
            entity_id: Unique entity identifier.

        Returns:
            ``EntityNode`` if found, ``None`` on 404.

        Raises:
            httpx.HTTPStatusError: On non-2xx/404 responses.
        """
        log.debug("gateway_client_get_entity", entity_id=entity_id)
        resp = await self._client.get(f"/api/v1/knowledge/entities/{entity_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return EntityNode(**resp.json())

    async def store_fact(self, fact: Entity, ctx: TraceContext) -> str:
        """Persist an entity to the knowledge graph via the gateway.

        Args:
            fact: Entity to store.
            ctx: Trace context.

        Returns:
            Entity identifier returned by the gateway.

        Raises:
            httpx.HTTPStatusError: On non-2xx responses.
        """
        log.debug(
            "gateway_client_store_fact",
            entity=fact.name,
            entity_type=fact.entity_type,
            trace_id=ctx.trace_id,
        )
        payload: dict[str, Any] = {
            "entity": fact.name,
            "entity_type": fact.entity_type,
            "metadata": fact.properties,
        }
        resp = await self._client.post(
            "/api/v1/knowledge/entities",
            json=payload,
            headers={"X-Trace-Id": ctx.trace_id},
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return str(data.get("id", fact.name))

    async def get_relationships(self, entity_id: str) -> Sequence[Relationship]:
        """Retrieve all direct relationships for an entity.

        Args:
            entity_id: Unique entity identifier.

        Returns:
            Sequence of ``Relationship`` objects.

        Raises:
            httpx.HTTPStatusError: On non-2xx responses.
        """
        log.debug("gateway_client_get_relationships", entity_id=entity_id)
        resp = await self._client.get(f"/api/v1/knowledge/entities/{entity_id}/relationships")
        resp.raise_for_status()
        return [Relationship(**r) for r in resp.json()]

    async def query_memory(self, query: MemoryQuery) -> MemoryQueryResult:
        """Execute a structured memory query.

        This method is not exposed via the gateway REST API (it uses the
        high-level ``search`` endpoint internally) and returns an empty result.
        Override this in a future version when a dedicated endpoint exists.

        Args:
            query: Structured memory query.

        Returns:
            Empty ``MemoryQueryResult`` (stub).
        """
        log.warning(
            "gateway_client_query_memory_not_implemented",
            note="Use search() for free-text queries over the gateway",
        )
        return MemoryQueryResult()

    async def aclose(self) -> None:
        """Close the underlying httpx client.

        Returns:
            None
        """
        await self._client.aclose()

    async def __aenter__(self) -> "GatewayKnowledgeGraphClient":
        """Support async context manager usage.

        Returns:
            Self.
        """
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Close the client on context manager exit.

        Args:
            *args: Exception info (ignored).
        """
        await self.aclose()
