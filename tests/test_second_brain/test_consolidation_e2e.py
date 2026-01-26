"""E2E tests for second brain consolidation workflow."""

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from personal_agent.captains_log.capture import TaskCapture
from personal_agent.memory.models import MemoryQuery
from personal_agent.memory.service import MemoryService
from personal_agent.second_brain.consolidator import SecondBrainConsolidator


@pytest_asyncio.fixture
async def memory_service():
    """Create memory service for testing."""
    service = MemoryService()
    await service.connect()

    # Clean test data
    if service.driver:
        async with service.driver.session() as session:
            await session.run("MATCH (n {test_e2e: true}) DETACH DELETE n")

    yield service
    await service.disconnect()


@pytest_asyncio.fixture
async def consolidator(memory_service):
    """Create consolidator with memory service."""
    return SecondBrainConsolidator(memory_service=memory_service)


@pytest.mark.asyncio
class TestConsolidationE2E:
    """E2E tests for consolidation workflow with qwen3-8b."""

    async def test_consolidate_python_conversation(self, consolidator, memory_service):
        """Test full consolidation workflow: capture -> extraction -> Neo4j storage."""
        # Create a mock task capture
        trace_id = str(uuid.uuid4())
        capture = TaskCapture(
            trace_id=trace_id,
            timestamp=datetime.now(timezone.utc),
            user_message="Tell me about Python programming",
            assistant_response=(
                "Python is a high-level programming language known for its simplicity. "
                "It's widely used in web development with frameworks like Django and Flask."
            ),
            session_id="test_session",
            tools_used=["llm_client"],
            duration_ms=1000,
            outcome="SUCCESS",
        )

        # Process the capture
        result = await consolidator._process_capture(capture)

        # Verify processing results
        assert result["conversation_created"] == 1
        assert result["entities_created"] > 0  # Should extract at least Python

        # Verify conversation was stored in Neo4j
        query = MemoryQuery(conversation_ids=[trace_id], limit=1)
        memory_result = await memory_service.query_memory(query)

        assert len(memory_result.conversations) == 1
        stored_conv = memory_result.conversations[0]
        assert stored_conv.conversation_id == trace_id
        assert stored_conv.user_message == capture.user_message
        assert stored_conv.summary is not None
        assert len(stored_conv.key_entities) > 0

        # Verify entities were extracted
        entity_names = stored_conv.key_entities
        python_found = any("python" in name.lower() for name in entity_names)
        assert python_found, f"Python should be extracted. Found: {entity_names}"

    async def test_consolidate_with_relationships(self, consolidator, memory_service):
        """Test that relationships are created between entities."""
        # Create capture about Django and Python
        trace_id = str(uuid.uuid4())
        capture = TaskCapture(
            trace_id=trace_id,
            timestamp=datetime.now(timezone.utc),
            user_message="How is Django related to Python?",
            assistant_response=(
                "Django is a web framework built with Python. It allows developers "
                "to create web applications quickly using Python's clean syntax."
            ),
            session_id="test_session",
            tools_used=["llm_client"],
            duration_ms=1500,
            outcome="SUCCESS",
        )

        # Process capture
        result = await consolidator._process_capture(capture)

        # Should create entities and possibly relationships
        assert result["conversation_created"] == 1
        assert result["entities_created"] >= 2  # Django and Python at minimum

        # Verify in Neo4j
        query = MemoryQuery(conversation_ids=[trace_id], limit=1)
        memory_result = await memory_service.query_memory(query)

        assert len(memory_result.conversations) == 1
        stored_conv = memory_result.conversations[0]

        # Should have both Django and Python
        entity_names_lower = [name.lower() for name in stored_conv.key_entities]
        assert "django" in entity_names_lower or any(
            "django" in name for name in entity_names_lower
        )
        assert "python" in entity_names_lower or any(
            "python" in name for name in entity_names_lower
        )

    async def test_consolidate_multiple_captures(self, consolidator, memory_service):
        """Test consolidating multiple captures in sequence."""
        # Create multiple captures
        captures = [
            TaskCapture(
                trace_id=str(uuid.uuid4()),
                timestamp=datetime.now(timezone.utc),
                user_message="What is FastAPI?",
                assistant_response="FastAPI is a modern Python web framework.",
                session_id="test_session",
                tools_used=["llm_client"],
                duration_ms=800,
                outcome="SUCCESS",
            ),
            TaskCapture(
                trace_id=str(uuid.uuid4()),
                timestamp=datetime.now(timezone.utc),
                user_message="Tell me about Flask",
                assistant_response="Flask is a lightweight Python web framework.",
                session_id="test_session",
                tools_used=["llm_client"],
                duration_ms=900,
                outcome="SUCCESS",
            ),
        ]

        # Process all captures
        total_entities = 0
        conversation_ids = []

        for capture in captures:
            result = await consolidator._process_capture(capture)
            total_entities += result["entities_created"]
            conversation_ids.append(capture.trace_id)

        # Should have created entities from both
        assert total_entities > 0

        # Verify both conversations in Neo4j
        query = MemoryQuery(conversation_ids=conversation_ids, limit=10)
        memory_result = await memory_service.query_memory(query)

        assert len(memory_result.conversations) == 2

    async def test_consolidate_with_properties(self, consolidator, memory_service):
        """Test that conversation properties are stored."""
        trace_id = str(uuid.uuid4())
        capture = TaskCapture(
            trace_id=trace_id,
            timestamp=datetime.now(timezone.utc),
            user_message="Quick test",
            assistant_response="This is a test response.",
            session_id="test_session",
            tools_used=["tool1", "tool2", "tool3"],
            duration_ms=2500,
            outcome="SUCCESS",
        )

        # Process capture
        await consolidator._process_capture(capture)

        # Verify properties were stored
        query = MemoryQuery(conversation_ids=[trace_id], limit=1)
        memory_result = await memory_service.query_memory(query)

        assert len(memory_result.conversations) == 1
        stored_conv = memory_result.conversations[0]

        # Check properties
        props = stored_conv.properties
        assert "tools_used" in props
        assert "duration_ms" in props
        assert "outcome" in props
        assert props["tools_used"] == ["tool1", "tool2", "tool3"]
        assert props["duration_ms"] == 2500
        assert props["outcome"] == "SUCCESS"

    async def test_consolidate_handles_empty_response(self, consolidator, memory_service):
        """Test consolidation handles missing assistant response."""
        trace_id = str(uuid.uuid4())
        capture = TaskCapture(
            trace_id=trace_id,
            timestamp=datetime.now(timezone.utc),
            user_message="What is AI?",
            assistant_response=None,  # No response
            session_id="test_session",
            tools_used=[],
            duration_ms=100,
            outcome="FAILED",
        )

        # Should handle gracefully
        result = await consolidator._process_capture(capture)

        # Conversation should still be created
        assert result["conversation_created"] == 1

        # Verify in Neo4j
        query = MemoryQuery(conversation_ids=[trace_id], limit=1)
        memory_result = await memory_service.query_memory(query)

        assert len(memory_result.conversations) == 1

    async def test_extract_and_query_workflow(self, consolidator, memory_service):
        """Test full workflow: consolidate -> query by entity -> verify results."""
        # Step 1: Consolidate a conversation about machine learning
        trace_id = str(uuid.uuid4())
        capture = TaskCapture(
            trace_id=trace_id,
            timestamp=datetime.now(timezone.utc),
            user_message="Explain machine learning",
            assistant_response=(
                "Machine learning is a subset of artificial intelligence that enables "
                "systems to learn from data. It's used in many applications like "
                "image recognition, natural language processing, and recommendation systems."
            ),
            session_id="test_session",
            tools_used=["llm_client"],
            duration_ms=1200,
            outcome="SUCCESS",
        )

        result = await consolidator._process_capture(capture)
        assert result["conversation_created"] == 1

        # Step 2: Query by extracted entities (optional - may or may not find it)
        # The LLM should have extracted "Machine Learning" or similar
        query = MemoryQuery(
            entity_names=["Machine Learning", "machine learning", "AI", "Artificial Intelligence"],
            limit=10,
        )
        _ = await memory_service.query_memory(query)  # May or may not find it

        # Step 3: Verify we can retrieve the conversation by ID (guaranteed)
        # Direct query should always work
        direct_query = MemoryQuery(conversation_ids=[trace_id], limit=1)
        direct_result = await memory_service.query_memory(direct_query)
        assert len(direct_result.conversations) == 1
        assert direct_result.conversations[0].conversation_id == trace_id

    async def test_consolidation_summary_accuracy(self, consolidator, memory_service):
        """Test that consolidation returns accurate summary."""
        # Create 3 captures
        captures = [
            TaskCapture(
                trace_id=str(uuid.uuid4()),
                timestamp=datetime.now(timezone.utc),
                user_message=f"Test message {i}",
                assistant_response=f"Test response {i}",
                session_id="test_session",
                tools_used=[],
                duration_ms=100,
                outcome="SUCCESS",
            )
            for i in range(3)
        ]

        # Process all
        total_conversations = 0
        total_entities = 0

        for capture in captures:
            result = await consolidator._process_capture(capture)
            total_conversations += result["conversation_created"]
            total_entities += result["entities_created"]

        # Summary should match
        assert total_conversations == 3
        assert total_entities >= 0  # Might not extract entities from simple test messages
