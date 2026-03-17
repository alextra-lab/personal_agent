"""Tests for MemoryProtocol definition and adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.memory.protocol import (
    BroadRecallResult,
    Episode,
    MemoryProtocol,
    MemoryRecallQuery,
    MemoryRecallResult,
    MemoryType,
    RecallScope,
)
from personal_agent.memory.protocol_adapter import MemoryServiceAdapter


class TestMemoryTypes:
    """Tests for the MemoryType enum."""

    def test_all_types_defined(self) -> None:
        """All six memory types must be defined with correct values."""
        assert MemoryType.WORKING.value == "working"
        assert MemoryType.EPISODIC.value == "episodic"
        assert MemoryType.SEMANTIC.value == "semantic"
        assert MemoryType.PROCEDURAL.value == "procedural"
        assert MemoryType.PROFILE.value == "profile"
        assert MemoryType.DERIVED.value == "derived"

    def test_exactly_six_types(self) -> None:
        """There must be exactly six memory types."""
        assert len(MemoryType) == 6

    def test_recall_scope(self) -> None:
        """RecallScope must define the expected filter values."""
        assert RecallScope.ALL.value == "all"
        assert RecallScope.EPISODIC.value == "episodic"
        assert RecallScope.SEMANTIC.value == "semantic"

    def test_recall_scope_includes_procedural_and_derived(self) -> None:
        """RecallScope must include procedural and derived filters."""
        assert RecallScope.PROCEDURAL.value == "procedural"
        assert RecallScope.DERIVED.value == "derived"


class TestEpisode:
    """Tests for the Episode frozen dataclass."""

    def test_construction(self) -> None:
        """Episode should construct correctly with all required fields."""
        from datetime import datetime, timezone

        ep = Episode(
            turn_id="turn-123",
            session_id="session-456",
            timestamp=datetime.now(tz=timezone.utc),
            user_message="Hello",
            assistant_response="Hi there",
            tools_used=[],
            entities=["greeting"],
        )
        assert ep.turn_id == "turn-123"
        assert ep.session_id == "session-456"

    def test_optional_assistant_response(self) -> None:
        """Episode should accept None for assistant_response."""
        from datetime import datetime, timezone

        ep = Episode(
            turn_id="turn-789",
            session_id="session-000",
            timestamp=datetime.now(tz=timezone.utc),
            user_message="Hello",
            assistant_response=None,
        )
        assert ep.assistant_response is None

    def test_default_tools_and_entities(self) -> None:
        """tools_used and entities should default to empty lists."""
        from datetime import datetime, timezone

        ep = Episode(
            turn_id="turn-abc",
            session_id="session-def",
            timestamp=datetime.now(tz=timezone.utc),
            user_message="Test",
            assistant_response="Response",
        )
        assert ep.tools_used == []
        assert ep.entities == []

    def test_frozen(self) -> None:
        """Episode should be immutable (frozen dataclass)."""
        from datetime import datetime, timezone

        ep = Episode(
            turn_id="turn-freeze",
            session_id="session-freeze",
            timestamp=datetime.now(tz=timezone.utc),
            user_message="Test",
            assistant_response="Response",
        )
        with pytest.raises(AttributeError):
            ep.turn_id = "modified"  # type: ignore[misc]


class TestMemoryRecallQuery:
    """Tests for the MemoryRecallQuery frozen dataclass."""

    def test_defaults(self) -> None:
        """MemoryRecallQuery should have sensible defaults."""
        query = MemoryRecallQuery()
        assert query.entity_names == []
        assert query.entity_types == []
        assert query.memory_types == [RecallScope.ALL]
        assert query.recency_days == 30
        assert query.limit == 10
        assert query.query_text is None

    def test_custom_construction(self) -> None:
        """MemoryRecallQuery should accept custom values."""
        query = MemoryRecallQuery(
            entity_names=["Alice"],
            entity_types=["Person"],
            memory_types=[RecallScope.EPISODIC, RecallScope.SEMANTIC],
            recency_days=7,
            limit=5,
            query_text="recent conversations",
        )
        assert query.entity_names == ["Alice"]
        assert query.limit == 5
        assert len(query.memory_types) == 2

    def test_frozen(self) -> None:
        """MemoryRecallQuery should be immutable."""
        query = MemoryRecallQuery()
        with pytest.raises(AttributeError):
            query.limit = 99  # type: ignore[misc]


class TestMemoryRecallResult:
    """Tests for the MemoryRecallResult frozen dataclass."""

    def test_defaults(self) -> None:
        """MemoryRecallResult should default to empty collections."""
        result = MemoryRecallResult(episodes=[], entities=[])
        assert result.episodes == []
        assert result.entities == []
        assert result.relevance_scores == {}

    def test_frozen(self) -> None:
        """MemoryRecallResult should be immutable."""
        result = MemoryRecallResult(episodes=[], entities=[])
        with pytest.raises(AttributeError):
            result.episodes = []  # type: ignore[misc]


class TestBroadRecallResult:
    """Tests for the BroadRecallResult frozen dataclass."""

    def test_construction(self) -> None:
        """BroadRecallResult should construct with all fields."""
        result = BroadRecallResult(
            entities_by_type={"Person": [{"name": "Alice"}]},
            recent_sessions=[{"id": "s1"}],
            total_entity_count=42,
        )
        assert result.total_entity_count == 42
        assert "Person" in result.entities_by_type
        assert len(result.recent_sessions) == 1

    def test_frozen(self) -> None:
        """BroadRecallResult should be immutable."""
        result = BroadRecallResult(
            entities_by_type={},
            recent_sessions=[],
            total_entity_count=0,
        )
        with pytest.raises(AttributeError):
            result.total_entity_count = 99  # type: ignore[misc]


class TestProtocolIsRuntimeCheckable:
    """Tests for the MemoryProtocol runtime-checkable property."""

    def test_protocol_is_runtime_checkable(self) -> None:
        """Verify MemoryProtocol can be used with isinstance checks.

        If @runtime_checkable were removed, isinstance() would raise TypeError.
        """
        # This would raise TypeError if @runtime_checkable were absent
        assert isinstance(object(), MemoryProtocol) is False

    def test_non_implementing_class_fails_isinstance(self) -> None:
        """A class without the protocol methods should not pass isinstance."""

        class NotAMemory:
            pass

        assert not isinstance(NotAMemory(), MemoryProtocol)

    def test_implementing_class_passes_isinstance(self) -> None:
        """A class implementing all protocol methods should pass isinstance."""

        class FakeMemory:
            async def recall(self, query: MemoryRecallQuery, trace_id: str) -> MemoryRecallResult:
                return MemoryRecallResult(episodes=[], entities=[])

            async def recall_broad(
                self,
                entity_types: list[str] | None,
                recency_days: int,
                limit: int,
                trace_id: str,
            ) -> BroadRecallResult:
                return BroadRecallResult(
                    entities_by_type={}, recent_sessions=[], total_entity_count=0
                )

            async def store_episode(self, episode: Episode, trace_id: str) -> str:
                return "ep-1"

            async def is_connected(self) -> bool:
                return True

        assert isinstance(FakeMemory(), MemoryProtocol)


class TestMemoryServiceAdapter:
    """Verify adapter satisfies MemoryProtocol."""

    def test_adapter_satisfies_protocol(self) -> None:
        """Verify adapter passes isinstance check for MemoryProtocol."""
        mock_service = MagicMock()
        adapter = MemoryServiceAdapter(service=mock_service)
        assert isinstance(adapter, MemoryProtocol)

    @pytest.mark.asyncio
    async def test_recall_delegates_to_query_memory(self) -> None:
        """Verify recall() converts types and delegates to service."""
        mock_service = MagicMock()
        mock_service.query_memory = AsyncMock(
            return_value=MagicMock(
                conversations=[],
                entities=[],
                relevance_scores={},
            )
        )
        adapter = MemoryServiceAdapter(service=mock_service)
        query = MemoryRecallQuery(entity_names=["Neo4j"], limit=5)

        result = await adapter.recall(query, trace_id="test-trace")

        assert isinstance(result, MemoryRecallResult)
        mock_service.query_memory.assert_called_once()

    @pytest.mark.asyncio
    async def test_recall_broad_delegates(self) -> None:
        """Verify recall_broad() delegates to query_memory_broad."""
        mock_service = MagicMock()
        mock_service.query_memory_broad = AsyncMock(
            return_value={
                "entities": [
                    {"name": "Alice", "type": "Person", "mentions": 3},
                    {"name": "Neo4j", "type": "Technology", "mentions": 5},
                ],
                "sessions": [{"session_id": "s1"}],
                "turns_summary": [],
            }
        )
        adapter = MemoryServiceAdapter(service=mock_service)

        result = await adapter.recall_broad(
            entity_types=None, recency_days=90, limit=20, trace_id="test"
        )

        assert isinstance(result, BroadRecallResult)
        assert "Person" in result.entities_by_type
        assert "Technology" in result.entities_by_type
        assert len(result.entities_by_type["Person"]) == 1
        assert result.total_entity_count == 2
        assert result.recent_sessions == [{"session_id": "s1"}]
        mock_service.query_memory_broad.assert_called_once_with(
            entity_types=None, recency_days=90, limit=20
        )

    @pytest.mark.asyncio
    async def test_is_connected_when_driver_exists(self) -> None:
        """Verify is_connected returns True when driver is available."""
        mock_service = MagicMock()
        mock_service.driver = MagicMock()
        adapter = MemoryServiceAdapter(service=mock_service)

        result = await adapter.is_connected()
        assert result is True

    @pytest.mark.asyncio
    async def test_is_connected_when_no_driver(self) -> None:
        """Verify is_connected returns False when no driver."""
        mock_service = MagicMock()
        mock_service.driver = None
        adapter = MemoryServiceAdapter(service=mock_service)

        result = await adapter.is_connected()
        assert result is False

    @pytest.mark.asyncio
    async def test_store_episode_returns_turn_id(self) -> None:
        """Verify store_episode stub returns the episode's turn_id."""
        from datetime import datetime, timezone

        mock_service = MagicMock()
        adapter = MemoryServiceAdapter(service=mock_service)
        episode = Episode(
            turn_id="trace-abc",
            session_id="sess-123",
            timestamp=datetime.now(tz=timezone.utc),
            user_message="Hello",
            assistant_response="Hi",
        )

        result = await adapter.store_episode(episode, trace_id="trace-abc")
        assert result == "trace-abc"
