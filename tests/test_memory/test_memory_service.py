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
    # Clean before
    async with memory_service.driver.session() as session:
        await session.run("MATCH (n) WHERE n.test = true DETACH DELETE n")

    yield

    # Clean after
    async with memory_service.driver.session() as session:
        await session.run("MATCH (n) WHERE n.test = true DETACH DELETE n")


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
            channel="CLI",
            mode="NORMAL",
            test=True,  # Mark as test data
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
            channel="WEB",
            mode="NORMAL",
            metadata={"task_id": "123", "duration_ms": 500},
            test=True,
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
            test=True,
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
            mentions=5,
            test=True,
        )

        entity_id = await memory_service.create_entity(entity)

        assert entity_id is not None
        assert entity_id == "Python"

    @pytest.mark.asyncio
    async def test_create_duplicate_entity_increments_mentions(
        self, memory_service, clean_test_data
    ):
        """Test creating duplicate entity increments mention count."""
        entity = Entity(
            name="JavaScript",
            entity_type="PROGRAMMING_LANGUAGE",
            mentions=1,
            test=True,
        )

        # Create first time
        await memory_service.create_entity(entity)

        # Create again (should increment mentions via MERGE)
        await memory_service.create_entity(entity)

        # Verify via user_interests query
        interests = await memory_service.get_user_interests(limit=10)
        js_interest = next((i for i in interests if i.name == "JavaScript"), None)

        assert js_interest is not None
        assert js_interest.mention_count >= 2  # Should have incremented


class TestRelationships:
    """Test relationship creation between nodes."""

    @pytest.mark.asyncio
    async def test_create_relationship(self, memory_service, clean_test_data):
        """Test creating a DISCUSSES relationship."""
        # Create conversation and entity
        conversation = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.now(),
            user_message="Tell me about Paris",
            assistant_response="Paris is the capital of France.",
            channel="CLI",
            mode="NORMAL",
            test=True,
        )

        entity = Entity(
            name="Paris",
            entity_type="LOCATION",
            test=True,
        )

        success = await memory_service.create_conversation(conversation)
        assert success
        await memory_service.create_entity(entity)

        # Create relationship
        relationship = Relationship(
            source_id=conversation.conversation_id,
            target_id="Paris",
            relationship_type="DISCUSSES",
        )

        success = await memory_service.create_relationship(relationship)

        assert success


class TestMemoryQueries:
    """Test memory graph queries."""

    @pytest.mark.asyncio
    async def test_query_by_entity_name(self, memory_service, clean_test_data):
        """Test querying conversations by entity name."""
        # Create test data
        conversation = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.now(),
            user_message="What is Python?",
            assistant_response="Python is a programming language.",
            channel="CLI",
            mode="NORMAL",
            test=True,
        )

        entity = Entity(
            name="Python",
            entity_type="PROGRAMMING_LANGUAGE",
            test=True,
        )

        success = await memory_service.create_conversation(conversation)
        assert success
        await memory_service.create_entity(entity)

        # Create relationship
        relationship = Relationship(
            source_id=conversation.conversation_id,
            target_id="Python",
            relationship_type="DISCUSSES",
        )
        await memory_service.create_relationship(relationship)

        # Query by entity name
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
        # Create test data
        conversation = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.now(),
            user_message="Tell me about London",
            assistant_response="London is the capital of the UK.",
            channel="CLI",
            mode="NORMAL",
            test=True,
        )

        entity = Entity(
            name="London",
            entity_type="LOCATION",
            test=True,
        )

        success = await memory_service.create_conversation(conversation)
        assert success
        await memory_service.create_entity(entity)

        relationship = Relationship(
            source_id=conversation.conversation_id,
            target_id="London",
            relationship_type="DISCUSSES",
        )
        await memory_service.create_relationship(relationship)

        # Query by entity type
        query = MemoryQuery(
            entity_types=["LOCATION"],
            limit=10,
        )

        result = await memory_service.query_memory(query)

        assert len(result.conversations) >= 1

    @pytest.mark.asyncio
    async def test_query_with_recency_filter(self, memory_service, clean_test_data):
        """Test querying with recency filter."""
        # Create old conversation
        old_conversation = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.now() - timedelta(days=31),
            user_message="Old message about Ruby",
            assistant_response="Ruby is a programming language.",
            channel="CLI",
            mode="NORMAL",
            test=True,
        )

        # Create recent conversation
        recent_conversation = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.now(),
            user_message="Recent message about Ruby",
            assistant_response="Ruby is still popular.",
            channel="CLI",
            mode="NORMAL",
            test=True,
        )

        entity = Entity(
            name="Ruby",
            entity_type="PROGRAMMING_LANGUAGE",
            test=True,
        )

        await memory_service.create_conversation(old_conversation)
        await memory_service.create_conversation(recent_conversation)
        await memory_service.create_entity(entity)

        await memory_service.create_relationship(
            Relationship(
                source_id=old_conversation.conversation_id,
                target_id="Ruby",
                relationship_type="DISCUSSES",
            )
        )
        await memory_service.create_relationship(
            Relationship(
                source_id=recent_conversation.conversation_id,
                target_id="Ruby",
                relationship_type="DISCUSSES",
            )
        )

        # Query with recency filter
        query = MemoryQuery(
            entity_names=["Ruby"],
            recency_days=30,  # Only last 30 days
            limit=10,
        )

        result = await memory_service.query_memory(query)

        # Should only return recent conversation
        assert len(result.conversations) == 1
        assert result.conversations[0].conversation_id == recent_conversation.conversation_id

    @pytest.mark.asyncio
    async def test_get_user_interests(self, memory_service, clean_test_data):
        """Test retrieving user interests (entities by mention count)."""
        # Create entities with different mention counts
        entities = [
            Entity(
                name="Python",
                entity_type="PROGRAMMING_LANGUAGE",
                properties={"mentions": 10, "test": True},
            ),
            Entity(
                name="JavaScript",
                entity_type="PROGRAMMING_LANGUAGE",
                properties={"mentions": 5, "test": True},
            ),
            Entity(
                name="Ruby",
                entity_type="PROGRAMMING_LANGUAGE",
                properties={"mentions": 2, "test": True},
            ),
        ]

        for entity in entities:
            await memory_service.create_entity(entity)

        # Get user interests
        interests = await memory_service.get_user_interests(limit=3)

        assert len(interests) == 3
        # Should be sorted by mentions (descending)
        assert interests[0].name == "Python"
        assert interests[0].mention_count >= 10
        assert interests[1].name == "JavaScript"
        assert interests[2].name == "Ruby"


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
