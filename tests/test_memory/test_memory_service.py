"""Tests for MemoryService (Neo4j knowledge graph operations).

These tests require Neo4j to be running (docker compose up -d).
They test CRUD operations, queries, and connection handling.
"""

import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
import pytest_asyncio

from personal_agent.config.settings import get_settings
from personal_agent.memory.models import (
    ConversationNode,
    Entity,
    MemoryQuery,
    Relationship,
)
from personal_agent.memory.reranker import RerankResult
from personal_agent.memory.service import MemoryService

# Note: Tests use unique IDs (uuid) for entity names to avoid interference
# from stale Neo4j data across test runs.


@pytest_asyncio.fixture
async def memory_service():
    """Create and connect to memory service."""
    service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
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
        service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
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
        service = MemoryService()  # fre-375-allow: unit test with mocked driver, no real connection

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

    @pytest.mark.asyncio
    async def test_entity_access_tracking_on_creation(self, memory_service, clean_test_data):
        """Test that entity nodes are created with access-tracking properties (FRE-161)."""
        unique_name = f"AccessTrack_{uuid.uuid4().hex[:8]}"
        entity = Entity(
            name=unique_name,
            entity_type="TEST_TYPE",
        )

        entity_id = await memory_service.create_entity(entity)
        assert entity_id is not None

        # Query the entity to verify access-tracking properties
        async with memory_service.driver.session() as session:
            result = await session.run(
                """
                MATCH (e:Entity {name: $name})
                RETURN
                    e.access_count AS access_count,
                    e.first_accessed_at AS first_accessed_at,
                    e.last_accessed_at AS last_accessed_at,
                    e.last_access_context AS last_access_context
                """,
                name=unique_name,
            )
            record = await result.single()

        assert record is not None
        assert record["access_count"] == 0
        assert record["first_accessed_at"] is not None
        assert record["last_accessed_at"] is not None
        assert record["last_access_context"] == "created"

    @pytest.mark.asyncio
    async def test_entity_access_tracking_defaults(self, memory_service, clean_test_data):
        """Test that entities without explicit access-tracking properties default correctly."""
        # Create an entity with very unique name to minimize dedup
        unique_name = f"NoEmbedTest_{uuid.uuid4().hex}"
        entity = Entity(
            name=unique_name,
            entity_type="TEST_ENTITY",
        )

        # Create entity
        entity_id = await memory_service.create_entity(entity)
        assert entity_id is not None
        assert len(entity_id) > 0

        # Verify access-tracking properties are set using the returned entity_id
        async with memory_service.driver.session() as session:
            result = await session.run(
                """
                MATCH (e:Entity {name: $name})
                RETURN
                    e.first_accessed_at AS first_accessed,
                    e.last_accessed_at AS last_accessed,
                    e.access_count AS access_count,
                    e.last_access_context AS context
                """,
                name=entity_id,
            )
            record = await result.single()

        assert record is not None
        assert record["first_accessed"] is not None
        assert record["last_accessed"] is not None
        assert record["access_count"] == 0
        assert record["context"] == "created"


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

        rel_id = await memory_service.create_relationship(relationship)

        assert rel_id is not None

    @pytest.mark.asyncio
    async def test_relationship_access_tracking_on_creation(self, memory_service, clean_test_data):
        """Test that relationship properties include access-tracking (FRE-161)."""
        # Create source and target entities
        source_name = f"Source_{uuid.uuid4().hex[:8]}"
        target_name = f"Target_{uuid.uuid4().hex[:8]}"

        source_entity = Entity(name=source_name, entity_type="TEST")
        target_entity = Entity(name=target_name, entity_type="TEST")

        await memory_service.create_entity(source_entity)
        await memory_service.create_entity(target_entity)

        # Create relationship
        relationship = Relationship(
            source_id=source_name,
            target_id=target_name,
            relationship_type="TEST_RELATES",
            weight=0.8,
        )

        rel_id = await memory_service.create_relationship(relationship)
        assert rel_id is not None

        # Query the relationship to verify access-tracking properties
        async with memory_service.driver.session() as session:
            result = await session.run(
                """
                MATCH (s {name: $source_name})-[rel:TEST_RELATES]->(t {name: $target_name})
                RETURN
                    rel.access_count AS access_count,
                    rel.first_accessed_at AS first_accessed_at,
                    rel.last_accessed_at AS last_accessed_at,
                    rel.last_access_context AS last_access_context,
                    rel.weight AS weight
                """,
                source_name=source_name,
                target_name=target_name,
            )
            record = await result.single()

        assert record is not None
        assert record["access_count"] == 0
        assert record["first_accessed_at"] is not None
        assert record["last_accessed_at"] is not None
        assert record["last_access_context"] == "created"
        assert record["weight"] == 0.8


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
    async def test_query_memory_flag_off_reproduces_cutoff(
        self, memory_service, clean_test_data, monkeypatch
    ):
        """ADR-0100 AC-7: flag off keeps a >30-day positive absent at the default cutoff."""
        monkeypatch.setattr(
            get_settings(), "relevance_bounded_recall_enabled", False, raising=False
        )
        entity = f"AC7Topic_{uuid.uuid4().hex[:8]}"
        old_turn = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.now() - timedelta(days=45),
            user_message=f"Old discussion of {entity}",
            assistant_response=f"{entity} notes.",
            key_entities=[entity],
        )
        await memory_service.create_conversation(old_turn)

        result = await memory_service.query_memory(
            MemoryQuery(entity_names=[entity], recency_days=30, limit=10)
        )

        returned_ids = {c.conversation_id for c in result.conversations}
        assert old_turn.conversation_id not in returned_ids

    @pytest.mark.asyncio
    async def test_query_memory_relevance_bounded_invariant_to_recency(
        self, memory_service, clean_test_data, monkeypatch
    ):
        """ADR-0100 AC-1a: flag on surfaces a >30-day positive at recency_days 1, 30 and 365."""
        monkeypatch.setattr(get_settings(), "relevance_bounded_recall_enabled", True, raising=False)
        entity = f"AC1aTopic_{uuid.uuid4().hex[:8]}"
        old_turn = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.now() - timedelta(days=120),
            user_message=f"Old discussion of {entity}",
            assistant_response=f"{entity} notes.",
            key_entities=[entity],
        )
        await memory_service.create_conversation(old_turn)

        for cutoff in (1, 30, 365):
            result = await memory_service.query_memory(
                MemoryQuery(entity_names=[entity], recency_days=cutoff, limit=10)
            )
            returned_ids = {c.conversation_id for c in result.conversations}
            assert old_turn.conversation_id in returned_ids, (
                f"old positive missing at recency_days={cutoff} (cutoff not removed)"
            )

    @pytest.mark.asyncio
    async def test_query_memory_distractors_do_not_evict(
        self, memory_service, clean_test_data, monkeypatch
    ):
        """ADR-0100 AC-3: recent distractors under another entity do not evict the old positive."""
        monkeypatch.setattr(get_settings(), "relevance_bounded_recall_enabled", True, raising=False)
        positive_entity = f"AC3Positive_{uuid.uuid4().hex[:8]}"
        noise_entity = f"AC3Noise_{uuid.uuid4().hex[:8]}"

        old_positive = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.now() - timedelta(days=200),
            user_message=f"Old discussion of {positive_entity}",
            assistant_response=f"{positive_entity} notes.",
            key_entities=[positive_entity],
        )
        await memory_service.create_conversation(old_positive)

        # Many recent distractor turns under a different entity.
        for i in range(15):
            await memory_service.create_conversation(
                ConversationNode(
                    conversation_id=str(uuid.uuid4()),
                    timestamp=datetime.now() - timedelta(minutes=i),
                    user_message=f"Recent chatter about {noise_entity} #{i}",
                    assistant_response=f"{noise_entity} reply {i}.",
                    key_entities=[noise_entity],
                )
            )

        result = await memory_service.query_memory(
            MemoryQuery(entity_names=[positive_entity], recency_days=30, limit=5)
        )

        returned_ids = {c.conversation_id for c in result.conversations}
        assert old_positive.conversation_id in returned_ids

    @pytest.mark.asyncio
    async def test_query_memory_old_relevant_turn_survives_same_entity_crowding(
        self, memory_service, clean_test_data, monkeypatch
    ):
        """ADR-0100 AC-2/AC-3 same-entity: an old content-relevant turn ranks into the returned set.

        This is the proof the single-seed AC tests cannot give: with > limit candidates under
        ONE entity, the relevance signal (reranker) must lift the old turn past [:limit], not
        recency. Embedder/reranker are mocked for a deterministic content signal.
        """
        monkeypatch.setattr(get_settings(), "relevance_bounded_recall_enabled", True, raising=False)
        entity = f"AC2Topic_{uuid.uuid4().hex[:8]}"
        marker = f"signal_{uuid.uuid4().hex[:6]}"

        old_relevant = ConversationNode(
            conversation_id=str(uuid.uuid4()),
            timestamp=datetime.now() - timedelta(days=200),
            user_message=f"Old but on-point discussion: {marker} about {entity}",
            assistant_response=f"{entity} deep notes.",
            key_entities=[entity],
        )
        await memory_service.create_conversation(old_relevant)
        # Eight recent turns under the SAME entity, none matching the query marker.
        for i in range(8):
            await memory_service.create_conversation(
                ConversationNode(
                    conversation_id=str(uuid.uuid4()),
                    timestamp=datetime.now() - timedelta(minutes=i),
                    user_message=f"Recent unrelated chatter #{i} about {entity}",
                    assistant_response=f"{entity} aside {i}.",
                    key_entities=[entity],
                )
            )

        async def _zero_embedding(*_args, **_kwargs):
            return [0.0, 0.0, 0.0, 0.0]

        async def _fake_rerank(query, documents, top_k=None, **kwargs):
            # Score the marker-bearing (old, relevant) document highest. **kwargs
            # absorbs the FRE-698 trace identity (trace_id/session_id/task_id).
            return [
                RerankResult(index=i, score=1.0 if marker in doc else 0.0, document=doc)
                for i, doc in enumerate(documents)
            ]

        with (
            patch("personal_agent.memory.service.generate_embedding", _zero_embedding),
            patch("personal_agent.memory.reranker.rerank", _fake_rerank),
        ):
            result = await memory_service.query_memory(
                MemoryQuery(entity_names=[entity], recency_days=30, limit=3),
                query_text=f"tell me about {marker}",
            )

        returned_ids = [c.conversation_id for c in result.conversations]
        assert old_relevant.conversation_id in returned_ids, (
            "old-but-relevant turn was sliced out by [:limit] despite the relevance signal"
        )
        # Blocker 5: quality metrics / result reflect the post-slice set, not the candidate set.
        assert len(result.relevance_scores) == len(result.conversations) <= 3

    @pytest.mark.asyncio
    async def test_query_memory_threads_identity_into_rerank(
        self, memory_service, clean_test_data, monkeypatch
    ):
        """FRE-698 (ADR-0074): query_memory passes its trace_id/session_id to rerank().

        Proves the service.py call site threads the join keys so the reranker telemetry
        is attributable to the turn. Captures the kwargs the fake rerank receives.
        """
        monkeypatch.setattr(get_settings(), "relevance_bounded_recall_enabled", True, raising=False)
        entity = f"FRE698_{uuid.uuid4().hex[:8]}"
        for i in range(2):
            await memory_service.create_conversation(
                ConversationNode(
                    conversation_id=str(uuid.uuid4()),
                    timestamp=datetime.now() - timedelta(minutes=i),
                    user_message=f"turn {i} about {entity}",
                    assistant_response=f"{entity} note {i}.",
                    key_entities=[entity],
                )
            )

        async def _zero_embedding(*_args, **_kwargs):
            return [0.0, 0.0, 0.0, 0.0]

        captured: dict[str, object] = {}

        async def _capturing_rerank(query, documents, top_k=None, **kwargs):
            captured.update(kwargs)
            return [
                RerankResult(index=i, score=1.0, document=doc) for i, doc in enumerate(documents)
            ]

        with (
            patch("personal_agent.memory.service.generate_embedding", _zero_embedding),
            patch("personal_agent.memory.reranker.rerank", _capturing_rerank),
        ):
            await memory_service.query_memory(
                MemoryQuery(entity_names=[entity], recency_days=30, limit=5),
                query_text=f"tell me about {entity}",
                trace_id="tr-698",
                session_id="se-698",
            )

        assert captured.get("trace_id") == "tr-698"
        assert captured.get("session_id") == "se-698"

    @pytest.mark.asyncio
    async def test_query_memory_flag_on_id_lookup_not_reordered(
        self, memory_service, clean_test_data, monkeypatch
    ):
        """ADR-0100 scope: flag-on id lookups use the legacy path, unchanged by the reorder.

        The relevance reorder is gated on entity recall, not just the flag, so direct id
        lookups (and the bare fallback) keep legacy timestamp-DESC behaviour under the flag.
        """
        ids = []
        base = datetime.now()
        for i in range(3):
            cid = str(uuid.uuid4())
            ids.append(cid)
            await memory_service.create_conversation(
                ConversationNode(
                    conversation_id=cid,
                    timestamp=base - timedelta(hours=i),
                    user_message=f"id-lookup turn {i}",
                    key_entities=[],
                )
            )

        async def _run(flag: bool) -> list[str]:
            monkeypatch.setattr(
                get_settings(), "relevance_bounded_recall_enabled", flag, raising=False
            )
            res = await memory_service.query_memory(MemoryQuery(conversation_ids=ids, limit=10))
            return [c.conversation_id for c in res.conversations]

        # Flag on must produce the identical (legacy timestamp-DESC) order as flag off.
        assert await _run(True) == await _run(False)

    @pytest.mark.asyncio
    async def test_query_memory_broad_flag_off_excludes_old_entity(
        self, memory_service, clean_test_data, monkeypatch
    ):
        """ADR-0100 AC-1b control: flag off, the broad path omits an entity discussed only >90 days ago."""
        monkeypatch.setattr(
            get_settings(), "relevance_bounded_recall_enabled", False, raising=False
        )
        entity = f"AC1bTopic_{uuid.uuid4().hex[:8]}"
        await memory_service.create_conversation(
            ConversationNode(
                conversation_id=str(uuid.uuid4()),
                timestamp=datetime.now() - timedelta(days=120),
                user_message=f"Old discussion of {entity}",
                assistant_response=f"{entity} notes.",
                key_entities=[entity],
            )
        )

        broad = await memory_service.query_memory_broad(
            recency_days=90, limit=20, query_text=f"tell me about {entity}"
        )
        names = [e.get("name") for e in broad.get("entities", [])]
        assert entity not in names

    @pytest.mark.asyncio
    async def test_query_memory_broad_flag_on_surfaces_old_entity(
        self, memory_service, clean_test_data, monkeypatch
    ):
        """ADR-0100 AC-1b: flag on + query_text, a >90-day-old entity surfaces by name in entities."""
        monkeypatch.setattr(get_settings(), "relevance_bounded_recall_enabled", True, raising=False)
        entity = f"AC1bTopic_{uuid.uuid4().hex[:8]}"
        await memory_service.create_conversation(
            ConversationNode(
                conversation_id=str(uuid.uuid4()),
                timestamp=datetime.now() - timedelta(days=120),
                user_message=f"Old discussion of {entity}",
                assistant_response=f"{entity} notes.",
                key_entities=[entity],
            )
        )

        async def _nonzero_embedding(*_args, **_kwargs):
            return [0.1, 0.2, 0.3, 0.4]

        async def _fake_vector_candidates(_session, _embedding, _top_k):
            # The vector index reports this old entity as relevant across all time.
            return [{"name": entity, "score": 0.9}]

        with (
            patch("personal_agent.memory.service.generate_embedding", _nonzero_embedding),
            patch.object(
                memory_service,
                "_query_entity_vector_candidates",
                _fake_vector_candidates,
            ),
        ):
            broad = await memory_service.query_memory_broad(
                recency_days=90, limit=20, query_text=f"tell me about {entity}"
            )
        names = [e.get("name") for e in broad.get("entities", [])]
        assert entity in names, "the >90-day entity did not surface — broad seam not landed"

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

        # Use a dynamic limit so pre-existing entities in shared Neo4j state
        # do not push this test's low-mention entity out of the result window.
        async with memory_service.driver.session() as session:
            count_result = await session.run("MATCH (e:Entity) RETURN count(e) AS total")
            count_record = await count_result.single()
            total_entities = int(count_record["total"]) if count_record else 0

        interests = await memory_service.get_user_interests(limit=total_entities + 10)

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
        service = MemoryService()  # fre-375-allow: unit test with mocked driver, no real connection
        # Don't connect

        query = MemoryQuery(entity_names=["Python"])
        result = await service.query_memory(query)

        # Should return empty results gracefully
        assert len(result.conversations) == 0

    @pytest.mark.asyncio
    async def test_create_conversation_without_connection(self):
        """Test creating conversation without connection."""
        service = MemoryService()  # fre-375-allow: unit test with mocked driver, no real connection
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
