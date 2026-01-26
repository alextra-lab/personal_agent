"""Test that memory service builds a proper connected graph structure."""

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
    """Create memory service for testing."""
    service = MemoryService()
    await service.connect()
    yield service
    await service.disconnect()


@pytest_asyncio.fixture
async def knowledge_graph(memory_service):
    """Build a small knowledge graph for testing.

    Structure:
    - Conversation 1: "How do I start with Python?" -> discusses Python, Programming
    - Conversation 2: "Tell me about Django" -> discusses Django, Python, Web Development
    - Conversation 3: "What is FastAPI?" -> discusses FastAPI, Python, Web Development
    - Conversation 4: "Explain JavaScript" -> discusses JavaScript, Programming, Web Development

    This creates a connected graph where:
    - Python is central (3 conversations)
    - Web Development connects Django, FastAPI, JavaScript (3 conversations)
    - Programming connects Python and JavaScript (2 conversations)
    """
    # Clean test data
    if memory_service.driver:
        async with memory_service.driver.session() as session:
            await session.run("MATCH (n {test: true}) DETACH DELETE n")

    # Create conversations with entities
    conversations = [
        {
            "conv": ConversationNode(
                conversation_id=str(uuid.uuid4()),
                timestamp=datetime.utcnow() - timedelta(hours=4),
                user_message="How do I start with Python?",
                assistant_response="Python is a beginner-friendly language...",
                key_entities=["Python", "Programming"],
                properties={"test": True},
            ),
            "entities": [
                Entity(
                    name="Python", entity_type="PROGRAMMING_LANGUAGE", properties={"test": True}
                ),
                Entity(name="Programming", entity_type="TOPIC", properties={"test": True}),
            ],
        },
        {
            "conv": ConversationNode(
                conversation_id=str(uuid.uuid4()),
                timestamp=datetime.utcnow() - timedelta(hours=3),
                user_message="Tell me about Django",
                assistant_response="Django is a Python web framework...",
                key_entities=["Django", "Python", "Web Development"],
                properties={"test": True},
            ),
            "entities": [
                Entity(name="Django", entity_type="FRAMEWORK", properties={"test": True}),
                Entity(
                    name="Python", entity_type="PROGRAMMING_LANGUAGE", properties={"test": True}
                ),
                Entity(name="Web Development", entity_type="TOPIC", properties={"test": True}),
            ],
        },
        {
            "conv": ConversationNode(
                conversation_id=str(uuid.uuid4()),
                timestamp=datetime.utcnow() - timedelta(hours=2),
                user_message="What is FastAPI?",
                assistant_response="FastAPI is a modern Python web framework...",
                key_entities=["FastAPI", "Python", "Web Development"],
                properties={"test": True},
            ),
            "entities": [
                Entity(name="FastAPI", entity_type="FRAMEWORK", properties={"test": True}),
                Entity(
                    name="Python", entity_type="PROGRAMMING_LANGUAGE", properties={"test": True}
                ),
                Entity(name="Web Development", entity_type="TOPIC", properties={"test": True}),
            ],
        },
        {
            "conv": ConversationNode(
                conversation_id=str(uuid.uuid4()),
                timestamp=datetime.utcnow() - timedelta(hours=1),
                user_message="Explain JavaScript",
                assistant_response="JavaScript is a web programming language...",
                key_entities=["JavaScript", "Programming", "Web Development"],
                properties={"test": True},
            ),
            "entities": [
                Entity(
                    name="JavaScript", entity_type="PROGRAMMING_LANGUAGE", properties={"test": True}
                ),
                Entity(name="Programming", entity_type="TOPIC", properties={"test": True}),
                Entity(name="Web Development", entity_type="TOPIC", properties={"test": True}),
            ],
        },
    ]

    # Create all conversations and entities
    for item in conversations:
        await memory_service.create_conversation(item["conv"])
        for entity in item["entities"]:
            await memory_service.create_entity(entity)

    return conversations


@pytest.mark.asyncio
class TestGraphStructure:
    """Test graph connectivity and traversal."""

    async def test_entity_has_multiple_conversation_relationships(
        self, memory_service, knowledge_graph
    ):
        """Verify that entities have DISCUSSES relationships to multiple conversations."""
        # Python should be discussed in 3 conversations
        if not memory_service.driver:
            pytest.skip("No driver connection")

        async with memory_service.driver.session() as session:
            result = await session.run(
                """
                MATCH (e:Entity {name: 'Python', test: true})<-[:DISCUSSES]-(c:Conversation)
                RETURN count(c) as conversation_count
                """
            )
            record = await result.single()
            assert (
                record["conversation_count"] == 3
            ), "Python should be discussed in 3 conversations"

    async def test_find_related_conversations_through_shared_entity(
        self, memory_service, knowledge_graph
    ):
        """Test finding conversations related through a shared entity."""
        # Find all conversations that discuss Web Development
        query = MemoryQuery(entity_names=["Web Development"], limit=10)
        result = await memory_service.query_memory(query)

        # Should find Django, FastAPI, and JavaScript conversations
        assert len(result.conversations) == 3
        messages = {conv.user_message for conv in result.conversations}
        assert "Tell me about Django" in messages
        assert "What is FastAPI?" in messages
        assert "Explain JavaScript" in messages

    async def test_graph_traversal_finds_related_topics(self, memory_service, knowledge_graph):
        """Test graph traversal to find related topics."""
        # Starting from Django conversation, find related entities
        if not memory_service.driver:
            pytest.skip("No driver connection")

        async with memory_service.driver.session() as session:
            # Find entities related to Django through shared conversations
            result = await session.run(
                """
                MATCH (django:Entity {name: 'Django', test: true})<-[:DISCUSSES]-(c:Conversation)-[:DISCUSSES]->(related:Entity)
                WHERE related.name <> 'Django'
                RETURN DISTINCT related.name as entity_name, count(c) as connection_strength
                ORDER BY connection_strength DESC
                """
            )

            related_entities = {}
            async for record in result:
                related_entities[record["entity_name"]] = record["connection_strength"]

            # Django conversation also discusses Python and Web Development
            assert "Python" in related_entities
            assert "Web Development" in related_entities

    async def test_find_entity_neighbors(self, memory_service, knowledge_graph):
        """Test finding neighboring entities (2-hop traversal)."""
        if not memory_service.driver:
            pytest.skip("No driver connection")

        async with memory_service.driver.session() as session:
            # Find entities that share conversations with Python
            result = await session.run(
                """
                MATCH (python:Entity {name: 'Python', test: true})<-[:DISCUSSES]-(c:Conversation)-[:DISCUSSES]->(neighbor:Entity)
                WHERE neighbor.name <> 'Python'
                RETURN DISTINCT neighbor.name as neighbor_name, neighbor.entity_type as entity_type
                """
            )

            neighbors = []
            async for record in result:
                neighbors.append(
                    {
                        "name": record["neighbor_name"],
                        "type": record["entity_type"],
                    }
                )

            neighbor_names = {n["name"] for n in neighbors}

            # Python is discussed alongside these entities
            assert "Django" in neighbor_names
            assert "FastAPI" in neighbor_names
            assert "Programming" in neighbor_names
            assert "Web Development" in neighbor_names

    async def test_entity_mention_counts_incremented(self, memory_service, knowledge_graph):
        """Verify that mention counts are tracked correctly."""
        interests = await memory_service.get_user_interests(limit=10)

        # Filter to test entities
        test_interests = {i.name: i.mention_count for i in interests if i.properties.get("test")}

        # Python mentioned in 3 conversations
        assert test_interests.get("Python", 0) >= 3
        # Web Development mentioned in 3 conversations
        assert test_interests.get("Web Development", 0) >= 3
        # Programming mentioned in 2 conversations
        assert test_interests.get("Programming", 0) >= 2
        # Django, FastAPI, JavaScript each mentioned once
        assert test_interests.get("Django", 0) >= 1
        assert test_interests.get("FastAPI", 0) >= 1
        assert test_interests.get("JavaScript", 0) >= 1

    async def test_graph_clustering_web_development(self, memory_service, knowledge_graph):
        """Test that related concepts cluster together."""
        if not memory_service.driver:
            pytest.skip("No driver connection")

        async with memory_service.driver.session() as session:
            # Find all frameworks that are connected to Web Development
            result = await session.run(
                """
                MATCH (framework:Entity {entity_type: 'FRAMEWORK', test: true})<-[:DISCUSSES]-(c:Conversation)-[:DISCUSSES]->(webdev:Entity {name: 'Web Development'})
                RETURN DISTINCT framework.name as framework_name
                """
            )

            frameworks = []
            async for record in result:
                frameworks.append(record["framework_name"])

            # Both Django and FastAPI should be connected to Web Development
            assert "Django" in frameworks
            assert "FastAPI" in frameworks

    async def test_temporal_ordering_preserved(self, memory_service, knowledge_graph):
        """Verify that temporal ordering is preserved in the graph."""
        query = MemoryQuery(entity_names=["Python"], limit=10)
        result = await memory_service.query_memory(query)

        # Should return conversations in reverse chronological order
        assert len(result.conversations) == 3

        # Most recent first (FastAPI is most recent Python conversation)
        assert "FastAPI" in result.conversations[0].user_message or "FastAPI" in str(
            result.conversations[0].key_entities
        )

        # Verify timestamps are descending
        for i in range(len(result.conversations) - 1):
            assert result.conversations[i].timestamp >= result.conversations[i + 1].timestamp

    async def test_create_explicit_relationships(self, memory_service, knowledge_graph):
        """Test creating explicit relationships between entities."""
        # Create a relationship: Django IS_BUILT_WITH Python
        relationship = Relationship(
            source_id="Django",
            target_id="Python",
            relationship_type="IS_BUILT_WITH",
            weight=1.0,
            properties={"test": True},
        )

        success = await memory_service.create_relationship(relationship)
        assert success

        # Verify the relationship exists
        if not memory_service.driver:
            pytest.skip("No driver connection")

        async with memory_service.driver.session() as session:
            result = await session.run(
                """
                MATCH (django:Entity {name: 'Django'})-[r:RELATIONSHIP {type: 'IS_BUILT_WITH'}]->(python:Entity {name: 'Python'})
                RETURN r.type as rel_type, r.weight as weight
                """
            )
            record = await result.single()
            assert record is not None
            assert record["rel_type"] == "IS_BUILT_WITH"
            assert record["weight"] == 1.0
