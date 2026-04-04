"""Neo4j memory service for knowledge graph operations."""

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import orjson
import structlog

try:
    from neo4j import AsyncGraphDatabase
except ModuleNotFoundError:  # pragma: no cover - optional dependency in test environments
    AsyncGraphDatabase = None  # type: ignore[assignment]

from personal_agent.config.settings import get_settings
from personal_agent.events import (
    STREAM_MEMORY_ACCESSED,
    AccessContext,
    MemoryAccessedEvent,
    get_event_bus,
)
from personal_agent.memory.embeddings import generate_embedding
from personal_agent.memory.fact import PromotionCandidate
from personal_agent.memory.models import (
    Entity,
    EntityNode,
    MemoryQuery,
    MemoryQueryResult,
    Relationship,
    SessionNode,
    TurnNode,
)

# Backward-compatibility alias
ConversationNode = TurnNode

log = structlog.get_logger()
settings = get_settings()


class MemoryService:
    """Neo4j-based memory service for persistent knowledge graph.

    Usage:
        service = MemoryService()
        await service.connect()
        await service.create_conversation(conversation_node)
        results = await service.query_memory(MemoryQuery(entity_names=["France"]))
        await service.disconnect()
    """

    def __init__(self) -> None:  # noqa: D107
        """Initialize memory service with Neo4j connection settings."""
        self.driver: Any | None = None
        self.connected = False
        self._query_feedback_by_key: dict[str, dict[str, Any]] = {}

    async def connect(self) -> bool:
        """Connect to Neo4j database.

        Returns:
            True if connected successfully, False otherwise
        """
        if AsyncGraphDatabase is None:
            log.error("neo4j_dependency_missing")
            self.connected = False
            return False

        try:
            uri = settings.neo4j_uri
            user = settings.neo4j_user
            password = settings.neo4j_password

            self.driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
            await self.driver.verify_connectivity()
            self.connected = True
            log.info("neo4j_connected", uri=uri)
            return True
        except Exception as e:
            log.error("neo4j_connection_failed", error=str(e), exc_info=True)
            self.connected = False
            return False

    async def disconnect(self) -> None:
        """Close Neo4j connection."""
        if self.driver:
            await self.driver.close()
            self.driver = None
            self.connected = False
            log.info("neo4j_disconnected")

    async def turn_exists(self, turn_id: str) -> bool:
        """Check if a Turn node already exists (i.e. already consolidated).

        Args:
            turn_id: Turn ID (equals trace_id for the originating request).

        Returns:
            True if a Turn node with this id exists, False otherwise.
        """
        if not self.connected or not self.driver:
            return False
        try:
            async with self.driver.session() as session:
                result = await session.run(
                    "MATCH (t:Turn {turn_id: $turn_id}) RETURN t LIMIT 1",
                    turn_id=turn_id,
                )
                record = await result.single()
                return record is not None
        except Exception as e:
            log.warning("turn_exists_check_failed", error=str(e))
            return False

    async def conversation_exists(self, conversation_id: str) -> bool:
        """Backward-compatible alias for turn_exists.

        Args:
            conversation_id: Conversation/turn ID (trace_id).

        Returns:
            True if the Turn node exists.
        """
        return await self.turn_exists(conversation_id)

    async def create_conversation(self, conversation: TurnNode) -> bool:
        """Create a Turn node in the graph.

        Args:
            conversation: Turn node to create (accepts TurnNode or legacy ConversationNode).

        Returns:
            True if successful, False otherwise.
        """
        # Support both TurnNode (turn_id) and legacy ConversationNode (conversation_id)
        turn_id = getattr(conversation, "turn_id", None) or getattr(
            conversation, "conversation_id", None
        )
        if not turn_id:
            log.warning("create_conversation_missing_id")
            return False

        if not self.connected or not self.driver:
            log.warning("neo4j_not_connected")
            return False

        try:
            async with self.driver.session() as session:
                await session.run(
                    """
                    MERGE (t:Turn {turn_id: $turn_id})
                    SET t.trace_id = $trace_id,
                        t.session_id = $session_id,
                        t.sequence_number = $sequence_number,
                        t.timestamp = $timestamp,
                        t.summary = $summary,
                        t.user_message = $user_message,
                        t.assistant_response = $assistant_response,
                        t.key_entities = $key_entities,
                        t.properties = $properties
                    """,
                    turn_id=turn_id,
                    trace_id=conversation.trace_id,
                    session_id=conversation.session_id,
                    sequence_number=getattr(conversation, "sequence_number", 0),
                    timestamp=conversation.timestamp.isoformat(),
                    summary=conversation.summary,
                    user_message=conversation.user_message,
                    assistant_response=conversation.assistant_response,
                    key_entities=conversation.key_entities,
                    properties=orjson.dumps(conversation.properties).decode(),
                )

                # Create Turn→Entity DISCUSSES edges.
                # entity_types_map lets us set entity_type on the node when we know it;
                # falls back to preserving any existing type if unknown.
                entity_types_map: dict[str, str] = {}
                for entity_data in getattr(conversation, "_entity_data", []):
                    if isinstance(entity_data, dict) and entity_data.get("name"):
                        entity_types_map[entity_data["name"]] = entity_data.get("type", "")

                for entity_name in conversation.key_entities:
                    entity_type = entity_types_map.get(entity_name, "")
                    await session.run(
                        """
                        MERGE (e:Entity {name: $name})
                        SET e.last_seen = $timestamp,
                            e.mention_count = COALESCE(e.mention_count, 0) + 1,
                            e.first_seen = COALESCE(e.first_seen, $timestamp),
                            e.entity_type = CASE WHEN $entity_type <> '' THEN $entity_type
                                                 ELSE COALESCE(e.entity_type, '') END
                        WITH e
                        MATCH (t:Turn {turn_id: $turn_id})
                        MERGE (t)-[:DISCUSSES]->(e)
                        """,
                        name=entity_name,
                        entity_type=entity_type,
                        timestamp=conversation.timestamp.isoformat(),
                        turn_id=turn_id,
                    )

                log.info(
                    "turn_created",
                    turn_id=turn_id,
                    session_id=conversation.session_id,
                    entity_count=len(conversation.key_entities),
                )
                return True
        except Exception as e:
            log.error("turn_creation_failed", error=str(e), exc_info=True)
            return False

    async def create_session(self, session_node: SessionNode) -> bool:
        """Create or update a Session node in the graph.

        Args:
            session_node: Session to create or update.

        Returns:
            True if successful, False otherwise.
        """
        if not self.connected or not self.driver:
            log.warning("neo4j_not_connected")
            return False

        try:
            async with self.driver.session() as db_session:
                await db_session.run(
                    """
                    MERGE (s:Session {session_id: $session_id})
                    SET s.started_at = $started_at,
                        s.ended_at = $ended_at,
                        s.turn_count = $turn_count,
                        s.dominant_entities = $dominant_entities,
                        s.session_summary = $session_summary
                    """,
                    session_id=session_node.session_id,
                    started_at=session_node.started_at.isoformat(),
                    ended_at=session_node.ended_at.isoformat(),
                    turn_count=session_node.turn_count,
                    dominant_entities=session_node.dominant_entities,
                    session_summary=session_node.session_summary,
                )
                log.info(
                    "session_created",
                    session_id=session_node.session_id,
                    turn_count=session_node.turn_count,
                )
                return True
        except Exception as e:
            log.error("session_creation_failed", error=str(e), exc_info=True)
            return False

    async def link_session_turns(self, session_id: str) -> int:
        """Wire all Turn nodes for a session into an ordered sequence.

        Creates:
        - (Session)-[:CONTAINS {sequence}]->(Turn) for every turn
        - (Turn)-[:NEXT]->(Turn) chain ordered by timestamp
        - (Session)-[:DISCUSSES]->(Entity) aggregated from all turns

        Args:
            session_id: Session ID to link.

        Returns:
            Number of turns linked.
        """
        if not self.connected or not self.driver:
            log.warning("neo4j_not_connected")
            return 0

        try:
            async with self.driver.session() as db_session:
                # CONTAINS + sequence_number update (ordered by timestamp)
                await db_session.run(
                    """
                    MATCH (s:Session {session_id: $session_id})
                    MATCH (t:Turn {session_id: $session_id})
                    WITH s, t ORDER BY t.timestamp ASC
                    WITH s, collect(t) AS turns
                    UNWIND range(0, size(turns)-1) AS idx
                    WITH s, turns[idx] AS t, idx+1 AS seq
                    SET t.sequence_number = seq
                    MERGE (s)-[:CONTAINS {sequence: seq}]->(t)
                    """,
                    session_id=session_id,
                )

                # NEXT chain between consecutive turns
                await db_session.run(
                    """
                    MATCH (t:Turn {session_id: $session_id})
                    WITH t ORDER BY t.timestamp ASC
                    WITH collect(t) AS turns
                    UNWIND range(0, size(turns)-2) AS idx
                    WITH turns[idx] AS t1, turns[idx+1] AS t2
                    MERGE (t1)-[:NEXT]->(t2)
                    """,
                    session_id=session_id,
                )

                # Session DISCUSSES entities — aggregate from all turns
                await db_session.run(
                    """
                    MATCH (s:Session {session_id: $session_id})
                    MATCH (t:Turn {session_id: $session_id})-[:DISCUSSES]->(e:Entity)
                    WITH s, e, count(t) AS turn_count
                    MERGE (s)-[r:DISCUSSES]->(e)
                    SET r.turn_count = turn_count
                    """,
                    session_id=session_id,
                )

                # Count linked turns
                result = await db_session.run(
                    "MATCH (:Session {session_id: $session_id})-[:CONTAINS]->(t:Turn) RETURN count(t) AS cnt",
                    session_id=session_id,
                )
                record = await result.single()
                count: int = record["cnt"] if record else 0
                log.info("session_turns_linked", session_id=session_id, turn_count=count)
                return count
        except Exception as e:
            log.error("link_session_turns_failed", error=str(e), exc_info=True)
            return 0

    async def create_entity(self, entity: Entity) -> str:
        """Create or update an entity node with dedup and optional embedding.

        Args:
            entity: Entity to create.

        Returns:
            Entity ID (name-based, may be canonical name if deduplicated).
        """
        if not self.connected or not self.driver:
            log.warning("neo4j_not_connected")
            return ""

        try:
            # Generate embedding if not provided
            embedding = entity.embedding
            if embedding is None and entity.description:
                embed_text = f"{entity.name}: {entity.description}"
                embedding = await generate_embedding(embed_text)

            # Single session for dedup check + MERGE write (atomicity)
            effective_name = entity.name
            async with self.driver.session() as session:
                # Dedup check
                if embedding and any(x != 0.0 for x in embedding):
                    from personal_agent.memory.dedup import (  # noqa: PLC0415
                        DedupDecision,
                        check_entity_duplicate,
                    )

                    dedup_result = await check_entity_duplicate(
                        name=entity.name,
                        entity_type=entity.entity_type,
                        embedding=embedding,
                        neo4j_session=session,
                    )
                    if (
                        dedup_result.decision == DedupDecision.MERGE_EXISTING
                        and dedup_result.canonical_name
                    ):
                        effective_name = dedup_result.canonical_name
                        log.info(
                            "entity_deduplicated",
                            original_name=entity.name,
                            canonical_name=effective_name,
                            similarity=dedup_result.similarity_score,
                        )

                # MERGE using effective_name
                set_clauses = [
                    "e.entity_id = COALESCE(e.entity_id, $entity_id)",
                    "e.entity_type = $entity_type",
                    "e.description = $description",
                    "e.properties = $properties",
                    "e.last_seen = datetime()",
                    "e.mention_count = COALESCE(e.mention_count, 0) + 1",
                    "e.first_seen = COALESCE(e.first_seen, datetime())",
                    # Access tracking (FRE-161: KG Freshness)
                    "e.first_accessed_at = COALESCE(e.first_accessed_at, datetime())",
                    "e.last_accessed_at = datetime()",
                    "e.access_count = COALESCE(e.access_count, 0)",
                    "e.last_access_context = COALESCE(e.last_access_context, 'created')",
                ]
                params: dict[str, Any] = {
                    "name": effective_name,
                    "entity_id": effective_name,
                    "entity_type": entity.entity_type,
                    "description": entity.description,
                    "properties": orjson.dumps(entity.properties).decode(),
                }

                if embedding is not None:
                    set_clauses.append("e.embedding = $embedding")
                    params["embedding"] = embedding

                if entity.coordinates is not None:
                    set_clauses.append(
                        "e.location = point({latitude: $latitude, longitude: $longitude})"
                    )
                    params["latitude"] = entity.coordinates[0]
                    params["longitude"] = entity.coordinates[1]

                if entity.geocoded:
                    set_clauses.append("e.geocoded = $geocoded")
                    params["geocoded"] = entity.geocoded

                query = (
                    "MERGE (e:Entity {name: $name})\n"
                    "SET " + ",\n    ".join(set_clauses) + "\n"
                    "RETURN e.name as entity_id"
                )

                result = await session.run(query, **params)
                record = await result.single()
                entity_id: str = record["entity_id"] if record else effective_name
                log.info("entity_created", entity_id=entity_id, entity_type=entity.entity_type)
                return entity_id
        except Exception as e:
            log.error("entity_creation_failed", error=str(e), exc_info=True)
            return ""

    async def ensure_vector_index(self) -> bool:
        """Create Neo4j vector index on Entity.embedding, recreating if dimensions changed.

        Drops and recreates the index when the configured embedding dimensions differ
        from what is already indexed (e.g. after switching from 768-dim to 1024-dim
        embeddings). Requires Neo4j 5.11+.

        Returns:
            True if index exists or was created successfully.
        """
        if not self.connected or not self.driver:
            return False

        try:
            current_settings = get_settings()
            target_dims = current_settings.embedding_dimensions

            async with self.driver.session() as session:
                # Check existing index dimensions
                result = await session.run(
                    """
                    SHOW VECTOR INDEXES
                    YIELD name, options
                    WHERE name = 'entity_embedding'
                    RETURN options
                    """,
                )
                rows = await result.data()
                if rows:
                    existing_dims = (
                        rows[0].get("options", {}).get("indexConfig", {}).get("vector.dimensions")
                    )
                    if existing_dims is not None and int(existing_dims) != target_dims:
                        log.warning(
                            "vector_index_dimension_mismatch",
                            existing_dims=existing_dims,
                            target_dims=target_dims,
                            action="drop_and_recreate",
                        )
                        await session.run("DROP INDEX entity_embedding IF EXISTS")

                await session.run(
                    """
                    CREATE VECTOR INDEX entity_embedding IF NOT EXISTS
                    FOR (e:Entity)
                    ON (e.embedding)
                    OPTIONS {
                        indexConfig: {
                            `vector.dimensions`: $dimensions,
                            `vector.similarity_function`: 'cosine'
                        }
                    }
                    """,
                    dimensions=target_dims,
                )
                log.info(
                    "vector_index_ensured",
                    index_name="entity_embedding",
                    dimensions=target_dims,
                )
                return True
        except Exception as e:
            log.error("vector_index_creation_failed", error=str(e), exc_info=True)
            return False

    async def create_relationship(self, relationship: Relationship) -> bool:
        """Create a relationship between nodes.

        Args:
            relationship: Relationship to create

        Returns:
            True if successful, False otherwise
        """
        if not self.connected or not self.driver:
            log.warning("neo4j_not_connected")
            return False

        try:
            async with self.driver.session() as session:
                # Use APOC to create a relationship with a dynamic type label.
                # Standard Cypher cannot parameterize relationship type labels;
                # apoc.merge.relationship handles this cleanly.
                # Access tracking properties (FRE-161: KG Freshness) are initialized on creation.
                await session.run(
                    """
                    MATCH (source)
                    WHERE source.entity_id = $source_id OR source.name = $source_id
                    MATCH (target)
                    WHERE target.entity_id = $target_id OR target.name = $target_id
                    CALL apoc.merge.relationship(
                        source, $relationship_type,
                        {},
                        {
                            weight: $weight,
                            created_at: datetime(),
                            first_accessed_at: datetime(),
                            last_accessed_at: datetime(),
                            access_count: 0,
                            last_access_context: 'created'
                        },
                        target
                    ) YIELD rel
                    RETURN rel
                    """,
                    source_id=relationship.source_id,
                    target_id=relationship.target_id,
                    relationship_type=relationship.relationship_type,
                    weight=relationship.weight,
                )
                log.info(
                    "relationship_created",
                    source=relationship.source_id,
                    target=relationship.target_id,
                    type=relationship.relationship_type,
                )
                return True
        except Exception as e:
            log.error("relationship_creation_failed", error=str(e), exc_info=True)
            return False

    async def query_memory(
        self,
        query: MemoryQuery,
        feedback_key: str | None = None,
        query_text: str | None = None,
        access_context: AccessContext = AccessContext.SEARCH,
        trace_id: str | None = None,
        session_id: str | None = None,
    ) -> MemoryQueryResult:
        """Query memory graph for relevant conversations and entities.

        Args:
            query: Query parameters
            feedback_key: Optional session/user key for implicit feedback tracking.
            query_text: Optional original user query text. When provided,
                generates a vector embedding for hybrid similarity search
                and enables implicit rephrase detection for feedback tracking.
            access_context: Typed context where the query originated.
                Used for access tracking events (ADR-0042).
            trace_id: Optional request trace identifier for event correlation.
            session_id: Optional session identifier for event correlation.

        Returns:
            MemoryQueryResult with conversations, entities, and relationships
        """
        if not self.connected or not self.driver:
            log.warning("neo4j_not_connected")
            return MemoryQueryResult()

        try:
            async with self.driver.session() as session:
                # Build Cypher query dynamically based on query parameters
                cypher_parts = []

                # Build main query - find turns related to entities
                if query.entity_names or query.entity_types:
                    base_query = """
                    MATCH (c:Turn)-[:DISCUSSES]->(e:Entity)
                    WHERE """
                    if query.entity_names:
                        base_query += "e.name IN $entity_names"
                        cypher_parts.append("entity_names: $entity_names")
                    elif query.entity_types:
                        base_query += "e.entity_type IN $entity_types"
                        cypher_parts.append("entity_types: $entity_types")
                elif query.conversation_ids or query.trace_ids:
                    # Direct turn/trace lookup (conversation_ids maps to turn_id)
                    base_query = "MATCH (c:Turn) WHERE "
                    if query.conversation_ids:
                        base_query += "c.turn_id IN $conversation_ids"
                        cypher_parts.append("conversation_ids: $conversation_ids")
                    elif query.trace_ids:
                        base_query += "c.trace_id IN $trace_ids"
                        cypher_parts.append("trace_ids: $trace_ids")
                else:
                    base_query = "MATCH (c:Turn)"

                # Add WHERE clauses for recency
                if query.recency_days:
                    cutoff_date = (
                        datetime.utcnow() - timedelta(days=query.recency_days)
                    ).isoformat()
                    if "WHERE" in base_query:
                        base_query += " AND c.timestamp >= $cutoff_date"
                    else:
                        base_query += " WHERE c.timestamp >= $cutoff_date"

                # Add ordering and limiting
                base_query += """
                RETURN DISTINCT c
                ORDER BY c.timestamp DESC
                LIMIT $limit
                """

                # Execute query
                params: dict[str, Any] = {
                    "limit": query.limit,
                    "max_depth": query.max_depth,
                }

                if query.entity_names:
                    params["entity_names"] = query.entity_names
                if query.entity_types:
                    params["entity_types"] = query.entity_types
                if query.conversation_ids:
                    params["conversation_ids"] = query.conversation_ids
                if query.trace_ids:
                    params["trace_ids"] = query.trace_ids
                if query.recency_days:
                    params["cutoff_date"] = cutoff_date

                result = await session.run(base_query, parameters=params)
                records = await result.values()

                # Parse results
                conversations = []
                for record in records:
                    if record and record[0]:
                        node = record[0]
                        # Support both Turn nodes (turn_id) and legacy Conversation nodes
                        turn_id = node.get("turn_id") or node.get("conversation_id", "")
                        conversations.append(
                            TurnNode(
                                turn_id=turn_id,
                                trace_id=node.get("trace_id"),
                                session_id=node.get("session_id"),
                                sequence_number=node.get("sequence_number", 0),
                                timestamp=datetime.fromisoformat(
                                    node.get("timestamp", datetime.utcnow().isoformat())
                                ),
                                summary=node.get("summary"),
                                user_message=node.get("user_message", ""),
                                assistant_response=node.get("assistant_response"),
                                key_entities=node.get("key_entities", []),
                                properties=orjson.loads(node.get("properties", "{}"))
                                if isinstance(node.get("properties"), str)
                                else node.get("properties", {}),
                            )
                        )

                # --- Hybrid: vector similarity search ---
                vector_results: list[Any] = []
                if query_text:
                    try:
                        query_embedding = await generate_embedding(query_text, mode="query")
                        if any(x != 0.0 for x in query_embedding):
                            vector_result = await session.run(
                                """
                                CALL db.index.vector.queryNodes(
                                    'entity_embedding', $top_k, $embedding
                                )
                                YIELD node, score
                                RETURN node.name AS name,
                                       node.entity_type AS entity_type,
                                       node.description AS description,
                                       score
                                ORDER BY score DESC
                                """,
                                top_k=min(query.limit, 20),
                                embedding=query_embedding,
                            )
                            vector_results = await vector_result.data()
                    except Exception as vec_exc:
                        log.warning(
                            "vector_search_failed",
                            error=str(vec_exc),
                            query_text_length=len(query_text),
                        )

                # Build vector_scores dict for relevance calculation
                vector_scores: dict[str, float] = {}
                for vr in vector_results:
                    if "name" in vr and "score" in vr:
                        vector_scores[vr["name"]] = float(vr["score"])

                # --- Reranker: re-score candidates via cross-attention ---
                reranker_scores: dict[str, float] = {}
                current_settings = get_settings()
                if current_settings.reranker_enabled and query_text and len(conversations) > 1:
                    try:
                        from personal_agent.memory.reranker import rerank  # noqa: PLC0415

                        docs = [c.summary or c.user_message or "" for c in conversations]
                        rerank_results = await rerank(
                            query=query_text,
                            documents=docs,
                            top_k=current_settings.reranker_top_k,
                        )
                        # Map conversation turn_id to normalized reranker score
                        if rerank_results:
                            max_score = max(r.score for r in rerank_results)
                            for rr in rerank_results:
                                if rr.index < len(conversations):
                                    norm = rr.score / max_score if max_score > 0 else 0.0
                                    reranker_scores[conversations[rr.index].turn_id] = norm
                    except Exception as rerank_exc:
                        log.warning(
                            "reranker_integration_failed",
                            error=str(rerank_exc),
                        )

                # Calculate plausibility/relevance scores
                relevance_scores = await self._calculate_relevance_scores(
                    conversations,
                    query,
                    vector_scores=vector_scores,
                    reranker_scores=reranker_scores,
                )

                log.info(
                    "memory_query_completed",
                    query_params=cypher_parts,
                    result_count=len(conversations),
                )

                self._log_query_quality_metrics(
                    query=query,
                    relevance_scores=relevance_scores,
                    feedback_key=feedback_key,
                    query_text=query_text,
                )

                result = MemoryQueryResult(
                    conversations=conversations,
                    relevance_scores=relevance_scores,
                )

                # Publish memory access event (Phase 4)
                # Collect entity IDs from query parameters and conversation results
                accessed_entity_ids = list(query.entity_names or [])
                for conversation in conversations:
                    accessed_entity_ids.extend(conversation.key_entities or [])
                # Remove duplicates while preserving order
                accessed_entity_ids = list(dict.fromkeys(accessed_entity_ids))

                if settings.freshness_enabled and accessed_entity_ids and trace_id:
                    event = MemoryAccessedEvent(
                        entity_ids=accessed_entity_ids,
                        relationship_ids=[],
                        access_context=access_context,
                        query_type="query_memory",
                        trace_id=trace_id,
                        session_id=session_id,
                    )
                    bus = get_event_bus()
                    try:
                        await bus.publish(STREAM_MEMORY_ACCESSED, event)
                        log.debug(
                            "memory_access_event_published",
                            trace_id=trace_id,
                            entity_count=len(accessed_entity_ids),
                            access_context=access_context.value,
                        )
                    except Exception as e:
                        log.warning(
                            "memory_access_event_publish_failed",
                            error=str(e),
                            event_id=event.event_id,
                            trace_id=trace_id,
                        )

                return result

        except Exception as e:
            log.error("memory_query_failed", error=str(e), exc_info=True)
            return MemoryQueryResult()

    async def query_memory_broad(
        self,
        entity_types: list[str] | None = None,
        recency_days: int = 90,
        limit: int = 20,
        access_context: AccessContext = AccessContext.SEARCH,
        trace_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Broad memory recall: return entities and session summaries (ADR-0025).

        Used for recall-intent queries ("what have I asked about?") where
        there are no specific entity names to search for.

        Args:
            entity_types: Optional filter e.g. ["Location", "Person"]. None = all types.
            recency_days: How far back to look.
            limit: Maximum entities to return.
            access_context: Typed context where the query originated (ADR-0042).
            trace_id: Optional request trace identifier for event correlation.
            session_id: Optional session identifier for event correlation.

        Returns:
            Dict with keys:
              - entities: list of {name, type, mentions, description}
              - sessions: list of {session_id, dominant_entities, turn_count, started_at}
              - turns_summary: list of recent turn summaries
        """
        if not self.connected or not self.driver:
            return {"entities": [], "sessions": [], "turns_summary": []}

        cutoff = (datetime.now(timezone.utc) - timedelta(days=recency_days)).isoformat()

        try:
            async with self.driver.session() as db_session:
                # Entities (optionally filtered by type)
                if entity_types:
                    entity_q = """
                        MATCH (e:Entity)<-[:DISCUSSES]-(t:Turn)
                        WHERE e.entity_type IN $entity_types
                          AND t.timestamp >= $cutoff
                        RETURN e.name as name, e.entity_type as type,
                               e.description as description,
                               count(t) as mentions
                        ORDER BY mentions DESC LIMIT $limit
                    """
                    r = await db_session.run(
                        entity_q,
                        entity_types=entity_types,
                        cutoff=cutoff,
                        limit=limit,
                    )
                else:
                    entity_q = """
                        MATCH (e:Entity)<-[:DISCUSSES]-(t:Turn)
                        WHERE t.timestamp >= $cutoff
                        RETURN e.name as name, e.entity_type as type,
                               e.description as description,
                               count(t) as mentions
                        ORDER BY mentions DESC LIMIT $limit
                    """
                    r = await db_session.run(entity_q, cutoff=cutoff, limit=limit)
                entities = await r.data()

                # Recent sessions with dominant topics
                session_q = """
                    MATCH (s:Session)
                    WHERE s.started_at >= $cutoff
                    RETURN s.session_id as session_id,
                           s.dominant_entities as dominant_entities,
                           s.turn_count as turn_count,
                           s.started_at as started_at
                    ORDER BY s.started_at DESC LIMIT 10
                """
                r = await db_session.run(session_q, cutoff=cutoff)
                sessions = await r.data()

                # Recent turn summaries
                turn_q = """
                    MATCH (t:Turn)
                    WHERE t.timestamp >= $cutoff
                    RETURN t.summary as summary, t.key_entities as entities,
                           t.timestamp as ts
                    ORDER BY t.timestamp DESC LIMIT 10
                """
                r = await db_session.run(turn_q, cutoff=cutoff)
                turns = await r.data()

                payload = {
                    "entities": entities,
                    "sessions": sessions,
                    "turns_summary": turns,
                }

                # Publish memory access event (Phase 4 / ADR-0042)
                if settings.freshness_enabled and trace_id and entities:
                    accessed_entity_ids = [
                        e["name"] for e in entities if isinstance(e, dict) and e.get("name")
                    ]
                    if accessed_entity_ids:
                        event = MemoryAccessedEvent(
                            entity_ids=accessed_entity_ids,
                            relationship_ids=[],
                            access_context=access_context,
                            query_type="query_memory_broad",
                            trace_id=trace_id,
                            session_id=session_id,
                        )
                        bus = get_event_bus()
                        try:
                            await bus.publish(STREAM_MEMORY_ACCESSED, event)
                            log.debug(
                                "memory_access_event_published",
                                trace_id=trace_id,
                                entity_count=len(accessed_entity_ids),
                                access_context=access_context.value,
                            )
                        except Exception as pub_exc:
                            log.warning(
                                "memory_access_event_publish_failed",
                                error=str(pub_exc),
                                event_id=event.event_id,
                                trace_id=trace_id,
                            )

                return payload

        except Exception as e:
            log.error("query_memory_broad_failed", error=str(e), exc_info=True)
            return {"entities": [], "sessions": [], "turns_summary": []}

    def _log_query_quality_metrics(
        self,
        query: MemoryQuery,
        relevance_scores: dict[str, float],
        feedback_key: str | None,
        query_text: str | None,
    ) -> None:
        """Emit memory query quality metrics and implicit feedback signal."""
        result_count = len(relevance_scores)
        avg_relevance = sum(relevance_scores.values()) / result_count if result_count > 0 else 0.0
        max_relevance = max(relevance_scores.values(), default=0.0)
        min_relevance = min(relevance_scores.values(), default=0.0)
        query_signature = self._build_query_signature(query, query_text)
        state_key = feedback_key or "global"
        previous_state = self._query_feedback_by_key.get(state_key)
        implicit_rephrase = self._detect_implicit_rephrase(previous_state, query_signature)

        log.info(
            "memory_query_quality_metrics",
            query_type=self._classify_query_type(query),
            result_count=result_count,
            avg_relevance_score=round(avg_relevance, 4),
            max_relevance_score=round(max_relevance, 4),
            min_relevance_score=round(min_relevance, 4),
            entity_filter_count=len(query.entity_names),
            entity_type_filter_count=len(query.entity_types),
            trace_filter_count=len(query.trace_ids),
            conversation_filter_count=len(query.conversation_ids),
            recency_days=query.recency_days,
            implicit_rephrase_detected=implicit_rephrase,
            previous_result_count=(previous_state or {}).get("result_count"),
        )
        self._query_feedback_by_key[state_key] = {
            "signature": query_signature,
            "result_count": result_count,
            "timestamp": datetime.now(timezone.utc),
        }

    def _classify_query_type(self, query: MemoryQuery) -> str:
        """Classify query shape for analytics aggregation."""
        if query.entity_names:
            return "entity_name_lookup"
        if query.entity_types:
            return "entity_type_lookup"
        if query.conversation_ids:
            return "conversation_lookup"
        if query.trace_ids:
            return "trace_lookup"
        return "recent_conversations"

    def _build_query_signature(self, query: MemoryQuery, query_text: str | None) -> str:
        """Create normalized signature for implicit feedback tracking."""
        normalized_text = (query_text or "").strip().lower()
        entity_names = ",".join(sorted(name.lower() for name in query.entity_names))
        entity_types = ",".join(sorted(entity_type.lower() for entity_type in query.entity_types))
        conversation_ids = ",".join(sorted(query.conversation_ids))
        trace_ids = ",".join(sorted(query.trace_ids))
        return (
            f"text={normalized_text}|entities={entity_names}|types={entity_types}|"
            f"conversations={conversation_ids}|traces={trace_ids}|recency={query.recency_days}"
        )

    def _detect_implicit_rephrase(
        self,
        previous_state: dict[str, Any] | None,
        current_signature: str,
    ) -> bool:
        """Detect likely rephrase from sequential query behavior."""
        if not previous_state:
            return False

        previous_signature = str(previous_state.get("signature", ""))
        previous_result_count = int(previous_state.get("result_count", 0) or 0)
        previous_timestamp = previous_state.get("timestamp")
        if not isinstance(previous_timestamp, datetime):
            return False

        recency_seconds = (datetime.now(timezone.utc) - previous_timestamp).total_seconds()
        if recency_seconds > 600:  # 10 minutes
            return False
        if previous_signature == current_signature:
            return False
        return previous_result_count <= 1

    async def _calculate_relevance_scores(
        self,
        conversations: list[TurnNode],
        query: MemoryQuery,
        vector_scores: dict[str, float] | None = None,
        reranker_scores: dict[str, float] | None = None,
    ) -> dict[str, float]:
        """Calculate relevance/plausibility scores for conversations.

        Scoring factors depend on which signals are available:

        Base weights (no vector, no reranker):
        1. Recency: 0-0.4
        2. Entity match: 0-0.4
        3. Entity importance: 0-0.2

        Hybrid (vector only):
        1. Recency: 0-0.3
        2. Entity match: 0-0.3
        3. Entity importance: 0-0.15
        4. Vector similarity: 0-0.25

        Full pipeline (vector + reranker):
        1. Recency: 0-0.20
        2. Entity match: 0-0.20
        3. Entity importance: 0-0.10
        4. Vector similarity: 0-0.15
        5. Reranker score: 0-0.35

        Full pipeline + freshness (all signals including access data):
        1. Recency: 0-0.15
        2. Entity match: 0-0.20
        3. Entity importance: 0-0.05
        4. Vector similarity: 0-0.15
        5. Reranker score: 0-0.30
        6. Freshness: 0-0.15

        Args:
            conversations: List of conversations to score.
            query: Original query with entity filters.
            vector_scores: Optional dict mapping entity name to cosine similarity
                score (0-1) from vector index search.
            reranker_scores: Optional dict mapping turn_id to normalized reranker
                relevance score (0-1) from cross-attention reranking.

        Returns:
            Dict mapping conversation_id to relevance score (0.0-1.0).
        """
        if not conversations:
            return {}

        scores: dict[str, float] = {}

        # Normalize optional score dicts to concrete dicts (simplifies type narrowing)
        _vector_scores: dict[str, float] = vector_scores if vector_scores is not None else {}
        _reranker_scores: dict[str, float] = reranker_scores if reranker_scores is not None else {}

        # Normalize all timestamps to naive UTC to avoid mixed tz comparisons
        now = datetime.utcnow()

        def _to_naive_utc(dt: datetime) -> datetime:
            if dt.tzinfo is not None:
                return dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt

        # Find oldest conversation for recency normalization
        oldest_timestamp = min(_to_naive_utc(c.timestamp) for c in conversations)
        time_range = (now - oldest_timestamp).total_seconds()

        # Get entity importance scores if querying by entities
        entity_importance: dict[str, float] = {}
        if query.entity_names and self.driver:
            try:
                async with self.driver.session() as session:
                    result = await session.run(
                        """
                        MATCH (e:Entity)
                        WHERE e.name IN $entity_names
                        RETURN e.name as name, e.mention_count as mentions
                        """,
                        entity_names=query.entity_names,
                    )
                    async for record in result:
                        name = record["name"]
                        mentions = record.get("mentions", 0)
                        # Normalize to 0-1 (cap at 100 mentions)
                        entity_importance[name] = min(mentions / 100.0, 1.0)
            except Exception as e:
                log.warning("entity_importance_fetch_failed", error=str(e))

        # Fetch freshness data when access tracking is enabled (ADR-0042 Step 5)
        # entity_name -> freshness score in [0.0, 1.0]
        freshness_scores: dict[str, float] = {}
        current_settings = get_settings()
        if current_settings.freshness_enabled and query.entity_names and self.driver:
            from personal_agent.memory.freshness import compute_freshness  # noqa: PLC0415

            try:
                async with self.driver.session() as session:
                    result = await session.run(
                        """
                        MATCH (e:Entity)
                        WHERE e.name IN $entity_names
                          AND e.access_count IS NOT NULL
                          AND e.access_count > 0
                        RETURN e.name AS name,
                               e.last_accessed_at AS last_accessed_at,
                               e.access_count AS access_count
                        """,
                        entity_names=query.entity_names,
                    )
                    async for record in result:
                        raw_ts = record.get("last_accessed_at")
                        last_accessed_at: datetime | None = None
                        if raw_ts is not None:
                            try:
                                last_accessed_at = datetime.fromisoformat(str(raw_ts))
                            except (ValueError, TypeError):
                                last_accessed_at = None
                        fs = compute_freshness(
                            last_accessed_at=last_accessed_at,
                            access_count=int(record.get("access_count") or 0),
                            half_life_days=current_settings.freshness_half_life_days,
                            alpha=current_settings.freshness_frequency_boost_alpha,
                            max_boost=current_settings.freshness_frequency_boost_max,
                        )
                        freshness_scores[record["name"]] = fs
            except Exception as e:
                log.warning("freshness_scores_fetch_failed", error=str(e))

        # Determine weight scheme based on available signals
        use_vector = bool(_vector_scores)
        use_reranker = bool(_reranker_scores)
        use_freshness = current_settings.freshness_enabled and bool(freshness_scores)
        # freshness_weight from config; only active when access data is available.
        # When active, all other weights are scaled by (1 - w_freshness) so they
        # sum to 1.0 regardless of which signals are present (graceful degradation).
        w_freshness_cfg = current_settings.freshness_relevance_weight if use_freshness else 0.0
        w_scale = 1.0 - w_freshness_cfg  # redistribution factor for non-freshness signals
        if use_vector and use_reranker:
            w_recency = 0.20 * w_scale
            w_entity_match = 0.20 * w_scale
            w_importance = 0.10 * w_scale
            w_vector = 0.15 * w_scale
            w_reranker = 0.35 * w_scale
        elif use_vector:
            w_recency = 0.30 * w_scale
            w_entity_match = 0.30 * w_scale
            w_importance = 0.15 * w_scale
            w_vector = 0.25 * w_scale
            w_reranker = 0.0
        else:
            w_recency = 0.40 * w_scale
            w_entity_match = 0.40 * w_scale
            w_importance = 0.20 * w_scale
            w_vector = 0.0
            w_reranker = 0.0

        # Calculate scores for each conversation
        for conv in conversations:
            # Per-conversation vector overlap check: if this conversation has
            # no entities matching the vector results, fall back to non-hybrid
            # weights to avoid score deflation (the 0.25 vector slot would be 0).
            conv_has_vector_hit = False
            best_vector_score = 0.0
            if use_vector and conv.key_entities:
                entity_vector_scores = [
                    _vector_scores[entity]
                    for entity in conv.key_entities
                    if entity in _vector_scores
                ]
                if entity_vector_scores:
                    conv_has_vector_hit = True
                    best_vector_score = max(entity_vector_scores)

            # Check if this conversation has a reranker score
            conv_reranker_score = _reranker_scores.get(conv.turn_id, 0.0)
            conv_has_reranker = use_reranker and conv.turn_id in _reranker_scores

            if conv_has_vector_hit:
                cw_recency, cw_entity, cw_importance, cw_vector = (
                    w_recency,
                    w_entity_match,
                    w_importance,
                    w_vector,
                )
                cw_reranker = w_reranker if conv_has_reranker else 0.0
            else:
                # Non-hybrid weights for this conversation (redistribute reranker weight)
                if conv_has_reranker:
                    cw_recency, cw_entity, cw_importance, cw_vector = (
                        0.25 * w_scale,
                        0.25 * w_scale,
                        0.15 * w_scale,
                        0.0,
                    )
                    cw_reranker = 0.35 * w_scale
                else:
                    cw_recency, cw_entity, cw_importance, cw_vector = (
                        0.40 * w_scale,
                        0.40 * w_scale,
                        0.20 * w_scale,
                        0.0,
                    )
                    cw_reranker = 0.0

            score = 0.0

            # 1. Recency score
            if time_range > 0:
                age_seconds = (now - _to_naive_utc(conv.timestamp)).total_seconds()
                recency_ratio = 1.0 - (age_seconds / time_range)
                score += recency_ratio * cw_recency
            else:
                score += cw_recency  # All same timestamp

            # 2. Entity match score
            if query.entity_names:
                matched_entities = set(query.entity_names) & set(conv.key_entities)
                match_ratio = len(matched_entities) / len(query.entity_names)
                score += match_ratio * cw_entity
            else:
                score += cw_entity * 0.5  # No entity filter, give neutral score

            # 3. Entity importance score
            if entity_importance:
                matched_importances = [
                    entity_importance.get(entity, 0.0)
                    for entity in conv.key_entities
                    if entity in entity_importance
                ]
                if matched_importances:
                    avg_importance = sum(matched_importances) / len(matched_importances)
                    score += avg_importance * cw_importance

            # 4. Vector similarity score (hybrid mode only)
            if conv_has_vector_hit:
                score += best_vector_score * cw_vector

            # 5. Reranker score (cross-attention relevance)
            if conv_has_reranker:
                score += conv_reranker_score * cw_reranker

            # 6. Freshness score (access recency × frequency, ADR-0042)
            # Uses max freshness score across matched query entities for this conversation.
            # When no freshness data is available for any of this conversation's entities,
            # the factor is skipped and the redistributed weight stays with the other signals.
            if use_freshness and conv.key_entities:
                conv_freshness_scores = [
                    freshness_scores[e]
                    for e in conv.key_entities
                    if e in freshness_scores and freshness_scores[e] > 0.0
                ]
                if conv_freshness_scores:
                    score += max(conv_freshness_scores) * w_freshness_cfg

            scores[conv.turn_id] = min(score, 1.0)  # Cap at 1.0

        return scores

    async def get_related_conversations(
        self, entity_names: list[str], limit: int = 10
    ) -> list[TurnNode]:
        """Get conversations related to given entities.

        Args:
            entity_names: List of entity names to search for
            limit: Maximum number of conversations to return

        Returns:
            List of related conversations
        """
        query = MemoryQuery(entity_names=entity_names, limit=limit)
        result = await self.query_memory(query)
        return result.conversations

    async def get_user_interests(self, limit: int = 20) -> list[EntityNode]:
        """Get entities the user frequently mentions (interest profile).

        Args:
            limit: Maximum number of entities to return

        Returns:
            List of entities sorted by mention frequency
        """
        if not self.connected or not self.driver:
            log.warning("neo4j_not_connected")
            return []

        try:
            async with self.driver.session() as session:
                result = await session.run(
                    """
                    MATCH (e:Entity)
                    WHERE e.mention_count > 0
                    RETURN e
                    ORDER BY e.mention_count DESC, e.last_seen DESC
                    LIMIT $limit
                    """,
                    limit=limit,
                )

                entities = []
                async for record in result:
                    node = record["e"]

                    # Handle datetime fields (Neo4j returns neo4j.time.DateTime objects)
                    first_seen = node.get("first_seen")
                    if hasattr(first_seen, "to_native"):
                        first_seen = first_seen.to_native()
                    elif isinstance(first_seen, str):
                        first_seen = datetime.fromisoformat(first_seen)
                    elif first_seen is None:
                        first_seen = datetime.utcnow()

                    last_seen = node.get("last_seen")
                    if hasattr(last_seen, "to_native"):
                        last_seen = last_seen.to_native()
                    elif isinstance(last_seen, str):
                        last_seen = datetime.fromisoformat(last_seen)
                    elif last_seen is None:
                        last_seen = datetime.utcnow()

                    # Handle properties (stored as JSON string, needs deserialization)
                    properties = node.get("properties", "{}")
                    if isinstance(properties, str):
                        properties = orjson.loads(properties)
                    elif properties is None:
                        properties = {}

                    entities.append(
                        EntityNode(
                            entity_id=node.get("name", ""),
                            name=node.get("name", ""),
                            entity_type=node.get("entity_type", "Unknown"),
                            description=node.get("description"),
                            interest_weight=min(
                                node.get("mention_count", 0) / 100.0, 1.0
                            ),  # Normalize to 0-1
                            first_seen=first_seen,
                            last_seen=last_seen,
                            mention_count=node.get("mention_count", 0),
                            properties=properties,
                        )
                    )

                log.info("user_interests_retrieved", count=len(entities))
                return entities
        except Exception as e:
            log.error("user_interests_query_failed", error=str(e), exc_info=True)
            return []

    async def promote_entity(
        self,
        entity_name: str,
        confidence: float,
        source_turn_ids: list[str],
        trace_id: str = "",
    ) -> bool:
        """Promote an entity to semantic memory.

        Sets memory_type='semantic', confidence, promoted_at on the Entity node.

        Args:
            entity_name: The entity to promote.
            confidence: Confidence score for the semantic fact.
            source_turn_ids: Turn IDs supporting this promotion.
            trace_id: Request trace identifier.

        Returns:
            True if the entity was found and promoted.
        """
        if not self.driver:
            log.warning(
                "promote_entity_no_driver",
                entity_name=entity_name,
                trace_id=trace_id,
            )
            return False

        query = """
        MATCH (e:Entity {name: $name})
        SET e.memory_type = 'semantic',
            e.confidence = $confidence,
            e.promoted_at = datetime(),
            e.source_turn_ids = $source_turn_ids
        RETURN e.name AS name, e.entity_type AS entity_type,
               e.mention_count AS mention_count
        """

        try:
            async with self.driver.session() as session:
                result = await session.run(
                    query,
                    name=entity_name,
                    confidence=confidence,
                    source_turn_ids=source_turn_ids,
                )
                record = await result.single()
                if record is None:
                    log.debug(
                        "promote_entity_not_found",
                        entity_name=entity_name,
                        trace_id=trace_id,
                    )
                    return False

                log.info(
                    "promote_entity_success",
                    entity_name=entity_name,
                    entity_type=record["entity_type"],
                    confidence=confidence,
                    trace_id=trace_id,
                )
                return True
        except Exception:
            log.warning(
                "promote_entity_neo4j_error",
                entity_name=entity_name,
                trace_id=trace_id,
                exc_info=True,
            )
            return False

    async def get_promotion_candidates(
        self,
        min_mentions: int = 1,
        exclude_already_promoted: bool = True,
    ) -> Sequence[PromotionCandidate]:
        """Query Neo4j for entities eligible for episodic→semantic promotion.

        Args:
            min_mentions: Minimum mention count to include an entity.
            exclude_already_promoted: If True, skip entities already promoted
                to semantic memory.

        Returns:
            Sequence of PromotionCandidate ordered by mention count descending.
        """
        if not self.driver:
            log.warning("get_promotion_candidates_no_driver")
            return []

        where_clause = "WHERE e.mention_count >= $min_mentions"
        if exclude_already_promoted:
            where_clause += " AND (e.memory_type IS NULL OR e.memory_type <> 'semantic')"

        query = f"""
        MATCH (e:Entity)
        {where_clause}
        OPTIONAL MATCH (e)<-[:DISCUSSES]-(t:Turn)
        WITH e, collect(t.turn_id) AS turn_ids
        RETURN e.name AS name,
               e.entity_type AS entity_type,
               coalesce(e.mention_count, 1) AS mention_count,
               e.first_seen AS first_seen,
               e.last_seen AS last_seen,
               e.description AS description,
               turn_ids
        ORDER BY e.mention_count DESC
        """

        try:
            async with self.driver.session() as session:
                result = await session.run(query, min_mentions=min_mentions)
                records = await result.data()

            now = datetime.now(timezone.utc)
            candidates: list[PromotionCandidate] = []
            for row in records:
                first_seen = row.get("first_seen")
                last_seen = row.get("last_seen")
                # Neo4j returns its own DateTime type — convert to timezone-aware Python datetime
                if hasattr(first_seen, "to_native"):
                    first_seen = first_seen.to_native()
                elif isinstance(first_seen, str):
                    first_seen = datetime.fromisoformat(first_seen)
                if isinstance(first_seen, datetime) and first_seen.tzinfo is None:
                    first_seen = first_seen.replace(tzinfo=timezone.utc)
                if hasattr(last_seen, "to_native"):
                    last_seen = last_seen.to_native()
                elif isinstance(last_seen, str):
                    last_seen = datetime.fromisoformat(last_seen)
                if isinstance(last_seen, datetime) and last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)
                candidates.append(
                    PromotionCandidate(
                        entity_name=row["name"],
                        entity_type=row.get("entity_type") or "unknown",
                        mention_count=row["mention_count"],
                        first_seen=first_seen or now,
                        last_seen=last_seen or now,
                        source_turn_ids=[t for t in (row.get("turn_ids") or []) if t],
                        description=row.get("description"),
                    )
                )

            log.info(
                "promotion_candidates_queried",
                total=len(candidates),
                min_mentions=min_mentions,
                exclude_promoted=exclude_already_promoted,
            )
            return candidates

        except Exception:
            log.warning("get_promotion_candidates_failed", exc_info=True)
            return []
