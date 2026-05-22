"""Tests for FRE-375: entity description/type/properties are first-write-wins in create_entity.

The bug being fixed: before FRE-375, create_entity() used unconditional SET clauses
for description, entity_type, and properties. This meant every call (including from
test scripts) would overwrite the existing entity's characterisation in Neo4j.

The fix: CASE WHEN ... IS NULL OR ... = '' semantics so only the first writer sets
the identity fields. Telemetry fields (last_seen, mention_count) continue to update
unconditionally.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from personal_agent.memory.models import Entity


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_service_with_mock() -> tuple:
    """Build a MemoryService bypassing __init__ and return the mock session."""
    from personal_agent.memory.service import MemoryService

    service = MemoryService.__new__(MemoryService)
    service.connected = True
    service._query_feedback_by_key = {}

    mock_session = AsyncMock()
    service.driver = MagicMock()
    service.driver.session = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    return service, mock_session


# ---------------------------------------------------------------------------
# First-write-wins Cypher clause assertions
# ---------------------------------------------------------------------------


class TestEntityDescriptionFirstWriteWins:
    @pytest.mark.asyncio
    async def test_description_clause_is_not_unconditional_overwrite(self) -> None:
        """The description SET clause must not use bare assignment (FRE-375).

        Bare ``e.description = $description`` overwrites existing values on every
        create_entity() call, including calls from test scripts. The clause must
        use CASE WHEN ... IS NULL semantics instead.
        """
        service, mock_session = _make_service_with_mock()

        captured_cypher: list[str] = []

        entity_result = AsyncMock()
        entity_result.single = AsyncMock(return_value={"entity_id": "Python"})

        async def capture_run(cypher: str, **kwargs: object) -> AsyncMock:
            captured_cypher.append(cypher)
            return entity_result

        mock_session.run = AsyncMock(side_effect=capture_run)

        entity = Entity(
            name="Python",
            entity_type="TechnologyConcept",
            description="A programming language",
        )
        await service.create_entity(entity, visibility="group")

        merged = " ".join(captured_cypher)

        # The key assertion: no bare unconditional assignment for description
        assert "e.description = $description" not in merged, (
            "e.description must use CASE WHEN (first-write-wins), "
            "not bare '= $description' which overwrites on every call (FRE-375)"
        )

    @pytest.mark.asyncio
    async def test_entity_type_clause_is_not_unconditional_overwrite(self) -> None:
        """The entity_type SET clause must not use bare assignment (FRE-375)."""
        service, mock_session = _make_service_with_mock()

        captured_cypher: list[str] = []

        entity_result = AsyncMock()
        entity_result.single = AsyncMock(return_value={"entity_id": "Python"})

        async def capture_run(cypher: str, **kwargs: object) -> AsyncMock:
            captured_cypher.append(cypher)
            return entity_result

        mock_session.run = AsyncMock(side_effect=capture_run)

        entity = Entity(name="Python", entity_type="TechnologyConcept")
        await service.create_entity(entity)

        merged = " ".join(captured_cypher)

        assert "e.entity_type = $entity_type" not in merged, (
            "e.entity_type must use CASE WHEN (first-write-wins), "
            "not bare '= $entity_type' (FRE-375)"
        )

    @pytest.mark.asyncio
    async def test_properties_clause_is_not_unconditional_overwrite(self) -> None:
        """The properties SET clause must not use bare assignment (FRE-375)."""
        service, mock_session = _make_service_with_mock()

        captured_cypher: list[str] = []

        entity_result = AsyncMock()
        entity_result.single = AsyncMock(return_value={"entity_id": "Python"})

        async def capture_run(cypher: str, **kwargs: object) -> AsyncMock:
            captured_cypher.append(cypher)
            return entity_result

        mock_session.run = AsyncMock(side_effect=capture_run)

        entity = Entity(name="Python", entity_type="TechnologyConcept")
        await service.create_entity(entity)

        merged = " ".join(captured_cypher)

        assert "e.properties = $properties" not in merged, (
            "e.properties must use CASE WHEN (first-write-wins), "
            "not bare '= $properties' (FRE-375)"
        )

    @pytest.mark.asyncio
    async def test_description_clause_uses_case_when(self) -> None:
        """The MERGE query must use CASE WHEN for description (FRE-375).

        Patches generate_embedding to return None so the dedup code path is
        bypassed and only the MERGE query is captured.
        """
        service, mock_session = _make_service_with_mock()

        captured_cypher: list[str] = []

        entity_result = AsyncMock()
        entity_result.single = AsyncMock(return_value={"entity_id": "Neo4j"})

        async def capture_run(cypher: str, **kwargs: object) -> AsyncMock:
            captured_cypher.append(cypher)
            return entity_result

        mock_session.run = AsyncMock(side_effect=capture_run)

        entity = Entity(
            name="Neo4j",
            entity_type="Database",
            description="A graph database management system",
        )
        # Patch generate_embedding so no embedding is produced → dedup path skipped
        with patch(
            "personal_agent.memory.service.generate_embedding",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await service.create_entity(entity)

        # There should be exactly one MERGE query (no dedup cosine-similarity call)
        merge_queries = [c for c in captured_cypher if "MERGE (e:Entity" in c]
        assert len(merge_queries) >= 1, "Expected at least one MERGE (e:Entity ...) query"

        merge_cypher = merge_queries[-1]

        assert "CASE WHEN" in merge_cypher, (
            "create_entity MERGE query must use CASE WHEN for identity fields (FRE-375)"
        )

    @pytest.mark.asyncio
    async def test_telemetry_fields_still_update_unconditionally(self) -> None:
        """Telemetry fields (last_seen, mention_count) must still update on every call.

        Only identity fields (description, entity_type, properties) are first-write-wins.
        Telemetry fields should remain unconditional updates.
        """
        service, mock_session = _make_service_with_mock()

        captured_cypher: list[str] = []

        entity_result = AsyncMock()
        entity_result.single = AsyncMock(return_value={"entity_id": "Python"})

        async def capture_run(cypher: str, **kwargs: object) -> AsyncMock:
            captured_cypher.append(cypher)
            return entity_result

        mock_session.run = AsyncMock(side_effect=capture_run)

        entity = Entity(name="Python", entity_type="TechnologyConcept")
        await service.create_entity(entity)

        merged = " ".join(captured_cypher)

        # last_seen must be unconditional (always update)
        assert "e.last_seen = datetime()" in merged, (
            "e.last_seen must update unconditionally on every create_entity() call"
        )
        # mention_count must increment unconditionally
        assert "e.mention_count = COALESCE(e.mention_count, 0) + 1" in merged, (
            "e.mention_count must increment unconditionally on every create_entity() call"
        )
