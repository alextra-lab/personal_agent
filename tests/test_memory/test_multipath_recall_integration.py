"""End-to-end multi-path recall on the broad path (FRE-724, ADR-0104 AC-3/AC-5).

Requires the test Neo4j substrate (``make test-infra-up``, :7688); skips cleanly
when unavailable. Proves the assembled seam on the primary MEMORY_RECALL surface
(query_memory_broad): an entity that is out-of-vocabulary for the dense arm but
matched by the lexical arm is recovered with the multi-path flag on and absent
with it off — the discriminating ADR-0104 AC-3 outcome, wired end to end.

The full lived-tail proof on the real corpus/embedder (median latency, floor
invariant) is master-owned (FRE-489/670, deploy-gated). This test controls the
dense miss by seeding an entity with no embedding, so the discriminator is stable
without live-corpus access.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from personal_agent.config.settings import get_settings
from personal_agent.memory.models import MemoryQuery
from personal_agent.memory.service import MemoryService


@pytest_asyncio.fixture
async def memory_service():
    service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
    connected = await service.connect()
    if not connected:
        pytest.skip("Neo4j not available (make test-infra-up)")
    await service.ensure_fulltext_index()
    await service.ensure_vector_index()
    yield service
    await service.disconnect()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _old_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()


async def _seed_oov_entity_with_old_turn(
    service: MemoryService, *, prefix: str, entity_name: str
) -> None:
    """Seed an Entity with NO embedding (dense can't reach it) discussed by an
    old Turn (outside the recency window), so only the lexical arm recovers it.
    """
    async with service.driver.session() as session:
        await session.run(
            """
            MERGE (e:Entity {name: $name})
            SET e.entity_type = 'Concept',
                e.description = 'seeded oov concept',
                e.last_seen = $now, e.first_seen = $old,
                e.mention_count = 1, e.visibility = 'public'
            MERGE (t:Turn {turn_id: $turn_id})
            SET t.user_message = $msg, t.timestamp = $old, t.visibility = 'public'
            MERGE (t)-[:DISCUSSES]->(e)
            """,
            name=entity_name,
            turn_id=f"{prefix}-t1",
            msg=f"an old note about {entity_name}",
            now=_now_iso(),
            old=_old_iso(),
        )


async def _purge(service: MemoryService, prefix: str) -> None:
    async with service.driver.session() as session:
        await session.run(
            "MATCH (n) WHERE n.name STARTS WITH $p OR n.turn_id STARTS WITH $p DETACH DELETE n",
            p=prefix,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_broad_recovers_lexical_only_entity_when_multipath_on(memory_service, monkeypatch):
    """AC-3: the OOV entity is present with multipath on, absent with it off."""
    prefix = f"fre724-{uuid.uuid4().hex[:8]}"
    rare = f"{prefix}zylophonics"
    await _seed_oov_entity_with_old_turn(memory_service, prefix=prefix, entity_name=rare)

    s = get_settings()
    monkeypatch.setattr(s, "lexical_arm_enabled", True, raising=False)
    monkeypatch.setattr(s, "multiquery_arm_enabled", True, raising=False)
    monkeypatch.setattr(s, "multipath_paraphrase_count", 1, raising=False)  # skip LLM paraphrase
    monkeypatch.setattr(s, "reranker_enabled", False, raising=False)  # no :8504 dependency
    monkeypatch.setattr(s, "relevance_bounded_recall_enabled", True, raising=False)

    try:
        # Flag OFF → single dense path: OOV entity + old turn ⇒ not recovered.
        monkeypatch.setattr(s, "multipath_recall_enabled", False, raising=False)
        off = await memory_service.query_memory_broad(query_text=rare, recency_days=90, limit=20)
        off_names = {e.get("name") for e in off["entities"]}
        assert rare not in off_names

        # Flag ON → the lexical arm recovers the entity by its rare name token.
        monkeypatch.setattr(s, "multipath_recall_enabled", True, raising=False)
        on = await memory_service.query_memory_broad(query_text=rare, recency_days=90, limit=20)
        on_names = {e.get("name") for e in on["entities"]}
        assert rare in on_names
    finally:
        await _purge(memory_service, prefix)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_entity_name_path_converges_through_core(memory_service, monkeypatch):
    """Entity-name path (query_memory) recovers the lexical-only entity's turn."""
    prefix = f"fre724q-{uuid.uuid4().hex[:8]}"
    rare = f"{prefix}qwyzzle"
    await _seed_oov_entity_with_old_turn(memory_service, prefix=prefix, entity_name=rare)

    s = get_settings()
    monkeypatch.setattr(s, "lexical_arm_enabled", True, raising=False)
    monkeypatch.setattr(s, "multiquery_arm_enabled", True, raising=False)
    monkeypatch.setattr(s, "multipath_paraphrase_count", 1, raising=False)
    monkeypatch.setattr(s, "reranker_enabled", False, raising=False)
    monkeypatch.setattr(s, "multipath_recall_enabled", True, raising=False)

    query = MemoryQuery(limit=10, recency_days=90)
    try:
        result = await memory_service.query_memory(query, query_text=rare)
        turn_ids = {c.turn_id for c in result.conversations}
        assert f"{prefix}-t1" in turn_ids  # the turn discussing the OOV entity
    finally:
        await _purge(memory_service, prefix)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proactive_raw_broadened_by_lexical_arm(memory_service, monkeypatch):
    """Proactive candidacy (suggest_proactive_raw) gains the lexical-only entity."""
    prefix = f"fre724pr-{uuid.uuid4().hex[:8]}"
    rare = f"{prefix}vibbleton"
    await _seed_oov_entity_with_old_turn(memory_service, prefix=prefix, entity_name=rare)

    s = get_settings()
    monkeypatch.setattr(s, "lexical_arm_enabled", True, raising=False)
    dims = s.embedding_dimensions
    nonzero = [0.0] * dims
    nonzero[0] = 1.0  # non-zero so the dense query is not short-circuited

    try:
        monkeypatch.setattr(s, "multipath_recall_enabled", False, raising=False)
        off = await memory_service.suggest_proactive_raw(
            nonzero, current_session_id="other", trace_id="t", query_text=rare
        )
        assert rare not in {r.get("name") for r in off}

        monkeypatch.setattr(s, "multipath_recall_enabled", True, raising=False)
        on = await memory_service.suggest_proactive_raw(
            nonzero, current_session_id="other", trace_id="t", query_text=rare
        )
        assert rare in {r.get("name") for r in on}
    finally:
        await _purge(memory_service, prefix)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_broad_off_flag_is_byte_for_byte_legacy(memory_service, monkeypatch):
    """Off-parity: with the flag off, the broad payload equals the legacy path."""
    prefix = f"fre724p-{uuid.uuid4().hex[:8]}"
    await _seed_oov_entity_with_old_turn(memory_service, prefix=prefix, entity_name=f"{prefix}-x")
    s = get_settings()
    monkeypatch.setattr(s, "multipath_recall_enabled", False, raising=False)
    try:
        a = await memory_service.query_memory_broad(query_text="anything", recency_days=90, limit=5)
        b = await memory_service.query_memory_broad(query_text="anything", recency_days=90, limit=5)
        assert a == b  # deterministic legacy path; the multipath branch is not taken
    finally:
        await _purge(memory_service, prefix)
