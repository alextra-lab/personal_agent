"""ADR-0049 Phase 1: Protocol interfaces for knowledge graph and session store.

These protocols define the structural contracts that knowledge graph and session
persistence implementations must satisfy. They enable dependency inversion across
the memory boundary so consumers depend on abstractions, not concrete Neo4j/Postgres
classes.

See: docs/architecture_decisions/ADR-0049.md
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from personal_agent.memory.models import (
    Entity,
    EntityNode,
    MemoryQuery,
    MemoryQueryResult,
    Relationship,
    SessionNode,
    TurnNode,
)
from personal_agent.telemetry.trace import TraceContext


class KnowledgeGraphProtocol(Protocol):
    """Protocol for knowledge graph storage and retrieval.

    Structural contract for Neo4j (and future) knowledge graph backends.
    All implementations must satisfy this interface implicitly via structural
    subtyping — no explicit `class Impl(KnowledgeGraphProtocol)` needed.

    Key invariants:
        - ``search`` returns ranked results with higher relevance first.
        - ``store_fact`` is idempotent when the same entity is stored twice.
        - ``get_relationships`` returns all direct relationships for an entity.
    """

    async def search(
        self,
        query: str,
        limit: int,
        ctx: TraceContext,
    ) -> Sequence[EntityNode]:
        """Search the knowledge graph by free-text query.

        Args:
            query: Free-text search string.
            limit: Maximum number of results to return.
            ctx: Trace context for observability.

        Returns:
            Sequence of EntityNode results ordered by relevance.
        """
        ...

    async def get_entity(self, entity_id: str) -> EntityNode | None:
        """Retrieve a single entity by its identifier.

        Args:
            entity_id: Unique entity identifier.

        Returns:
            The EntityNode if found, None otherwise.
        """
        ...

    async def store_fact(self, fact: Entity, ctx: TraceContext) -> str:
        """Persist an entity (fact) to the knowledge graph.

        Args:
            fact: Entity to store.
            ctx: Trace context for observability.

        Returns:
            The entity identifier assigned by the storage backend.
        """
        ...

    async def get_relationships(self, entity_id: str) -> Sequence[Relationship]:
        """Retrieve all direct relationships for an entity.

        Args:
            entity_id: Unique entity identifier.

        Returns:
            Sequence of Relationship objects connected to the entity.
        """
        ...

    async def query_memory(self, query: MemoryQuery) -> MemoryQueryResult:
        """Execute a structured memory query against the knowledge graph.

        Args:
            query: Structured query with filters, depth, and limits.

        Returns:
            MemoryQueryResult containing matched turns, entities, and relationships.
        """
        ...


class SessionStoreProtocol(Protocol):
    """Protocol for conversation session persistence.

    Structural contract for PostgreSQL (and future) session storage backends.
    Implementations are responsible for maintaining message ordering within a
    session and providing efficient recent-history retrieval.

    Key invariants:
        - ``get_session`` returns None (not an exception) for unknown session IDs.
        - ``save_message`` appends without overwriting prior messages.
        - ``get_messages`` returns messages in chronological order.
    """

    async def get_session(self, session_id: str) -> SessionNode | None:
        """Retrieve a session by its identifier.

        Args:
            session_id: Unique session identifier.

        Returns:
            The SessionNode if found, None otherwise.
        """
        ...

    async def save_message(self, session_id: str, message: TurnNode) -> None:
        """Persist a new message (turn) to a session.

        Args:
            session_id: Target session identifier.
            message: The conversation turn to append.
        """
        ...

    async def get_messages(self, session_id: str, limit: int) -> Sequence[TurnNode]:
        """Retrieve the most recent messages for a session.

        Args:
            session_id: Session identifier to query.
            limit: Maximum number of messages to return.

        Returns:
            Sequence of TurnNode objects in chronological order (oldest first).
        """
        ...


class SearchIndexProtocol(Protocol):
    """Protocol for full-text and semantic search indexing.

    Structural contract for Elasticsearch (and future) search backends.
    Used for indexing structured trace events and querying across them.

    Key invariants:
        - ``index`` is idempotent when called with the same doc_id.
        - ``search`` returns at most ``limit`` results.
        - Result ordering is by relevance (implementation-defined scoring).
    """

    async def index(self, doc_id: str, document: Mapping[str, Any]) -> None:
        """Index a document under the given identifier.

        Args:
            doc_id: Unique document identifier (used for idempotent upserts).
            document: JSON-serialisable mapping to index.
        """
        ...

    async def search(self, query: str, index: str, limit: int) -> Sequence[Mapping[str, Any]]:
        """Search an index for documents matching the query.

        Args:
            query: Free-text or structured query string.
            index: Target index name (or pattern, e.g. ``"agent-logs-*"``).
            limit: Maximum number of results to return.

        Returns:
            Sequence of document mappings ordered by relevance.
        """
        ...
