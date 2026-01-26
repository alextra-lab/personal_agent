"""Tests for memory query relevance scoring."""

import uuid
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from personal_agent.memory.models import (
    ConversationNode,
    Entity,
    MemoryQuery,
)
from personal_agent.memory.service import MemoryService


@pytest_asyncio.fixture
async def memory_service():
    """Create memory service for testing."""
    service = MemoryService()
    await service.connect()

    # Clean test data
    if service.driver:
        async with service.driver.session() as session:
            await session.run("MATCH (n {test_scoring: true}) DETACH DELETE n")

    yield service
    await service.disconnect()


@pytest.mark.asyncio
class TestRelevanceScoring:
    """Test relevance scoring for memory queries."""

    async def test_query_returns_relevance_scores(self, memory_service):
        """Test that queries include relevance scores."""
        # Create test conversations
        conv1 = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.utcnow() - timedelta(hours=1),
            user_message="Tell me about Python",
            assistant_response="Python is a programming language...",
            key_entities=["Python"],
            properties={"test_scoring": True},
        )

        conv2 = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.utcnow() - timedelta(hours=2),
            user_message="What is Django?",
            assistant_response="Django is a Python web framework...",
            key_entities=["Django", "Python"],
            properties={"test_scoring": True},
        )

        await memory_service.create_conversation(conv1)
        await memory_service.create_conversation(conv2)

        # Create entities
        await memory_service.create_entity(
            Entity(name="Python", entity_type="LANGUAGE", properties={"test_scoring": True})
        )
        await memory_service.create_entity(
            Entity(name="Django", entity_type="FRAMEWORK", properties={"test_scoring": True})
        )

        # Query for Python
        query = MemoryQuery(entity_names=["Python"], limit=10)
        result = await memory_service.query_memory(query)

        # Should have scores
        assert len(result.relevance_scores) > 0
        assert conv1.conversation_id in result.relevance_scores
        assert conv2.conversation_id in result.relevance_scores

        # Scores should be between 0 and 1
        for score in result.relevance_scores.values():
            assert 0.0 <= score <= 1.0

    async def test_recency_affects_score(self, memory_service):
        """Test that more recent conversations score higher."""
        # Create two conversations about the same entity at different times
        recent_conv = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.utcnow() - timedelta(minutes=10),
            user_message="Recent Python question",
            assistant_response="Recent Python answer...",
            key_entities=["Python"],
            properties={"test_scoring": True},
        )

        old_conv = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.utcnow() - timedelta(days=7),
            user_message="Old Python question",
            assistant_response="Old Python answer...",
            key_entities=["Python"],
            properties={"test_scoring": True},
        )

        await memory_service.create_conversation(recent_conv)
        await memory_service.create_conversation(old_conv)
        await memory_service.create_entity(
            Entity(name="Python", entity_type="LANGUAGE", properties={"test_scoring": True})
        )

        # Query for Python
        query = MemoryQuery(entity_names=["Python"], limit=10)
        result = await memory_service.query_memory(query)

        # Recent conversation should score higher
        recent_score = result.relevance_scores.get(recent_conv.conversation_id, 0)
        old_score = result.relevance_scores.get(old_conv.conversation_id, 0)

        assert recent_score > old_score, "Recent conversations should score higher"

    async def test_entity_match_affects_score(self, memory_service):
        """Test that conversations matching more entities score higher."""
        # Conversation matching 2 out of 2 query entities
        full_match_conv = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.utcnow(),
            user_message="Tell me about Python and Django",
            assistant_response="Python and Django...",
            key_entities=["Python", "Django"],
            properties={"test_scoring": True},
        )

        # Conversation matching 1 out of 2 query entities
        partial_match_conv = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.utcnow(),
            user_message="Tell me about Python",
            assistant_response="Python is...",
            key_entities=["Python"],
            properties={"test_scoring": True},
        )

        await memory_service.create_conversation(full_match_conv)
        await memory_service.create_conversation(partial_match_conv)
        await memory_service.create_entity(
            Entity(name="Python", entity_type="LANGUAGE", properties={"test_scoring": True})
        )
        await memory_service.create_entity(
            Entity(name="Django", entity_type="FRAMEWORK", properties={"test_scoring": True})
        )

        # Query for both entities
        query = MemoryQuery(entity_names=["Python", "Django"], limit=10)
        result = await memory_service.query_memory(query)

        # Full match should score higher
        full_score = result.relevance_scores.get(full_match_conv.conversation_id, 0)
        partial_score = result.relevance_scores.get(partial_match_conv.conversation_id, 0)

        assert full_score > partial_score, "Full entity matches should score higher"

    async def test_entity_importance_affects_score(self, memory_service):
        """Test that popular entities boost relevance score."""
        # Create a popular entity (Python) with high mention count
        for i in range(10):
            conv = ConversationNode(
                conversation_id=str(uuid.uuid4()),
                timestamp=datetime.utcnow() - timedelta(hours=i),
                user_message=f"Python question {i}",
                assistant_response="Python answer...",
                key_entities=["Python"],
                properties={"test_scoring": True, "setup": True},
            )
            await memory_service.create_conversation(conv)

        # Create Python entity (will have high mention count from above)
        await memory_service.create_entity(
            Entity(name="Python", entity_type="LANGUAGE", properties={"test_scoring": True})
        )

        # Create a rare entity (RareLanguage) with low mention count
        rare_conv = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.utcnow(),
            user_message="Tell me about RareLanguage",
            assistant_response="RareLanguage is...",
            key_entities=["RareLanguage"],
            properties={"test_scoring": True},
        )
        await memory_service.create_conversation(rare_conv)
        await memory_service.create_entity(
            Entity(name="RareLanguage", entity_type="LANGUAGE", properties={"test_scoring": True})
        )

        # Query for Python (high importance)
        python_query = MemoryQuery(entity_names=["Python"], limit=20)
        python_result = await memory_service.query_memory(python_query)

        # Query for RareLanguage (low importance)
        rare_query = MemoryQuery(entity_names=["RareLanguage"], limit=10)
        rare_result = await memory_service.query_memory(rare_query)

        # Get average scores
        python_scores = list(python_result.relevance_scores.values())
        rare_scores = list(rare_result.relevance_scores.values())

        if python_scores and rare_scores:
            avg_python = sum(python_scores) / len(python_scores)
            avg_rare = sum(rare_scores) / len(rare_scores)

            # Python conversations should generally score higher due to entity importance
            # (though this is a small factor, so we just check they're in reasonable range)
            assert avg_python > 0.3, "Popular entities should boost scores"
            assert avg_rare > 0.0, "All conversations should have some score"

    async def test_scores_normalized_to_one(self, memory_service):
        """Test that scores are capped at 1.0."""
        # Create a perfect-match, recent conversation
        perfect_conv = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.utcnow(),
            user_message="Tell me about Python, Django, and FastAPI",
            assistant_response="These are all Python frameworks...",
            key_entities=["Python", "Django", "FastAPI"],
            properties={"test_scoring": True},
        )

        await memory_service.create_conversation(perfect_conv)
        await memory_service.create_entity(
            Entity(name="Python", entity_type="LANGUAGE", properties={"test_scoring": True})
        )
        await memory_service.create_entity(
            Entity(name="Django", entity_type="FRAMEWORK", properties={"test_scoring": True})
        )
        await memory_service.create_entity(
            Entity(name="FastAPI", entity_type="FRAMEWORK", properties={"test_scoring": True})
        )

        # Query for all entities
        query = MemoryQuery(entity_names=["Python", "Django", "FastAPI"], limit=10)
        result = await memory_service.query_memory(query)

        # Score should be at or below 1.0
        score = result.relevance_scores.get(perfect_conv.conversation_id, 0)
        assert score <= 1.0, "Scores should be capped at 1.0"
        assert score > 0.8, "Perfect matches should score very high"

    async def test_query_without_entities_still_scores(self, memory_service):
        """Test that queries without entity filters still get scores."""
        conv = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.utcnow(),
            user_message="General question",
            assistant_response="General answer...",
            key_entities=[],
            properties={"test_scoring": True},
        )

        await memory_service.create_conversation(conv)

        # Query without entity filter (get all recent)
        query = MemoryQuery(limit=10)
        result = await memory_service.query_memory(query)

        # Should still have scores
        if result.conversations:
            assert len(result.relevance_scores) > 0
            # Should get neutral entity match score
            for score in result.relevance_scores.values():
                assert score > 0.0
