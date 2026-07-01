"""Mocked-driver unit tests for the FRE-711 living-description Cypher.

FRE-711 retires the Entity-description first-write-wins: the description becomes a
correctable, confidence + eval-gated value with superseded history, in ONE atomic
Cypher statement. These lock the emitted Cypher shape (gate expressions, the
HAD_DESCRIPTION archive, strict '>' confidence, the eval gate, proposed_name) and
the new params, without a live Neo4j. Behavioural proof is in the integration file.
"""

# ruff: noqa: D103

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.memory.models import Entity
from personal_agent.memory.service import MemoryService


def _make_service_with_mock() -> tuple[MemoryService, list[tuple[str, dict]]]:
    service = MemoryService.__new__(MemoryService)
    service.connected = True
    service._query_feedback_by_key = {}

    captured: list[tuple[str, dict]] = []
    result = AsyncMock()
    result.single = AsyncMock(return_value={"entity_id": "Neo4j"})

    async def capture_run(cypher: str, **kwargs: object) -> AsyncMock:
        captured.append((cypher, dict(kwargs)))
        return result

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(side_effect=capture_run)
    service.driver = MagicMock()
    service.driver.session = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    return service, captured


async def _run(service: MemoryService, entity: Entity, **kwargs: object) -> None:
    # Zero embedding → dedup path skipped → the MERGE runs and is captured.
    with patch(
        "personal_agent.memory.service.generate_embedding",
        new=AsyncMock(return_value=[0.0, 0.0]),
    ):
        await service.create_entity(entity, **kwargs)


@pytest.mark.asyncio
async def test_description_uses_gated_correction_not_first_write_freeze() -> None:
    service, captured = _make_service_with_mock()
    entity = Entity(name="Neo4j", entity_type="Technology", description="A graph database")
    await _run(service, entity, description_confidence=0.8, eval_mode=False)

    cypher = " ".join(c for c, _ in captured)
    # The blanket first-write-wins description freeze is gone.
    assert "e.description = CASE WHEN e.description IS NULL OR e.description = ''" not in cypher
    # Superseded history archive.
    assert "HAD_DESCRIPTION" in cypher and "EntityDescriptionVersion" in cypher
    # Strict '>' confidence gate (not >=), and the proposed surface name recorded.
    assert "$description_confidence >" in cypher
    assert ">=" not in cypher.split("$description_confidence")[1][:3]
    assert "proposed_name" in cypher


@pytest.mark.asyncio
async def test_entity_type_and_properties_remain_first_write_wins() -> None:
    service, captured = _make_service_with_mock()
    entity = Entity(name="Neo4j", entity_type="Technology", description="A graph database")
    await _run(service, entity)

    cypher = " ".join(c for c, _ in captured)
    assert "e.entity_type = CASE WHEN e.entity_type IS NULL OR e.entity_type = ''" in cypher
    assert "e.properties = CASE WHEN e.properties IS NULL OR e.properties = '{}'" in cypher


@pytest.mark.asyncio
async def test_new_params_are_bound() -> None:
    service, captured = _make_service_with_mock()
    entity = Entity(name="Neo4j", entity_type="Technology", description="A graph database")
    await _run(
        service,
        entity,
        description_confidence=0.9,
        eval_mode=True,
        originating_trace_id="trace-xyz",
    )

    # The MERGE call (the one carrying the description gate) binds the new params.
    merge_params = next(p for c, p in captured if "HAD_DESCRIPTION" in c)
    assert merge_params["description_confidence"] == 0.9
    assert merge_params["eval_mode"] is True
    assert merge_params["proposed_name"] == "Neo4j"
