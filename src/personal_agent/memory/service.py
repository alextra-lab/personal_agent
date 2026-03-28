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
                    SET e.entity_id = COALESCE(e.entity_id, $entity_id),
                        e.entity_type = $entity_type,
                        e.description = $description,
                        e.properties = $properties,
                        e.last_seen = datetime(),
                        e.mention_count = COALESCE(e.mention_count, 0) + 1,
                        e.first_seen = COALESCE(e.first_seen, datetime())
                    RETURN e.name as entity_id
                    """,
                    name=entity.name,
                    entity_id=entity.name,
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
                # Use APOC to create a relationship with a dynamic type label.
                # Standard Cypher cannot parameterize relationship type labels;
                # apoc.merge.relationship handles this cleanly.
                await session.run(
                    """
                    MATCH (source)
                    WHERE source.entity_id = $source_id OR source.name = $source_id
                    MATCH (target)
                    WHERE target.entity_id = $target_id OR target.name = $target_id
                    CALL apoc.merge.relationship(
                        source, $relationship_type,
                        {},
                        {weight: $weight, created_at: datetime()},
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
    ) -> MemoryQueryResult:
        """Query memory graph for relevant conversations and entities.

        Args:
            query: Query parameters
            feedback_key: Optional session/user key for implicit feedback tracking.
            query_text: Optional original user query text for rephrase detection.

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

                # Calculate plausibility/relevance scores
                relevance_scores = await self._calculate_relevance_scores(conversations, query)

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

                return MemoryQueryResult(
                    conversations=conversations,
                    relevance_scores=relevance_scores,
                )

        except Exception as e:
            log.error("memory_query_failed", error=str(e), exc_info=True)
            return MemoryQueryResult()

    async def query_memory_broad(
        self,
        entity_types: list[str] | None = None,
        recency_days: int = 90,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Broad memory recall: return entities and session summaries (ADR-0025).

        Used for recall-intent queries ("what have I asked about?") where
        there are no specific entity names to search for.

        Args:
            entity_types: Optional filter e.g. ["Location", "Person"]. None = all types.
            recency_days: How far back to look.
            limit: Maximum entities to return.

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

                return {
                    "entities": entities,
                    "sessions": sessions,
                    "turns_summary": turns,
                }

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
        self, conversations: list[TurnNode], query: MemoryQuery
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

        # Calculate scores for each conversation
        for conv in conversations:
            score = 0.0

            # 1. Recency score (0-0.4)
            if time_range > 0:
                age_seconds = (now - _to_naive_utc(conv.timestamp)).total_seconds()
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
