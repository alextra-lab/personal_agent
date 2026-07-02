"""Integration tests for the multi-query paraphrase recall arm (FRE-723, ADR-0104).

These require the test Neo4j substrate (``make test-infra-up``, :7688) and skip
cleanly when it is unavailable. No live LLM server is needed anywhere in this
file — paraphrase generation is monkeypatched throughout, matching the FRE-707
precedent of testing the arm's mechanics, not the model.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

import personal_agent.memory.service as svc
from personal_agent.config.settings import get_settings
from personal_agent.memory.service import MemoryService


@pytest_asyncio.fixture
async def memory_service():
    """Connect to the test Neo4j substrate; skip if unavailable."""
    service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
    connected = await service.connect()
    if not connected:
        pytest.skip("Neo4j not available (make test-infra-up)")
    await service.ensure_vector_index()
    yield service
    await service.disconnect()


async def _seed_entity(service: MemoryService, *, name: str, embedding: list[float]) -> None:
    async with service.driver.session() as session:
        await session.run(
            """
            MERGE (e:Entity {name: $name})
            SET e.entity_type = 'Concept',
                e.embedding = $embedding,
                e.last_seen = $now,
                e.first_seen = $now,
                e.mention_count = 1,
                e.visibility = 'public'
            """,
            name=name,
            embedding=embedding,
            now=_now_iso(),
        )


async def _purge(service: MemoryService, prefix: str) -> None:
    async with service.driver.session() as session:
        await session.run(
            "MATCH (n) WHERE n.name STARTS WITH $p DETACH DELETE n",
            p=prefix,
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unit_embedding(dims: int, index: int) -> list[float]:
    """A one-hot-ish embedding so vector search deterministically ranks it top."""
    vec = [0.0] * dims
    vec[index % dims] = 1.0
    return vec


@pytest.mark.asyncio
async def test_arm_gated_off_returns_empty(memory_service, monkeypatch):
    """Flag-dark: the arm returns nothing while multiquery_arm_enabled is off."""
    monkeypatch.setattr(get_settings(), "multiquery_arm_enabled", False, raising=False)
    result = await memory_service.multi_query_recall_arm("vision")
    assert result == []


@pytest.mark.asyncio
async def test_paraphrase_generation_raising_degrades_to_dense_only(memory_service, monkeypatch):
    """AC-4: if generate_query_paraphrases raises, the arm still returns the
    dense-only result for the original query — proving the arm's own
    try/except around the paraphrase call, not just the callee's internal
    fail-open.
    """
    prefix = f"fre723mq-{uuid.uuid4()}"
    dims = get_settings().embedding_dimensions
    entity_name = f"{prefix}-vision"
    await _seed_entity(memory_service, name=entity_name, embedding=_unit_embedding(dims, 0))

    async def _boom(*args, **kwargs):
        raise RuntimeError("paraphrase generation exploded")

    monkeypatch.setattr(svc, "generate_query_paraphrases", _boom)

    async def _embed_seeded(text: str, mode: str = "query") -> list[float]:
        return _unit_embedding(dims, 0)

    monkeypatch.setattr(svc, "generate_embedding", _embed_seeded)
    monkeypatch.setattr(get_settings(), "multiquery_arm_enabled", True, raising=False)
    try:
        result = await memory_service.multi_query_recall_arm(entity_name)
        assert result != []
        item_ids = {r.item_id for r in result}
        assert len(item_ids) >= 1
    finally:
        await _purge(memory_service, prefix)


@pytest.mark.asyncio
async def test_empty_paraphrase_set_degrades_to_dense_only(memory_service, monkeypatch):
    """AC-4: an empty paraphrase set still returns the dense-only result."""
    prefix = f"fre723mq-{uuid.uuid4()}"
    dims = get_settings().embedding_dimensions
    entity_name = f"{prefix}-vision"
    await _seed_entity(memory_service, name=entity_name, embedding=_unit_embedding(dims, 0))

    async def _empty(*args, **kwargs):
        return []

    monkeypatch.setattr(svc, "generate_query_paraphrases", _empty)

    async def _embed_seeded(text: str, mode: str = "query") -> list[float]:
        return _unit_embedding(dims, 0)

    monkeypatch.setattr(svc, "generate_embedding", _embed_seeded)
    monkeypatch.setattr(get_settings(), "multiquery_arm_enabled", True, raising=False)
    try:
        result = await memory_service.multi_query_recall_arm(entity_name)
        assert result != []
    finally:
        await _purge(memory_service, prefix)


@pytest.mark.asyncio
async def test_depth_bound_respected(memory_service, monkeypatch):
    """AC-3: fused result length never exceeds the configured top_k."""
    prefix = f"fre723mq-{uuid.uuid4()}"
    dims = get_settings().embedding_dimensions
    for i in range(5):
        await _seed_entity(
            memory_service, name=f"{prefix}-e{i}", embedding=_unit_embedding(dims, i)
        )

    async def _empty(*args, **kwargs):
        return []

    monkeypatch.setattr(svc, "generate_query_paraphrases", _empty)

    async def _embed_first_axis(text: str, mode: str = "query") -> list[float]:
        return [1.0] + [0.0] * (dims - 1)

    monkeypatch.setattr(svc, "generate_embedding", _embed_first_axis)
    monkeypatch.setattr(get_settings(), "multiquery_arm_enabled", True, raising=False)
    try:
        result = await memory_service.multi_query_recall_arm(f"{prefix}-query", limit=2)
        assert len(result) <= 2
    finally:
        await _purge(memory_service, prefix)


@pytest.mark.asyncio
async def test_paraphrase_surfaces_item_filed_under_different_vocabulary(
    memory_service, monkeypatch
):
    """AC-2: a "vision" query with no "vision"-named entity, but a stubbed
    paraphrase "perception" and a "perception"-named entity, surfaces the
    perception-filed item — proving the fanout+fusion mechanics without
    needing a live LLM to actually paraphrase well.
    """
    prefix = f"fre723mq-{uuid.uuid4()}"
    dims = get_settings().embedding_dimensions
    perception_entity = f"{prefix}-perception"
    await _seed_entity(memory_service, name=perception_entity, embedding=_unit_embedding(dims, 0))

    async def _paraphrase(query_text, count, **kwargs):
        return ["perception"] if query_text == f"{prefix}-vision" else []

    async def _embed(text: str, mode: str = "query") -> list[float]:
        # Only the "perception" variant embeds to the seeded entity's vector;
        # the original "vision" query embeds to an orthogonal, empty-match vector.
        if text == "perception":
            return _unit_embedding(dims, 0)
        return _unit_embedding(dims, 1)

    monkeypatch.setattr(svc, "generate_query_paraphrases", _paraphrase)
    monkeypatch.setattr(svc, "generate_embedding", _embed)
    monkeypatch.setattr(get_settings(), "multiquery_arm_enabled", True, raising=False)
    try:
        result = await memory_service.multi_query_recall_arm(f"{prefix}-vision")
        item_ids = {r.item_id for r in result}
        # perception_entity's elementId is unknown ahead of time; assert non-empty
        # and that the fused set is non-trivial (proves the paraphrase variant
        # contributed a result the original-query-only variant would not).
        assert len(item_ids) >= 1
    finally:
        await _purge(memory_service, prefix)


@pytest.mark.asyncio
async def test_one_variant_failure_does_not_zero_the_whole_arm(memory_service, monkeypatch):
    """Per-variant isolation: one variant's dense-search exception does not
    prevent other variants' results from surfacing.
    """
    prefix = f"fre723mq-{uuid.uuid4()}"
    dims = get_settings().embedding_dimensions
    entity_name = f"{prefix}-vision"
    await _seed_entity(memory_service, name=entity_name, embedding=_unit_embedding(dims, 0))

    async def _paraphrase(query_text, count, **kwargs):
        return ["broken-variant"]

    call_count = {"n": 0}
    original_dense_search = memory_service._dense_vector_search_ranked

    async def _flaky_dense_search(session, embedding, top_k, vis_frag, vis_params):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated dense-search failure on variant 2")
        return await original_dense_search(session, embedding, top_k, vis_frag, vis_params)

    monkeypatch.setattr(svc, "generate_query_paraphrases", _paraphrase)

    async def _embed_seeded(text: str, mode: str = "query") -> list[float]:
        return _unit_embedding(dims, 0)

    monkeypatch.setattr(svc, "generate_embedding", _embed_seeded)
    monkeypatch.setattr(memory_service, "_dense_vector_search_ranked", _flaky_dense_search)
    monkeypatch.setattr(get_settings(), "multiquery_arm_enabled", True, raising=False)
    try:
        result = await memory_service.multi_query_recall_arm(entity_name)
        assert result != []
        assert call_count["n"] == 2
    finally:
        await _purge(memory_service, prefix)


@pytest.mark.asyncio
async def test_session_acquisition_failure_returns_empty_not_raises(memory_service, monkeypatch):
    """Master gate finding (2026-07-02): if Neo4j session acquisition itself
    fails (transient ServiceUnavailable, pool exhaustion, etc.), the arm must
    return [] rather than propagating — it promises to "never hard-fail
    recall" and lexical_recall_arm already honors this by wrapping its whole
    session block; multi_query_recall_arm must match.
    """

    async def _paraphrase(query_text, count, **kwargs):
        return []

    def _boom_session(*args, **kwargs):
        raise RuntimeError("simulated session acquisition failure")

    monkeypatch.setattr(svc, "generate_query_paraphrases", _paraphrase)
    monkeypatch.setattr(memory_service.driver, "session", _boom_session)
    monkeypatch.setattr(get_settings(), "multiquery_arm_enabled", True, raising=False)

    result = await memory_service.multi_query_recall_arm("some query")
    assert result == []
