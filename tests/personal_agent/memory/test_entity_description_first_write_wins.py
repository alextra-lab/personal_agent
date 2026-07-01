"""Tests for FRE-375 first-write-wins in create_entity — as amended by FRE-711.

FRE-375 made description/entity_type/properties first-write-wins so a test script
could not overwrite an entity's characterisation. FRE-711 then made the **description**
a *living* value (ADR-0098 D2): it is now correctable by a strictly-higher-confidence
write, and the anti-test-overwrite guarantee moved from the freeze to an **eval-mode
gate** (an eval write never overwrites a non-eval description). ``entity_type`` and
``properties`` remain first-write-wins.

So these tests keep the shape assertions for entity_type/properties, and assert the
description is now the gated living value (not the old freeze). The behavioural
anti-clobber guarantee (eval cannot overwrite non-eval) is proven against live Neo4j
in ``test_world_description_correction.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    """entity_type/properties stay first-write-wins; description is now the FRE-711 living value."""

    @pytest.mark.asyncio
    async def test_description_is_gated_living_value_not_frozen(self) -> None:
        """The description is now a gated living value, not the FRE-375 freeze (FRE-711).

        The blanket first-write freeze (``IS NULL OR = ''``) is gone; the SET is gated
        on the ``_do_correct``/``_do_fill`` decision, and a superseded value is archived.
        Bare unconditional ``e.description = $description`` remains forbidden.
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
        # Zero embedding → dedup path skipped → the MERGE (with the gate) is captured.
        with patch(
            "personal_agent.memory.service.generate_embedding",
            new=AsyncMock(return_value=[0.0, 0.0]),
        ):
            await service.create_entity(entity, visibility="group")

        merged = " ".join(captured_cypher)

        # The old FRE-375 first-write freeze is retired for the description (the on-MATCH
        # SET no longer guards with IS NULL OR = ''); ON CREATE still sets the first value.
        assert "e.description = CASE WHEN e.description IS NULL OR e.description = ''" not in merged
        # It is the FRE-711 gated living value with a superseded-history archive.
        assert "_do_correct OR _do_fill" in merged
        assert "HAD_DESCRIPTION" in merged

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
            "e.properties must use CASE WHEN (first-write-wins), not bare '= $properties' (FRE-375)"
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
