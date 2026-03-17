"""Seshat Memory Protocol -- abstract memory interface.

Defines the contract that all memory implementations must satisfy.
The first implementation wraps the existing MemoryService.

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 5.4
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class MemoryType(Enum):
    """Six memory types with different lifecycles.

    Each type maps to a distinct storage and retrieval strategy:

    Attributes:
        WORKING: Short-lived context for the current conversation turn.
        EPISODIC: Timestamped interaction records (who said what, when).
        SEMANTIC: Extracted facts, entities, and relationships.
        PROCEDURAL: Learned patterns and tool-usage strategies.
        PROFILE: Long-lived user preferences and traits.
        DERIVED: Computed summaries and aggregations.
    """

    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    PROFILE = "profile"
    DERIVED = "derived"


class RecallScope(Enum):
    """Filter for which memory types to search.

    WORKING and PROFILE are excluded: working memory is ephemeral
    (not searchable), and profile data is always implicitly included.

    Attributes:
        ALL: Search across all memory types.
        EPISODIC: Search only episodic memories.
        SEMANTIC: Search only semantic memories.
        PROCEDURAL: Search only procedural memories.
        DERIVED: Search only derived memories.
    """

    ALL = "all"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    DERIVED = "derived"


@dataclass(frozen=True)
class Episode:
    """A single interaction episode.

    Wraps TurnNode + context for storage in the episodic memory layer.

    Args:
        turn_id: Unique identifier (typically the trace_id).
        session_id: Session this episode belongs to.
        timestamp: When the episode occurred.
        user_message: What the user said.
        assistant_response: What the agent replied (None if not yet generated).
        tools_used: Tool names invoked during this episode.
        entities: Entity names extracted from the episode.
    """

    turn_id: str
    session_id: str
    timestamp: datetime
    user_message: str
    assistant_response: str | None
    tools_used: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MemoryRecallQuery:
    """Query parameters for memory recall.

    Evolves the existing MemoryQuery with memory type filtering.

    Args:
        entity_names: Filter by entity names.
        entity_types: Filter by entity types.
        memory_types: Which memory scopes to search (default: all).
        recency_days: Only return memories from the last N days (None = no limit).
        limit: Maximum results to return.
        query_text: Free-text query for relevance scoring.
    """

    entity_names: list[str] = field(default_factory=list)
    entity_types: list[str] = field(default_factory=list)
    memory_types: list[RecallScope] = field(default_factory=lambda: [RecallScope.ALL])
    recency_days: int | None = 30
    limit: int = 10
    query_text: str | None = None


@dataclass(frozen=True)
class MemoryRecallResult:
    """Result of a memory recall query.

    Args:
        episodes: Matched episodic memories as dicts (heterogeneous data).
        entities: Matched entity information as dicts (heterogeneous data).
        relevance_scores: Per-result relevance scores keyed by ID.
    """

    episodes: list[dict[str, Any]]
    entities: list[dict[str, Any]]
    relevance_scores: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class BroadRecallResult:
    """Result of a broad recall query ("what have I asked about?").

    Args:
        entities_by_type: Entities grouped by type.
        recent_sessions: Recent session summaries.
        total_entity_count: Total entities in memory.
    """

    entities_by_type: dict[str, list[dict[str, Any]]]
    recent_sessions: list[dict[str, Any]]
    total_entity_count: int


@runtime_checkable
class MemoryProtocol(Protocol):
    """Abstract memory interface -- the Seshat contract.

    All memory access goes through this protocol. Implementations
    can be swapped (Neo4j, Graphiti, AgentDB) without changing
    consuming code.

    Slice 1 implements: recall, recall_broad, store_episode.
    Remaining methods are stubs until Slice 2/3.
    """

    async def recall(self, query: MemoryRecallQuery, trace_id: str) -> MemoryRecallResult:
        """Query memory for relevant episodes and entities.

        Args:
            query: Structured recall query with filters and limits.
            trace_id: Trace identifier for observability.

        Returns:
            MemoryRecallResult containing matched episodes and entities.
        """
        ...

    async def recall_broad(
        self,
        entity_types: list[str] | None,
        recency_days: int,
        limit: int,
        trace_id: str,
    ) -> BroadRecallResult:
        """Broad recall for open-ended memory queries.

        Args:
            entity_types: Filter by entity types (None = all types).
            recency_days: Only include memories from last N days.
            limit: Maximum entities per type to return.
            trace_id: Trace identifier for observability.

        Returns:
            BroadRecallResult with entities grouped by type and recent sessions.
        """
        ...

    async def store_episode(self, episode: Episode, trace_id: str) -> str:
        """Store a new episode in episodic memory.

        Args:
            episode: The interaction episode to persist.
            trace_id: Trace identifier for observability.

        Returns:
            The episode ID assigned by the storage backend.
        """
        ...

    async def is_connected(self) -> bool:
        """Check if the memory backend is reachable.

        Returns:
            True if the backend is healthy and accepting requests.
        """
        ...
