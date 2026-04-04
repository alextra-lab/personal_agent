"""Unit tests for memory.accessed event publishing across all query paths (FRE-163).

Verifies that:
- Each query path publishes MemoryAccessedEvent with the correct payload.
- Events are suppressed when freshness_enabled=False.
- Correct AccessContext discriminator is used per path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.events import (
    AccessContext,
    MemoryAccessedEvent,
    STREAM_MEMORY_ACCESSED,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entity_node(name: str) -> MagicMock:
    """Create a minimal entity node dict mirroring Neo4j record shape."""
    node = MagicMock()
    node.get = lambda k, default=None: {
        "name": name,
        "entity_type": "Topic",
        "description": f"{name} description",
        "type": "Topic",
        "mentions": 3,
    }.get(k, default)
    return node


# ---------------------------------------------------------------------------
# service.query_memory
# ---------------------------------------------------------------------------


class TestQueryMemoryEventPublishing:
    """Tests for MemoryAccessedEvent publishing in MemoryService.query_memory."""

    @pytest.mark.asyncio
    async def test_publishes_event_when_freshness_enabled(self) -> None:
        """query_memory publishes MemoryAccessedEvent when freshness_enabled=True."""
        from personal_agent.memory.models import MemoryQuery, MemoryQueryResult
        from personal_agent.memory.service import MemoryService

        service = MemoryService()
        service.connected = True
        service.driver = MagicMock()

        mock_session = AsyncMock()
        mock_result = AsyncMock()
        mock_result.values = AsyncMock(return_value=[])
        mock_result.data = AsyncMock(return_value=[])
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        service.driver.session = MagicMock(return_value=mock_session)

        published_events: list[tuple[str, MemoryAccessedEvent]] = []

        async def capture_publish(stream: str, event: object) -> None:
            if isinstance(event, MemoryAccessedEvent):
                published_events.append((stream, event))

        mock_bus = AsyncMock()
        mock_bus.publish = AsyncMock(side_effect=capture_publish)

        query = MemoryQuery(entity_names=["Python", "FastAPI"], limit=5)

        with (
            patch("personal_agent.memory.service.settings") as mock_settings,
            patch("personal_agent.memory.service.get_event_bus", return_value=mock_bus),
        ):
            mock_settings.freshness_enabled = True
            mock_settings.reranker_enabled = False
            mock_settings.embedding_dimensions = 768

            await service.query_memory(
                query,
                trace_id="trace-abc",
                session_id="session-xyz",
                access_context=AccessContext.SEARCH,
            )

        assert len(published_events) == 1
        stream, event = published_events[0]
        assert stream == STREAM_MEMORY_ACCESSED
        assert event.access_context == AccessContext.SEARCH
        assert event.query_type == "query_memory"
        assert event.trace_id == "trace-abc"
        assert event.session_id == "session-xyz"
        assert "Python" in event.entity_ids
        assert "FastAPI" in event.entity_ids
        assert event.relationship_ids == []

    @pytest.mark.asyncio
    async def test_suppresses_event_when_freshness_disabled(self) -> None:
        """query_memory does NOT publish when freshness_enabled=False."""
        from personal_agent.memory.models import MemoryQuery
        from personal_agent.memory.service import MemoryService

        service = MemoryService()
        service.connected = True
        service.driver = MagicMock()

        mock_session = AsyncMock()
        mock_result = AsyncMock()
        mock_result.values = AsyncMock(return_value=[])
        mock_result.data = AsyncMock(return_value=[])
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        service.driver.session = MagicMock(return_value=mock_session)

        mock_bus = AsyncMock()

        query = MemoryQuery(entity_names=["Python"], limit=5)

        with (
            patch("personal_agent.memory.service.settings") as mock_settings,
            patch("personal_agent.memory.service.get_event_bus", return_value=mock_bus),
        ):
            mock_settings.freshness_enabled = False
            mock_settings.reranker_enabled = False
            mock_settings.embedding_dimensions = 768

            await service.query_memory(query, trace_id="trace-abc")

        mock_bus.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_suppresses_event_when_no_trace_id(self) -> None:
        """query_memory does NOT publish when trace_id is None."""
        from personal_agent.memory.models import MemoryQuery
        from personal_agent.memory.service import MemoryService

        service = MemoryService()
        service.connected = True
        service.driver = MagicMock()

        mock_session = AsyncMock()
        mock_result = AsyncMock()
        mock_result.values = AsyncMock(return_value=[])
        mock_result.data = AsyncMock(return_value=[])
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        service.driver.session = MagicMock(return_value=mock_session)

        mock_bus = AsyncMock()

        query = MemoryQuery(entity_names=["Python"], limit=5)

        with (
            patch("personal_agent.memory.service.settings") as mock_settings,
            patch("personal_agent.memory.service.get_event_bus", return_value=mock_bus),
        ):
            mock_settings.freshness_enabled = True
            mock_settings.reranker_enabled = False
            mock_settings.embedding_dimensions = 768

            await service.query_memory(query, trace_id=None)

        mock_bus.publish.assert_not_called()


# ---------------------------------------------------------------------------
# service.query_memory_broad
# ---------------------------------------------------------------------------


class TestQueryMemoryBroadEventPublishing:
    """Tests for MemoryAccessedEvent publishing in MemoryService.query_memory_broad."""

    @pytest.mark.asyncio
    async def test_publishes_event_with_entity_names(self) -> None:
        """query_memory_broad publishes event with entity names as IDs."""
        from personal_agent.memory.service import MemoryService

        service = MemoryService()
        service.connected = True
        service.driver = MagicMock()

        entities_data = [
            {"name": "Greece", "type": "Location", "description": "A country", "mentions": 5},
            {"name": "Athens", "type": "Location", "description": "Capital", "mentions": 3},
        ]

        mock_session = AsyncMock()
        mock_entity_result = AsyncMock()
        mock_entity_result.data = AsyncMock(return_value=entities_data)
        mock_session_result = AsyncMock()
        mock_session_result.data = AsyncMock(return_value=[])
        mock_turn_result = AsyncMock()
        mock_turn_result.data = AsyncMock(return_value=[])
        mock_session.run = AsyncMock(
            side_effect=[mock_entity_result, mock_session_result, mock_turn_result]
        )
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        service.driver.session = MagicMock(return_value=mock_session)

        published_events: list[tuple[str, MemoryAccessedEvent]] = []

        async def capture_publish(stream: str, event: object) -> None:
            if isinstance(event, MemoryAccessedEvent):
                published_events.append((stream, event))

        mock_bus = AsyncMock()
        mock_bus.publish = AsyncMock(side_effect=capture_publish)

        with (
            patch("personal_agent.memory.service.settings") as mock_settings,
            patch("personal_agent.memory.service.get_event_bus", return_value=mock_bus),
        ):
            mock_settings.freshness_enabled = True

            await service.query_memory_broad(
                trace_id="trace-broad-1",
                session_id="session-1",
                access_context=AccessContext.CONTEXT_ASSEMBLY,
            )

        assert len(published_events) == 1
        stream, event = published_events[0]
        assert stream == STREAM_MEMORY_ACCESSED
        assert event.access_context == AccessContext.CONTEXT_ASSEMBLY
        assert event.query_type == "query_memory_broad"
        assert event.trace_id == "trace-broad-1"
        assert event.session_id == "session-1"
        assert "Greece" in event.entity_ids
        assert "Athens" in event.entity_ids

    @pytest.mark.asyncio
    async def test_suppresses_when_freshness_disabled(self) -> None:
        """query_memory_broad does NOT publish when freshness_enabled=False."""
        from personal_agent.memory.service import MemoryService

        service = MemoryService()
        service.connected = True
        service.driver = MagicMock()

        mock_session = AsyncMock()
        mock_result = AsyncMock()
        mock_result.data = AsyncMock(return_value=[])
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        service.driver.session = MagicMock(return_value=mock_session)

        mock_bus = AsyncMock()

        with (
            patch("personal_agent.memory.service.settings") as mock_settings,
            patch("personal_agent.memory.service.get_event_bus", return_value=mock_bus),
        ):
            mock_settings.freshness_enabled = False

            await service.query_memory_broad(trace_id="trace-broad-2")

        mock_bus.publish.assert_not_called()


# ---------------------------------------------------------------------------
# protocol_adapter.recall  and  recall_broad
# ---------------------------------------------------------------------------


class TestProtocolAdapterAccessContext:
    """Tests that MemoryServiceAdapter passes CONTEXT_ASSEMBLY to service calls."""

    @pytest.mark.asyncio
    async def test_recall_passes_context_assembly(self) -> None:
        """recall() passes AccessContext.CONTEXT_ASSEMBLY and trace_id to query_memory."""
        from personal_agent.memory.protocol import MemoryRecallQuery
        from personal_agent.memory.protocol_adapter import MemoryServiceAdapter
        from personal_agent.memory.service import MemoryService

        mock_service = AsyncMock(spec=MemoryService)
        mock_service.query_memory = AsyncMock()
        mock_service.query_memory.return_value = MagicMock(
            conversations=[], entities=[], relevance_scores={}
        )

        adapter = MemoryServiceAdapter(mock_service)
        query = MemoryRecallQuery(entity_names=["Claude"], limit=5)
        await adapter.recall(query, trace_id="trace-recall-1")

        mock_service.query_memory.assert_called_once()
        call_kwargs = mock_service.query_memory.call_args.kwargs
        assert call_kwargs["access_context"] == AccessContext.CONTEXT_ASSEMBLY
        assert call_kwargs["trace_id"] == "trace-recall-1"

    @pytest.mark.asyncio
    async def test_recall_broad_passes_context_assembly(self) -> None:
        """recall_broad() passes AccessContext.CONTEXT_ASSEMBLY and trace_id to query_memory_broad."""
        from personal_agent.memory.protocol_adapter import MemoryServiceAdapter
        from personal_agent.memory.service import MemoryService

        mock_service = AsyncMock(spec=MemoryService)
        mock_service.query_memory_broad = AsyncMock(
            return_value={"entities": [], "sessions": [], "turns_summary": []}
        )

        adapter = MemoryServiceAdapter(mock_service)
        await adapter.recall_broad(
            entity_types=["Location"],
            recency_days=30,
            limit=10,
            trace_id="trace-broad-ctx",
        )

        mock_service.query_memory_broad.assert_called_once()
        call_kwargs = mock_service.query_memory_broad.call_args.kwargs
        assert call_kwargs["access_context"] == AccessContext.CONTEXT_ASSEMBLY
        assert call_kwargs["trace_id"] == "trace-broad-ctx"


# ---------------------------------------------------------------------------
# tools/memory_search.py
# ---------------------------------------------------------------------------


class TestMemorySearchToolAccessContext:
    """Tests that search_memory_executor passes AccessContext.TOOL_CALL."""

    @pytest.mark.asyncio
    async def test_entity_path_passes_tool_call(self) -> None:
        """search_memory_executor passes TOOL_CALL context on entity-match path."""
        from personal_agent.tools.memory_search import search_memory_executor

        mock_service = AsyncMock()
        mock_query_result = MagicMock()
        mock_query_result.conversations = []
        mock_query_result.entities = []
        mock_service.connected = True
        mock_service.query_memory = AsyncMock(return_value=mock_query_result)

        ctx = MagicMock()
        ctx.trace_id = "trace-tool-1"

        with patch(
            "personal_agent.service.app.memory_service",
            mock_service,
            create=True,
        ):
            await search_memory_executor(
                query_text="Python async",
                entity_names=["Python"],
                ctx=ctx,
            )

        mock_service.query_memory.assert_called_once()
        call_kwargs = mock_service.query_memory.call_args.kwargs
        assert call_kwargs.get("access_context") == AccessContext.TOOL_CALL
        assert call_kwargs.get("trace_id") == "trace-tool-1"

    @pytest.mark.asyncio
    async def test_broad_path_passes_tool_call(self) -> None:
        """search_memory_executor passes TOOL_CALL context on broad-recall path."""
        from personal_agent.tools.memory_search import search_memory_executor

        mock_service = AsyncMock()
        mock_service.connected = True
        mock_service.query_memory_broad = AsyncMock(
            return_value={"entities": [], "sessions": [], "turns_summary": []}
        )

        ctx = MagicMock()
        ctx.trace_id = "trace-tool-2"

        with patch(
            "personal_agent.service.app.memory_service",
            mock_service,
            create=True,
        ):
            # "everything" triggers broad-recall heuristic with no entity_types filter
            await search_memory_executor(
                query_text="show me everything",
                entity_types=[],
                ctx=ctx,
            )

        mock_service.query_memory_broad.assert_called_once()
        call_kwargs = mock_service.query_memory_broad.call_args.kwargs
        assert call_kwargs.get("access_context") == AccessContext.TOOL_CALL
        assert call_kwargs.get("trace_id") == "trace-tool-2"


# ---------------------------------------------------------------------------
# consolidator
# ---------------------------------------------------------------------------


class TestConsolidatorAccessContext:
    """Tests that SecondBrainConsolidator publishes CONSOLIDATION MemoryAccessedEvent."""

    @pytest.mark.asyncio
    async def test_publishes_consolidation_event_when_freshness_enabled(self) -> None:
        """consolidate_recent_captures publishes CONSOLIDATION event when freshness_enabled=True."""
        from personal_agent.second_brain.consolidator import SecondBrainConsolidator

        mock_service = AsyncMock()
        mock_service.connected = True
        mock_service.turn_exists = AsyncMock(return_value=False)
        mock_service.get_promotion_candidates = AsyncMock(return_value=[])

        consolidator = SecondBrainConsolidator(memory_service=mock_service)

        published_events: list[tuple[str, MemoryAccessedEvent]] = []

        async def capture_publish(stream: str, event: object) -> None:
            if isinstance(event, MemoryAccessedEvent):
                published_events.append((stream, event))

        mock_bus = AsyncMock()
        mock_bus.publish = AsyncMock(side_effect=capture_publish)

        with (
            patch("personal_agent.second_brain.consolidator.read_captures", return_value=[]),
            patch("personal_agent.second_brain.consolidator.get_event_bus", return_value=mock_bus),
            patch("personal_agent.second_brain.consolidator.get_settings") as mock_get_settings,
        ):
            mock_settings = MagicMock()
            mock_settings.freshness_enabled = True
            mock_get_settings.return_value = mock_settings

            await consolidator.consolidate_recent_captures(days=1)

        # With no captures, all_entity_ids is empty → no event
        assert len(published_events) == 0

    @pytest.mark.asyncio
    async def test_consolidation_event_has_correct_payload(self) -> None:
        """MemoryAccessedEvent from consolidator uses CONSOLIDATION context and entity IDs."""
        from personal_agent.captains_log.capture import TaskCapture
        from personal_agent.second_brain.consolidator import SecondBrainConsolidator
        from datetime import datetime, timezone

        mock_service = AsyncMock()
        mock_service.connected = True
        mock_service.turn_exists = AsyncMock(return_value=False)
        mock_service.create_conversation = AsyncMock(return_value=True)
        mock_service.create_entity = AsyncMock(side_effect=["entity-1", "entity-2"])
        mock_service.create_relationship = AsyncMock(return_value=True)
        mock_service.get_promotion_candidates = AsyncMock(return_value=[])
        mock_service.create_session = AsyncMock(return_value=True)
        mock_service.link_session_turns = AsyncMock(return_value=1)
        mock_service.driver = MagicMock()
        mock_db_session = AsyncMock()
        mock_result = AsyncMock()
        mock_result.values = AsyncMock(return_value=[])
        mock_db_session.run = AsyncMock(return_value=mock_result)
        mock_db_session.__aenter__ = AsyncMock(return_value=mock_db_session)
        mock_db_session.__aexit__ = AsyncMock(return_value=None)
        mock_service.driver.session = MagicMock(return_value=mock_db_session)

        capture = TaskCapture(
            trace_id="trace-consolidation-1",
            session_id="session-con-1",
            timestamp=datetime.now(timezone.utc),
            user_message="Tell me about FastAPI",
            assistant_response="FastAPI is a web framework.",
            tools_used=[],
            duration_ms=100,
            outcome="success",
        )

        extraction = {
            "summary": "Discussion about FastAPI framework",
            "entities": [
                {"name": "FastAPI", "type": "Technology", "description": "Web framework"},
                {"name": "Python", "type": "Technology", "description": "Language"},
            ],
            "entity_names": ["FastAPI", "Python"],
            "relationships": [],
        }

        consolidator = SecondBrainConsolidator(memory_service=mock_service)

        published_events: list[tuple[str, MemoryAccessedEvent]] = []

        async def capture_publish(stream: str, event: object) -> None:
            if isinstance(event, MemoryAccessedEvent):
                published_events.append((stream, event))

        mock_bus = AsyncMock()
        mock_bus.publish = AsyncMock(side_effect=capture_publish)

        with (
            patch(
                "personal_agent.second_brain.consolidator.read_captures",
                return_value=[capture],
            ),
            patch(
                "personal_agent.second_brain.consolidator.extract_entities_and_relationships",
                AsyncMock(return_value=extraction),
            ),
            patch(
                "personal_agent.second_brain.consolidator.load_model_config",
                MagicMock(return_value=MagicMock(entity_extraction_role="test")),
            ),
            patch(
                "personal_agent.second_brain.consolidator.get_event_bus",
                return_value=mock_bus,
            ),
            patch(
                "personal_agent.second_brain.consolidator.get_settings",
            ) as mock_get_settings,
        ):
            mock_settings = MagicMock()
            mock_settings.freshness_enabled = True
            mock_get_settings.return_value = mock_settings

            await consolidator.consolidate_recent_captures(days=7)

        assert len(published_events) == 1
        stream, event = published_events[0]
        assert stream == STREAM_MEMORY_ACCESSED
        assert event.access_context == AccessContext.CONSOLIDATION
        assert event.query_type == "consolidation_traversal"
        assert event.trace_id == "consolidation"
        assert set(event.entity_ids) == {"entity-1", "entity-2"}

    @pytest.mark.asyncio
    async def test_consolidation_suppresses_when_freshness_disabled(self) -> None:
        """consolidate_recent_captures does NOT publish MemoryAccessedEvent when freshness_enabled=False."""
        from personal_agent.captains_log.capture import TaskCapture
        from personal_agent.second_brain.consolidator import SecondBrainConsolidator
        from datetime import datetime, timezone

        mock_service = AsyncMock()
        mock_service.connected = True
        mock_service.turn_exists = AsyncMock(return_value=False)
        mock_service.create_conversation = AsyncMock(return_value=True)
        mock_service.create_entity = AsyncMock(return_value="entity-1")
        mock_service.create_relationship = AsyncMock(return_value=False)
        mock_service.get_promotion_candidates = AsyncMock(return_value=[])
        mock_service.driver = MagicMock()
        mock_db_session = AsyncMock()
        mock_result = AsyncMock()
        mock_result.values = AsyncMock(return_value=[])
        mock_db_session.run = AsyncMock(return_value=mock_result)
        mock_db_session.__aenter__ = AsyncMock(return_value=mock_db_session)
        mock_db_session.__aexit__ = AsyncMock(return_value=None)
        mock_service.driver.session = MagicMock(return_value=mock_db_session)

        capture = TaskCapture(
            trace_id="trace-consolidation-2",
            session_id="session-con-2",
            timestamp=datetime.now(timezone.utc),
            user_message="Hello world",
            assistant_response="Hi there.",
            tools_used=[],
            duration_ms=50,
            outcome="success",
        )

        extraction = {
            "summary": "Greeting exchange",
            "entities": [{"name": "World", "type": "Concept", "description": "The world"}],
            "entity_names": ["World"],
            "relationships": [],
        }

        consolidator = SecondBrainConsolidator(memory_service=mock_service)

        published_streams: list[str] = []

        async def capture_publish(stream: str, event: object) -> None:
            published_streams.append(stream)

        mock_bus = AsyncMock()
        mock_bus.publish = AsyncMock(side_effect=capture_publish)

        with (
            patch(
                "personal_agent.second_brain.consolidator.read_captures",
                return_value=[capture],
            ),
            patch(
                "personal_agent.second_brain.consolidator.extract_entities_and_relationships",
                AsyncMock(return_value=extraction),
            ),
            patch(
                "personal_agent.second_brain.consolidator.load_model_config",
                MagicMock(return_value=MagicMock(entity_extraction_role="test")),
            ),
            patch(
                "personal_agent.second_brain.consolidator.get_event_bus",
                return_value=mock_bus,
            ),
            patch(
                "personal_agent.second_brain.consolidator.get_settings",
            ) as mock_get_settings,
        ):
            mock_settings = MagicMock()
            mock_settings.freshness_enabled = False
            mock_get_settings.return_value = mock_settings

            await consolidator.consolidate_recent_captures(days=7)

        # Only the MemoryEntitiesUpdatedEvent should be published, not MemoryAccessedEvent
        assert STREAM_MEMORY_ACCESSED not in published_streams
