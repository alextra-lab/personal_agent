"""Second Brain Consolidator: Background memory consolidation (Phase 2.2).

This component processes recent task captures, extracts entities and relationships
using Claude 4.5 or local SLMs, and updates the Neo4j memory graph.
"""

import asyncio
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from personal_agent.captains_log.capture import TaskCapture, read_captures
from personal_agent.config import load_model_config
from personal_agent.memory.models import Entity, Relationship, SessionNode, TurnNode
from personal_agent.memory.service import MemoryService
from personal_agent.second_brain.entity_extraction import extract_entities_and_relationships
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


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
    ) -> None:  # noqa: D107
        """Initialize consolidator with optional dependencies.

        Args:
            memory_service: Optional memory service (creates new if None).
        """
        self.memory_service = memory_service or MemoryService()

        # Ensure memory service is connected
        if not self.memory_service.connected:
            # Note: In service mode, memory service should already be connected
            # In CLI mode, this will create a temporary connection
            pass

    async def consolidate_recent_captures(
        self,
        days: int = 7,
        limit: int = 50,
        should_pause: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Consolidate recent task captures into memory graph.

        Args:
            days: Number of days to look back
            limit: Maximum number of captures to process
            should_pause: Optional callback indicating whether consolidation
                should temporarily pause before processing the next capture.

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
                "captures_skipped": 0,
                "turns_created": 0,
                "sessions_created": 0,
                "entities_created": 0,
                "relationships_created": 0,
            }

        model_config = load_model_config()
        entity_extraction_role = model_config.entity_extraction_role

        log.info(
            "captures_found",
            count=len(captures),
            extraction_model=entity_extraction_role,
        )

        # Ensure memory service is connected
        if not self.memory_service.connected:
            await self.memory_service.connect()

        # Process each capture (skip ones already in the graph to avoid duplicate work)
        turns_created = 0
        entities_created = 0
        relationships_created = 0
        captures_skipped = 0
        sessions_with_new_turns: set[str] = set()

        for i, capture in enumerate(captures, 1):
            if should_pause and should_pause():
                log.info(
                    "consolidation_paused_request_active",
                    capture_num=i,
                    remaining=len(captures) - i + 1,
                )
                while should_pause():
                    await asyncio.sleep(1.0)
                log.info("consolidation_resumed", capture_num=i)
            try:
                if await self.memory_service.turn_exists(capture.trace_id):
                    captures_skipped += 1
                    log.debug(
                        "consolidation_skipped_already_consolidated",
                        capture_num=i,
                        trace_id=capture.trace_id,
                    )
                    continue
                log.debug(
                    "consolidation_processing_capture",
                    capture_num=i,
                    total=len(captures),
                    trace_id=capture.trace_id,
                )
                result = await self._process_capture(capture)
                if result.get("turns_created"):
                    turns_created += result["turns_created"]
                    if capture.session_id:
                        sessions_with_new_turns.add(capture.session_id)
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

        # Build Session nodes for every session that received new turns this run
        sessions_created = await self._consolidate_sessions(captures, sessions_with_new_turns)

        summary = {
            "captures_processed": len(captures),
            "captures_skipped": captures_skipped,
            "turns_created": turns_created,
            "sessions_created": sessions_created,
            "entities_created": entities_created,
            "relationships_created": relationships_created,
        }

        log.info(
            "consolidation_completed",
            **summary,
            extraction_model=entity_extraction_role,
        )
        return summary

    async def _consolidate_sessions(
        self,
        all_captures: list[TaskCapture],
        sessions_with_new_turns: set[str],
    ) -> int:
        """Create or update Session nodes for sessions that received new turns.

        For each affected session:
        1. Derive metadata (timestamps, turn count, dominant entities) from captures
        2. MERGE the Session node
        3. Wire CONTAINS + NEXT + Session-DISCUSSES-Entity relationships

        Args:
            all_captures: All captures from this consolidation run.
            sessions_with_new_turns: session_ids that had at least one new turn.

        Returns:
            Number of sessions created/updated.
        """
        if not sessions_with_new_turns:
            return 0

        # Group captures by session_id
        by_session: dict[str, list[TaskCapture]] = defaultdict(list)
        for capture in all_captures:
            if capture.session_id in sessions_with_new_turns:
                by_session[capture.session_id].append(capture)

        sessions_created = 0
        for session_id, session_captures in by_session.items():
            try:
                ordered = sorted(session_captures, key=lambda c: c.timestamp)
                # Collect dominant entities from key_entities across all turns
                entity_counts: dict[str, int] = defaultdict(int)
                for capture in ordered:
                    # key_entities not directly on capture — inferred from graph
                    pass

                session_node = SessionNode(
                    session_id=session_id,
                    started_at=ordered[0].timestamp,
                    ended_at=ordered[-1].timestamp,
                    turn_count=len(ordered),
                    dominant_entities=[],  # Populated by link_session_turns via graph query
                    session_summary=None,  # Generated lazily in future
                )
                created = await self.memory_service.create_session(session_node)
                if created:
                    linked = await self.memory_service.link_session_turns(session_id)
                    # Refresh dominant_entities from graph after linking
                    await self._update_session_dominant_entities(session_id)
                    sessions_created += 1
                    log.debug(
                        "session_consolidated",
                        session_id=session_id,
                        turns_linked=linked,
                    )
            except Exception as e:
                log.error(
                    "session_consolidation_failed",
                    session_id=session_id,
                    error=str(e),
                    exc_info=True,
                )

        return sessions_created

    async def _update_session_dominant_entities(self, session_id: str) -> None:
        """Update Session.dominant_entities from the top entities discussed in its turns.

        Args:
            session_id: Session to update.
        """
        if not self.memory_service.connected or not self.memory_service.driver:
            return
        try:
            async with self.memory_service.driver.session() as db_session:
                result = await db_session.run(
                    """
                    MATCH (s:Session {session_id: $session_id})-[r:DISCUSSES]->(e:Entity)
                    RETURN e.name AS name, r.turn_count AS cnt
                    ORDER BY r.turn_count DESC
                    LIMIT 10
                    """,
                    session_id=session_id,
                )
                records = await result.values()
                dominant = [row[0] for row in records if row[0]]
                if dominant:
                    await db_session.run(
                        "MATCH (s:Session {session_id: $session_id}) SET s.dominant_entities = $dominant",
                        session_id=session_id,
                        dominant=dominant,
                    )
        except Exception as e:
            log.warning("update_dominant_entities_failed", session_id=session_id, error=str(e))

    async def _process_capture(self, capture: TaskCapture) -> dict[str, Any]:
        """Process a single capture: extract entities and update graph.

        Args:
            capture: Task capture to process

        Returns:
            Processing result summary
        """
        # Strip <think>…</think> blocks from the assistant response before extraction.
        # The full response (including thinking) is preserved in the TurnNode below for
        # debugging, but passing raw thinking to the extraction model inflates the prompt
        # and causes extraction of internal tool names (e.g. mcp_perplexity_ask) that the
        # model was only reasoning about, not actually recommending.
        raw_response = capture.assistant_response or ""
        extraction_response = re.sub(
            r"<think>.*?</think>", "", raw_response, flags=re.DOTALL
        ).strip()

        # Extract entities and relationships using configured model (local SLM or Claude)
        extraction_result = await extract_entities_and_relationships(
            capture.user_message,
            extraction_response,
        )

        # If extraction fell back (LLM error/crash), skip writing to Neo4j entirely.
        # A fallback result has summary == user_message and empty entities. Writing a
        # Conversation node here would permanently block future retries via
        # conversation_exists(), so we bail early and let the next consolidation run retry.
        summary = extraction_result.get("summary", "")
        is_fallback = (
            not extraction_result.get("entities")
            and summary.strip() == capture.user_message.strip()[:200]
        )
        if is_fallback:
            log.warning(
                "consolidation_extraction_fallback_skip",
                trace_id=capture.trace_id,
                reason="extraction returned fallback result; will retry next run",
            )
            return {"turns_created": 0, "entities_created": 0, "relationships_created": 0}

        # Create Turn node
        turn = TurnNode(
            turn_id=capture.trace_id,
            trace_id=capture.trace_id,
            session_id=capture.session_id,
            timestamp=capture.timestamp,
            summary=summary,
            user_message=capture.user_message,
            assistant_response=capture.assistant_response,
            key_entities=extraction_result.get("entity_names", []),
            properties={
                "tools_used": capture.tools_used,
                "duration_ms": capture.duration_ms,
                "outcome": capture.outcome,
            },
        )
        # Attach full entity data so create_conversation can set entity_type on inline nodes.
        # This is a transient attribute — not part of the Pydantic model — used only during write.
        object.__setattr__(turn, "_entity_data", extraction_result.get("entities", []))

        await self.memory_service.create_conversation(turn)
        turns_created = 1

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
            "turns_created": turns_created,
            "entities_created": entities_created,
            "relationships_created": relationships_created,
        }
