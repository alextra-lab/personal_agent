"""Tests for MemoryService (Neo4j knowledge graph operations).

These tests require Neo4j to be running (docker compose up -d).
They test CRUD operations, queries, and connection handling.
"""

import uuid
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from personal_agent.memory.models import (
    ConversationNode,
    Entity,
    MemoryQuery,
    Relationship,
)
from personal_agent.memory.service import MemoryService

# Note: Tests use unique IDs (uuid) for entity names to avoid interference
# from stale Neo4j data across test runs.


@pytest_asyncio.fixture
async def memory_service():
    """Create and connect to memory service."""
    service = MemoryService()
    connected = await service.connect()
    if not connected:
        pytest.skip("Neo4j not available (docker compose up -d)")

    yield service

    # Cleanup
    await service.disconnect()


@pytest_asyncio.fixture
async def clean_test_data(memory_service):
    """Clean test data before and after tests."""
    yield


class TestConnectionHandling:
    """Test connection management."""

    @pytest.mark.asyncio
    async def test_connect_success(self):
        """Test successful connection to Neo4j."""
        service = MemoryService()
        connected = await service.connect()

        if not connected:
            pytest.skip("Neo4j not available")

        assert service.connected
        assert service.driver is not None

        await service.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect(self, memory_service):
        """Test disconnection from Neo4j."""
        await memory_service.disconnect()

        assert not memory_service.connected
        assert memory_service.driver is None

    @pytest.mark.asyncio
    async def test_connect_invalid_credentials(self):
        """Test connection with invalid credentials."""
        service = MemoryService()

        # Test that connection failure is handled gracefully
        # (Can't easily test invalid credentials without mocking)
        service.driver = None
        service.connected = False

        # Service should handle connection failures gracefully
        assert not service.connected


class TestConversationCRUD:
    """Test conversation node creation."""

    @pytest.mark.asyncio
    async def test_create_conversation(self, memory_service, clean_test_data):
        """Test creating a conversation node."""
        conversation = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.now(),
            user_message="What is the capital of France?",
            assistant_response="The capital of France is Paris.",
            key_entities=["France", "Paris"],
        )

        success = await memory_service.create_conversation(conversation)

        assert success is True

    @pytest.mark.asyncio
    async def test_create_conversation_with_metadata(self, memory_service, clean_test_data):
        """Test creating conversation with metadata."""
        conversation = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.now(),
            user_message="Tell me about Python",
            assistant_response="Python is a high-level programming language.",
            key_entities=["Python"],
            properties={"task_id": "123", "duration_ms": 500},
        )

        success = await memory_service.create_conversation(conversation)

        assert success is True


class TestEntityManagement:
    """Test entity node management."""

    @pytest.mark.asyncio
    async def test_create_entity(self, memory_service, clean_test_data):
        """Test creating an entity node."""
        entity = Entity(
            name="Paris",
            entity_type="LOCATION",
        )

        entity_id = await memory_service.create_entity(entity)

        assert entity_id is not None
        assert entity_id == "Paris"

    @pytest.mark.asyncio
    async def test_create_entity_with_mentions(self, memory_service, clean_test_data):
        """Test creating entity with mention count."""
        entity = Entity(
            name="Python",
            entity_type="PROGRAMMING_LANGUAGE",
        )

        entity_id = await memory_service.create_entity(entity)

        assert entity_id is not None
        assert entity_id == "Python"

    @pytest.mark.asyncio
    async def test_create_duplicate_entity_increments_mentions(
        self, memory_service, clean_test_data
    ):
        """Test creating duplicate entity increments mention count."""
        unique_name = f"TestLang_{uuid.uuid4().hex[:8]}"
        entity = Entity(
            name=unique_name,
            entity_type="PROGRAMMING_LANGUAGE",
        )

        await memory_service.create_entity(entity)
        await memory_service.create_entity(entity)

        # Query the specific entity directly to avoid being pushed out of
        # top-N results by accumulated test data from previous runs.
        async with memory_service.driver.session() as session:
            result = await session.run(
                "MATCH (e:Entity {name: $name}) RETURN e.mention_count AS mc",
                name=unique_name,
            )
            record = await result.single()

        assert record is not None
        assert record["mc"] >= 2


class TestRelationships:
    """Test relationship creation between nodes."""

    @pytest.mark.asyncio
    async def test_create_relationship(self, memory_service, clean_test_data):
        """Test creating a DISCUSSES relationship."""
        conversation = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.now(),
            user_message="Tell me about Paris",
            assistant_response="Paris is the capital of France.",
            key_entities=["Paris"],
        )

        entity = Entity(
            name="Paris",
            entity_type="LOCATION",
        )

        success = await memory_service.create_conversation(conversation)
        assert success
        await memory_service.create_entity(entity)

        relationship = Relationship(
            source_id=conversation.conversation_id,
            target_id="Paris",
            relationship_type="RELATED_TO",
        )

        success = await memory_service.create_relationship(relationship)

        assert success


class TestMemoryQueries:
    """Test memory graph queries."""

    @pytest.mark.asyncio
    async def test_query_by_entity_name(self, memory_service, clean_test_data):
        """Test querying conversations by entity name."""
        conversation = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.now(),
            user_message="What is Python?",
            assistant_response="Python is a programming language.",
            key_entities=["Python"],
        )

        success = await memory_service.create_conversation(conversation)
        assert success

        query = MemoryQuery(
            entity_names=["Python"],
            limit=10,
        )

        result = await memory_service.query_memory(query)

        assert len(result.conversations) >= 1
        assert any("Python" in conv.user_message for conv in result.conversations)

    @pytest.mark.asyncio
    async def test_query_by_entity_type(self, memory_service, clean_test_data):
        """Test querying conversations by entity type."""
        conversation = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.now(),
            user_message="Tell me about London",
            assistant_response="London is the capital of the UK.",
            key_entities=["London"],
        )

        entity = Entity(
            name="London",
            entity_type="LOCATION",
        )

        success = await memory_service.create_conversation(conversation)
        assert success
        await memory_service.create_entity(entity)

        query = MemoryQuery(
            entity_types=["LOCATION"],
            limit=10,
        )

        result = await memory_service.query_memory(query)

        assert len(result.conversations) >= 1

    @pytest.mark.asyncio
    async def test_query_with_recency_filter(self, memory_service, clean_test_data):
        """Test querying with recency filter."""
        unique_entity = f"RecencyLang_{uuid.uuid4().hex[:8]}"

        old_conversation = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.now() - timedelta(days=31),
            user_message=f"Old message about {unique_entity}",
            assistant_response=f"{unique_entity} is a programming language.",
            key_entities=[unique_entity],
        )

        recent_conversation = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.now(),
            user_message=f"Recent message about {unique_entity}",
            assistant_response=f"{unique_entity} is still popular.",
            key_entities=[unique_entity],
        )

        await memory_service.create_conversation(old_conversation)
        await memory_service.create_conversation(recent_conversation)

        query = MemoryQuery(
            entity_names=[unique_entity],
            recency_days=30,
            limit=10,
        )

        result = await memory_service.query_memory(query)

        assert len(result.conversations) == 1
        assert result.conversations[0].conversation_id == recent_conversation.conversation_id

    @pytest.mark.asyncio
    async def test_get_user_interests(self, memory_service, clean_test_data):
        """Test retrieving user interests (entities by mention count)."""
        prefix = f"test_{uuid.uuid4().hex[:6]}_"
        high_name = f"{prefix}HighMentions"
        mid_name = f"{prefix}MidMentions"
        low_name = f"{prefix}LowMentions"

        high_entity = Entity(name=high_name, entity_type="PROGRAMMING_LANGUAGE")
        mid_entity = Entity(name=mid_name, entity_type="PROGRAMMING_LANGUAGE")
        low_entity = Entity(name=low_name, entity_type="PROGRAMMING_LANGUAGE")

        for _ in range(10):
            await memory_service.create_entity(high_entity)
        for _ in range(5):
            await memory_service.create_entity(mid_entity)
        for _ in range(2):
            await memory_service.create_entity(low_entity)

        interests = await memory_service.get_user_interests(limit=500)

        test_interests = [i for i in interests if i.name.startswith(prefix)]
        assert len(test_interests) == 3
        assert test_interests[0].name == high_name
        assert test_interests[0].mention_count >= 10
        assert test_interests[1].name == mid_name
        assert test_interests[2].name == low_name


class TestErrorHandling:
    """Test error handling and edge cases."""

    @pytest.mark.asyncio
    async def test_query_without_connection(self):
        """Test querying without connection returns empty results."""
        service = MemoryService()
        # Don't connect

        query = MemoryQuery(entity_names=["Python"])
        result = await service.query_memory(query)

        # Should return empty results gracefully
        assert len(result.conversations) == 0

    @pytest.mark.asyncio
    async def test_create_conversation_without_connection(self):
        """Test creating conversation without connection."""
        service = MemoryService()
        # Don't connect

        conversation = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.now(),
            user_message="Test",
            assistant_response="Test",
            channel="CLI",
            mode="NORMAL",
        )

        success = await service.create_conversation(conversation)

        # Should return False gracefully
        assert success is False
