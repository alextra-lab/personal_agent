"""Data models for memory graph."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Entity(BaseModel):
    """An entity extracted from conversations."""

    name: str
    entity_type: str  # "Person", "Place", "Topic", "Concept", etc.
    description: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)


class Relationship(BaseModel):
    """A relationship between entities or conversations."""

    source_id: str
    target_id: str
    relationship_type: str  # "DISCUSSES", "PART_OF", "SIMILAR_TO", "HAPPENED_BEFORE", etc.
    weight: float = Field(default=1.0, ge=0.0, le=1.0)
    properties: dict[str, Any] = Field(default_factory=dict)


class TurnNode(BaseModel):
    """A single turn (one user message + one assistant response) in the memory graph.

    Stored with the Neo4j label ``Turn``. The ``turn_id`` equals the ``trace_id``
    for the originating request and is used as the deduplication key.
    """

    turn_id: str  # UUID as string — equals trace_id
    trace_id: str | None = None
    session_id: str | None = None
    sequence_number: int = 0  # Position within the session (1-indexed)
    timestamp: datetime
    summary: str | None = None
    user_message: str
    assistant_response: str | None = None
    key_entities: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)


# Backward-compatibility alias — remove once all callers use TurnNode
ConversationNode = TurnNode


class SessionNode(BaseModel):
    """A session grouping an ordered sequence of turns.

    Stored with the Neo4j label ``Session``. One Session per unique
    ``session_id`` in the captured turns.
    """

    session_id: str  # UUID as string — matches TurnNode.session_id
    started_at: datetime  # Timestamp of the first turn
    ended_at: datetime  # Timestamp of the last turn
    turn_count: int = 0
    dominant_entities: list[str] = Field(default_factory=list)
    session_summary: str | None = None


class EntityNode(BaseModel):
    """An entity node in the graph."""

    entity_id: str
    name: str
    entity_type: str
    description: str | None = None
    interest_weight: float = Field(default=0.0, ge=0.0, le=1.0)  # How often user mentions this
    first_seen: datetime
    last_seen: datetime
    mention_count: int = 0
    properties: dict[str, Any] = Field(default_factory=dict)


class MemoryQuery(BaseModel):
    """Query parameters for memory retrieval."""

    entity_names: list[str] = Field(default_factory=list)
    entity_types: list[str] = Field(default_factory=list)
    relationship_types: list[str] = Field(default_factory=list)
    conversation_ids: list[str] = Field(default_factory=list)
    trace_ids: list[str] = Field(default_factory=list)
    max_depth: int = Field(default=3, ge=1, le=10)
    limit: int = Field(default=10, ge=1, le=100)
    min_interest_weight: float = Field(default=0.0, ge=0.0, le=1.0)
    recency_days: int | None = None  # Only return conversations from last N days


class MemoryQueryResult(BaseModel):
    """Result of a memory query."""

    conversations: list[TurnNode] = Field(default_factory=list)
    entities: list[EntityNode] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    relevance_scores: dict[str, float] = Field(default_factory=dict)  # turn_id -> score
