"""Neo4j memory service for knowledge graph operations."""

from datetime import datetime, timedelta, timezone
from typing import Any

import orjson
import structlog
from neo4j import AsyncGraphDatabase

from personal_agent.config.settings import get_settings
from personal_agent.memory.models import (
    ConversationNode,
    Entity,
    EntityNode,
    MemoryQuery,
    MemoryQueryResult,
    Relationship,
)

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

    async def connect(self) -> bool:
        """Connect to Neo4j database.

        Returns:
            True if connected successfully, False otherwise
        """
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

    async def create_conversation(self, conversation: ConversationNode) -> bool:
        """Create a conversation node in the graph.

        Args:
            conversation: Conversation node to create

        Returns:
            True if successful, False otherwise
        """
        if not self.connected or not self.driver:
            log.warning("neo4j_not_connected")
            return False

        try:
            async with self.driver.session() as session:
                # Create conversation node
                await session.run(
                    """
                    MERGE (c:Conversation {conversation_id: $conversation_id})
                    SET c.trace_id = $trace_id,
                        c.session_id = $session_id,
                        c.timestamp = $timestamp,
                        c.summary = $summary,
                        c.user_message = $user_message,
                        c.assistant_response = $assistant_response,
                        c.key_entities = $key_entities,
                        c.properties = $properties
                    """,
                    conversation_id=conversation.conversation_id,
                    trace_id=conversation.trace_id,
                    session_id=conversation.session_id,
                    timestamp=conversation.timestamp.isoformat(),
                    summary=conversation.summary,
                    user_message=conversation.user_message,
                    assistant_response=conversation.assistant_response,
                    key_entities=conversation.key_entities,
                    properties=orjson.dumps(
                        conversation.properties
                    ).decode(),  # Serialize dict to JSON string
                )

                # Create entity nodes and relationships
                for entity_name in conversation.key_entities:
                    await session.run(
                        """
                        MERGE (e:Entity {name: $name})
                        SET e.last_seen = $timestamp,
                            e.mention_count = COALESCE(e.mention_count, 0) + 1,
                            e.first_seen = COALESCE(e.first_seen, $timestamp)
                        MERGE (c:Conversation {conversation_id: $conversation_id})
                        MERGE (c)-[:DISCUSSES]->(e)
                        """,
                        name=entity_name,
                        timestamp=conversation.timestamp.isoformat(),
                        conversation_id=conversation.conversation_id,
                    )

                log.info(
                    "conversation_created",
                    conversation_id=conversation.conversation_id,
                    entity_count=len(conversation.key_entities),
                )
                return True
        except Exception as e:
            log.error("conversation_creation_failed", error=str(e), exc_info=True)
            return False

    async def create_entity(self, entity: Entity) -> str:
        """Create or update an entity node.

        Args:
            entity: Entity to create

        Returns:
            Entity ID (name-based)
        """
        if not self.connected or not self.driver:
            log.warning("neo4j_not_connected")
            return ""

        try:
            async with self.driver.session() as session:
                result = await session.run(
                    """
                    MERGE (e:Entity {name: $name})
                    SET e.entity_type = $entity_type,
                        e.description = $description,
                        e.properties = $properties,
                        e.last_seen = datetime(),
                        e.mention_count = COALESCE(e.mention_count, 0) + 1,
                        e.first_seen = COALESCE(e.first_seen, datetime())
                    RETURN e.name as entity_id
                    """,
                    name=entity.name,
                    entity_type=entity.entity_type,
                    description=entity.description,
                    properties=orjson.dumps(
                        entity.properties
                    ).decode(),  # Serialize dict to JSON string
                )
                record = await result.single()
                entity_id: str = record["entity_id"] if record else entity.name
                log.info("entity_created", entity_id=entity_id, entity_type=entity.entity_type)
                return entity_id
        except Exception as e:
            log.error("entity_creation_failed", error=str(e), exc_info=True)
            return ""

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
                await session.run(
                    """
                    MATCH (source)
                    WHERE id(source) = toInteger($source_id) OR source.conversation_id = $source_id OR source.name = $source_id
                    MATCH (target)
                    WHERE id(target) = toInteger($target_id) OR target.conversation_id = $target_id OR target.name = $target_id
                    MERGE (source)-[r:RELATIONSHIP {type: $relationship_type}]->(target)
                    SET r.weight = $weight,
                        r.properties = $properties,
                        r.created_at = datetime()
                    """,
                    source_id=relationship.source_id,
                    target_id=relationship.target_id,
                    relationship_type=relationship.relationship_type,
                    weight=relationship.weight,
                    properties=orjson.dumps(
                        relationship.properties
                    ).decode(),  # Serialize dict to JSON string
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

    async def query_memory(self, query: MemoryQuery) -> MemoryQueryResult:
        """Query memory graph for relevant conversations and entities.

        Args:
            query: Query parameters

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

                # Build main query - find conversations related to entities
                if query.entity_names or query.entity_types:
                    # Find conversations that discuss the specified entities
                    base_query = """
                    MATCH (c:Conversation)-[:DISCUSSES]->(e:Entity)
                    WHERE """
                    if query.entity_names:
                        base_query += "e.name IN $entity_names"
                        cypher_parts.append("entity_names: $entity_names")
                    elif query.entity_types:
                        base_query += "e.entity_type IN $entity_types"
                        cypher_parts.append("entity_types: $entity_types")
                elif query.conversation_ids or query.trace_ids:
                    # Direct conversation/trace lookup
                    base_query = "MATCH (c:Conversation) WHERE "
                    if query.conversation_ids:
                        base_query += "c.conversation_id IN $conversation_ids"
                        cypher_parts.append("conversation_ids: $conversation_ids")
                    elif query.trace_ids:
                        base_query += "c.trace_id IN $trace_ids"
                        cypher_parts.append("trace_ids: $trace_ids")
                else:
                    # Get all recent conversations
                    base_query = "MATCH (c:Conversation)"

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

                result = await session.run(base_query, **params)
                records = await result.values()

                # Parse results
                conversations = []
                for record in records:
                    if record and record[0]:
                        node = record[0]
                        conversations.append(
                            ConversationNode(
                                conversation_id=node.get("conversation_id", ""),
                                trace_id=node.get("trace_id"),
                                session_id=node.get("session_id"),
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

                # Calculate plausibility/relevance scores
                relevance_scores = await self._calculate_relevance_scores(conversations, query)

                log.info(
                    "memory_query_completed",
                    query_params=cypher_parts,
                    result_count=len(conversations),
                )

                return MemoryQueryResult(
                    conversations=conversations,
                    relevance_scores=relevance_scores,
                )

        except Exception as e:
            log.error("memory_query_failed", error=str(e), exc_info=True)
            return MemoryQueryResult()

    async def _calculate_relevance_scores(
        self, conversations: list[ConversationNode], query: MemoryQuery
    ) -> dict[str, float]:
        """Calculate relevance/plausibility scores for conversations.

        Scoring factors:
        1. Recency: More recent conversations score higher (0-0.4)
        2. Entity match: Conversations with more query entities score higher (0-0.4)
        3. Entity importance: Entities with higher mention counts boost score (0-0.2)

        Args:
            conversations: List of conversations to score
            query: Original query with entity filters

        Returns:
            Dict mapping conversation_id to relevance score (0.0-1.0)
        """
        if not conversations:
            return {}

        scores: dict[str, float] = {}

        # Get current time for recency calculation (timezone-aware)
        now = datetime.now(timezone.utc)

        # Find oldest conversation for recency normalization
        oldest_timestamp = min(c.timestamp for c in conversations)
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

        # Calculate scores for each conversation
        for conv in conversations:
            score = 0.0

            # 1. Recency score (0-0.4)
            if time_range > 0:
                age_seconds = (now - conv.timestamp).total_seconds()
                recency_ratio = 1.0 - (age_seconds / time_range)
                score += recency_ratio * 0.4
            else:
                score += 0.4  # All same timestamp

            # 2. Entity match score (0-0.4)
            if query.entity_names:
                matched_entities = set(query.entity_names) & set(conv.key_entities)
                match_ratio = len(matched_entities) / len(query.entity_names)
                score += match_ratio * 0.4
            else:
                score += 0.2  # No entity filter, give neutral score

            # 3. Entity importance score (0-0.2)
            if entity_importance:
                # Average importance of matched entities
                matched_importances = [
                    entity_importance.get(entity, 0.0)
                    for entity in conv.key_entities
                    if entity in entity_importance
                ]
                if matched_importances:
                    avg_importance = sum(matched_importances) / len(matched_importances)
                    score += avg_importance * 0.2

            scores[conv.conversation_id] = min(score, 1.0)  # Cap at 1.0

        return scores

    async def get_related_conversations(
        self, entity_names: list[str], limit: int = 10
    ) -> list[ConversationNode]:
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
