# tests/personal_agent/memory/test_dedup.py
"""Tests for fuzzy entity deduplication.

The dedup pipeline checks vector similarity before MERGE to prevent
near-duplicate explosion (40 mentions → 500 nodes → should be ~10).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.memory.dedup import (
    DedupDecision,
    DedupResult,
    _find_similar_entities,
    check_entity_duplicate,
)


@pytest.fixture(autouse=True)
def _pin_dedup_settings() -> None:
    """Pin dedup threshold so tests are independent of config."""
    mock_settings = MagicMock()
    mock_settings.dedup_similarity_threshold = 0.85
    with patch("personal_agent.memory.dedup.get_settings", return_value=mock_settings):
        yield  # type: ignore[misc]


class TestCheckEntityDuplicate:
    @pytest.mark.asyncio
    async def test_no_existing_entities_no_dedup(self) -> None:
        """No existing entities → create new (no duplicate)."""
        with patch(
            "personal_agent.memory.dedup._find_similar_entities",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await check_entity_duplicate(
                name="PostgreSQL",
                entity_type="Technology",
                embedding=[0.1] * 1536,
                neo4j_session=AsyncMock(),
            )
        assert result.decision == DedupDecision.CREATE_NEW
        assert result.canonical_name is None

    @pytest.mark.asyncio
    async def test_exact_match_merges(self) -> None:
        """Exact name match → merge with existing."""
        with patch(
            "personal_agent.memory.dedup._find_similar_entities",
            new_callable=AsyncMock,
            return_value=[{"name": "PostgreSQL", "similarity": 1.0, "entity_type": "Technology"}],
        ):
            result = await check_entity_duplicate(
                name="PostgreSQL",
                entity_type="Technology",
                embedding=[0.1] * 1536,
                neo4j_session=AsyncMock(),
            )
        assert result.decision == DedupDecision.MERGE_EXISTING
        assert result.canonical_name == "PostgreSQL"

    @pytest.mark.asyncio
    async def test_high_similarity_merges(self) -> None:
        """Above threshold similarity → merge with canonical name."""
        with patch(
            "personal_agent.memory.dedup._find_similar_entities",
            new_callable=AsyncMock,
            return_value=[{"name": "Postgres", "similarity": 0.92, "entity_type": "Technology"}],
        ):
            result = await check_entity_duplicate(
                name="PostgreSQL Database",
                entity_type="Technology",
                embedding=[0.1] * 1536,
                neo4j_session=AsyncMock(),
            )
        assert result.decision == DedupDecision.MERGE_EXISTING
        assert result.canonical_name == "Postgres"

    @pytest.mark.asyncio
    async def test_low_similarity_creates_new(self) -> None:
        """Below threshold similarity → create new entity."""
        with patch(
            "personal_agent.memory.dedup._find_similar_entities",
            new_callable=AsyncMock,
            return_value=[{"name": "Redis", "similarity": 0.3, "entity_type": "Technology"}],
        ):
            result = await check_entity_duplicate(
                name="PostgreSQL",
                entity_type="Technology",
                embedding=[0.1] * 1536,
                neo4j_session=AsyncMock(),
            )
        assert result.decision == DedupDecision.CREATE_NEW


class TestFindSimilarEntities:
    """Tests for the Cypher candidate query itself."""

    @pytest.mark.asyncio
    async def test_query_excludes_user_id_bound_nodes(self) -> None:
        """Cypher must filter out user_id-bound :Person nodes (FRE-342).

        Owner/user-anchored :Person nodes (FRE-213 schema) must never appear
        as merge candidates for extracted entities, otherwise an extracted
        "Alex" would collide into the harness owner Person and destroy the
        user_id anchor invariant.
        """
        session = AsyncMock()
        result_obj = AsyncMock()
        result_obj.data = AsyncMock(return_value=[])
        session.run = AsyncMock(return_value=result_obj)

        await _find_similar_entities(
            embedding=[0.1] * 1536,
            entity_type="Person",
            neo4j_session=session,
            top_k=5,
        )

        session.run.assert_awaited_once()
        cypher = session.run.await_args.args[0]
        assert "node.user_id IS NULL" in cypher


class TestDedupResult:
    def test_create_new(self) -> None:
        result = DedupResult(decision=DedupDecision.CREATE_NEW)
        assert result.canonical_name is None

    def test_merge_existing(self) -> None:
        result = DedupResult(
            decision=DedupDecision.MERGE_EXISTING,
            canonical_name="PostgreSQL",
            similarity_score=0.95,
        )
        assert result.canonical_name == "PostgreSQL"
