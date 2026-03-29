# src/personal_agent/memory/dedup.py
"""Fuzzy entity deduplication pipeline.

Two-tier dedup on entity creation:
1. Vector similarity check against existing entities (via Neo4j vector index)
2. Above-threshold matches are merged to the canonical entity name

Prevents the 500-node explosion from 40 mentions of 10 entities
(EVAL-02 Scenario 4).

See: ADR-0035, Enhancement 2 (fuzzy entity deduplication)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, cast

import structlog

from personal_agent.config import get_settings

logger = structlog.get_logger(__name__)


class DedupDecision(Enum):
    """Deduplication decision for an entity."""

    CREATE_NEW = "create_new"
    MERGE_EXISTING = "merge_existing"


@dataclass(frozen=True)
class DedupResult:
    """Result of a deduplication check.

    Args:
        decision: Whether to create a new entity or merge with existing.
        canonical_name: Name of the existing entity to merge with (if MERGE).
        similarity_score: Cosine similarity with the best match.
    """

    decision: DedupDecision
    canonical_name: str | None = None
    similarity_score: float = 0.0


async def check_entity_duplicate(
    name: str,
    entity_type: str,
    embedding: list[float],
    neo4j_session: Any,
) -> DedupResult:
    """Check if an entity is a duplicate of an existing entity.

    Uses vector similarity search against the entity_embedding index.

    Args:
        name: Proposed entity name.
        entity_type: Entity type (e.g., "Technology").
        embedding: Embedding vector for the proposed entity.
        neo4j_session: Active Neo4j async session.

    Returns:
        DedupResult with merge decision.
    """
    settings = get_settings()
    threshold = settings.dedup_similarity_threshold

    similar = await _find_similar_entities(
        embedding=embedding,
        entity_type=entity_type,
        neo4j_session=neo4j_session,
        top_k=5,
    )

    if not similar:
        return DedupResult(decision=DedupDecision.CREATE_NEW)

    best = similar[0]

    # Exact name match always merges
    if best["name"].lower() == name.lower():
        return DedupResult(
            decision=DedupDecision.MERGE_EXISTING,
            canonical_name=best["name"],
            similarity_score=best["similarity"],
        )

    # Above threshold — merge with canonical
    if best["similarity"] >= threshold:
        logger.info(
            "entity_dedup_merge",
            proposed_name=name,
            canonical_name=best["name"],
            similarity=round(best["similarity"], 3),
        )
        return DedupResult(
            decision=DedupDecision.MERGE_EXISTING,
            canonical_name=best["name"],
            similarity_score=best["similarity"],
        )

    return DedupResult(decision=DedupDecision.CREATE_NEW)


async def _find_similar_entities(
    embedding: list[float],
    entity_type: str,
    neo4j_session: Any,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Find entities similar to the given embedding vector.

    Args:
        embedding: Query embedding vector.
        entity_type: Filter to same entity type.
        neo4j_session: Active Neo4j async session.
        top_k: Number of results to return.

    Returns:
        List of dicts with name, similarity, entity_type.
    """
    try:
        result = await neo4j_session.run(
            """
            CALL db.index.vector.queryNodes(
                'entity_embedding', $top_k, $embedding
            )
            YIELD node, score
            WHERE node.entity_type = $entity_type
            RETURN node.name AS name,
                   node.entity_type AS entity_type,
                   score AS similarity
            ORDER BY score DESC
            """,
            top_k=top_k,
            embedding=embedding,
            entity_type=entity_type,
        )
        return cast(list[dict[str, Any]], await result.data())

    except Exception as exc:
        logger.warning(
            "dedup_vector_search_failed",
            error=str(exc),
        )
        return []
