"""FRE-659: zero-vector embeddings must never be persisted; missing/zeroed
embeddings are repaired by an idempotent, outage-safe backfill.

When the embedder is unreachable ``generate_embedding`` degrades to a zero
vector. ``create_entity`` must NOT persist it (a zero vector has no meaningful
cosine and silently corrupts the ``entity_embedding`` index), and
``backfill_missing_embeddings`` must re-embed such entities once the embedder
returns — writing only non-zero results, under a guard that never clobbers a
fresher concurrent embedding.

Uses a mocked Neo4j driver (same pattern as
``test_neo4j_origination_properties.py``) — runs in ``make test``.
"""

# ruff: noqa: D103

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from personal_agent.memory.models import Entity
from personal_agent.memory.service import MemoryService

_DIM = 8


def _make_service_with_mock() -> tuple[MemoryService, list[tuple[str, dict[str, Any]]]]:
    """Build a MemoryService with a mock driver that captures every session.run call.

    The mock resolves the FRE-659 backfill read (``ORDER BY e.name`` candidate
    query) to ``_candidate_rows`` and every ``count(*)``-returning write to
    ``_write_filled``; both are settable on the returned service for per-test control.
    """
    service = MemoryService.__new__(MemoryService)
    service.connected = True
    service._candidate_rows = []  # type: ignore[attr-defined]
    service._write_filled = 0  # type: ignore[attr-defined]

    captured: list[tuple[str, dict[str, Any]]] = []

    async def capture_run(cypher: str, **kwargs: object):
        captured.append((cypher, dict(kwargs)))
        result = AsyncMock()
        # Default: no rows (dedup vector query → CREATE_NEW, no interference).
        result.data = AsyncMock(return_value=[])
        # Backfill candidate read → materialized rows.
        if "ORDER BY e.name" in cypher and "RETURN e.name AS name" in cypher:
            result.data = AsyncMock(return_value=list(service._candidate_rows))  # type: ignore[attr-defined]
        # Any count(*) write (backfill UNWIND) → filled count.
        result.single = AsyncMock(
            return_value={
                "filled": service._write_filled,  # type: ignore[attr-defined]
                "entity_id": kwargs.get("name", "x"),
            }
        )
        return result

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.run = capture_run

    mock_driver = AsyncMock()
    mock_driver.session = lambda: mock_session
    service.driver = mock_driver
    return service, captured


# --- Write-path guard (AC-1) -------------------------------------------------


@pytest.mark.asyncio
async def test_create_entity_skips_zero_vector_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    service, captured = _make_service_with_mock()
    monkeypatch.setattr(
        "personal_agent.memory.service.generate_embedding",
        AsyncMock(return_value=[0.0] * _DIM),
    )

    entity_id = await service.create_entity(Entity(name="Acme", entity_type="Org", description="d"))
    assert entity_id == "Acme"

    cypher, kwargs = captured[-1]
    assert "e.embedding = $embedding" not in cypher
    assert "embedding" not in kwargs


@pytest.mark.asyncio
async def test_create_entity_persists_nonzero_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    service, captured = _make_service_with_mock()
    vec = [0.1] * _DIM
    monkeypatch.setattr(
        "personal_agent.memory.service.generate_embedding",
        AsyncMock(return_value=vec),
    )

    await service.create_entity(Entity(name="Acme", entity_type="Org", description="d"))

    cypher, kwargs = captured[-1]
    assert "e.embedding = $embedding" in cypher
    assert kwargs["embedding"] == vec


# --- Backfill (AC-2) ---------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_populates_missing_embedding_when_embedder_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, captured = _make_service_with_mock()
    service._candidate_rows = [{"name": "Acme", "description": "d"}]  # type: ignore[attr-defined]
    service._write_filled = 1  # type: ignore[attr-defined]
    vec = [0.2] * _DIM
    monkeypatch.setattr(
        "personal_agent.memory.service.generate_embeddings_batch",
        AsyncMock(return_value=[vec]),
    )

    filled = await service.backfill_missing_embeddings()
    assert filled == 1

    writes = [c for c in captured if "UNWIND $updates AS u" in c[0]]
    assert writes, "expected a guarded UNWIND write"
    _cypher, kwargs = writes[-1]
    assert kwargs["updates"] == [{"name": "Acme", "embedding": vec}]


@pytest.mark.asyncio
async def test_backfill_skips_when_embedder_still_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, captured = _make_service_with_mock()
    service._candidate_rows = [{"name": "Acme", "description": "d"}]  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "personal_agent.memory.service.generate_embeddings_batch",
        AsyncMock(return_value=[[0.0] * _DIM]),
    )

    assert await service.backfill_missing_embeddings() == 0
    # Idempotent / outage-safe: a second run is still a no-op.
    assert await service.backfill_missing_embeddings() == 0
    assert not [c for c in captured if "UNWIND $updates AS u" in c[0]]


@pytest.mark.asyncio
async def test_backfill_write_is_guarded_against_concurrent_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, captured = _make_service_with_mock()
    service._candidate_rows = [{"name": "Acme", "description": "d"}]  # type: ignore[attr-defined]
    service._write_filled = 1  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "personal_agent.memory.service.generate_embeddings_batch",
        AsyncMock(return_value=[[0.3] * _DIM]),
    )

    await service.backfill_missing_embeddings()

    writes = [c for c in captured if "UNWIND $updates AS u" in c[0]]
    assert writes
    cypher = writes[-1][0]
    assert "e.embedding IS NULL OR none(x IN e.embedding WHERE x <> 0.0)" in cypher
