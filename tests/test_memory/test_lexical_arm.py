"""Integration tests for the lexical full-text recall arm (FRE-723, ADR-0104).

These require the test Neo4j substrate (``make test-infra-up``, :7688) and skip
cleanly when it is unavailable. Mirrors the FRE-707 structural-arm test
conventions (tests/test_memory/test_structural_arm.py).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from personal_agent.config.settings import get_settings
from personal_agent.memory.service import MemoryService


@pytest_asyncio.fixture
async def memory_service():
    """Connect to the test Neo4j substrate; skip if unavailable."""
    service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
    connected = await service.connect()
    if not connected:
        pytest.skip("Neo4j not available (make test-infra-up)")
    await service.ensure_fulltext_index()
    yield service
    await service.disconnect()


async def _seed_turn(
    service: MemoryService, *, turn_id: str, user_message: str, visibility: str = "public"
) -> None:
    async with service.driver.session() as session:
        await session.run(
            """
            MERGE (t:Turn {turn_id: $turn_id})
            SET t.user_message = $user_message,
                t.timestamp = $timestamp,
                t.visibility = $visibility
            """,
            turn_id=turn_id,
            user_message=user_message,
            timestamp=_now_iso(),
            visibility=visibility,
        )


async def _seed_entity(
    service: MemoryService,
    *,
    name: str,
    entity_type: str = "Technology",
    visibility: str = "public",
) -> None:
    async with service.driver.session() as session:
        await session.run(
            """
            MERGE (e:Entity {name: $name})
            SET e.entity_type = $entity_type,
                e.last_seen = $now,
                e.first_seen = $now,
                e.mention_count = 1,
                e.visibility = $visibility
            """,
            name=name,
            entity_type=entity_type,
            now=_now_iso(),
            visibility=visibility,
        )


async def _purge(service: MemoryService, prefix: str) -> None:
    async with service.driver.session() as session:
        await session.run(
            "MATCH (n) WHERE n.name STARTS WITH $p OR n.turn_id STARTS WITH $p DETACH DELETE n",
            p=prefix,
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.mark.asyncio
async def test_arm_gated_off_returns_empty(memory_service, monkeypatch):
    """Flag-dark: the arm returns nothing while lexical_arm_enabled is off."""
    prefix = f"fre723-{uuid.uuid4()}"
    rare_token = f"{prefix}-xylophonic"
    await _seed_turn(memory_service, turn_id=f"{prefix}-t1", user_message=f"about {rare_token}")
    monkeypatch.setattr(get_settings(), "lexical_arm_enabled", False, raising=False)
    try:
        result = await memory_service.lexical_recall_arm(rare_token)
        assert result == []
    finally:
        await _purge(memory_service, prefix)


@pytest.mark.asyncio
async def test_rare_token_in_turn_content_is_found(memory_service, monkeypatch):
    """AC-1: an exact rare token in Turn.user_message is found and ranked."""
    prefix = f"fre723-{uuid.uuid4()}"
    rare_token = f"{prefix}xylophonic"
    turn_id = f"{prefix}-t1"
    await _seed_turn(memory_service, turn_id=turn_id, user_message=f"tell me about {rare_token}")
    monkeypatch.setattr(get_settings(), "lexical_arm_enabled", True, raising=False)
    try:
        result = await memory_service.lexical_recall_arm(rare_token)
        item_ids = {r.item_id for r in result}
        assert turn_id in item_ids
    finally:
        await _purge(memory_service, prefix)


@pytest.mark.asyncio
async def test_entity_name_match_returns_element_id_not_name(memory_service, monkeypatch):
    """item_id is the Entity elementId, never the free-text name."""
    prefix = f"fre723-{uuid.uuid4()}"
    entity_name = f"{prefix}-vision"
    await _seed_entity(memory_service, name=entity_name)
    monkeypatch.setattr(get_settings(), "lexical_arm_enabled", True, raising=False)
    try:
        result = await memory_service.lexical_recall_arm(entity_name)
        assert len(result) >= 1
        item_ids = {r.item_id for r in result}
        assert entity_name not in item_ids
    finally:
        await _purge(memory_service, prefix)


@pytest.mark.asyncio
async def test_depth_bound_respected(memory_service, monkeypatch):
    """AC-3: result length never exceeds the configured top_k."""
    prefix = f"fre723-{uuid.uuid4()}"
    shared_token = f"{prefix}-widget"
    for i in range(5):
        await _seed_turn(
            memory_service, turn_id=f"{prefix}-t{i}", user_message=f"discussing {shared_token}"
        )
    monkeypatch.setattr(get_settings(), "lexical_arm_enabled", True, raising=False)
    try:
        result = await memory_service.lexical_recall_arm(shared_token, limit=3)
        assert len(result) <= 3
    finally:
        await _purge(memory_service, prefix)


@pytest.mark.asyncio
async def test_result_is_1_based_ranked(memory_service, monkeypatch):
    """Results carry sequential 1-based rank, best-first."""
    prefix = f"fre723-{uuid.uuid4()}"
    shared_token = f"{prefix}-gadget"
    for i in range(3):
        await _seed_turn(
            memory_service, turn_id=f"{prefix}-t{i}", user_message=f"discussing {shared_token}"
        )
    monkeypatch.setattr(get_settings(), "lexical_arm_enabled", True, raising=False)
    try:
        result = await memory_service.lexical_recall_arm(shared_token)
        assert [r.rank for r in result] == list(range(1, len(result) + 1))
    finally:
        await _purge(memory_service, prefix)


@pytest.mark.asyncio
async def test_lucene_special_characters_do_not_raise(memory_service, monkeypatch):
    """Free text with Lucene-meaningful punctuation must not raise a parse error."""
    monkeypatch.setattr(get_settings(), "lexical_arm_enabled", True, raising=False)
    result = await memory_service.lexical_recall_arm('trace_id: (abc) "def"')
    assert result == []
