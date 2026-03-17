"""Adapter wrapping MemoryService to satisfy MemoryProtocol.

This is the Slice 1 implementation -- wraps the existing MemoryService
without adding new capabilities. Enables protocol-based consumption
while the underlying service remains unchanged.
"""

from __future__ import annotations

import structlog

from personal_agent.memory.models import MemoryQuery
from personal_agent.memory.protocol import (
    BroadRecallResult,
    Episode,
    MemoryRecallQuery,
    MemoryRecallResult,
)
from personal_agent.memory.service import MemoryService

logger = structlog.get_logger(__name__)


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
        )
        result = await self._service.query_memory(
            service_query,
            feedback_key=trace_id,
            query_text=query.query_text,
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
    ) -> BroadRecallResult:
        """Broad recall delegating to query_memory_broad().

        Args:
            entity_types: Filter by entity types (None = all).
            recency_days: Lookback window in days.
            limit: Maximum entities to return.
            trace_id: Request trace identifier.

        Returns:
            Broad recall result with entities grouped by type.
        """
        raw = await self._service.query_memory_broad(
            entity_types=entity_types,
            recency_days=recency_days,
            limit=limit,
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
        """Store episode -- Slice 1 stub.

        Full implementation in Slice 2 when episodic/semantic distinction
        is added. For now, logs the intent without persisting (consolidation
        handles persistence via the existing SecondBrainConsolidator).

        Args:
            episode: The episode to store.
            trace_id: Request trace identifier.

        Returns:
            The episode's turn_id.
        """
        logger.info(
            "memory_store_episode_stub",
            turn_id=episode.turn_id,
            session_id=episode.session_id,
            trace_id=trace_id,
        )
        return episode.turn_id

    async def is_connected(self) -> bool:
        """Check if the underlying Neo4j driver is available.

        Returns:
            True if the driver is initialized.
        """
        return self._service.driver is not None
