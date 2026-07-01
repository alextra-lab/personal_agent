"""Live-Neo4j behavioural proof of FRE-638 acceptance criteria (ADR-0098 D2/D3).

Marked ``integration`` (out of ``make test``); runs against the isolated test Neo4j
(:7688). ``generate_embedding`` is patched so Claim similarity is deterministic —
this exercises the *real* bitemporal Cypher and graph state, which is what the ACs
assert, without depending on the live embedder.

- AC-1 (correction): a wrong Claim, then a higher-confidence correction → current
  query returns the corrected value; the original is retained as superseded.
- AC-2 (evolution): a Personal fact that was true then changed → prior Claim has
  valid_to/invalid_at, still present; current query returns only the new; the two
  validity intervals do not overlap.
- AC-5 (native Stance traversal): owner -[:HAS_STANCE]-> WorldConcept -[:RELATED_TO]->
  WorldConcept returns in one Cypher query, no cross-store hop.
- REJECT: a weaker contradicting Claim does not clobber the current one.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from personal_agent.memory.models import Claim, Stance
from personal_agent.memory.service import MemoryService

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(days=90)


def _fake_embed(text: str) -> list[float]:
    """Deterministic stand-in: lease facts cluster, everything else is orthogonal."""
    return [1.0, 0.0] if "lease" in text.lower() else [0.0, 1.0]


@pytest_asyncio.fixture
async def owner_service():
    """Connected MemoryService with exactly one clean owner on the test graph."""
    service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
    if not await service.connect():
        pytest.skip("Neo4j not available (make test-infra-up)")

    assert service.driver is not None
    async with service.driver.session() as s:
        # Isolate: the test graph's Claims/Stances/owner are all test data.
        await s.run("MATCH (c:Claim) DETACH DELETE c")
        await s.run("MATCH (:Person {is_owner: true})-[r:HAS_STANCE]->() DELETE r")
        await s.run("MATCH (p:Person {is_owner: true}) DETACH DELETE p")
        await s.run("MATCH (e:Entity) WHERE e.name STARTS WITH 'FRE638_' DETACH DELETE e")
        await s.run(
            "CREATE (o:Person {user_id: 'fre638-owner', is_owner: true, name: 'Test Owner'})"
        )

    yield service

    async with service.driver.session() as s:
        await s.run("MATCH (c:Claim) DETACH DELETE c")
        await s.run("MATCH (p:Person {is_owner: true}) DETACH DELETE p")
        await s.run("MATCH (e:Entity) WHERE e.name STARTS WITH 'FRE638_' DETACH DELETE e")
    await service.disconnect()


async def _current_claims(service: MemoryService) -> list[dict]:
    assert service.driver is not None
    async with service.driver.session() as s:
        result = await s.run(
            "MATCH (:Person {is_owner: true})-[:HAS_FACT]->(c:Claim)\n"
            "WHERE c.valid_to IS NULL AND c.invalid_at IS NULL\n"
            "RETURN c.content AS content, c.claim_id AS claim_id"
        )
        return [dict(r) async for r in result]


async def _all_claims(service: MemoryService) -> list[dict]:
    assert service.driver is not None
    async with service.driver.session() as s:
        result = await s.run(
            "MATCH (:Person {is_owner: true})-[:HAS_FACT]->(c:Claim)\n"
            "RETURN c.content AS content, c.claim_id AS claim_id, c.valid_from AS valid_from,\n"
            "       c.valid_to AS valid_to, c.invalid_at AS invalid_at,\n"
            "       c.superseded_by AS superseded_by, c.supersession_reason AS reason"
        )
        return [dict(r) async for r in result]


@pytest.mark.asyncio
async def test_ac1_wrong_first_fact_is_correctable(owner_service: MemoryService) -> None:
    with patch(
        "personal_agent.memory.service.generate_embedding",
        new=AsyncMock(side_effect=lambda t: _fake_embed(t)),
    ):
        await owner_service.assert_claim(
            Claim(content="The lease ends in Jaunary.", confidence=0.5, observed_at=_T0)
        )
        await owner_service.assert_claim(
            Claim(content="The lease ends in March.", confidence=0.8, observed_at=_T1)
        )

    current = await _current_claims(owner_service)
    assert len(current) == 1
    assert current[0]["content"] == "The lease ends in March."  # corrected value

    all_claims = await _all_claims(owner_service)
    assert len(all_claims) == 2  # original retained, not destroyed
    superseded = [c for c in all_claims if c["content"] == "The lease ends in Jaunary."][0]
    assert superseded["invalid_at"] is not None
    assert superseded["superseded_by"] is not None
    assert superseded["reason"] == "correction"


@pytest.mark.asyncio
async def test_ac2_evolution_is_bitemporal_not_destructive(owner_service: MemoryService) -> None:
    with patch(
        "personal_agent.memory.service.generate_embedding",
        new=AsyncMock(side_effect=lambda t: _fake_embed(t)),
    ):
        await owner_service.assert_claim(
            Claim(content="The lease ends in March.", confidence=0.8, observed_at=_T0)
        )
        await owner_service.assert_claim(
            Claim(content="The lease ends in June.", confidence=0.8, observed_at=_T1)
        )

    current = await _current_claims(owner_service)
    assert [c["content"] for c in current] == ["The lease ends in June."]  # only the new

    all_claims = await _all_claims(owner_service)
    prior = [c for c in all_claims if c["content"] == "The lease ends in March."][0]
    new = [c for c in all_claims if c["content"] == "The lease ends in June."][0]
    # Prior retained with both temporal bounds set.
    assert prior["valid_to"] is not None
    assert prior["invalid_at"] is not None
    assert prior["reason"] == "evolution"
    # Non-overlap: prior's interval closes exactly where the new one opens.
    assert prior["valid_to"] == new["valid_from"]


@pytest.mark.asyncio
async def test_reject_weaker_contradiction_does_not_clobber(
    owner_service: MemoryService,
) -> None:
    with patch(
        "personal_agent.memory.service.generate_embedding",
        new=AsyncMock(side_effect=lambda t: _fake_embed(t)),
    ):
        await owner_service.assert_claim(
            Claim(content="The lease ends in March.", confidence=0.8, observed_at=_T0)
        )
        await owner_service.assert_claim(
            Claim(content="The lease ends in December.", confidence=0.4, observed_at=_T1)
        )

    current = await _current_claims(owner_service)
    assert [c["content"] for c in current] == ["The lease ends in March."]  # unchanged
    assert len(await _all_claims(owner_service)) == 2  # weaker claim retained as audit


@pytest.mark.asyncio
async def test_ac5_stance_traversal_is_native(owner_service: MemoryService) -> None:
    assert owner_service.driver is not None
    async with owner_service.driver.session() as s:
        await s.run(
            "CREATE (a:Entity {name: 'FRE638_RAV4', class: 'World'})\n"
            "CREATE (b:Entity {name: 'FRE638_HybridPowertrain', class: 'World'})\n"
            "CREATE (a)-[:RELATED_TO]->(b)"
        )

    ok = await owner_service.assert_stance(
        Stance(target="FRE638_RAV4", affect="loves it", mastery=None, observed_at=_T0)
    )
    assert ok is True

    async with owner_service.driver.session() as s:
        result = await s.run(
            "MATCH (o:Person {is_owner: true})-[st:HAS_STANCE]->(w1:Entity)-[:RELATED_TO]->(w2:Entity)\n"
            "WHERE st.valid_to IS NULL\n"
            "RETURN w1.name AS w1, w2.name AS w2, st.affect AS affect"
        )
        rows = [dict(r) async for r in result]

    assert len(rows) == 1
    assert rows[0]["w1"] == "FRE638_RAV4"
    assert rows[0]["w2"] == "FRE638_HybridPowertrain"
    assert rows[0]["affect"] == "loves it"


@pytest.mark.asyncio
async def test_stance_re_assertion_supersedes_prior(owner_service: MemoryService) -> None:
    assert owner_service.driver is not None
    async with owner_service.driver.session() as s:
        await s.run("CREATE (:Entity {name: 'FRE638_Rust', class: 'World'})")

    await owner_service.assert_stance(
        Stance(target="FRE638_Rust", affect="learning it", mastery=0.2, observed_at=_T0)
    )
    await owner_service.assert_stance(
        Stance(target="FRE638_Rust", affect="mastered it", mastery=0.9, observed_at=_T1)
    )

    async with owner_service.driver.session() as s:
        result = await s.run(
            "MATCH (:Person {is_owner: true})-[st:HAS_STANCE]->(:Entity {name: 'FRE638_Rust'})\n"
            "RETURN st.affect AS affect, st.valid_to AS valid_to"
        )
        rows = [dict(r) async for r in result]

    current = [r for r in rows if r["valid_to"] is None]
    superseded = [r for r in rows if r["valid_to"] is not None]
    assert len(current) == 1 and current[0]["affect"] == "mastered it"
    assert len(superseded) == 1 and superseded[0]["affect"] == "learning it"


# ---------------------------------------------------------------------------
# FRE-712: facet-aware matching + explicit update_kind label
# ---------------------------------------------------------------------------


def _vec(cos: float) -> list[float]:
    """A unit vector whose cosine with [1, 0] is exactly ``cos``."""
    return [cos, math.sqrt(max(0.0, 1.0 - cos * cos))]


def _embed_map(mapping: dict[str, list[float]]):
    """Deterministic embedder: content → vector (default orthogonal to [1, 0])."""

    async def _embed(text: str) -> list[float]:
        return mapping.get(text, [0.0, 1.0])

    return _embed


@pytest.mark.asyncio
async def test_ac_a_same_facet_groups_the_slot(owner_service: MemoryService) -> None:
    old = "Lease ends in March."
    new = "Lease ends in June."
    mapping = {old: _vec(0.70), new: _vec(1.0)}  # same facet → 0.60 floor, 0.70 matches
    with patch(
        "personal_agent.memory.service.generate_embedding",
        new=AsyncMock(side_effect=_embed_map(mapping)),
    ):
        await owner_service.assert_claim(
            Claim(content=old, confidence=0.8, observed_at=_T0, facet="lease_end_date")
        )
        await owner_service.assert_claim(
            Claim(content=new, confidence=0.8, observed_at=_T1, facet="lease_end_date")
        )
    current = await _current_claims(owner_service)
    assert [c["content"] for c in current] == [new]  # grouped + superseded


@pytest.mark.asyncio
async def test_ac_b_different_facet_moderate_similarity_does_not_collide(
    owner_service: MemoryService,
) -> None:
    a = "Lease ends in March."
    b = "Rent is 2000 a month."
    mapping = {a: _vec(0.85), b: _vec(1.0)}  # 0.85 < DIFF_FACET_FLOOR (0.95) → no match
    with patch(
        "personal_agent.memory.service.generate_embedding",
        new=AsyncMock(side_effect=_embed_map(mapping)),
    ):
        await owner_service.assert_claim(
            Claim(content=a, confidence=0.8, observed_at=_T0, facet="lease_end_date")
        )
        await owner_service.assert_claim(
            Claim(content=b, confidence=0.8, observed_at=_T1, facet="monthly_rent")
        )
    current = await _current_claims(owner_service)
    assert {c["content"] for c in current} == {a, b}  # both stay current — no false collide


@pytest.mark.asyncio
async def test_ac_c_facet_drift_recovers_on_near_identical_content(
    owner_service: MemoryService,
) -> None:
    old = "Lease ends in March."
    new = "Lease ends in June."
    mapping = {old: _vec(0.97), new: _vec(1.0)}  # different facet, 0.97 >= 0.95 → merge
    with patch(
        "personal_agent.memory.service.generate_embedding",
        new=AsyncMock(side_effect=_embed_map(mapping)),
    ):
        await owner_service.assert_claim(
            Claim(content=old, confidence=0.8, observed_at=_T0, facet="lease_end_date")
        )
        await owner_service.assert_claim(
            Claim(content=new, confidence=0.8, observed_at=_T1, facet="current_lease_expiration")
        )
    current = await _current_claims(owner_service)
    assert [c["content"] for c in current] == [new]  # drift did not strand the stale fact


@pytest.mark.asyncio
async def test_ac_c2_new_facet_supersedes_legacy_no_facet_claim(
    owner_service: MemoryService,
) -> None:
    old = "Lease ends in March."
    new = "Lease ends in June."
    mapping = {old: _vec(0.90), new: _vec(1.0)}  # either-empty → base 0.83, 0.90 matches
    with patch(
        "personal_agent.memory.service.generate_embedding",
        new=AsyncMock(side_effect=_embed_map(mapping)),
    ):
        # Legacy claim: no facet (FRE-638-era row).
        await owner_service.assert_claim(Claim(content=old, confidence=0.8, observed_at=_T0))
        await owner_service.assert_claim(
            Claim(content=new, confidence=0.8, observed_at=_T1, facet="lease_end_date")
        )
    current = await _current_claims(owner_service)
    assert [c["content"] for c in current] == [new]


@pytest.mark.asyncio
async def test_ac_d_explicit_correction_label(owner_service: MemoryService) -> None:
    old = "Lease ends in March."
    new = "Lease ends in June."
    mapping = {old: _vec(0.70), new: _vec(1.0)}
    with patch(
        "personal_agent.memory.service.generate_embedding",
        new=AsyncMock(side_effect=_embed_map(mapping)),
    ):
        await owner_service.assert_claim(
            Claim(content=old, confidence=0.8, observed_at=_T0, facet="lease_end_date")
        )
        await owner_service.assert_claim(
            Claim(
                content=new,
                confidence=0.8,  # EQUAL confidence — heuristic would say 'evolution'
                observed_at=_T1,
                facet="lease_end_date",
                update_kind="correction",
            )
        )
    superseded = [c for c in await _all_claims(owner_service) if c["content"] == old][0]
    assert superseded["reason"] == "correction"  # explicit signal, not the heuristic


@pytest.mark.asyncio
async def test_ac_e_explicit_evolution_label(owner_service: MemoryService) -> None:
    old = "Lease ends in March."
    new = "Lease ends in June."
    mapping = {old: _vec(0.70), new: _vec(1.0)}
    with patch(
        "personal_agent.memory.service.generate_embedding",
        new=AsyncMock(side_effect=_embed_map(mapping)),
    ):
        await owner_service.assert_claim(
            Claim(content=old, confidence=0.5, observed_at=_T0, facet="lease_end_date")
        )
        await owner_service.assert_claim(
            Claim(
                content=new,
                confidence=0.9,  # HIGHER confidence — heuristic would say 'correction'
                observed_at=_T1,
                facet="lease_end_date",
                update_kind="evolution",
            )
        )
    superseded = [c for c in await _all_claims(owner_service) if c["content"] == old][0]
    assert superseded["reason"] == "evolution"  # explicit signal overrides the heuristic
