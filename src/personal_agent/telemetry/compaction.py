"""D3: Compaction logging for the context pipeline (ADR-0047).

Provides a structured CompactionRecord dataclass and log_compaction() emitter.
Records are emitted to Elasticsearch via the structlog pipeline whenever context
compaction is triggered by the budget stage.

A module-level dropped-entity cache (session_id -> set[entity_id]) is maintained
so that the recall controller can detect when a recalled entity was recently
dropped by compaction (compaction quality feedback loop).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import structlog

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Dropped-entity cache — compaction quality feedback (ADR-0047 D3)
# Maps session_id → set of entity_ids dropped in the most recent compaction.
# Cleared at session start/end by the caller.
# ---------------------------------------------------------------------------
_dropped_entities_by_session: dict[str, set[str]] = {}


@dataclass(frozen=True)
class CompactionRecord:
    """Structured record of a context compaction event.

    Emitted to Elasticsearch via structlog whenever compaction is triggered.
    Used by the recall controller for compaction quality feedback.

    Attributes:
        trace_id: Request trace identifier.
        session_id: Client session identifier.
        timestamp: UTC time of compaction event.
        trigger: What caused compaction.
        tier_affected: Which context tier was compacted.
        tokens_before: Token count before compaction.
        tokens_after: Token count after compaction.
        tokens_removed: Tokens removed (tokens_before - tokens_after).
        strategy: Compaction strategy applied.
        content_summary: Short human-readable description of what was removed.
        entities_preserved: Entity IDs / names kept after compaction.
        entities_dropped: Entity IDs / names removed by compaction.
    """

    trace_id: str
    session_id: str
    timestamp: datetime
    trigger: Literal["budget_exceeded", "tier_rebalance", "manual"]
    tier_affected: Literal["near", "episodic", "long_term"]
    tokens_before: int
    tokens_after: int
    tokens_removed: int
    strategy: Literal["summarize", "truncate", "drop_oldest"]
    content_summary: str
    entities_preserved: tuple[str, ...]  # tuple for frozen dataclass compatibility
    entities_dropped: tuple[str, ...]


def log_compaction(record: CompactionRecord) -> None:
    """Emit a compaction record to the structured log (Elasticsearch sink).

    Also updates the module-level dropped-entity cache so that the recall
    controller can detect poor-quality recalls of dropped entities.

    Args:
        record: The compaction event to log.
    """
    log.info(
        "context.compaction",
        trace_id=record.trace_id,
        session_id=record.session_id,
        trigger=record.trigger,
        tier_affected=record.tier_affected,
        tokens_before=record.tokens_before,
        tokens_after=record.tokens_after,
        tokens_removed=record.tokens_removed,
        strategy=record.strategy,
        content_summary=record.content_summary,
        entities_preserved=list(record.entities_preserved),
        entities_dropped=list(record.entities_dropped),
    )

    # Update dropped-entity cache for compaction quality feedback
    if record.entities_dropped:
        session_dropped = _dropped_entities_by_session.setdefault(record.session_id, set())
        session_dropped.update(record.entities_dropped)


def get_dropped_entities(session_id: str) -> set[str]:
    """Return the set of entity IDs/names dropped in compaction for this session.

    Used by the recall controller to detect when a recalled entity was recently
    compacted out (compaction quality feedback, ADR-0047 D3).

    Args:
        session_id: Client session identifier.

    Returns:
        Set of entity identifiers that were dropped by compaction this session.
        Empty set if no compaction has occurred.
    """
    return _dropped_entities_by_session.get(session_id, set())


def clear_dropped_entities(session_id: str) -> None:
    """Clear the dropped-entity cache for a session.

    Should be called at session end to prevent unbounded memory growth.

    Args:
        session_id: Client session identifier to clear.
    """
    _dropped_entities_by_session.pop(session_id, None)
