"""Stage 6+7: Context Assembly and Budget.

Assembles the final message list for the LLM from:
- Session history
- Seshat memory (via MemoryProtocol adapter)
- User message

In Slice 1, skill loading and budget trimming are deferred.
The budget stage is a pass-through that counts tokens.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog

from personal_agent.memory.protocol import BroadRecallResult, MemoryProtocol, MemoryRecallQuery
from personal_agent.request_gateway.state_document import build_state_document
from personal_agent.request_gateway.types import (
    AssembledContext,
    IntentResult,
    RecallResult,
    TaskType,
)

logger = structlog.get_logger(__name__)


def _format_broad_recall_context(
    broad: BroadRecallResult,
) -> list[dict[str, Any]]:
    """Format broad recall result as memory context for the LLM.

    Args:
        broad: The broad recall result from Seshat.

    Returns:
        List of formatted memory context items.
    """
    context: list[dict[str, Any]] = []

    for entity_type, entities in broad.entities_by_type.items():
        for entity in entities:
            context.append(
                {
                    "type": "entity",
                    "entity_type": entity_type,
                    "name": entity.get("name", "unknown"),
                    "description": entity.get("description"),
                    "mention_count": entity.get("mention_count", 0),
                }
            )

    for session in broad.recent_sessions:
        context.append(
            {
                "type": "session",
                "session_id": session.get("session_id"),
                "summary": session.get("session_summary"),
                "dominant_entities": session.get("dominant_entities", []),
            }
        )

    return context


async def _query_memory_for_intent(
    intent: IntentResult,
    user_message: str,
    memory_adapter: MemoryProtocol,
    trace_id: str,
) -> list[dict[str, Any]] | None:
    """Query memory based on intent type.

    Args:
        intent: Classified intent result.
        user_message: The user's message.
        memory_adapter: Seshat protocol adapter.
        trace_id: Request trace identifier.

    Returns:
        Memory context list, or None if no relevant memory found.
    """
    try:
        if not await memory_adapter.is_connected():
            logger.warning("memory_unavailable", trace_id=trace_id)
            return None

        if intent.task_type == TaskType.MEMORY_RECALL:
            broad = await memory_adapter.recall_broad(
                entity_types=None,
                recency_days=90,
                limit=20,
                trace_id=trace_id,
            )
            return _format_broad_recall_context(broad)

        # Entity-name matching for analysis and other task types (Slice 2).
        # Extract capitalised words > 3 chars as potential entity names.
        words = user_message.split()
        entity_names = [
            w.strip('",.:;!?') for w in words if len(w) > 3 and w[0].isupper()
        ]
        if not entity_names:
            return None

        query = MemoryRecallQuery(
            entity_names=entity_names[:5],
            recency_days=30,
            limit=5,
            query_text=user_message,
        )
        result = await memory_adapter.recall(query, trace_id=trace_id)
        context: list[dict[str, Any]] = []
        for entity in result.entities:
            context.append(
                {
                    "type": "entity",
                    "name": entity.get("name", "unknown"),
                    "entity_type": entity.get("entity_type"),
                    "description": entity.get("description"),
                    "mention_count": entity.get("mention_count", 0),
                }
            )
        for ep in result.episodes:
            context.append(
                {
                    "type": "episode",
                    "user_message": ep.get("user_message"),
                    "summary": ep.get("summary") or ep.get("user_message", "")[:200],
                    "key_entities": ep.get("key_entities", []),
                }
            )
        return context if context else None

    except Exception:
        logger.exception("memory_query_failed", trace_id=trace_id)
        return None


async def assemble_context(
    user_message: str,
    session_messages: Sequence[dict[str, Any]],
    intent: IntentResult,
    memory_adapter: MemoryProtocol | None,
    trace_id: str,
    recall_context: RecallResult | None = None,
) -> AssembledContext:
    """Assemble the full context for the primary agent.

    Combines session history, memory enrichment, and user message
    into a final message list. In Slice 1, skill loading and
    budget trimming are stubs.

    Args:
        user_message: The current user message.
        session_messages: Prior conversation history (OpenAI format).
        intent: Classified intent from Stage 4.
        memory_adapter: Seshat protocol adapter (None if unavailable).
        trace_id: Request trace identifier.
        recall_context: Recall controller result from Stage 4b (None if not triggered).

    Returns:
        AssembledContext with messages and metadata.
    """
    messages: list[dict[str, Any]] = []
    memory_context: list[dict[str, Any]] | None = None

    # Include session history
    messages.extend(session_messages)

    # Prepend structured state document for multi-turn sessions (Phase 4.5).
    state_doc = build_state_document(session_messages, trace_id=trace_id)
    if state_doc:
        messages.insert(0, {"role": "system", "content": state_doc})

    # Query memory if adapter is available
    if memory_adapter is not None:
        memory_context = await _query_memory_for_intent(
            intent=intent,
            user_message=user_message,
            memory_adapter=memory_adapter,
            trace_id=trace_id,
        )

    # Inject session fact candidates from recall controller (as system message
    # in the main message list, not memory_context, to avoid schema mismatch
    # and budget-trimming that silently drops memory_context items).
    if recall_context and recall_context.reclassified and recall_context.candidates:
        recall_section = "## Session Fact Recall\n"
        recall_section += "The user appears to be referring to something discussed earlier.\n"
        recall_section += "Relevant facts from the conversation:\n"
        for c in recall_context.candidates:
            recall_section += f'- Turn {c.source_turn}: "{c.fact}" (matched: "{c.noun_phrase}")\n'
        recall_section += "\nUse these facts to answer accurately. Do not claim you don't know."
        messages.append({"role": "system", "content": recall_section})

    # Add the current user message
    messages.append({"role": "user", "content": user_message})

    # Slice 1: simple token estimation (word count * 1.3)
    total_text = " ".join(m.get("content", "") for m in messages)
    estimated_tokens = int(len(total_text.split()) * 1.3)

    logger.debug(
        "context_assembled",
        message_count=len(messages),
        has_memory=memory_context is not None,
        estimated_tokens=estimated_tokens,
        task_type=intent.task_type.value,
        trace_id=trace_id,
    )

    return AssembledContext(
        messages=messages,
        memory_context=memory_context,
        tool_definitions=None,  # Populated by executor's existing tool logic
        token_count=estimated_tokens,
        trimmed=False,  # Slice 1: no budget trimming
    )
