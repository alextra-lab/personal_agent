"""ADR-0114 D3/D4 accretion writer for the study sandbox (FRE-839).

Appends `Mention`+`MembershipAssertion`s to the evidence layer — never
overwrites — and recomputes the derived `MEMBER_OF` edge from the enlarged
assertion set. A fresh conversation re-mentioning an existing concept
resolves to the same hub via alias lookup (`resolve_concept_hub`) and
deepens (never replaces) its memberships.

Alias resolution (`resolve_concept_hub`) mirrors the algorithm already
established in `personal_agent.memory.dedup.check_entity_duplicate` for
prod entity dedup, adapted to the new `Concept`/`Surface`/`kind` schema —
with one deliberate asymmetry (codex plan-review, FRE-839 plan review):

- **Exact case-insensitive match (`normalized_name` equality) is kind-BLIND
  and always merges.** This is specifically the "same written token,
  different casing" case ADR-0114 D2 names (`Arterial calcification` /
  `Arterial Calcification` were tagged with *different* kinds in prod —
  that inconsistency is precisely the bug this ADR exists to fix).
  Gating this path on `kind` equality would fail to collapse the ADR's own
  named example. The shared hub's `kind` is first-write-wins by
  construction (this path never touches `Concept.kind` once created).
- **The embedding-similarity fallback (no exact match) is kind-GATED**,
  matching `dedup.py`'s existing homonym guard — this is where the real
  risk of merging unrelated concepts (e.g. `Python` the language vs
  `python` the animal) lives, and `kind` is the correct disambiguator
  there.

Known, documented limitation (not fixed by this ticket): two byte-identical
strings (same spelling, same case) referring to genuinely different things
are not distinguishable by case-fold at all. Resolving that requires more
signal than "alias resolution to one hub" (this ticket's mechanism-only
AC-2 scope) provides — left to FRE-841/843's fuller hard-negative test.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from personal_agent.config import get_settings
from scripts.study.neo4j_types import Neo4jSession

_ALLCAPS_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")

_VECTOR_SEARCH_TOP_K = 5

#: Shared Cypher fragments for deriving `MEMBER_OF` edge properties from
#: backing `MembershipAssertion`s — the one place this formula lives, so
#: `consolidator.apply_canonicalization_to_graph` (FRE-842) reuses it exactly
#: rather than risking drift on a formula this module's own docstring calls
#: "deliberately unsophisticated for v0" and flags for future tuning
#: (code-review finding, FRE-842).
MEMBER_OF_AGGREGATION_CLAUSE = (
    "avg(a.proposed_confidence) AS membership_confidence, "
    "count(DISTINCT ep) AS support_count, max(a.when) AS last_supported_at"
)
MEMBER_OF_SET_CLAUSE = (
    "SET m.membership_confidence = membership_confidence, "
    "    m.support_count = support_count, "
    "    m.last_supported_at = last_supported_at"
)


def _normalize(name: str) -> str:
    """The shared case-fold/trim key used for `Surface`/`Category` lookups."""
    return name.strip().lower()


def _is_allcaps_identifier(name: str) -> bool:
    """True for ALL_CAPS constant-style identifiers (mirrors `memory/dedup.py`).

    FSM states, enum values, and constants embed close to related names but
    represent distinct concepts — an ALL_CAPS name must never merge with a
    differently-cased one via the embedding-similarity fallback.
    """
    return bool(_ALLCAPS_RE.match(name))


@dataclass(frozen=True)
class ProposedMembership:
    """One category proposal for one concept, from one categorizer call."""

    concept_name: str
    kind: str
    category_name: str
    proposed_confidence: float


@dataclass(frozen=True)
class AssertionProvenance:
    """Provenance stamped by Python onto every `MembershipAssertion` — never
    asked of the model (mirrors `entity_extraction.py`'s `_build_provenance`
    split).
    """

    model: str
    prompt_version: str
    seed: int
    when: datetime


@dataclass(frozen=True)
class ResolvedConceptMemberships:
    """One already-hub-resolved concept's proposed memberships for one episode."""

    concept_id: str
    memberships: list[ProposedMembership]


async def resolve_concept_hub(
    session: Neo4jSession,
    *,
    surface_name: str,
    kind: str,
    embedding: list[float] | None,
) -> str:
    """Find-or-create the `Concept` hub for *surface_name*, resolving aliases.

    Args:
        session: Active Neo4j async session against the study sandbox.
        surface_name: The surface form as it appears in the source (e.g. the
            frozen corpus's `Entity.name`).
        kind: The preserved ADR-0109 entity kind for this surface's source
            `Entity` (`Entity.entity_type`), carried as a control property.
        embedding: The surface's embedding vector, or `None` if unavailable —
            only the embedding-similarity fallback needs it.

    Returns:
        The resolved (or newly created) `Concept.id`.
    """
    normalized_name = _normalize(surface_name)

    exact_result = await session.run(
        "MATCH (s:Surface {normalized_name: $normalized_name})-[:ALIAS_OF]->(c:Concept) "
        "RETURN c.id AS concept_id, c.kind AS kind LIMIT 1",
        {"normalized_name": normalized_name},
    )
    async for record in exact_result:
        return str(record["concept_id"])

    return await _resolve_after_exact_miss(
        session,
        surface_name=surface_name,
        kind=kind,
        embedding=embedding,
        normalized_name=normalized_name,
    )


async def resolve_concept_hubs_batch(
    session: Neo4jSession, *, surfaces: list[tuple[str, str, list[float] | None]]
) -> dict[str, str]:
    """Resolve every `(surface_name, kind, embedding)` in one batch.

    Code-review finding (FRE-839): a plain per-concept loop over
    `resolve_concept_hub` is 1-3 Neo4j round trips per concept (the exact
    same N+1-per-episode pattern `write_mentions_and_assertions`/
    `recompute_member_of_batch` were rewritten to avoid). The exact-match
    path — the common case, since most concepts recur across conversations
    once a hub exists — is batchable in one `UNWIND`; only genuine misses
    (new concepts, or ones needing the embedding-similarity fallback) still
    need individual round trips.

    Args:
        session: Active Neo4j async session against the study sandbox.
        surfaces: `(surface_name, kind, embedding)` for every distinct
            concept an episode touches.

    Returns:
        `{surface_name: concept_id}` for every input surface.
    """
    normalized_by_surface = {
        surface_name: _normalize(surface_name) for surface_name, _, _ in surfaces
    }

    result = await session.run(
        "UNWIND $normalized_names AS normalized_name "
        "OPTIONAL MATCH (s:Surface {normalized_name: normalized_name})-[:ALIAS_OF]->(c:Concept) "
        "RETURN normalized_name, c.id AS concept_id",
        {"normalized_names": list(set(normalized_by_surface.values()))},
    )
    concept_id_by_normalized = {
        str(r["normalized_name"]): str(r["concept_id"])
        async for r in result
        if r["concept_id"] is not None
    }

    resolved: dict[str, str] = {}
    for surface_name, kind, embedding in surfaces:
        normalized_name = normalized_by_surface[surface_name]
        if normalized_name in concept_id_by_normalized:
            resolved[surface_name] = concept_id_by_normalized[normalized_name]
        else:
            resolved[surface_name] = await _resolve_after_exact_miss(
                session,
                surface_name=surface_name,
                kind=kind,
                embedding=embedding,
                normalized_name=normalized_name,
            )
    return resolved


async def _resolve_after_exact_miss(
    session: Neo4jSession,
    *,
    surface_name: str,
    kind: str,
    embedding: list[float] | None,
    normalized_name: str,
) -> str:
    """The embedding-similarity fallback + create-new path, shared by
    `resolve_concept_hub` and `resolve_concept_hubs_batch` once an exact
    `normalized_name` match has already been ruled out.
    """
    if embedding is not None and any(x != 0.0 for x in embedding):
        settings = get_settings()
        threshold = settings.dedup_similarity_threshold
        vector_result = await session.run(
            "CALL db.index.vector.queryNodes('concept_embedding', $top_k, $embedding) "
            "YIELD node, score "
            "WHERE node.kind = $kind "
            "RETURN node.id AS id, node.canonical_name AS canonical_name, score "
            "ORDER BY score DESC",
            {"top_k": _VECTOR_SEARCH_TOP_K, "embedding": embedding, "kind": kind},
        )
        candidates = [record async for record in vector_result]
        if candidates:
            best = candidates[0]
            same_allcaps_class = _is_allcaps_identifier(surface_name) == _is_allcaps_identifier(
                str(best["canonical_name"])
            )
            if best["score"] >= threshold and same_allcaps_class:
                concept_id = str(best["id"])
                await session.run(
                    "MATCH (c:Concept {id: $concept_id}) "
                    "MERGE (s:Surface {normalized_name: $normalized_name}) "
                    "ON CREATE SET s.display_name = $surface_name, s.kind = $kind "
                    "MERGE (s)-[:ALIAS_OF]->(c)",
                    {
                        "concept_id": concept_id,
                        "normalized_name": normalized_name,
                        "surface_name": surface_name,
                        "kind": kind,
                    },
                )
                return concept_id

    concept_id = str(uuid.uuid4())
    await session.run(
        "CREATE (c:Concept {id: $concept_id, canonical_name: $surface_name, kind: $kind, "
        "embedding: $embedding, valence: null, arousal: null}) "
        "CREATE (s:Surface {normalized_name: $normalized_name, display_name: $surface_name, kind: $kind}) "
        "CREATE (s)-[:ALIAS_OF]->(c)",
        {
            "concept_id": concept_id,
            "surface_name": surface_name,
            "kind": kind,
            "embedding": embedding,
            "normalized_name": normalized_name,
        },
    )
    return concept_id


async def write_episode(session: Neo4jSession, *, episode_id: str, source_session_id: str) -> None:
    """Upsert the `Episode` node for one conversation."""
    await session.run(
        "MERGE (e:Episode {id: $episode_id}) SET e.source_session_id = $source_session_id",
        {"episode_id": episode_id, "source_session_id": source_session_id},
    )


async def write_mentions_and_assertions(
    session: Neo4jSession,
    *,
    episode_id: str,
    resolved: list[ResolvedConceptMemberships],
    provenance: AssertionProvenance,
) -> list[tuple[str, str]]:
    """Append `Mention`+`MembershipAssertion`s for one episode's memberships.

    One `UNWIND`-batched Cypher statement for the whole episode (not a
    per-concept or per-membership loop — codex plan-review finding: a loop
    here would reintroduce FRE-838's own N+1 write mistake at real corpus
    volume). Every assertion is a fresh `CREATE` — evidence is never
    overwritten.

    Args:
        session: Active Neo4j async session against the study sandbox.
        episode_id: The episode (conversation) these mentions belong to.
        resolved: Each already-hub-resolved concept's proposed memberships.
        provenance: The model/prompt_version/seed/when to stamp on every
            assertion this call creates.

    Returns:
        The distinct `(concept_id, category_normalized_name)` pairs touched,
        for the batched `recompute_member_of_batch` call.
    """
    rows = [
        {
            "concept_id": rcm.concept_id,
            "mention_id": f"{episode_id}:{rcm.concept_id}",
            "category_normalized_name": _normalize(membership.category_name),
            "category_display_name": membership.category_name,
            "proposed_confidence": membership.proposed_confidence,
            "assertion_id": str(uuid.uuid4()),
            "model": provenance.model,
            "prompt_version": provenance.prompt_version,
            "seed": provenance.seed,
            "when": provenance.when.isoformat(),
        }
        for rcm in resolved
        for membership in rcm.memberships
    ]

    result = await session.run(
        "UNWIND $rows AS row "
        "MATCH (ep:Episode {id: $episode_id}) "
        "MATCH (c:Concept {id: row.concept_id}) "
        "MERGE (m:Mention {id: row.mention_id}) "
        "MERGE (ep)-[:HAS_MENTION]->(m) "
        "MERGE (m)-[:REFERS_TO]->(c) "
        "MERGE (cat:Category {normalized_name: row.category_normalized_name}) "
        "  ON CREATE SET cat.display_name = row.category_display_name "
        "CREATE (a:MembershipAssertion {"
        "  id: row.assertion_id, proposed_confidence: row.proposed_confidence, "
        "  model: row.model, prompt_version: row.prompt_version, seed: row.seed, when: row.when"
        "}) "
        "CREATE (m)-[:PRODUCED]->(a) "
        "CREATE (a)-[:ABOUT]->(c) "
        "CREATE (a)-[:PROPOSES]->(cat) "
        "MERGE (c)-[men:MENTIONED_IN]->(ep) "
        "  ON CREATE SET men.when = row.when "
        "RETURN DISTINCT row.concept_id AS concept_id, "
        "row.category_normalized_name AS category_normalized_name",
        {"rows": rows, "episode_id": episode_id},
    )
    return [(str(r["concept_id"]), str(r["category_normalized_name"])) async for r in result]


async def recompute_member_of_batch(session: Neo4jSession, *, pairs: list[tuple[str, str]]) -> None:
    """Recompute the derived `MEMBER_OF` edge for every touched (concept,
    category) pair, in one batched Cypher round-trip (codex plan-review
    finding: was a per-pair loop, reintroducing FRE-838's N+1 mistake).

    `membership_confidence` is a simple mean of all backing assertions —
    deliberately unsophisticated for v0 (a more sophisticated reinforcement
    curve is FRE-842's tuning concern; AC-1 only checks edge degree and
    support_count, not this number's precision). Always a full
    recomputation from the current assertion set — never an incremental
    delta from a stale read.

    Args:
        session: Active Neo4j async session against the study sandbox.
        pairs: The `(concept_id, category_normalized_name)` pairs to recompute.
    """
    await session.run(
        "UNWIND $pairs AS pair "
        "MATCH (c:Concept {id: pair.concept_id})<-[:ABOUT]-(a:MembershipAssertion)"
        "-[:PROPOSES]->(cat:Category {normalized_name: pair.category_normalized_name}) "
        "MATCH (a)<-[:PRODUCED]-(:Mention)<-[:HAS_MENTION]-(ep:Episode) "
        f"WITH c, cat, {MEMBER_OF_AGGREGATION_CLAUSE} "
        "MERGE (c)-[m:MEMBER_OF]->(cat) "
        f"{MEMBER_OF_SET_CLAUSE}",
        {
            "pairs": [
                {"concept_id": concept_id, "category_normalized_name": category_normalized_name}
                for concept_id, category_normalized_name in pairs
            ]
        },
    )
