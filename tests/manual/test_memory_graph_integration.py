"""Test full memory graph integration with Neo4j.

Prerequisites:
1. docker compose up -d (Neo4j running)
2. AGENT_ENABLE_MEMORY_GRAPH=true in .env
"""

import asyncio
from datetime import datetime, timezone

from personal_agent.memory.models import ConversationNode, Entity, Relationship
from personal_agent.memory.service import MemoryService
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


async def test_memory_service():
    """Test Neo4j connection and basic operations."""
    print("Testing Memory Service Integration\n")

    # Initialize service
    memory = MemoryService()

    try:
        # 1. Connect
        print("1. Connecting to Neo4j...")
        await memory.connect()
        print(f"   ✓ Connected: {memory.connected}")

        # 2. Create conversation node
        print("\n2. Creating conversation node...")
        conv = ConversationNode(
            conversation_id="test-conv-001",
            timestamp=datetime.now(timezone.utc),
            user_message="Tell me about Python programming",
            assistant_response="Python is a high-level programming language.",
            summary="Discussion about Python",
            key_entities=["Python"],
        )
        await memory.create_conversation(conv)
        print("   ✓ Conversation created")

        # 3. Create entities
        print("\n3. Creating entities...")
        python_entity = Entity(
            name="Python",
            entity_type="Technology",
            description="Programming language",
            properties={},  # Neo4j doesn't support nested dicts in properties
        )
        entity_id = await memory.create_entity(python_entity)
        print(f"   ✓ Entity created: {entity_id}")

        # 4. Create relationship
        print("\n4. Creating relationship...")
        rel = Relationship(
            source_id="test-conv-001",
            target_id="Python",
            relationship_type="DISCUSSES",
            weight=1.0,
        )
        await memory.create_relationship(rel)
        print("   ✓ Relationship created")

        # 5. Query memory
        print("\n5. Querying memory...")
        from personal_agent.memory.models import MemoryQuery

        query = MemoryQuery(entities=["Python"], limit=10)
        results = await memory.query_memory(query)
        print(f"   ✓ Found {len(results.conversations)} conversations")
        print(f"   ✓ Entities: {results.entities}")

        # 6. Get user interests
        print("\n6. Getting user interests...")
        interests = await memory.get_user_interests(limit=5)
        print(f"   ✓ Top interests: {[e.name for e in interests]}")

        print("\n✅ All memory service tests passed!")
        return True

    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        await memory.disconnect()
        print("\n✓ Disconnected from Neo4j")


if __name__ == "__main__":
    success = asyncio.run(test_memory_service())
    exit(0 if success else 1)
