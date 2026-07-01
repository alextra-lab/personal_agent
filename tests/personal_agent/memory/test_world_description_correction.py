"""Live-Neo4j behavioural proof of FRE-711 (ADR-0098 D2 — living World descriptions).

Marked ``integration`` (out of ``make test``); runs against the isolated test Neo4j
(:7688). ``generate_embedding`` is patched to a zero vector so the dedup path is
skipped and each test drives ``create_entity`` deterministically — except AC-6, which
patches the dedup *decision* to exercise the alias-correction path.

- AC-1 correctable by a higher-confidence write; AC-2 original retained as a version
  node with provenance; AC-3 the recalled property equals the corrected value;
  AC-4 an eval write never clobbers a non-eval description (FRE-375 preserved);
  AC-5 same-confidence/empty/idempotent safety; AC-6 alias-driven correction records
  the proposed surface name.
"""

from __future__ import annotations

# ruff: noqa: D103
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from personal_agent.memory.models import Entity
from personal_agent.memory.service import MemoryService

pytestmark = pytest.mark.integration

_ZERO_EMBED = patch(
    "personal_agent.memory.service.generate_embedding",
    new=AsyncMock(return_value=[0.0, 0.0]),
)


@pytest_asyncio.fixture
async def svc():
    service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
    if not await service.connect():
        pytest.skip("Neo4j not available (make test-infra-up)")
    assert service.driver is not None
    async with service.driver.session() as s:
        await s.run("MATCH (v:EntityDescriptionVersion) DETACH DELETE v")
        await s.run("MATCH (e:Entity) WHERE e.name STARTS WITH 'FRE711_' DETACH DELETE e")
    yield service
    async with service.driver.session() as s:
        await s.run("MATCH (v:EntityDescriptionVersion) DETACH DELETE v")
        await s.run("MATCH (e:Entity) WHERE e.name STARTS WITH 'FRE711_' DETACH DELETE e")
    await service.disconnect()


async def _desc(service: MemoryService, name: str) -> str | None:
    assert service.driver is not None
    async with service.driver.session() as s:
        r = await s.run("MATCH (e:Entity {name: $n}) RETURN e.description AS d", n=name)
        rec = await r.single()
        return rec["d"] if rec else None


async def _versions(service: MemoryService, name: str) -> list[dict]:
    assert service.driver is not None
    async with service.driver.session() as s:
        r = await s.run(
            "MATCH (e:Entity {name: $n})-[:HAD_DESCRIPTION]->(v:EntityDescriptionVersion)\n"
            "RETURN v.text AS text, v.confidence AS confidence, v.eval_mode AS eval_mode,\n"
            "       v.valid_to AS valid_to, v.proposed_name AS proposed_name",
            n=name,
        )
        return [dict(x) async for x in r]


def _entity(name: str, desc: str) -> Entity:
    return Entity(name=name, entity_type="Technology", description=desc)


@pytest.mark.asyncio
async def test_ac1_ac2_ac3_higher_confidence_correction(svc: MemoryService) -> None:
    with _ZERO_EMBED:
        await svc.create_entity(_entity("FRE711_Neo4j", "A databse"), description_confidence=0.5)
        await svc.create_entity(
            _entity("FRE711_Neo4j", "A graph database management system"),
            description_confidence=0.8,
            originating_trace_id="trace-correct",
        )

    # AC-1 / AC-3: the current (recalled) description is the corrected value.
    assert await _desc(svc, "FRE711_Neo4j") == "A graph database management system"
    # AC-2: original retained as a version node with provenance + valid_to.
    versions = await _versions(svc, "FRE711_Neo4j")
    assert len(versions) == 1
    assert versions[0]["text"] == "A databse"
    assert versions[0]["confidence"] == pytest.approx(0.5)
    assert versions[0]["valid_to"] is not None
    assert versions[0]["proposed_name"] == "FRE711_Neo4j"


@pytest.mark.asyncio
async def test_ac4_eval_write_cannot_clobber_non_eval(svc: MemoryService) -> None:
    with _ZERO_EMBED:
        await svc.create_entity(
            _entity("FRE711_ES", "A search engine"), description_confidence=0.8, eval_mode=False
        )
        # Eval write, even at higher confidence + different text, must not overwrite.
        await svc.create_entity(
            _entity("FRE711_ES", "WRONG eval-injected description"),
            description_confidence=0.99,
            eval_mode=True,
        )

    assert await _desc(svc, "FRE711_ES") == "A search engine"  # unchanged
    assert await _versions(svc, "FRE711_ES") == []  # no history node for the rejected write


@pytest.mark.asyncio
async def test_ac5_same_confidence_and_empty_and_idempotent(svc: MemoryService) -> None:
    with _ZERO_EMBED:
        await svc.create_entity(_entity("FRE711_Py", "A language"), description_confidence=0.8)
        # Same confidence, different text → strict '>' blocks it.
        await svc.create_entity(
            _entity("FRE711_Py", "A different claim"), description_confidence=0.8
        )
        # Empty new description → never overwrites.
        await svc.create_entity(_entity("FRE711_Py", ""), description_confidence=0.9)
        # Idempotent: same text again → no new version.
        await svc.create_entity(_entity("FRE711_Py", "A language"), description_confidence=0.9)

    assert await _desc(svc, "FRE711_Py") == "A language"
    assert await _versions(svc, "FRE711_Py") == []


@pytest.mark.asyncio
async def test_ac6_alias_correction_records_proposed_name(svc: MemoryService) -> None:
    from personal_agent.memory.dedup import DedupDecision

    # Seed the canonical entity.
    with _ZERO_EMBED:
        await svc.create_entity(_entity("FRE711_Canonical", "thin"), description_confidence=0.5)

    # A different surface form that dedups to the canonical entity, with a higher
    # confidence so it corrects. Patch the dedup decision to force the rename.
    dedup_result = AsyncMock(
        return_value=type(
            "R",
            (),
            {
                "decision": DedupDecision.MERGE_EXISTING,
                "canonical_name": "FRE711_Canonical",
                "similarity_score": 0.99,
            },
        )()
    )
    with (
        patch(
            "personal_agent.memory.service.generate_embedding",
            new=AsyncMock(return_value=[0.1, 0.2]),  # non-zero → dedup branch runs
        ),
        patch("personal_agent.memory.dedup.check_entity_duplicate", new=dedup_result),
    ):
        await svc.create_entity(
            _entity("FRE711_Alias", "a rich canonical description"),
            description_confidence=0.8,
        )

    # Correction landed on the canonical entity...
    assert await _desc(svc, "FRE711_Canonical") == "a rich canonical description"
    # ...and the archive records the surface form that drove it (auditability).
    versions = await _versions(svc, "FRE711_Canonical")
    assert len(versions) == 1
    assert versions[0]["proposed_name"] == "FRE711_Alias"
