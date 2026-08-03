"""FRE-1115: the consolidation write path must not mint description-less entities.

``create_conversation`` MERGEs a bare ``:Entity`` for every name in ``key_entities``
without a description. ``create_entity`` then runs and dedup may rewrite the write to a
*different* canonical name, so the description lands on the canonical node and the bare
node under the extractor's raw name is orphaned empty forever. Measured on the live
graph: 1,404 of 7,543 entities (18.6%) carry no description, every one of them with
``entity_id IS NULL`` (so ``create_entity`` never wrote it), and a per-node join found a
same-trace ``entity_deduplicated`` event naming the exact orphan for 60 of 60 sampled.

The fix resolves canonical names *first*, then writes the Turn against them. These tests
lock that ordering and the consequences Codex's plan review identified: relationship
endpoints must be translated too (2,115 live edges hang off orphan nodes and would
otherwise be silently dropped), several raw names can converge on one canonical, and a
failed entity write must not fall back to the raw name — that recreates the bug.
"""

# ruff: noqa: D103

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.captains_log.capture import TaskCapture
from personal_agent.second_brain.consolidator import SecondBrainConsolidator

_TURN_TS = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def _make_capture() -> TaskCapture:
    return TaskCapture(
        trace_id=str(uuid.uuid4()),
        timestamp=_TURN_TS,
        user_message="Tell me about mathematics and blueberries.",
        assistant_response="Here you go.",
        session_id=str(uuid.uuid4()),
        tools_used=[],
        duration_ms=100,
        outcome="completed",
        user_id=uuid.uuid4(),
        eval_mode=False,
    )


def _entity(name: str, description: str = "A real description.") -> dict[str, Any]:
    return {
        "name": name,
        "type": "DomainOrTopic",
        "class": "World",
        "description": description,
        "output_kind": "knowledge",
        "description_update_kind": "new",
    }


def _extraction(
    entities: list[dict[str, Any]], relationships: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "entities": entities,
        "relationships": relationships or [],
        "entity_names": [e.get("name", "") for e in entities if e.get("name")],
        "summary": "a turn",
        "stances": [],
        "claims": [],
    }


@pytest.fixture
def memory_service() -> MagicMock:
    svc = MagicMock()
    # create_conversation returns bool in the real service; the mock must too, so the
    # consolidator's result check is exercised rather than masked (FRE-1115).
    svc.create_conversation = AsyncMock(return_value=True)
    svc.create_entity = AsyncMock(side_effect=lambda entity, **kw: entity.name)
    svc.create_relationship = AsyncMock(return_value="rel-1")
    svc.fetch_turn_discusses_relationship_element_ids = AsyncMock(return_value=[])
    svc.assert_stance = AsyncMock(return_value=True)
    svc.assert_claim = AsyncMock(return_value="claim-id-1")
    return svc


@pytest.fixture
def consolidator(memory_service: MagicMock) -> SecondBrainConsolidator:
    return SecondBrainConsolidator(memory_service=memory_service)


def _patches(extraction: dict[str, Any]):
    return (
        patch(
            "personal_agent.second_brain.consolidator.extract_entities_and_relationships",
            new_callable=AsyncMock,
            return_value=extraction,
        ),
        patch(
            "personal_agent.second_brain.consolidator.previous_attempt_count",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch(
            "personal_agent.second_brain.consolidator.record_consolidation_attempt",
            new_callable=AsyncMock,
        ),
        patch(
            "personal_agent.second_brain.consolidator.get_default_sysgraph_repo",
            return_value=None,
        ),
    )


async def _run(consolidator: SecondBrainConsolidator, extraction: dict[str, Any]) -> dict[str, Any]:
    p1, p2, p3, p4 = _patches(extraction)
    with p1, p2, p3, p4:
        return await consolidator._process_capture(_make_capture())


def _turn_of(memory_service: MagicMock):
    return memory_service.create_conversation.await_args.args[0]


@pytest.mark.asyncio
async def test_turn_records_the_canonical_name_not_the_orphaned_raw_name(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    """A dedup-renamed entity leaves no bare node under its raw name (AC-3)."""
    memory_service.create_entity = AsyncMock(return_value="Predictive Processing")
    result = await _run(consolidator, _extraction([_entity("predictive processing")]))

    assert _turn_of(memory_service).key_entities == ["Predictive Processing"]
    assert "predictive processing" not in _turn_of(memory_service).key_entities
    assert result["entities_created"] == 1


@pytest.mark.asyncio
async def test_entities_are_written_before_the_turn(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    """Canonical names cannot be known unless create_entity runs first."""
    order: list[str] = []
    memory_service.create_entity = AsyncMock(
        side_effect=lambda entity, **kw: (order.append("entity"), entity.name)[1]
    )
    memory_service.create_conversation = AsyncMock(
        side_effect=lambda *a, **kw: (order.append("turn"), True)[1]
    )
    await _run(consolidator, _extraction([_entity("Neo4j")]))

    assert order == ["entity", "turn"]


@pytest.mark.asyncio
async def test_relationship_endpoints_are_translated_to_canonical_names(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    """Endpoints must follow the rename or the edge is silently dropped (AC-3b)."""
    renames = {"predictive processing": "Predictive Processing", "neo4j": "Neo4j"}
    memory_service.create_entity = AsyncMock(
        side_effect=lambda entity, **kw: renames.get(entity.name, entity.name)
    )
    extraction = _extraction(
        [_entity("predictive processing"), _entity("neo4j")],
        [{"source": "predictive processing", "target": "neo4j", "type": "RELATED_TO"}],
    )
    result = await _run(consolidator, extraction)

    assert result["relationships_created"] == 1
    rel = memory_service.create_relationship.await_args.args[0]
    assert rel.source_id == "Predictive Processing"
    assert rel.target_id == "Neo4j"


@pytest.mark.asyncio
async def test_several_raw_names_converging_on_one_canonical_are_deduplicated(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    """key_entities must not repeat a canonical name once per raw spelling."""
    memory_service.create_entity = AsyncMock(return_value="Neo4j")
    extraction = _extraction([_entity("neo4j"), _entity("Neo4J"), _entity("neo 4j")])
    await _run(consolidator, extraction)

    assert _turn_of(memory_service).key_entities == ["Neo4j"]


# Two tests were removed here by the FRE-1115 code review, both because they asserted
# behaviour that loses graph structure:
#
#   test_failed_entity_write_does_not_fall_back_to_the_raw_name — asserted key_entities
#   was emptied when create_entity failed. That costs the Turn its only DISCUSSES edge,
#   permanently, and nothing re-derives it. Superseded by
#   TestReorderDoesNotLoseGraphEdges::test_failed_entity_write_keeps_the_turns_discusses_edge.
#
#   test_relationship_touching_an_unresolved_endpoint_is_skipped — asserted an endpoint
#   absent from this turn's canonical map is dropped. The extractor is not required to
#   repeat an endpoint in entities[], so that silently deleted every cross-turn edge.
#   Superseded by TestReorderDoesNotLoseGraphEdges::
#   test_endpoint_from_an_earlier_turn_still_gets_its_edge. The "spliced onto a
#   same-named stranger" risk it was guarding is pre-existing (create_relationship has
#   always matched on name) and belongs to the dedup-semantics ticket, not here — paying
#   for it with the whole cross-turn edge population was the wrong trade.


@pytest.mark.asyncio
async def test_failed_turn_write_is_not_reported_as_a_created_turn(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    """create_conversation's result was ignored, so a failed Turn write read as success."""
    memory_service.create_conversation = AsyncMock(return_value=False)
    result = await _run(consolidator, _extraction([_entity("Neo4j")]))

    assert result["turns_created"] == 0


@pytest.mark.asyncio
async def test_entity_data_type_map_uses_canonical_names(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    """_entity_data keys the inline type map; it must match the canonical key_entities."""
    memory_service.create_entity = AsyncMock(return_value="Predictive Processing")
    await _run(consolidator, _extraction([_entity("predictive processing")]))

    turn = _turn_of(memory_service)
    names = {e.get("name") for e in getattr(turn, "_entity_data", [])}
    assert "Predictive Processing" in names


class TestReorderDoesNotLoseGraphEdges:
    """FRE-1115 code-review findings 1-3: the reorder must not silently drop edges.

    The first cut of this fix resolved relationship endpoints and stance targets ONLY
    through the current turn's canonical map, and dropped an entity entirely when
    create_entity failed. All three lose graph structure that the pre-reorder code wrote.
    """

    @pytest.mark.asyncio
    async def test_endpoint_from_an_earlier_turn_still_gets_its_edge(
        self, consolidator: SecondBrainConsolidator, memory_service: MagicMock
    ) -> None:
        """A relationship may name an entity this turn did not re-extract."""
        extraction = _extraction(
            [_entity("Neo4j")],
            [{"source": "Cypher", "target": "Neo4j", "type": "USES"}],
        )
        result = await _run(consolidator, extraction)

        assert result["relationships_created"] == 1, (
            "an endpoint written by an earlier turn must still resolve against the graph"
        )
        rel = memory_service.create_relationship.await_args.args[0]
        assert rel.source_id == "Cypher"
        assert rel.target_id == "Neo4j"

    @pytest.mark.asyncio
    async def test_renamed_endpoint_is_still_translated(
        self, consolidator: SecondBrainConsolidator, memory_service: MagicMock
    ) -> None:
        """Pass-through must not cost the rename translation it was added for."""
        memory_service.create_entity = AsyncMock(
            side_effect=lambda entity, **kw: (
                "Predictive Processing" if entity.name == "predictive processing" else entity.name
            )
        )
        extraction = _extraction(
            [_entity("predictive processing"), _entity("Neo4j")],
            [{"source": "predictive processing", "target": "Neo4j", "type": "RELATED_TO"}],
        )
        await _run(consolidator, extraction)

        rel = memory_service.create_relationship.await_args.args[0]
        assert rel.source_id == "Predictive Processing"

    @pytest.mark.asyncio
    async def test_stance_target_follows_the_rename(
        self, consolidator: SecondBrainConsolidator, memory_service: MagicMock
    ) -> None:
        """assert_stance MATCHes :Entity by name, so the raw name would find nothing."""
        memory_service.create_entity = AsyncMock(return_value="Météo France")
        extraction = _extraction([_entity("Météo-France")])
        extraction["stances"] = [
            {
                "subject": "owner",
                "target": "Météo-France",
                "affect": "trusts the forecasts",
                "mastery": None,
                "provenance": {
                    "observed_at": _TURN_TS.isoformat(),
                    "extracted_at": _TURN_TS.isoformat(),
                    "source_type": "conversation",
                },
            }
        ]
        result = await _run(consolidator, extraction)

        assert result["stances_created"] == 1
        stance = memory_service.assert_stance.await_args.args[0]
        assert stance.target == "Météo France", (
            "the stance must target the canonical node that actually exists"
        )

    @pytest.mark.asyncio
    async def test_failed_entity_write_keeps_the_turns_discusses_edge(
        self, consolidator: SecondBrainConsolidator, memory_service: MagicMock
    ) -> None:
        """DISCUSSES is the only path from a Turn to its entities; nothing re-derives it."""
        memory_service.create_entity = AsyncMock(return_value="")
        result = await _run(consolidator, _extraction([_entity("Blueberries")]))

        turn = _turn_of(memory_service)
        assert turn.key_entities == ["Blueberries"], (
            "a failed entity write must not cost the Turn its edge — create_entity "
            "returns '' only on failure, never on a rename, so this cannot re-mint "
            "the dedup orphan class"
        )
        assert result["entities_created"] == 0
        # Still flagged, so the failure stays diagnosable rather than looking normal.
        assert turn.properties.get("unresolved_entity_mentions") == ["Blueberries"]
