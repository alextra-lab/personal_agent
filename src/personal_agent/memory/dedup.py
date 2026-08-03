# src/personal_agent/memory/dedup.py
"""Fuzzy entity deduplication pipeline.

Two-tier dedup on entity creation:
1. Vector similarity check against existing entities (via Neo4j vector index)
2. Above-threshold matches are merged to the canonical entity name, but **only when
   the two names are the same name** (FRE-1115 — see ``_names_are_equivalent``)

Prevents the 500-node explosion from 40 mentions of 10 entities
(EVAL-02 Scenario 4).

**Why step 2 has a name guard.** Similarity alone was authorizing the rename, and a
rename is destructive: the losing name's turns and relationships are attached to the
winning node instead. Measured on the live graph at the 0.92 threshold, that merged
``mathematics`` into ``computer science`` (0.960), ``Blueberries`` into ``Apricots``
(0.957) and ``Azure`` into ``Bedrock`` (0.952) — roughly 8-9 of 14 different-name
renames inspected were wrong. Merges are therefore restricted to case / diacritic /
punctuation variants of one name, and every rejection is logged as
``entity_dedup_rejected_name_incompatible`` so the broader dedup-semantics question
(should ``Cumin`` and ``Ground cumin`` merge? under what threshold, per embedder?) can
be decided from evidence in its own ADR rather than by a cosine score.

See: ADR-0035, Enhancement 2 (fuzzy entity deduplication); FRE-412 (ALL_CAPS guard);
FRE-1115 (name-equivalence guard).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, cast

import structlog

from personal_agent.config import get_settings

logger = structlog.get_logger(__name__)

# FRE-1115: characters that carry no identity — a name differing only in these is the
# same name. Anything else differing means two different things.
_NAME_PUNCTUATION = re.compile(r"[\s\-_/.,'’()\[\]]+")


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
        entity_type: Entity type (e.g., "TechnicalArtifact").
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

    # FRE-1115: consider every candidate, not only the top-ranked one. The name guards
    # below can reject rank 1 while the entity that genuinely IS this name sits at rank
    # 2-5 — a topically-close neighbour outranking a case variant is exactly the ordering
    # the guards were added to distrust. Taking similar[0] alone would then create a
    # duplicate of a node that should have merged.
    exact = next((c for c in similar if c["name"].lower() == name.lower()), None)
    if exact is not None:
        return DedupResult(
            decision=DedupDecision.MERGE_EXISTING,
            canonical_name=exact["name"],
            similarity_score=exact["similarity"],
        )

    # Above threshold — merge with canonical, subject to the name-pattern guards.
    # FRE-1115: cosine similarity alone cannot authorize a rename. Measured on the live
    # graph, the 0.92 threshold merged `mathematics` into `computer science` (0.960) and
    # `Blueberries` into `Apricots` (0.957) — a merge destroys the losing entity's
    # identity and silently attaches its turns and relationships to an unrelated node. A
    # different-name merge therefore additionally requires the two names to *be* the same
    # name (case / diacritic / punctuation variants). Rejecting is fail-safe: it creates a
    # distinct entity rather than destroying one. Substantive merges (`Cumin` into `Ground
    # cumin`) are a design decision that belongs to the dedup-semantics ADR (FRE-1126),
    # not to a cosine score.
    above_threshold = [c for c in similar if c["similarity"] >= threshold]
    for candidate in above_threshold:
        # Guard: ALL_CAPS identifiers (FSM states, enum values, constants) must not
        # merge with differently-cased names. They embed close to related error/event
        # names but represent distinct concepts (FRE-412).
        if _is_allcaps_identifier(name) != _is_allcaps_identifier(candidate["name"]):
            continue
        if not _names_are_equivalent(name, candidate["name"]):
            continue
        logger.info(
            "entity_dedup_merge",
            proposed_name=name,
            canonical_name=candidate["name"],
            similarity=round(candidate["similarity"], 3),
        )
        return DedupResult(
            decision=DedupDecision.MERGE_EXISTING,
            canonical_name=candidate["name"],
            similarity_score=candidate["similarity"],
        )

    # Nothing above the threshold names the same thing. Log the top candidate as the
    # audit record for FRE-1126 — it is the merge that would have happened before.
    if above_threshold:
        rejected = above_threshold[0]
        logger.info(
            "entity_dedup_rejected_name_incompatible",
            proposed_name=name,
            candidate_name=rejected["name"],
            similarity=round(rejected["similarity"], 3),
            entity_type=entity_type,
        )

    return DedupResult(decision=DedupDecision.CREATE_NEW)


def _normalize_name(name: str) -> str:
    """Reduce a name to its identity-bearing form.

    Strips case, diacritics and punctuation/spacing, all of which vary between
    extractions of the same name without changing what is named.

    Args:
        name: Entity name to normalize.

    Returns:
        The normalized form, for equality comparison only — never for storage.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    without_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NAME_PUNCTUATION.sub("", without_marks).casefold()


def _names_are_equivalent(name: str, candidate: str) -> bool:
    """Return True when two names denote the same name (FRE-1115).

    Equivalent means differing only in case, diacritics, punctuation or spacing —
    ``"Pâté à bombe"`` and ``"Pate a bombe"``, ``"Météo-France"`` and ``"Météo France"``.
    Distinct names are never equivalent however close their embeddings.

    Args:
        name: Proposed entity name.
        candidate: Existing entity name dedup wants to merge into.

    Returns:
        True when the two names may be treated as one entity.
    """
    return _normalize_name(name) == _normalize_name(candidate)


def _is_allcaps_identifier(name: str) -> bool:
    """Return True if name is an ALL_CAPS constant-style identifier.

    Args:
        name: Entity name to test.

    Returns:
        True when name matches the ALL_CAPS_WITH_UNDERSCORES pattern.
    """
    return bool(re.match(r"^[A-Z][A-Z0-9_]+$", name))


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
        # node.user_id IS NULL excludes owner/user-anchored :Person nodes
        # (FRE-213 schema invariant); extracted entities must never collide
        # into them. See FRE-342, ADR-0052 amendment 2026-05-09.
        result = await neo4j_session.run(
            """
            CALL db.index.vector.queryNodes(
                'entity_embedding', $top_k, $embedding
            )
            YIELD node, score
            WHERE node.entity_type = $entity_type
              AND node.user_id IS NULL
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
