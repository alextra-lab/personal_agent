"""Tests for the gateway knowledge API endpoints (FRE-206 + FRE-379 user scoping).

Uses FastAPI's TestClient with mocked MemoryService backend.

FRE-379: endpoints now require ``Cf-Access-Authenticated-User-Email`` and
thread the resolved user_id into the ``TraceContext`` passed to the KG
backend. Each test attaches the header and patches the resolver so the
endpoint returns a stable mock user identity.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_agent.gateway.app import _KnowledgeGraphAdapter, create_gateway_router
from personal_agent.memory.models import Entity, EntityNode, MemoryQueryResult, Relationship

_TEST_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
_AUTH_HEADERS = {"Cf-Access-Authenticated-User-Email": "tester@example.com"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entity_node(name: str = "Paris", entity_type: str = "Place") -> EntityNode:
    return EntityNode(
        entity_id=name,
        name=name,
        entity_type=entity_type,
        description=f"Test entity: {name}",
        first_seen=datetime(2026, 1, 1),
        last_seen=datetime(2026, 1, 2),
        mention_count=5,
    )


def _build_app_with_kg(kg: Any) -> FastAPI:
    """Build a test app with the gateway router, mock KG, and a no-op DB factory.

    The DB factory yields an :class:`AsyncMock` — the endpoint's only DB use
    is feeding the (patched) user resolver, so we never actually execute SQL.
    """
    app = FastAPI()
    app.include_router(create_gateway_router())
    app.state.knowledge_graph = kg
    app.state.es_client = None

    @asynccontextmanager
    async def _factory() -> Any:
        yield AsyncMock()

    app.state.db_session_factory = _factory
    return app


def _patched_user_resolver(user_id: UUID = _TEST_USER_ID) -> Any:
    """Patch the CF Access → user_id helper in knowledge_api."""
    return patch(
        "personal_agent.gateway.knowledge_api._get_user_with_display_name",
        new_callable=AsyncMock,
        return_value=(user_id, None),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/knowledge/search
# ---------------------------------------------------------------------------


def test_search_returns_entities() -> None:
    """GET /search returns a list of entity dicts, threading user_id into ctx."""
    mock_kg = AsyncMock()
    entity = _make_entity_node("Paris")
    mock_kg.search.return_value = [entity]

    app = _build_app_with_kg(mock_kg)
    with _patched_user_resolver():
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/knowledge/search?q=Paris&limit=5", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert data[0]["name"] == "Paris"
    mock_kg.search.assert_awaited_once()
    # Verify user_id reached the trace context.
    ctx_arg = mock_kg.search.await_args.args[2]
    assert ctx_arg.user_id == _TEST_USER_ID


def test_search_401_without_cf_access_header() -> None:
    """GET /search returns 401 when the CF Access header is absent."""
    mock_kg = AsyncMock()
    app = _build_app_with_kg(mock_kg)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/knowledge/search?q=Paris")
    assert resp.status_code == 401
    mock_kg.search.assert_not_awaited()


def test_search_503_when_no_backend() -> None:
    """GET /search returns 503 when knowledge graph is not available."""
    app = FastAPI()
    app.include_router(create_gateway_router())
    app.state.knowledge_graph = None
    app.state.es_client = None

    @asynccontextmanager
    async def _factory() -> Any:
        yield AsyncMock()

    app.state.db_session_factory = _factory

    with _patched_user_resolver():
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/knowledge/search?q=test", headers=_AUTH_HEADERS)
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# GET /api/v1/knowledge/entities/{entity_id}
# ---------------------------------------------------------------------------


def test_get_entity_found() -> None:
    """GET /entities/{id} returns 200 with entity dict, threading user_id."""
    mock_kg = AsyncMock()
    entity = _make_entity_node("Rome")
    mock_kg.get_entity.return_value = entity

    app = _build_app_with_kg(mock_kg)
    with _patched_user_resolver():
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/knowledge/entities/Rome", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    assert resp.json()["name"] == "Rome"
    # get_entity now receives (entity_id, ctx) — verify ctx.user_id.
    args = mock_kg.get_entity.await_args.args
    assert args[0] == "Rome"
    assert args[1].user_id == _TEST_USER_ID


def test_get_entity_not_found() -> None:
    """GET /entities/{id} returns 404 when entity does not exist or is invisible."""
    mock_kg = AsyncMock()
    mock_kg.get_entity.return_value = None

    app = _build_app_with_kg(mock_kg)
    with _patched_user_resolver():
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/knowledge/entities/UnknownEntity", headers=_AUTH_HEADERS)

    assert resp.status_code == 404


def test_get_entity_401_without_cf_access_header() -> None:
    """GET /entities/{id} returns 401 when CF Access header missing."""
    mock_kg = AsyncMock()
    app = _build_app_with_kg(mock_kg)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/knowledge/entities/Anywhere")
    assert resp.status_code == 401
    mock_kg.get_entity.assert_not_awaited()


# ---------------------------------------------------------------------------
# POST /api/v1/knowledge/entities
# ---------------------------------------------------------------------------


def test_store_entity_returns_201() -> None:
    """POST /entities returns 201 with entity id."""
    mock_kg = AsyncMock()
    mock_kg.store_fact.return_value = "Berlin"

    app = _build_app_with_kg(mock_kg)
    payload = {"entity": "Berlin", "entity_type": "Place", "metadata": {}}
    with _patched_user_resolver():
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.post("/api/v1/knowledge/entities", json=payload, headers=_AUTH_HEADERS)

    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] == "Berlin"
    assert data["created"] is True


def test_store_entity_401_without_cf_access_header() -> None:
    """POST /entities returns 401 when CF Access header missing."""
    mock_kg = AsyncMock()
    app = _build_app_with_kg(mock_kg)
    payload = {"entity": "Berlin", "entity_type": "Place", "metadata": {}}
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/api/v1/knowledge/entities", json=payload)
    assert resp.status_code == 401
    mock_kg.store_fact.assert_not_awaited()


# ---------------------------------------------------------------------------
# GET /api/v1/knowledge/entities/{entity_id}/relationships
# ---------------------------------------------------------------------------


def test_get_relationships_returns_list() -> None:
    """GET /entities/{id}/relationships returns list of relationship dicts."""
    mock_kg = AsyncMock()
    rel = Relationship(
        source_id="Paris",
        target_id="France",
        relationship_type="LOCATED_IN",
        weight=0.9,
    )
    mock_kg.get_relationships.return_value = [rel]

    app = _build_app_with_kg(mock_kg)
    with _patched_user_resolver():
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(
                "/api/v1/knowledge/entities/Paris/relationships", headers=_AUTH_HEADERS
            )

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert data[0]["source_id"] == "Paris"
    assert data[0]["target_id"] == "France"
    # FRE-379: get_relationships now receives (entity_id, ctx).
    args = mock_kg.get_relationships.await_args.args
    assert args[0] == "Paris"
    assert args[1].user_id == _TEST_USER_ID


def test_get_relationships_401_without_cf_access_header() -> None:
    """GET /entities/{id}/relationships returns 401 when CF Access header missing."""
    mock_kg = AsyncMock()
    app = _build_app_with_kg(mock_kg)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/knowledge/entities/Paris/relationships")
    assert resp.status_code == 401
    mock_kg.get_relationships.assert_not_awaited()


# ---------------------------------------------------------------------------
# _KnowledgeGraphAdapter unit tests — FRE-379 verifies user_id flows into MemoryQuery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kg_adapter_search_threads_user_id_into_memory_query() -> None:
    """FRE-379: adapter.search reads ctx.user_id and sets MemoryQuery.user_id."""
    from personal_agent.telemetry.trace import TraceContext

    mock_service = AsyncMock()
    entity = _make_entity_node("Tokyo")
    mock_service.query_memory.return_value = MemoryQueryResult(entities=[entity])

    adapter = _KnowledgeGraphAdapter(mock_service)
    ctx = TraceContext.new_trace(user_id=_TEST_USER_ID)
    results = await adapter.search("tokyo", 10, ctx)

    assert len(results) == 1
    assert results[0].name == "Tokyo"
    # The MemoryQuery passed to query_memory should carry the user_id + authenticated.
    mq = mock_service.query_memory.await_args.args[0]
    assert mq.user_id == _TEST_USER_ID
    assert mq.authenticated is True


@pytest.mark.asyncio
async def test_kg_adapter_get_entity_threads_user_id_into_memory_query() -> None:
    """FRE-379: adapter.get_entity propagates ctx.user_id."""
    from personal_agent.telemetry.trace import TraceContext

    mock_service = AsyncMock()
    entity = _make_entity_node("Tokyo")
    mock_service.query_memory.return_value = MemoryQueryResult(entities=[entity])

    adapter = _KnowledgeGraphAdapter(mock_service)
    ctx = TraceContext.new_trace(user_id=_TEST_USER_ID)

    result = await adapter.get_entity("Tokyo", ctx)
    assert result is not None and result.name == "Tokyo"
    mq = mock_service.query_memory.await_args.args[0]
    assert mq.user_id == _TEST_USER_ID
    assert mq.authenticated is True


@pytest.mark.asyncio
async def test_kg_adapter_get_relationships_threads_user_id() -> None:
    """FRE-379: adapter.get_relationships propagates ctx.user_id."""
    from personal_agent.telemetry.trace import TraceContext

    mock_service = AsyncMock()
    rel = Relationship(source_id="X", target_id="Y", relationship_type="REL", weight=1.0)
    mock_service.query_memory.return_value = MemoryQueryResult(relationships=[rel])

    adapter = _KnowledgeGraphAdapter(mock_service)
    ctx = TraceContext.new_trace(user_id=_TEST_USER_ID)

    result = await adapter.get_relationships("X", ctx)
    assert len(result) == 1
    mq = mock_service.query_memory.await_args.args[0]
    assert mq.user_id == _TEST_USER_ID
    assert mq.authenticated is True


@pytest.mark.asyncio
async def test_kg_adapter_store_fact_calls_create_entity() -> None:
    """_KnowledgeGraphAdapter.store_fact delegates to service.create_entity."""
    from personal_agent.telemetry.trace import TraceContext

    mock_service = AsyncMock()
    mock_service.create_entity.return_value = "Madrid"

    adapter = _KnowledgeGraphAdapter(mock_service)
    fact = Entity(name="Madrid", entity_type="Place")
    ctx = TraceContext.new_trace()

    result = await adapter.store_fact(fact, ctx)
    assert result == "Madrid"
    # ADR-0074 §I5: store_fact threads ctx.trace_id / ctx.session_id as origination.
    mock_service.create_entity.assert_awaited_once_with(
        fact,
        visibility="public",
        originating_trace_id=ctx.trace_id,
        originating_session_id=ctx.session_id,
    )
