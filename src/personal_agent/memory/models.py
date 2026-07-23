"""Data models for memory graph."""

from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from personal_agent.memory.session_digest import SessionDigest
from personal_agent.memory.weight import KnowledgeWeight


class Visibility(str, Enum):
    """Memory node visibility scope (FRE-229 / ADR-0064 §D6).

    Stored as a plain string property on Neo4j nodes for cheap WHERE filtering.
    The PRIVATE level is reserved for a follow-up classification ticket; nodes
    are never tagged private in the current slice.

    Attributes:
        PUBLIC: Visible to everyone, including unauthenticated CLI paths.
        GROUP: Visible to all CF Access authenticated users (the household/family).
        PRIVATE: Visible only to the owning user; serialized as "private:<user_id>".
    """

    PUBLIC = "public"
    GROUP = "group"
    PRIVATE = "private"


class Entity(BaseModel):
    """An entity extracted from conversations."""

    name: str
    entity_type: str  # "Person", "Place", "Topic", "Concept", etc.
    description: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] | None = None
    coordinates: tuple[float, float] | None = None  # (latitude, longitude)
    geocoded: bool = False
    # Access tracking (FRE-161: KG Freshness)
    last_accessed_at: datetime | None = None
    access_count: int = 0
    last_access_context: str | None = (
        None  # "search", "context_assembly", "consolidation", "suggest_relevant", "tool_call"
    )
    first_accessed_at: datetime | None = None
    # D5: Knowledge confidence metadata (ADR-0047)
    weight: KnowledgeWeight = Field(default_factory=KnowledgeWeight)
    # FRE-229: visibility scope
    visibility: str = Visibility.PUBLIC
    # ADR-0115 D2: subject/ownership class, written as the Neo4j property `class`
    # (mirrors Claim.knowledge_class). None for callers outside the extraction
    # pipeline (e.g. gateway store_fact) that never classify the fact.
    knowledge_class: Literal["World", "Personal"] | None = None


class Relationship(BaseModel):
    """A relationship between entities or conversations."""

    source_id: str
    target_id: str
    relationship_type: str  # "DISCUSSES", "PART_OF", "SIMILAR_TO", "HAPPENED_BEFORE", etc.
    weight: float = Field(default=1.0, ge=0.0, le=1.0)
    properties: dict[str, Any] = Field(default_factory=dict)
    # Access tracking (FRE-161: KG Freshness)
    last_accessed_at: datetime | None = None
    access_count: int = 0
    last_access_context: str | None = (
        None  # "search", "context_assembly", "consolidation", "suggest_relevant", "tool_call"
    )
    first_accessed_at: datetime | None = None
    # FRE-229: visibility scope
    visibility: str = Visibility.PUBLIC


class Stance(BaseModel):
    """The owner's affect toward / mastery of a World concept (ADR-0098 D2).

    A Stance is a native ``HAS_STANCE`` edge inside Core — the owner ``:Person``
    (``is_owner=true``) to a World ``:Entity`` — never an entity or a description
    clause. It is provenance-bearing and temporally valid, so a changed stance
    supersedes the prior one (superseded ≠ deleted); supersession is keyed on the
    ``(owner, target)`` pair.

    Attributes:
        target: Name of the World concept the stance is about (an existing
            ``:Entity``; also emitted in the extractor's ``entities`` array).
        affect: Short sentiment/preference phrase ("loves the hybrid powertrain");
            empty when the stance is purely a mastery/skill level.
        mastery: Skill/learning level in [0.0, 1.0], or None for a pure preference.
        review_due: Spaced-repetition next-review time; None until a scheduler
            sets it (Stance lifecycle is ADR-0098 D4, out of this ticket's scope).
        trace_id: Originating capture's trace_id (provenance).
        session_id: Originating capture's session_id (provenance).
        source_type: Origin channel; "conversation" for extracted stances.
        observed_at: Turn time — the authoritative bitemporal ordering axis.
        extracted_at: Wall-clock when extraction ran (forensics only).
    """

    model_config = ConfigDict(frozen=True)

    target: str
    affect: str = ""
    mastery: float | None = Field(default=None, ge=0.0, le=1.0)
    review_due: datetime | None = None
    trace_id: str | None = None
    session_id: str | None = None
    source_type: str = "conversation"
    observed_at: datetime
    extracted_at: datetime | None = None


class Claim(BaseModel):
    """A first-class, provenance-bearing, temporally-valid durable fact (ADR-0098 D2).

    Replaces first-write-wins for durable knowledge: a Claim's value can change.
    Stored as a ``:Claim`` node hung off the owner via ``HAS_FACT``. This ticket
    (FRE-638) feeds Personal situational facts; the model is class-agnostic so
    World facts can migrate onto it later.

    Attributes:
        content: The fact as one self-contained declarative sentence.
        knowledge_class: One of "Personal"/"World"/"Stance"/"System" (ADR-0098 D1);
            "Personal" for the situational facts this ticket wires.
        facet: Normalized slot key for the fact (e.g. "lease_end_date"), so
            supersession can group same-slot claims deterministically (FRE-712);
            "" when the extractor names none (falls back to embedding matching).
        update_kind: The extractor's contradiction signal — "new"/"correction"/
            "evolution" (FRE-712) — driving the correction-vs-evolution supersession
            label instead of a confidence-delta guess. Defaults to "new".
        confidence: Confidence in [0.0, 1.0], derived from the source type; the
            weight the correction path adjudicates on.
        trace_id: Originating capture's trace_id (provenance).
        session_id: Originating capture's session_id (provenance).
        source_type: Origin channel; "conversation" for extracted claims.
        observed_at: Turn time — the authoritative bitemporal ordering axis
            (``valid_from`` is set from this at write time).
        extracted_at: Wall-clock when extraction ran (forensics only).
    """

    model_config = ConfigDict(frozen=True)

    content: str
    knowledge_class: str = "Personal"
    facet: str = ""
    update_kind: str = "new"
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    trace_id: str | None = None
    session_id: str | None = None
    source_type: str = "conversation"
    observed_at: datetime
    extracted_at: datetime | None = None


class TurnNode(BaseModel):
    """A single turn (one user message + one assistant response) in the memory graph.

    Stored with the Neo4j label ``Turn``. The ``turn_id`` equals the ``trace_id``
    for the originating request and is used as the deduplication key.
    """

    turn_id: str = Field(
        validation_alias=AliasChoices("turn_id", "conversation_id")
    )  # UUID as string — equals trace_id
    trace_id: str | None = None
    session_id: str | None = None
    sequence_number: int = 0  # Position within the session (1-indexed)
    timestamp: datetime
    summary: str | None = None
    user_message: str
    assistant_response: str | None = None
    key_entities: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    # FRE-229: visibility scope
    visibility: str = Visibility.PUBLIC

    @property
    def conversation_id(self) -> str:
        """Backward-compatible alias for legacy callers/tests."""
        return self.turn_id


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
    # ADR-0124 (FRE-947): the FRE-347 prose summary. NO LONGER WRITTEN — superseded
    # by session_label + session_digest. Historical rows keep their value
    # deliberately: they are the only pre-correction corpus, and the minimum-turns
    # floor applies only to sessions summarised after ADR-0124 Phase 0 shipped,
    # discriminated by summary_generated_at. Read for legacy display only.
    session_summary: str | None = None
    # ADR-0124 D3 — the two artifacts, stored independently. The label is useful in
    # states where the digest is not: absent under the single-turn floor, and
    # withheld after a generation failure.
    session_label: str | None = None
    session_digest: SessionDigest | None = None
    # ADR-0124 D1 — derived freshness. Dirty is
    # `summary_generated_at IS NULL OR summary_generated_at < ended_at`; there is no
    # summary_dirty column, no revision counter and no Postgres migration, because
    # the lifecycle state lives in the same substrate as the artifact it describes.
    #
    # It means PROJECTION FRESHNESS, not "a digest exists": a session deliberately
    # skipped under the minimum-turns floor is a *completed* projection with an empty
    # result, so it advances. Only a failure leaves it behind.
    summary_generated_at: datetime | None = None
    # ADR-0124 terminal-failure rule. A session is excluded from the population
    # checks only when it carries BOTH a stored reason and an attempt count at or
    # above the retry limit — and only for a deterministic reason, since a budget
    # denial is transient by nature and never terminal.
    summary_failure_reason: str | None = None
    summary_attempt_count: int = 0
    # FRE-229: visibility scope
    visibility: str = Visibility.PUBLIC


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
    # FRE-229: visibility scope
    visibility: str = Visibility.PUBLIC


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
    # FRE-658: an explicit caller-supplied HARD time window that survives the
    # ADR-0100 / ADR-0104 de-gate. None = no hard window (the automatic path, which
    # stays invariant to recency_days under the flags — ADR-0100 AC-1a); an int
    # re-applies a hard time bound on the de-gated recall paths (set only by the
    # memory_search tool when the caller passes an explicit positive recency_days).
    hard_recency_days: int | None = None
    # FRE-229: visibility scoping (chokepoint filter)
    user_id: UUID | None = None
    authenticated: bool = False


class MemoryQueryResult(BaseModel):
    """Result of a memory query."""

    conversations: list[TurnNode] = Field(default_factory=list)
    entities: list[EntityNode] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    relevance_scores: dict[str, float] = Field(default_factory=dict)  # turn_id -> score
