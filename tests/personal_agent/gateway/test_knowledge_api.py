"""Tests for the gateway knowledge API endpoints (FRE-206).

Uses FastAPI's TestClient with mocked MemoryService backend.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_agent.gateway.app import _KnowledgeGraphAdapter, create_gateway_router
from personal_agent.gateway.auth import _DEV_TOKEN, TokenInfo
from personal_agent.memory.models import Entity, EntityNode, MemoryQueryResult, Relationship


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


def _build_test_app(kg: Any | None = None) -> tuple[FastAPI, Any]:
    """Build a minimal FastAPI app with the gateway router mounted."""
    app = FastAPI()
    app.include_router(create_gateway_router())

    # Inject a mock knowledge graph via app state
    mock_kg = kg or MagicMock()
    app.state.knowledge_graph = mock_kg
    app.state.db_session_factory = None
    app.state.es_client = None

    return app, mock_kg


# ---------------------------------------------------------------------------
# GET /api/v1/knowledge/search
# ---------------------------------------------------------------------------


def test_search_returns_entities() -> None:
    """GET /search returns a list of entity dicts."""
    mock_kg = AsyncMock()
    entity = _make_entity_node("Paris")
    mock_kg.search.return_value = [entity]

    app = FastAPI()
    app.include_router(create_gateway_router())
    app.state.knowledge_graph = mock_kg
    app.state.db_session_factory = None
    app.state.es_client = None

    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/api/v1/knowledge/search?q=Paris&limit=5")

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert data[0]["name"] == "Paris"
    mock_kg.search.assert_awaited_once()


def test_search_503_when_no_backend() -> None:
    """GET /search returns 503 when knowledge graph is not available."""
    app = FastAPI()
    app.include_router(create_gateway_router())
    app.state.knowledge_graph = None
    app.state.db_session_factory = None
    app.state.es_client = None

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/knowledge/search?q=test")

    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# GET /api/v1/knowledge/entities/{entity_id}
# ---------------------------------------------------------------------------


def test_get_entity_found() -> None:
    """GET /entities/{id} returns 200 with entity dict."""
    mock_kg = AsyncMock()
    entity = _make_entity_node("Rome")
    mock_kg.get_entity.return_value = entity

    app = FastAPI()
    app.include_router(create_gateway_router())
    app.state.knowledge_graph = mock_kg
    app.state.db_session_factory = None
    app.state.es_client = None

    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/api/v1/knowledge/entities/Rome")

    assert resp.status_code == 200
    assert resp.json()["name"] == "Rome"


def test_get_entity_not_found() -> None:
    """GET /entities/{id} returns 404 when entity does not exist."""
    mock_kg = AsyncMock()
    mock_kg.get_entity.return_value = None

    app = FastAPI()
    app.include_router(create_gateway_router())
    app.state.knowledge_graph = mock_kg
    app.state.db_session_factory = None
    app.state.es_client = None

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/knowledge/entities/UnknownEntity")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/knowledge/entities
# ---------------------------------------------------------------------------


def test_store_entity_returns_201() -> None:
    """POST /entities returns 201 with entity id."""
    mock_kg = AsyncMock()
    mock_kg.store_fact.return_value = "Berlin"

    app = FastAPI()
    app.include_router(create_gateway_router())
    app.state.knowledge_graph = mock_kg
    app.state.db_session_factory = None
    app.state.es_client = None

    payload = {"entity": "Berlin", "entity_type": "Place", "metadata": {}}
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post("/api/v1/knowledge/entities", json=payload)

    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] == "Berlin"
    assert data["created"] is True


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

    app = FastAPI()
    app.include_router(create_gateway_router())
    app.state.knowledge_graph = mock_kg
    app.state.db_session_factory = None
    app.state.es_client = None

    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/api/v1/knowledge/entities/Paris/relationships")

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert data[0]["source_id"] == "Paris"
    assert data[0]["target_id"] == "France"


# ---------------------------------------------------------------------------
# KnowledgeGraphAdapter unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kg_adapter_search_uses_query_memory() -> None:
    """_KnowledgeGraphAdapter.search uses query_memory and filters by name."""
    from personal_agent.memory.models import MemoryQuery
    from personal_agent.telemetry.trace import TraceContext

    mock_service = AsyncMock()
    entity = _make_entity_node("Tokyo")
    mock_service.query_memory.return_value = MemoryQueryResult(entities=[entity])

    adapter = _KnowledgeGraphAdapter(mock_service)
    ctx = TraceContext.new_trace()
    results = await adapter.search("tokyo", 10, ctx)

    assert len(results) == 1
    assert results[0].name == "Tokyo"


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
    mock_service.create_entity.assert_awaited_once_with(fact)
