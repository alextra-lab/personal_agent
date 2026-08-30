"""FRE-1346 — live-Neo4j behavioural proof of the provenance write (ADR-0098 A4/A4b).

Marked ``integration`` (out of ``make test``); runs against the isolated test Neo4j
(:7688, FRE-375). Needs no LLM server — ``generate_embedding`` is patched to a zero
vector so the dedup path is skipped and each write is deterministic.

**Why this file has to exist and has to be run.** The mocked-driver tests in
``test_provenance_cypher.py`` assert substrings of the emitted statement, and a substring
assertion passes just as happily against syntactically invalid Cypher or a subquery that
never matches. AC-4 (accumulation) and AC-5 (the relationship list) are *behavioural*
criteria; only the substrate can decide them.
"""

from __future__ import annotations

# ruff: noqa: D103
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio

from personal_agent.memory.models import Entity, Relationship
from personal_agent.memory.provenance import SourceRecord
from personal_agent.memory.service import MemoryService

pytestmark = pytest.mark.integration

_ZERO_EMBED = patch(
    "personal_agent.memory.service.generate_embedding",
    new=AsyncMock(return_value=[0.0, 0.0]),
)
_TS = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def _source(source_id: str, referent: str) -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        referent=referent,
        authority="example.com",
        retrieved_at=_TS,
        content_hash=f"hash-{source_id}",
        retained_pointer=f"capture://t1#tool_results/{source_id}",
        content="SafeCart is a checkout platform.",
    )


@pytest_asyncio.fixture
async def service() -> MemoryService:
    svc = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
    await svc.connect()
    if not svc.connected:
        pytest.skip("test Neo4j (:7688) unavailable")
    await svc.ensure_source_id_constraint()
    async with svc.driver.session() as session:
        await session.run(
            "MATCH (n) WHERE n.name STARTS WITH 'FRE1346' OR n:Source DETACH DELETE n"
        )
    yield svc
    async with svc.driver.session() as session:
        await session.run(
            "MATCH (n) WHERE n.name STARTS WITH 'FRE1346' OR n:Source DETACH DELETE n"
        )
    await svc.disconnect()


async def _entity(service: MemoryService, name: str, sources: list[SourceRecord]) -> None:
    with _ZERO_EMBED:
        await service.create_entity(
            Entity(name=name, entity_type="Organization"), source_records=sources
        )


# --------------------------------------------------------------------------------------
# AC-4 — the merged canonical entity accumulates, never overwrites
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac4_a_second_source_accumulates_and_neither_is_lost(
    service: MemoryService,
) -> None:
    """A scalar property would overwrite on the second sighting or freeze on the first —
    first-write-wins reintroduced on a new axis, which is the failure D2 exists to kill.
    """
    await _entity(service, "FRE1346Corp", [_source("src-x", "https://example.com/x")])
    await _entity(service, "FRE1346Corp", [_source("src-y", "https://example.com/y")])

    async with service.driver.session() as session:
        result = await session.run(
            "MATCH (e:Entity {name: 'FRE1346Corp'})-[:SOURCED_FROM]->(s:Source) "
            "RETURN collect(s.referent) AS referents, e.provenance_state AS state"
        )
        record = await result.single()

    assert sorted(record["referents"]) == ["https://example.com/x", "https://example.com/y"]
    assert record["state"] == "provenanced"


@pytest.mark.asyncio
async def test_ac4_re_merging_the_same_source_does_not_duplicate_the_edge(
    service: MemoryService,
) -> None:
    """Append-only must not mean append-again: corroboration is counted by DISTINCT
    source identity, so a re-fetch of one unchanged page stays one reference.
    """
    await _entity(service, "FRE1346Corp", [_source("src-x", "https://example.com/x")])
    await _entity(service, "FRE1346Corp", [_source("src-x", "https://example.com/x")])

    async with service.driver.session() as session:
        result = await session.run(
            "MATCH (e:Entity {name: 'FRE1346Corp'})-[r:SOURCED_FROM]->(:Source) "
            "RETURN count(r) AS edges"
        )
        record = await result.single()

    assert record["edges"] == 1


@pytest.mark.asyncio
async def test_a_sourceless_write_never_demotes_an_already_provenanced_entity(
    service: MemoryService,
) -> None:
    """A5: the `none -> provenanced` transition is one way. An item never returns."""
    await _entity(service, "FRE1346Corp", [_source("src-x", "https://example.com/x")])
    await _entity(service, "FRE1346Corp", [])

    async with service.driver.session() as session:
        result = await session.run(
            "MATCH (e:Entity {name: 'FRE1346Corp'}) RETURN e.provenance_state AS state"
        )
        record = await result.single()

    assert record["state"] == "provenanced"


@pytest.mark.asyncio
async def test_an_entity_with_no_sources_is_stamped_none_not_null(
    service: MemoryService,
) -> None:
    """A5 forbids the silent third state: never an absent property to be inferred."""
    await _entity(service, "FRE1346Orphan", [])

    async with service.driver.session() as session:
        result = await session.run(
            "MATCH (e:Entity {name: 'FRE1346Orphan'}) RETURN e.provenance_state AS state"
        )
        record = await result.single()

    assert record["state"] == "none"


@pytest.mark.asyncio
async def test_source_node_carries_its_metadata_and_not_the_bytes(
    service: MemoryService,
) -> None:
    """D3: Core holds the small keyed pointer; the bytes stay in the Docs layer."""
    await _entity(service, "FRE1346Corp", [_source("src-x", "https://example.com/x")])

    async with service.driver.session() as session:
        result = await session.run(
            "MATCH (s:Source {source_id: 'src-x'}) RETURN properties(s) AS props"
        )
        record = await result.single()

    props = record["props"]
    assert props["referent"] == "https://example.com/x"
    assert props["authority"] == "example.com"
    assert props["content_hash"] == "hash-src-x"
    assert props["content_hash_scope"] == "captured_output_stripped"
    assert props["retained_pointer"].startswith("capture://")
    assert "content" not in props


# --------------------------------------------------------------------------------------
# AC-5 — the relationship path works, is a property, and its ids resolve
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac5_relationship_carries_source_ids_and_accumulates(
    service: MemoryService,
) -> None:
    await _entity(service, "FRE1346Corp", [])
    await _entity(service, "FRE1346City", [])

    rel = Relationship(
        source_id="FRE1346Corp", target_id="FRE1346City", relationship_type="BASED_IN"
    )
    assert await service.create_relationship(rel, source_records=[_source("src-x", "https://a")])
    assert await service.create_relationship(rel, source_records=[_source("src-y", "https://b")])

    async with service.driver.session() as session:
        result = await session.run(
            "MATCH (:Entity {name:'FRE1346Corp'})-[r:BASED_IN]->(:Entity {name:'FRE1346City'}) "
            "RETURN r.source_ids AS ids, r.provenance_state AS state"
        )
        record = await result.single()

    assert sorted(record["ids"]) == ["src-x", "src-y"]
    assert record["state"] == "provenanced"


@pytest.mark.asyncio
async def test_ac5_every_stored_source_id_resolves_to_exactly_one_source(
    service: MemoryService,
) -> None:
    """A dangling id is a provenance pointer that cannot be walked — the A4b
    referential-integrity failure a list-property model makes possible.
    """
    await _entity(service, "FRE1346Corp", [])
    await _entity(service, "FRE1346City", [])
    await service.create_relationship(
        Relationship(
            source_id="FRE1346Corp", target_id="FRE1346City", relationship_type="BASED_IN"
        ),
        source_records=[_source("src-x", "https://a"), _source("src-y", "https://b")],
    )

    async with service.driver.session() as session:
        result = await session.run(
            "MATCH (:Entity {name:'FRE1346Corp'})-[r:BASED_IN]->(:Entity {name:'FRE1346City'}) "
            "UNWIND r.source_ids AS sid "
            "OPTIONAL MATCH (s:Source {source_id: sid}) "
            "RETURN sid, count(s) AS resolved ORDER BY sid"
        )
        rows = [(row["sid"], row["resolved"]) async for row in result]

    assert rows == [("src-x", 1), ("src-y", 1)]


@pytest.mark.asyncio
async def test_an_unresolvable_endpoint_leaves_no_orphan_source(
    service: MemoryService,
) -> None:
    """No rows, no mint: the :Source subquery runs after the endpoint MATCHes.

    Minted before them, a relationship whose endpoint does not exist still created the
    :Source node, leaving one behind that nothing referenced.
    """
    assert (
        await service.create_relationship(
            Relationship(
                source_id="FRE1346Missing", target_id="FRE1346Absent", relationship_type="BASED_IN"
            ),
            source_records=[_source("src-orphan", "https://orphan.example.com")],
        )
        is None
    )

    async with service.driver.session() as session:
        result = await session.run(
            "MATCH (s:Source {source_id: 'src-orphan'}) RETURN count(s) AS n"
        )
        record = await result.single()

    assert record["n"] == 0


@pytest.mark.asyncio
async def test_ac5_relationship_with_no_sources_is_stamped_none(
    service: MemoryService,
) -> None:
    await _entity(service, "FRE1346Corp", [])
    await _entity(service, "FRE1346City", [])
    await service.create_relationship(
        Relationship(source_id="FRE1346Corp", target_id="FRE1346City", relationship_type="BASED_IN")
    )

    async with service.driver.session() as session:
        result = await session.run(
            "MATCH (:Entity {name:'FRE1346Corp'})-[r:BASED_IN]->(:Entity {name:'FRE1346City'}) "
            "RETURN r.provenance_state AS state, r.source_ids AS ids"
        )
        record = await result.single()

    assert record["state"] == "none"
    assert record["ids"] == []


# --------------------------------------------------------------------------------------
# The claim path
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_carries_a_sourced_from_edge(service: MemoryService) -> None:
    from personal_agent.memory.models import Claim

    user_id = uuid4()
    async with service.driver.session() as session:
        await session.run(
            "CREATE (p:Person {user_id: $uid, name: 'FRE1346Person'})", uid=str(user_id)
        )
    with _ZERO_EMBED:
        claim_id = await service.assert_claim(
            Claim(
                content="FRE1346Corp is a checkout platform.",
                knowledge_class="World",
                observed_at=_TS,
            ),
            user_id=user_id,
            source_records=[_source("src-x", "https://example.com/x")],
        )
    assert claim_id

    async with service.driver.session() as session:
        result = await session.run(
            "MATCH (c:Claim {claim_id: $cid})-[:SOURCED_FROM]->(s:Source) "
            "RETURN s.referent AS referent, c.provenance_state AS state",
            cid=claim_id,
        )
        record = await result.single()
        await session.run("MATCH (p:Person {user_id: $uid}) DETACH DELETE p", uid=str(user_id))

    assert record["referent"] == "https://example.com/x"
    assert record["state"] == "provenanced"
