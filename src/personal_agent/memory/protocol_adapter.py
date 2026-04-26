"""Adapter wrapping MemoryService to satisfy MemoryProtocol.

This is the Slice 1 implementation -- wraps the existing MemoryService
without adding new capabilities. Enables protocol-based consumption
while the underlying service remains unchanged.
"""

from __future__ import annotations

import time
from uuid import UUID

import structlog

from personal_agent.events import AccessContext
from personal_agent.memory.embeddings import generate_embedding
from personal_agent.memory.models import MemoryQuery
from personal_agent.memory.proactive import build_proactive_suggestions
from personal_agent.memory.proactive_types import ProactiveMemorySuggestions
from personal_agent.memory.protocol import (
    BroadRecallResult,
    Episode,
    MemoryRecallQuery,
    MemoryRecallResult,
)
from personal_agent.memory.service import MemoryService

log = structlog.get_logger(__name__)
logger = log  # alias used by earlier methods


class MemoryServiceAdapter:
    """Adapts MemoryService to the MemoryProtocol interface.

    Args:
        service: The existing MemoryService instance.
    """

    def __init__(self, service: MemoryService) -> None:
        """Initialize with the existing MemoryService instance."""
        self._service = service

    async def recall(self, query: MemoryRecallQuery, trace_id: str) -> MemoryRecallResult:
        """Query memory by converting protocol types to service types.

        Args:
            query: Protocol-level recall query.
            trace_id: Request trace identifier.

        Returns:
            Recall result with episodes, entities, and relevance scores.
        """
        service_query = MemoryQuery(
            entity_names=query.entity_names,
            entity_types=query.entity_types,
            recency_days=query.recency_days,
            limit=query.limit,
            user_id=query.user_id,
            authenticated=query.authenticated,
        )
        result = await self._service.query_memory(
            service_query,
            feedback_key=trace_id,
            query_text=query.query_text,
            access_context=AccessContext.CONTEXT_ASSEMBLY,
            trace_id=trace_id,
            user_id=query.user_id,
            authenticated=query.authenticated,
        )
        return MemoryRecallResult(
            episodes=[
                {
                    "turn_id": c.turn_id,
                    "session_id": c.session_id,
                    "timestamp": c.timestamp.isoformat() if c.timestamp else None,
                    "summary": c.summary,
                    "user_message": c.user_message,
                    "assistant_response": c.assistant_response,
                    "key_entities": c.key_entities,
                }
                for c in result.conversations
            ],
            entities=[
                {
                    "entity_id": e.entity_id,
                    "name": e.name,
                    "entity_type": e.entity_type,
                    "description": e.description,
                    "mention_count": e.mention_count,
                }
                for e in result.entities
            ],
            relevance_scores=result.relevance_scores,
        )

    async def recall_broad(
        self,
        entity_types: list[str] | None,
        recency_days: int,
        limit: int,
        trace_id: str,
        user_id: "UUID | None" = None,
        authenticated: bool = False,
    ) -> BroadRecallResult:
        """Broad recall delegating to query_memory_broad().

        Args:
            entity_types: Filter by entity types (None = all).
            recency_days: Lookback window in days.
            limit: Maximum entities to return.
            trace_id: Request trace identifier.
            user_id: Authenticated user UUID for visibility scoping (FRE-229).
            authenticated: Whether the request carries a verified identity (FRE-229).

        Returns:
            Broad recall result with entities grouped by type.
        """
        raw = await self._service.query_memory_broad(
            entity_types=entity_types,
            recency_days=recency_days,
            limit=limit,
            access_context=AccessContext.CONTEXT_ASSEMBLY,
            trace_id=trace_id,
            user_id=user_id,
            authenticated=authenticated,
        )
        entities = raw.get("entities", [])
        entities_by_type: dict[str, list[dict[str, object]]] = {}
        for entity in entities:
            entity_type = entity.get("type", "Unknown")
            entities_by_type.setdefault(entity_type, []).append(entity)
        return BroadRecallResult(
            entities_by_type=entities_by_type,
            recent_sessions=raw.get("sessions", []),
            total_entity_count=len(entities),
        )

    async def store_episode(self, episode: Episode, trace_id: str) -> str:
        """Store a new episode as a TurnNode in Neo4j.

        Replaces Slice 1 stub. Deduplicates by turn_id, creates a TurnNode
        via the existing create_conversation() method.

        Args:
            episode: The episode to store.
            trace_id: Request trace identifier.

        Returns:
            The episode's turn_id.
        """
        from personal_agent.memory.models import TurnNode

        # Dedup check
        if hasattr(self._service, "turn_exists"):
            exists = await self._service.turn_exists(episode.turn_id)
            if exists:
                logger.debug(
                    "store_episode_dedup_skip",
                    turn_id=episode.turn_id,
                    trace_id=trace_id,
                )
                return episode.turn_id

        turn = TurnNode(
            turn_id=episode.turn_id,
            trace_id=trace_id,
            session_id=episode.session_id,
            timestamp=episode.timestamp,
            user_message=episode.user_message,
            assistant_response=episode.assistant_response,
            key_entities=episode.entities,
        )

        try:
            await self._service.create_conversation(turn)
        except Exception:
            logger.warning(
                "store_episode_create_failed",
                turn_id=episode.turn_id,
                trace_id=trace_id,
                exc_info=True,
            )
            raise

        logger.info(
            "store_episode_created",
            turn_id=episode.turn_id,
            session_id=episode.session_id,
            trace_id=trace_id,
        )
        return episode.turn_id

    async def promote(
        self,
        entity_name: str,
        confidence: float,
        source_turn_ids: list[str],
        trace_id: str,
    ) -> bool:
        """Promote an entity to semantic memory via the service.

        Args:
            entity_name: Entity to promote.
            confidence: Confidence score.
            source_turn_ids: Supporting turn IDs.
            trace_id: Request trace identifier.

        Returns:
            True if promoted successfully.
        """
        try:
            return await self._service.promote_entity(
                entity_name=entity_name,
                confidence=confidence,
                source_turn_ids=source_turn_ids,
                trace_id=trace_id,
            )
        except Exception:
            logger.warning(
                "promote_adapter_failed",
                entity_name=entity_name,
                trace_id=trace_id,
                exc_info=True,
            )
            return False

    async def is_connected(self) -> bool:
        """Check if the underlying Neo4j driver is available.

        Returns:
            True if the driver is initialized.
        """
        return self._service.driver is not None

    async def suggest_relevant(
        self,
        user_message: str,
        session_entity_names: list[str],
        session_topic_hint: str | None,
        current_session_id: str,
        trace_id: str,
        user_id: UUID | None = None,
        authenticated: bool = False,
    ) -> ProactiveMemorySuggestions:
        """Proactive ranked memory for context injection (ADR-0039)."""
        log.info(
            "proactive_memory_suggest_start",
            trace_id=trace_id,
            user_message_length=len(user_message or ""),
            session_id=current_session_id or "",
        )
        try:
            t0 = time.perf_counter()
            embedding = await generate_embedding(user_message, mode="query")
            emb_ms = (time.perf_counter() - t0) * 1000.0
            if not any(x != 0.0 for x in embedding):
                log.info(
                    "proactive_memory_suggest_empty",
                    trace_id=trace_id,
                    reason="zero_embedding",
                )
                return ProactiveMemorySuggestions(candidates=[], query_embedding_ms=emb_ms)

            db_entities = await self._service.fetch_session_discussed_entity_names(
                current_session_id,
                user_id=user_id,
                authenticated=authenticated,
            )
            merged = set(session_entity_names) | set(db_entities)

            raw = await self._service.suggest_proactive_raw(
                embedding,
                current_session_id,
                trace_id,
                user_id=user_id,
                authenticated=authenticated,
            )
            if not raw:
                log.info(
                    "proactive_memory_suggest_empty",
                    trace_id=trace_id,
                    reason="no_raw_rows",
                )
                return ProactiveMemorySuggestions(candidates=[], query_embedding_ms=emb_ms)

            suggestions = build_proactive_suggestions(
                raw,
                merged,
                session_topic_hint,
                trace_id,
                emb_ms,
            )
            if not suggestions.candidates:
                log.info(
                    "proactive_memory_suggest_empty",
                    trace_id=trace_id,
                    reason="filtered_or_budget",
                    raw_row_count=len(raw),
                )
            else:
                log.info(
                    "proactive_memory_suggest_complete",
                    trace_id=trace_id,
                    candidate_count=len(suggestions.candidates),
                    raw_row_count=len(raw),
                    query_embedding_ms=emb_ms,
                )
            return suggestions
        except Exception:
            log.exception("proactive_memory_suggest_failed", trace_id=trace_id)
            return ProactiveMemorySuggestions(candidates=[], query_embedding_ms=None)
