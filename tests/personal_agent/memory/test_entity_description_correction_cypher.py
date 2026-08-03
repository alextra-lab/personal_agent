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
    # FRE-711 strict '>' confidence arm (unsignaled writes still need strictly-higher).
    assert "$description_confidence > coalesce" in cypher
    # FRE-725 equal-confidence signal arm: explicit-kind + '>=' + enrichment non-shrinking guard.
    assert "$description_update_kind IN $explicit_description_update_kinds" in cypher
    assert "$description_confidence >= coalesce" in cypher
    assert "size($description) >= size(_old_desc)" in cypher
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
    # FRE-725: the equal-confidence enrichment/correction signal + its explicit vocabulary.
    assert merge_params["description_update_kind"] == "new"  # default when unset
    assert set(merge_params["explicit_description_update_kinds"]) == {"enrichment", "correction"}


@pytest.mark.asyncio
async def test_off_vocabulary_kind_is_normalized_server_side() -> None:
    # FRE-725: the signal is write-authorizing, so the service (not only the extractor)
    # must coerce an off-vocabulary/None kind to "new" before it reaches Cypher — a
    # direct caller can never inject an unknown kind that authorizes an equal-conf write.
    service, captured = _make_service_with_mock()
    entity = Entity(name="Neo4j", entity_type="Technology", description="A graph database")
    await _run(service, entity, description_update_kind="bogus")

    merge_params = next(p for c, p in captured if "HAD_DESCRIPTION" in c)
    assert merge_params["description_update_kind"] == "new"

    service2, captured2 = _make_service_with_mock()
    await _run(service2, entity, description_update_kind="enrichment")
    merge_params2 = next(p for c, p in captured2 if "HAD_DESCRIPTION" in c)
    assert merge_params2["description_update_kind"] == "enrichment"


@pytest.mark.asyncio
async def test_gate_blocks_self_referential_overwrite_of_clean_description() -> None:
    """FRE-1115: the emitted gate refuses a framed description over a clean one.

    Measured on the live graph, 17 of 71 archived overwrites replaced a description of
    the subject with one of the discussion. The FRE-725 equal-confidence enrichment arm
    admits them because discussion-framing ("X discussed as Y") is *longer* than the
    definition it replaces, so the defect's own verbosity satisfies the anti-shrink
    guard. The gate therefore has to test the framing directly.
    """
    service, captured = _make_service_with_mock()
    entity = Entity(
        name="Clafoutis",
        entity_type="MethodOrConcept",
        description="A French baked dessert discussed as a cherry dish in the memory search context",
    )
    await _run(service, entity, description_confidence=0.8, description_update_kind="enrichment")

    cypher = " ".join(c for c, _ in captured)
    params = captured[-1][1]
    assert "$new_is_self_referential" in cypher, "the new-side predicate must reach the gate"
    assert "$self_referential_pattern" in cypher, (
        "the old side must be tested with the same pattern"
    )
    assert params["new_is_self_referential"] is True
    # The guard belongs to _do_correct only — filling an empty description stays open.
    correct_clause = cypher.split("AS _do_correct")[0]
    assert "$new_is_self_referential" in correct_clause


@pytest.mark.asyncio
async def test_clean_description_is_not_flagged_self_referential() -> None:
    """A real definition is not caught by the framing predicate."""
    service, captured = _make_service_with_mock()
    entity = Entity(
        name="Clafoutis",
        entity_type="MethodOrConcept",
        description="A French baked dessert of cherries in a custard-like batter",
    )
    await _run(service, entity, description_confidence=0.8, description_update_kind="enrichment")

    assert captured[-1][1]["new_is_self_referential"] is False


@pytest.mark.asyncio
async def test_fill_of_empty_description_is_not_blocked_by_the_framing_guard() -> None:
    """_do_fill stays unguarded — a framed description still beats no description."""
    service, captured = _make_service_with_mock()
    entity = Entity(name="Clafoutis", entity_type="MethodOrConcept", description="was discussed")
    await _run(service, entity, description_confidence=0.8)

    cypher = " ".join(c for c, _ in captured)
    fill_clause = cypher.split("AS _do_fill")[0].split("_do_correct")[-1]
    assert "$new_is_self_referential" not in fill_clause
