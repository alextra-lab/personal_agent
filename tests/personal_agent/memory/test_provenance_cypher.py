"""FRE-1346 — mocked-driver Cypher-shape tests for the provenance write (ADR-0098 A4b).

These lock the emitted statement without a live Neo4j: the ``:Source`` MERGE and
``SOURCED_FROM`` edge ride in the **same statement** as the node they justify (A4's
same-transaction rule), relationships take a de-duplicated ``source_ids`` list property
rather than an edge (structurally impossible), and ``provenance_state`` is stamped on
every write with a one-way ``none -> provenanced`` transition.

Behavioural proof — that the structures actually land and accumulate — is
``test_provenance_live.py``, which runs against the isolated test Neo4j. A substring
assertion here would pass with syntactically invalid Cypher, so it is evidence of shape
only, never of function.
"""

# ruff: noqa: D103

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from personal_agent.memory.models import Entity, Relationship
from personal_agent.memory.provenance import SourceRecord
from personal_agent.memory.service import MemoryService

_TS = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def _record(source_id: str = "src-1", referent: str = "https://example.com/a") -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        referent=referent,
        authority="example.com",
        retrieved_at=_TS,
        content_hash="hash-1",
        retained_pointer="capture://t1#tool_results/0",
        content="SafeCart is a checkout platform.",
    )


def _make_service() -> tuple[MemoryService, list[tuple[str, dict[str, Any]]]]:
    service = MemoryService.__new__(MemoryService)
    service.connected = True
    service._query_feedback_by_key = {}

    captured: list[tuple[str, dict[str, Any]]] = []
    result = AsyncMock()
    result.single = AsyncMock(return_value={"entity_id": "SafeCart", "element_id": "4:abc:1"})

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


async def _create_entity(service: MemoryService, **kwargs: object) -> None:
    with patch(
        "personal_agent.memory.service.generate_embedding",
        new=AsyncMock(return_value=[0.0, 0.0]),
    ):
        await service.create_entity(Entity(name="SafeCart", entity_type="Organization"), **kwargs)


# --------------------------------------------------------------------------------------
# Entity path — the edge rides in the same statement (A4)
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entity_merges_source_and_edge_in_the_same_statement() -> None:
    service, captured = _make_service()
    await _create_entity(service, source_records=[_record()])

    cypher, params = captured[-1]
    assert "MERGE (e:Entity {name: $name})" in cypher
    assert "UNWIND $source_records AS _src" in cypher
    assert "MERGE (src:Source {source_id: _src.source_id})" in cypher
    assert "MERGE (e)-[:SOURCED_FROM]->(src)" in cypher
    assert params["source_records"] == [_record().to_cypher_map()]


@pytest.mark.asyncio
async def test_entity_source_records_are_driver_encodable_primitives() -> None:
    """The frozen dataclass must never reach the driver — it cannot encode it."""
    service, captured = _make_service()
    await _create_entity(service, source_records=[_record()])

    payload = captured[-1][1]["source_records"]
    assert all(isinstance(item, dict) for item in payload)
    assert all(isinstance(v, str) for item in payload for v in item.values())
    assert "content" not in payload[0]


@pytest.mark.asyncio
async def test_entity_provenance_state_is_one_way() -> None:
    """A5: `none -> provenanced` is allowed, in that direction only."""
    service, captured = _make_service()
    await _create_entity(service, source_records=[_record()])

    cypher = captured[-1][0]
    assert (
        "e.provenance_state = CASE WHEN size($source_records) > 0 THEN 'provenanced' "
        "ELSE coalesce(e.provenance_state, 'none') END" in cypher
    )


@pytest.mark.asyncio
async def test_entity_with_no_sources_still_stamps_none_and_is_otherwise_a_no_op() -> None:
    """Every existing caller keeps its behaviour apart from the sentinel stamp."""
    service, captured = _make_service()
    await _create_entity(service)

    cypher, params = captured[-1]
    assert params["source_records"] == []
    assert "UNWIND $source_records AS _src" in cypher
    assert "provenance_state" in cypher


@pytest.mark.asyncio
async def test_entity_source_edge_is_append_only_never_an_overwrite() -> None:
    """AC-4: a scalar assignment would reintroduce first-write-wins on a new axis."""
    service, captured = _make_service()
    await _create_entity(service, source_records=[_record()])

    cypher = captured[-1][0]
    assert "e.sources =" not in cypher
    assert "e.source_id =" not in cypher
    assert "e.provenance =" not in cypher


# --------------------------------------------------------------------------------------
# Relationship path — a property, never an edge (A4b)
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relationship_appends_source_ids_on_yield_rel() -> None:
    service, captured = _make_service()
    await service.create_relationship(
        Relationship(source_id="SafeCart", target_id="Lisbon", relationship_type="based_in"),
        source_records=[_record()],
    )

    cypher, params = captured[-1]
    assert "YIELD rel" in cypher
    assert (
        "SET rel.source_ids = apoc.coll.toSet(coalesce(rel.source_ids, []) + $source_ids)" in cypher
    )
    assert params["source_ids"] == ["src-1"]


@pytest.mark.asyncio
async def test_relationship_never_attempts_a_sourced_from_edge() -> None:
    """AC-5: a Neo4j relationship cannot be the endpoint of another relationship."""
    service, captured = _make_service()
    await service.create_relationship(
        Relationship(source_id="SafeCart", target_id="Lisbon", relationship_type="based_in"),
        source_records=[_record()],
    )

    assert "-[:SOURCED_FROM]->" not in captured[-1][0].replace(
        "MERGE (src:Source {source_id: _src.source_id})", ""
    )


@pytest.mark.asyncio
async def test_relationship_mints_the_source_nodes_it_references() -> None:
    """A stored id that resolves to no :Source is a dangling provenance pointer."""
    service, captured = _make_service()
    await service.create_relationship(
        Relationship(source_id="SafeCart", target_id="Lisbon", relationship_type="based_in"),
        source_records=[_record()],
    )

    cypher = captured[-1][0]
    assert "MERGE (src:Source {source_id: _src.source_id})" in cypher
    assert "UNWIND $source_records AS _src" in cypher


@pytest.mark.asyncio
async def test_relationship_provenance_state_reads_the_merged_list() -> None:
    """Derived from the post-append value, so append-only makes the transition one-way."""
    service, captured = _make_service()
    await service.create_relationship(
        Relationship(source_id="SafeCart", target_id="Lisbon", relationship_type="based_in"),
        source_records=[_record()],
    )

    cypher = captured[-1][0]
    assert (
        "SET rel.provenance_state = CASE WHEN size(rel.source_ids) > 0 "
        "THEN 'provenanced' ELSE 'none' END" in cypher
    )


# --------------------------------------------------------------------------------------
# The inline DISCUSSES MERGE — the post-change silent-third-state hole
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inline_discusses_merge_stamps_provenance_state() -> None:
    """`create_conversation` bare-MERGEs an :Entity the consolidator falls through to.

    Reachable today: the consolidator deliberately records the mention this way when
    `create_entity` fails. Without the stamp those nodes carry no `provenance_state` at
    all — A5's forbidden silent third state, on a post-change write.
    """
    from personal_agent.memory.models import TurnNode

    service, captured = _make_service()
    turn = TurnNode(
        turn_id="t1",
        trace_id="t1",
        session_id="s1",
        timestamp=_TS,
        summary="s",
        user_message="u",
        assistant_response="a",
        key_entities=["SafeCart"],
    )
    await service.create_conversation(turn, user_id=uuid4(), visibility="group")

    inline = [c for c, _ in captured if "MERGE (t)-[:DISCUSSES]->(e)" in c]
    assert inline, "the inline DISCUSSES MERGE did not run"
    assert "e.provenance_state = COALESCE(e.provenance_state, 'none')" in inline[0]
