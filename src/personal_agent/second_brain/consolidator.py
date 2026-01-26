"""Second Brain Consolidator: Background memory consolidation (Phase 2.2).

This component processes recent task captures, extracts entities and relationships
using Claude 4.5, and updates the Neo4j memory graph.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from personal_agent.captains_log.capture import TaskCapture, read_captures
from personal_agent.config.settings import get_settings
from personal_agent.llm_client.claude import ClaudeClient
from personal_agent.memory.models import ConversationNode, Entity, Relationship
from personal_agent.memory.service import MemoryService
from personal_agent.second_brain.entity_extraction import extract_entities_and_relationships
from personal_agent.telemetry import get_logger

log = get_logger(__name__)
settings = get_settings()


class SecondBrainConsolidator:
    """Background consolidator for building and maintaining memory graph.

    This component:
    1. Reads recent task captures
    2. Uses Claude 4.5 to extract entities and relationships
    3. Updates Neo4j memory graph
    4. Creates reflection entries for insights

    Usage:
        consolidator = SecondBrainConsolidator()
        await consolidator.consolidate_recent_captures(days=7)
    """

    def __init__(
        self,
        memory_service: MemoryService | None = None,
        claude_client: ClaudeClient | None = None,
    ) -> None:  # noqa: D107
        """Initialize consolidator with optional dependencies.

        Args:
            memory_service: Optional memory service (creates new if None)
            claude_client: Optional Claude client (creates new if None)
        """
        self.memory_service = memory_service or MemoryService()
        self.claude_client = claude_client

        # Ensure memory service is connected
        if not self.memory_service.connected:
            # Note: In service mode, memory service should already be connected
            # In CLI mode, this will create a temporary connection
            pass

    async def consolidate_recent_captures(self, days: int = 7, limit: int = 50) -> dict[str, Any]:
        """Consolidate recent task captures into memory graph.

        Args:
            days: Number of days to look back
            limit: Maximum number of captures to process

        Returns:
            Summary dict with processing results
        """
        log.info("consolidation_started", days=days, limit=limit)

        # Read recent captures
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=days)
        captures = read_captures(start_date=start_date, end_date=end_date, limit=limit)

        if not captures:
            log.info("no_captures_to_consolidate", days=days)
            return {
                "captures_processed": 0,
                "conversations_created": 0,
                "entities_created": 0,
                "relationships_created": 0,
            }

        log.info(
            "captures_found",
            count=len(captures),
            extraction_model=settings.entity_extraction_model,
        )

        # Ensure memory service is connected
        if not self.memory_service.connected:
            await self.memory_service.connect()

        # Initialize Claude client only if using Claude for extraction
        if not self.claude_client and settings.entity_extraction_model == "claude":
            try:
                self.claude_client = ClaudeClient()
            except ValueError as e:
                log.warning(
                    "claude_client_unavailable_fallback_to_local",
                    error=str(e),
                    fallback_model=settings.entity_extraction_model or "qwen3-8b",
                )
                # Will use local SLM instead

        # Process each capture
        conversations_created = 0
        entities_created = 0
        relationships_created = 0

        for i, capture in enumerate(captures, 1):
            try:
                log.debug(
                    "consolidation_processing_capture",
                    capture_num=i,
                    total=len(captures),
                    trace_id=capture.trace_id,
                )
                result = await self._process_capture(capture)
                conversations_created += result.get("conversation_created", 0)
                entities_created += result.get("entities_created", 0)
                relationships_created += result.get("relationships_created", 0)
                log.debug(
                    "consolidation_capture_done",
                    capture_num=i,
                    entities=result.get("entities_created", 0),
                    relationships=result.get("relationships_created", 0),
                )
            except Exception as e:
                log.error(
                    "capture_processing_failed",
                    capture_num=i,
                    trace_id=capture.trace_id,
                    error=str(e),
                    error_type=type(e).__name__,
                    exc_info=True,
                )

        summary = {
            "captures_processed": len(captures),
            "conversations_created": conversations_created,
            "entities_created": entities_created,
            "relationships_created": relationships_created,
        }

        log.info(
            "consolidation_completed",
            **summary,
            extraction_model=settings.entity_extraction_model,
        )
        return summary

    async def _process_capture(self, capture: TaskCapture) -> dict[str, Any]:
        """Process a single capture: extract entities and update graph.

        Args:
            capture: Task capture to process

        Returns:
            Processing result summary
        """
        # Extract entities and relationships using configured model (local SLM or Claude)
        extraction_result = await extract_entities_and_relationships(
            capture.user_message,
            capture.assistant_response or "",
            self.claude_client,  # Optional: uses local SLM if None
        )

        # Create conversation node
        conversation = ConversationNode(
            conversation_id=capture.trace_id,
            trace_id=capture.trace_id,
            session_id=capture.session_id,
            timestamp=capture.timestamp,
            summary=extraction_result.get("summary"),
            user_message=capture.user_message,
            assistant_response=capture.assistant_response,
            key_entities=extraction_result.get("entity_names", []),
            properties={
                "tools_used": capture.tools_used,
                "duration_ms": capture.duration_ms,
                "outcome": capture.outcome,
            },
        )

        await self.memory_service.create_conversation(conversation)
        conversations_created = 1

        # Create entity nodes
        entities_created = 0
        for entity_data in extraction_result.get("entities", []):
            entity = Entity(
                name=entity_data.get("name", ""),
                entity_type=entity_data.get("type", "Unknown"),
                description=entity_data.get("description"),
                properties=entity_data.get("properties", {}),
            )
            entity_id = await self.memory_service.create_entity(entity)
            if entity_id:
                entities_created += 1

        # Create relationships
        relationships_created = 0
        for rel_data in extraction_result.get("relationships", []):
            relationship = Relationship(
                source_id=rel_data.get("source", ""),
                target_id=rel_data.get("target", ""),
                relationship_type=rel_data.get("type", "RELATED_TO"),
                weight=rel_data.get("weight", 1.0),
                properties=rel_data.get("properties", {}),
            )
            if await self.memory_service.create_relationship(relationship):
                relationships_created += 1

        return {
            "conversation_created": conversations_created,
            "entities_created": entities_created,
            "relationships_created": relationships_created,
        }
