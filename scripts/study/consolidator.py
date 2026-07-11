"""ADR-0114 D5 offline consolidator v0 for the study sandbox (FRE-842).

The slow, offline "anti-snowflake engine" that keeps the fast, messy ingest
output usable — the only place GDS integrates, always batch, never in the
recall path. This module implements v0's two ops only (D5): (1) two-stage
category canonicalization, alias-merges only; (3') decay+prune of derived
`MEMBER_OF` edges (evidence retained).

**Two-stage canonicalization, not one threshold.** A single merge score
cannot tell a synonym pair from a broader/narrower pair:

- Stage 1 (candidate generation) proposes top-*k* category pairs by
  member-set overlap (Jaccard on shared `Concept` members) — the ADR's
  "designated mechanism" is GDS Node Similarity (`generate_candidates_gds`),
  live-verified against this repo's real study sandbox; a pure-Python
  pairwise Jaccard fallback (`generate_candidates_pairwise`) is explicitly
  sanctioned at v0 sandbox scale and is what the τ_merge sweep (`sweep.py`)
  uses by default — one Neo4j/GDS round trip per sweep config would be slow
  and is unnecessary once member-set snapshots are already in hand. Both
  paths optionally blend in cosine similarity on category-name embeddings
  (`combined_score = jaccard_weight * jaccard + (1 - jaccard_weight) *
  name_cosine`) — the ADR calls for combining these two signals; embeddings
  are computed once per snapshot and reused across an entire τ_merge grid
  (they don't depend on τ_merge), never recomputed per config.
- Stage 2 (`decide_candidate_type`) labels each candidate pair `alias`
  (merge), `subsumed_by` (a hierarchy relation — never merge), `related`,
  `distinct`, or `uncertain`. The containment/size-ratio check runs BEFORE
  the τ_merge alias gate and wins regardless of score — this is the ADR's
  named correctness guard: merging a broader parent into a narrower one
  corrupts the plateau metric, it is not a tuning artefact. A minimum
  category size gates this check so a noisy 1-member category can't force a
  spurious hierarchy decision.

`canonicalize` unions only `alias`-decided pairs (never `subsumed_by`/
`related`) via union-find to GROUP categories, then picks each group's
canonical representative in a separate, deterministic pass (largest
member-set, then normalized_name) — so the result never depends on the
order candidate pairs were unioned in.

`apply_canonicalization_to_graph` is the real single-τ_merge* write-back
primitive (FRE-843's job to invoke for real, at one operating point — not
exercised against live infra by this ticket, which only sweeps τ_merge
read-only). It never rewrites or moves a `MembershipAssertion` — evidence is
immutable per D2. It records `(:Category)-[:CANONICALIZED_AS]->(:Category)`
for audit, then recomputes `MEMBER_OF` **from assertions, grouped by
canonical category identity** (walking `CANONICALIZED_AS` to the root) —
this is what correctly handles a concept that already belongs to both
merge-side categories: it is aggregated once from the union of its backing
assertions, never double-counted by copying two derived edges' stored
properties together.

`decay_and_prune` is D5 op (3'): multiplies `membership_confidence` on
unreinforced (stale) derived `MEMBER_OF` edges by a decay factor and
suppresses (deletes) those below a floor. Only the derived edge is ever
touched; the backing assertions remain, so a later reinforcing context can
resurrect the membership (D4). Dry-run by default (`apply=False`), mirroring
`export_snapshot.py`/`run_ingest.py`'s "cheap default, explicit flag for the
consequential action" posture.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from scripts.study.neo4j_types import Neo4jDriver, Neo4jSession
from scripts.study.writer import MEMBER_OF_AGGREGATION_CLAUSE, MEMBER_OF_SET_CLAUSE

_DEFAULT_SUBSUMPTION_CONTAINMENT_FLOOR = 0.8
_DEFAULT_SUBSUMPTION_SIZE_RATIO_FLOOR = 2.0
_DEFAULT_MIN_CATEGORY_SIZE_FOR_SUBSUMPTION = 2
_DEFAULT_RELATED_FLOOR = 0.3
_DEFAULT_UNCERTAIN_MARGIN = 0.15
_DEFAULT_JACCARD_WEIGHT = 0.6


@dataclass(frozen=True)
class CategoryMembers:
    """One category's current member set, as read off the derived `MEMBER_OF` layer."""

    normalized_name: str
    display_name: str
    concept_ids: frozenset[str]


@dataclass(frozen=True)
class CandidatePair:
    """One Stage-1 candidate category pair. `category_a <= category_b` always
    (see `_ordered_pair`) so callers never have to dedup `(a,b)`/`(b,a)`.
    """

    category_a: str
    category_b: str
    jaccard: float
    name_cosine: float | None
    combined_score: float


class TypedDecision(str, Enum):
    """Stage-2 typed decision for one candidate pair (ADR-0114 D5)."""

    ALIAS = "alias"
    SUBSUMED_BY = "subsumed_by"
    RELATED = "related"
    DISTINCT = "distinct"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class CandidateDecision:
    """One Stage-2 decision, with the human-readable rule that produced it."""

    pair: CandidatePair
    decision: TypedDecision
    rationale: str


@dataclass(frozen=True)
class CanonicalizationResult:
    """The outcome of canonicalizing one category snapshot at one τ_merge.

    `canonical_of` maps EVERY category's normalized_name to its canonical
    representative's normalized_name (including categories mapped to
    themselves — untouched or representative categories).
    """

    canonical_of: dict[str, str]
    decisions: list[CandidateDecision]
    canonical_category_count: int


@dataclass(frozen=True)
class DecayPruneResult:
    """Summary of one `decay_and_prune` pass."""

    edges_considered: int
    would_decay_count: int
    would_suppress_count: int


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity of two member sets; 0.0 if either (or both) is empty."""
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 if either is a zero vector."""
    dot = float(sum(x * y for x, y in zip(a, b, strict=True)))
    norm_a = float(sum(x * x for x in a)) ** 0.5
    norm_b = float(sum(y * y for y in b)) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(dot / (norm_a * norm_b))


def _combined_score(jaccard: float, name_cosine: float | None, *, jaccard_weight: float) -> float:
    """Blend Stage-1's two signals; falls back to jaccard alone with no embeddings
    (the ADR's explicit v0 sandbox-scale allowance).
    """
    if name_cosine is None:
        return jaccard
    return jaccard_weight * jaccard + (1 - jaccard_weight) * name_cosine


def _ordered_pair(a: str, b: str) -> tuple[str, str]:
    """A stable `(min, max)` ordering so every candidate pair has one canonical form."""
    return (a, b) if a <= b else (b, a)


async def fetch_category_membership_snapshot(session: Neo4jSession) -> dict[str, CategoryMembers]:
    """Read the current derived `MEMBER_OF` layer into a member-set snapshot.

    Args:
        session: Active Neo4j async session against the study sandbox.

    Returns:
        `{normalized_name: CategoryMembers}` for every `Category` with at
        least one member.
    """
    result = await session.run(
        "MATCH (c:Concept)-[:MEMBER_OF]->(cat:Category) "
        "RETURN cat.normalized_name AS normalized_name, cat.display_name AS display_name, "
        "collect(c.id) AS concept_ids"
    )
    return {
        str(r["normalized_name"]): CategoryMembers(
            normalized_name=str(r["normalized_name"]),
            display_name=str(r["display_name"]),
            concept_ids=frozenset(str(cid) for cid in r["concept_ids"]),
        )
        async for r in result
    }


async def embed_category_names(categories: list[CategoryMembers]) -> dict[str, list[float]]:
    """Embed every category's display name, once per snapshot.

    Reused across an entire τ_merge grid by the caller — embeddings don't
    depend on τ_merge, so this must never be called per-config inside a sweep.

    Args:
        categories: The snapshot's categories.

    Returns:
        `{normalized_name: embedding}`.
    """
    from personal_agent.memory.embeddings import generate_embeddings_batch  # noqa: PLC0415

    names = [c.display_name for c in categories]
    vectors = await generate_embeddings_batch(names, mode="document")
    return {c.normalized_name: vector for c, vector in zip(categories, vectors, strict=True)}


def generate_candidates_pairwise(
    memberships: dict[str, CategoryMembers],
    *,
    top_k: int,
    min_jaccard: float,
    name_embeddings: dict[str, list[float]] | None = None,
    jaccard_weight: float = _DEFAULT_JACCARD_WEIGHT,
) -> list[CandidatePair]:
    """Stage 1, pure-Python fallback: O(n²) pairwise Jaccard over member sets.

    The v0 sandbox-scale fallback the ADR explicitly sanctions ("a plain
    Cypher Jaccard aggregation is an acceptable fallback"); used by default
    by the τ_merge sweep, which needs many configs over the same snapshot
    with no per-config Neo4j/GDS round trip.

    Candidate discovery is gated on member-set overlap (`min_jaccard`) — this
    mirrors the ADR's framing of GDS Node Similarity as proposing the
    candidate SET by member overlap, with name-cosine COMBINED into the
    resulting pairs' scoring, not used as an independent zero-overlap
    discovery mechanism.

    Args:
        memberships: The category snapshot.
        top_k: Maximum candidates to return, ranked by `combined_score` desc.
        min_jaccard: Minimum member-overlap Jaccard for a pair to be
            considered a candidate at all.
        name_embeddings: Optional `{normalized_name: embedding}` to blend in
            as `name_cosine`. `combined_score` falls back to jaccard alone
            when omitted.
        jaccard_weight: Weight on jaccard in the blend (`1 - jaccard_weight`
            on name_cosine).

    Returns:
        Up to `top_k` candidate pairs, ranked by `combined_score` desc.
    """
    names = sorted(memberships)
    candidates: list[CandidatePair] = []
    for i, name_a in enumerate(names):
        for name_b in names[i + 1 :]:
            jaccard = _jaccard(memberships[name_a].concept_ids, memberships[name_b].concept_ids)
            if jaccard < min_jaccard:
                continue
            cosine = None
            if name_embeddings is not None:
                cosine = _cosine(name_embeddings[name_a], name_embeddings[name_b])
            combined = _combined_score(jaccard, cosine, jaccard_weight=jaccard_weight)
            a, b = _ordered_pair(name_a, name_b)
            candidates.append(
                CandidatePair(
                    category_a=a,
                    category_b=b,
                    jaccard=jaccard,
                    name_cosine=cosine,
                    combined_score=combined,
                )
            )
    candidates.sort(key=lambda c: c.combined_score, reverse=True)
    return candidates[:top_k]


async def generate_candidates_gds(
    driver: Neo4jDriver,
    *,
    graph_name: str = "consolidator_candidates",
    top_k: int,
    similarity_cutoff: float,
    name_embeddings: dict[str, list[float]] | None = None,
    jaccard_weight: float = _DEFAULT_JACCARD_WEIGHT,
) -> list[CandidatePair]:
    """Stage 1, the ADR's designated mechanism: GDS Node Similarity.

    Projects the `Category`-`Concept` bipartite graph with `MEMBER_OF`
    orientation reversed (so nodes with outgoing relationships in the
    projection are `Category` nodes, and `nodeSimilarity` computes pairwise
    Jaccard between categories over their shared `Concept` neighbors — live
    Cypher-verified against the real running study sandbox), streams top-*k*
    similar pairs per category, then always drops the projection (`finally`)
    even if streaming raises.

    Pairs are normalized before being returned: self-pairs excluded,
    `(a,b)`/`(b,a)` duplicates (GDS's stream is not guaranteed symmetric
    within one node's top-k) deduped keeping the higher similarity.

    Args:
        driver: Connected async Neo4j driver pointed at the study sandbox.
        graph_name: The ephemeral GDS projection name.
        top_k: GDS `nodeSimilarity`'s `topK` (candidates per node).
        similarity_cutoff: GDS `nodeSimilarity`'s `similarityCutoff`.
        name_embeddings: Optional `{normalized_name: embedding}` to blend in.
        jaccard_weight: Weight on jaccard in the blend.

    Returns:
        Normalized, deduped candidate pairs.
    """
    async with driver.session() as session:
        try:
            await session.run(
                "CALL gds.graph.project($graph_name, ['Concept', 'Category'], "
                "{MEMBER_OF: {orientation: 'REVERSE'}})",
                {"graph_name": graph_name},
            )
            result = await session.run(
                "CALL gds.nodeSimilarity.stream($graph_name, "
                "{topK: $top_k, similarityCutoff: $similarity_cutoff}) "
                "YIELD node1, node2, similarity "
                "RETURN gds.util.asNode(node1).normalized_name AS a, "
                "gds.util.asNode(node2).normalized_name AS b, similarity",
                {"graph_name": graph_name, "top_k": top_k, "similarity_cutoff": similarity_cutoff},
            )
            rows = [r async for r in result]
        finally:
            await session.run("CALL gds.graph.drop($graph_name, false)", {"graph_name": graph_name})

    best_by_pair: dict[tuple[str, str], float] = {}
    for row in rows:
        a, b = str(row["a"]), str(row["b"])
        if a == b:
            continue
        pair = _ordered_pair(a, b)
        similarity = float(row["similarity"])
        if pair not in best_by_pair or similarity > best_by_pair[pair]:
            best_by_pair[pair] = similarity

    candidates = [
        CandidatePair(
            category_a=a,
            category_b=b,
            jaccard=jaccard,
            name_cosine=(
                _cosine(name_embeddings[a], name_embeddings[b])
                if name_embeddings is not None
                else None
            ),
            combined_score=_combined_score(
                jaccard,
                _cosine(name_embeddings[a], name_embeddings[b])
                if name_embeddings is not None
                else None,
                jaccard_weight=jaccard_weight,
            ),
        )
        for (a, b), jaccard in best_by_pair.items()
    ]
    candidates.sort(key=lambda c: c.combined_score, reverse=True)
    return candidates


def decide_candidate_type(
    pair: CandidatePair,
    memberships: dict[str, CategoryMembers],
    *,
    tau_merge: float,
    subsumption_containment_floor: float = _DEFAULT_SUBSUMPTION_CONTAINMENT_FLOOR,
    subsumption_size_ratio_floor: float = _DEFAULT_SUBSUMPTION_SIZE_RATIO_FLOOR,
    min_category_size_for_subsumption: int = _DEFAULT_MIN_CATEGORY_SIZE_FOR_SUBSUMPTION,
    related_floor: float = _DEFAULT_RELATED_FLOOR,
    uncertain_margin: float = _DEFAULT_UNCERTAIN_MARGIN,
) -> CandidateDecision:
    """Stage 2: label one candidate pair (ADR-0114 D5).

    Decision order:
    1. **Containment guard (before the τ_merge gate, wins regardless of
       score).** Only when BOTH categories have at least
       `min_category_size_for_subsumption` members (guards against a noisy
       1-member category forcing a spurious hierarchy decision): if the
       larger/smaller member-count ratio is at least
       `subsumption_size_ratio_floor` AND the smaller set's containment in
       the larger (`|A∩B| / |smaller|`) is at least
       `subsumption_containment_floor` → `SUBSUMED_BY` (narrower subsumed by
       broader). This is the ADR's named correctness guard against merging a
       broader parent into a narrower one.
    2. `combined_score >= tau_merge` → `ALIAS`.
    3. `combined_score >= tau_merge - uncertain_margin` → `UNCERTAIN`.
    4. `combined_score >= related_floor` → `RELATED`.
    5. Else → `DISTINCT`.

    Args:
        pair: The candidate pair (with its precomputed jaccard/combined_score).
        memberships: The category snapshot (for member-set sizes/containment).
        tau_merge: The merge threshold (the study's one swept knob).
        subsumption_containment_floor: Minimum containment for the guard.
        subsumption_size_ratio_floor: Minimum size-ratio asymmetry for the guard.
        min_category_size_for_subsumption: Minimum member count on BOTH sides
            for the containment guard to apply at all.
        related_floor: Minimum combined_score for `RELATED` (below is `DISTINCT`).
        uncertain_margin: Band width below `tau_merge` that is `UNCERTAIN`
            rather than `RELATED`.

    Returns:
        The typed decision with a human-readable rationale.
    """
    members_a = memberships[pair.category_a].concept_ids
    members_b = memberships[pair.category_b].concept_ids
    size_a, size_b = len(members_a), len(members_b)

    if size_a >= min_category_size_for_subsumption and size_b >= min_category_size_for_subsumption:
        smaller_size = min(size_a, size_b)
        larger_size = max(size_a, size_b)
        size_ratio = larger_size / smaller_size if smaller_size else float("inf")
        shared = len(members_a & members_b)
        containment = shared / smaller_size if smaller_size else 0.0
        if (
            size_ratio >= subsumption_size_ratio_floor
            and containment >= subsumption_containment_floor
        ):
            return CandidateDecision(
                pair=pair,
                decision=TypedDecision.SUBSUMED_BY,
                rationale=(
                    f"containment={containment:.2f} >= {subsumption_containment_floor} and "
                    f"size_ratio={size_ratio:.2f} >= {subsumption_size_ratio_floor}: "
                    "narrower category is subsumed by the broader one, never merged"
                ),
            )

    if pair.combined_score >= tau_merge:
        return CandidateDecision(
            pair=pair,
            decision=TypedDecision.ALIAS,
            rationale=f"combined_score={pair.combined_score:.3f} >= tau_merge={tau_merge}",
        )
    if pair.combined_score >= tau_merge - uncertain_margin:
        return CandidateDecision(
            pair=pair,
            decision=TypedDecision.UNCERTAIN,
            rationale=(
                f"combined_score={pair.combined_score:.3f} within uncertain_margin="
                f"{uncertain_margin} below tau_merge={tau_merge}"
            ),
        )
    if pair.combined_score >= related_floor:
        return CandidateDecision(
            pair=pair,
            decision=TypedDecision.RELATED,
            rationale=f"combined_score={pair.combined_score:.3f} >= related_floor={related_floor}",
        )
    return CandidateDecision(
        pair=pair,
        decision=TypedDecision.DISTINCT,
        rationale=f"combined_score={pair.combined_score:.3f} < related_floor={related_floor}",
    )


def canonicalize(
    memberships: dict[str, CategoryMembers],
    candidates: list[CandidatePair],
    *,
    tau_merge: float,
    subsumption_containment_floor: float = _DEFAULT_SUBSUMPTION_CONTAINMENT_FLOOR,
    subsumption_size_ratio_floor: float = _DEFAULT_SUBSUMPTION_SIZE_RATIO_FLOOR,
    min_category_size_for_subsumption: int = _DEFAULT_MIN_CATEGORY_SIZE_FOR_SUBSUMPTION,
    related_floor: float = _DEFAULT_RELATED_FLOOR,
    uncertain_margin: float = _DEFAULT_UNCERTAIN_MARGIN,
) -> CanonicalizationResult:
    """Run Stage 2 over every candidate, then canonicalize the `ALIAS` graph.

    Union-find groups categories linked by an `ALIAS` decision (transitively
    — `a~b` and `b~c` puts `a`, `b`, `c` in one group even though `a`/`c`
    were never directly compared). Union order only affects which node is
    the union-find ROOT internally; the canonical REPRESENTATIVE returned to
    callers is always chosen in a separate, deterministic pass over each
    final group (largest member-set, then normalized_name ascending), so the
    result is independent of candidate input order.

    Args:
        memberships: The category snapshot.
        candidates: Stage-1 candidate pairs to run Stage 2 over.
        tau_merge: The merge threshold.
        subsumption_containment_floor: Forwarded to `decide_candidate_type`.
        subsumption_size_ratio_floor: Forwarded to `decide_candidate_type`.
        min_category_size_for_subsumption: Forwarded to `decide_candidate_type`.
        related_floor: Forwarded to `decide_candidate_type`.
        uncertain_margin: Forwarded to `decide_candidate_type`.

    Returns:
        The canonicalization result.
    """
    parent: dict[str, str] = {name: name for name in memberships}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(a: str, b: str) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    decisions: list[CandidateDecision] = []
    for pair in candidates:
        decision = decide_candidate_type(
            pair,
            memberships,
            tau_merge=tau_merge,
            subsumption_containment_floor=subsumption_containment_floor,
            subsumption_size_ratio_floor=subsumption_size_ratio_floor,
            min_category_size_for_subsumption=min_category_size_for_subsumption,
            related_floor=related_floor,
            uncertain_margin=uncertain_margin,
        )
        decisions.append(decision)
        if decision.decision is TypedDecision.ALIAS:
            union(pair.category_a, pair.category_b)

    groups: dict[str, list[str]] = {}
    for name in memberships:
        groups.setdefault(find(name), []).append(name)

    canonical_of: dict[str, str] = {}
    for members in groups.values():
        representative = min(members, key=lambda n: (-len(memberships[n].concept_ids), n))
        for name in members:
            canonical_of[name] = representative

    return CanonicalizationResult(
        canonical_of=canonical_of,
        decisions=decisions,
        canonical_category_count=len(groups),
    )


async def apply_canonicalization_to_graph(
    session: Neo4jSession, result: CanonicalizationResult, *, tau_merge: float
) -> None:
    """Write one canonicalization result back to the graph — the real
    single-τ_merge* operation, not invoked against live infra by this
    ticket's sweep (which is read-only by design; see module docstring).

    Never touches `MembershipAssertion` (immutable evidence, D2). For every
    absorbed→canonical pair: (1) records a `CANONICALIZED_AS` edge for audit
    (the absorbed `Category` node is kept, never deleted, so `PROPOSES`
    links from its assertions stay inspectable); (2) recomputes `MEMBER_OF`
    for every concept touched by the merge, aggregating **from assertions**
    grouped by canonical category identity (walking `CANONICALIZED_AS` to
    the root) — this is what correctly handles a concept that already
    belongs to both merge-side categories: it is aggregated once from the
    union of its backing assertions, never double-counted; (3) deletes the
    now-superseded `MEMBER_OF` edges to absorbed categories.

    Args:
        session: Active Neo4j async session against the study sandbox.
        result: A `canonicalize()` result.
        tau_merge: The operating τ_merge this canonicalization was computed
            at, stamped onto each `CANONICALIZED_AS` edge for audit.
    """
    merges = [
        {"absorbed": absorbed, "canonical": canonical}
        for absorbed, canonical in result.canonical_of.items()
        if absorbed != canonical
    ]
    if not merges:
        return

    decided_at = datetime.now(timezone.utc).isoformat()

    await session.run(
        "UNWIND $merges AS merge "
        "MATCH (absorbed:Category {normalized_name: merge.absorbed}) "
        "MATCH (canonical:Category {normalized_name: merge.canonical}) "
        "MERGE (absorbed)-[r:CANONICALIZED_AS]->(canonical) "
        "SET r.tau_merge = $tau_merge, r.decided_at = $decided_at",
        {"merges": merges, "tau_merge": tau_merge, "decided_at": decided_at},
    )

    absorbed_names = [m["absorbed"] for m in merges]
    canonical_names = list({m["canonical"] for m in merges})

    await session.run(
        "MATCH (canonical:Category) WHERE canonical.normalized_name IN $canonical_names "
        "MATCH (canonical)<-[:CANONICALIZED_AS*0..]-(member:Category) "
        "MATCH (member)<-[:PROPOSES]-(a:MembershipAssertion)-[:ABOUT]->(c:Concept) "
        "MATCH (a)<-[:PRODUCED]-(:Mention)<-[:HAS_MENTION]-(ep:Episode) "
        f"WITH c, canonical, {MEMBER_OF_AGGREGATION_CLAUSE} "
        "MERGE (c)-[m:MEMBER_OF]->(canonical) "
        f"{MEMBER_OF_SET_CLAUSE}",
        {"canonical_names": canonical_names},
    )

    await session.run(
        "MATCH (c:Concept)-[m:MEMBER_OF]->(absorbed:Category) "
        "WHERE absorbed.normalized_name IN $absorbed_names "
        "DELETE m",
        {"absorbed_names": absorbed_names},
    )


async def decay_and_prune(
    session: Neo4jSession,
    *,
    reference_time: datetime,
    decay_factor: float,
    floor: float,
    stale_after: timedelta,
    apply: bool = False,
) -> DecayPruneResult:
    """ADR-0114 D5 op (3'): decay unreinforced derived edges, suppress the weak tail.

    Only the derived `MEMBER_OF` edge is ever touched — never a
    `MembershipAssertion` — so suppression is reversible: a later reinforcing
    context can resurrect the membership (D4). Dry-run by default; pass
    `apply=True` to actually mutate.

    Args:
        session: Active Neo4j async session against the study sandbox.
        reference_time: "Now", for staleness comparison — a caller-supplied
            timestamp (never wall-clock inside this function; the frozen
            corpus has no real elapsing time of its own).
        decay_factor: Multiplier applied to `membership_confidence` on stale
            edges (e.g. 0.5 halves it).
        floor: Edges at or below this confidence, after decay, are suppressed.
        stale_after: An edge is "unreinforced" when
            `reference_time - last_supported_at >= stale_after`.
        apply: If `False` (default), computes and returns the would-be
            effect without writing. If `True`, actually decays and deletes.

    Returns:
        Counts of edges considered / that would decay / that would be
        suppressed (accurate whether or not `apply` actually wrote).
    """
    result = await session.run(
        "MATCH (c:Concept)-[m:MEMBER_OF]->(cat:Category) "
        "RETURN c.id AS concept_id, cat.normalized_name AS category_normalized_name, "
        "m.membership_confidence AS membership_confidence, m.last_supported_at AS last_supported_at"
    )
    rows = [r async for r in result]

    stale_cutoff = reference_time - stale_after
    edges_considered = len(rows)
    to_decay: list[dict[str, Any]] = []
    to_suppress: list[dict[str, Any]] = []

    for row in rows:
        last_supported_raw = row["last_supported_at"]
        if last_supported_raw is None:
            continue
        last_supported_at = datetime.fromisoformat(str(last_supported_raw))
        if last_supported_at.tzinfo is None:
            last_supported_at = last_supported_at.replace(tzinfo=reference_time.tzinfo)
        if last_supported_at > stale_cutoff:
            continue  # reinforced recently — not "unreinforced"

        confidence = float(row["membership_confidence"])
        decayed_confidence = confidence * decay_factor
        entry = {
            "concept_id": row["concept_id"],
            "category_normalized_name": row["category_normalized_name"],
            "decayed_confidence": decayed_confidence,
        }
        to_decay.append(entry)
        if decayed_confidence <= floor:
            to_suppress.append(entry)

    if apply and to_decay:
        # code-review finding (FRE-842): `e not in to_suppress` was an O(len(to_decay) *
        # len(to_suppress)) dict-equality scan; the membership condition is already
        # known at append time above, so re-testing it directly is O(n) and equivalent.
        decay_only = [e for e in to_decay if e["decayed_confidence"] > floor]
        if decay_only:
            await session.run(
                "UNWIND $rows AS row "
                "MATCH (c:Concept {id: row.concept_id})-[m:MEMBER_OF]->"
                "(cat:Category {normalized_name: row.category_normalized_name}) "
                "SET m.membership_confidence = row.decayed_confidence",
                {"rows": decay_only},
            )
        if to_suppress:
            await session.run(
                "UNWIND $rows AS row "
                "MATCH (c:Concept {id: row.concept_id})-[m:MEMBER_OF]->"
                "(cat:Category {normalized_name: row.category_normalized_name}) "
                "DELETE m",
                {"rows": to_suppress},
            )

    return DecayPruneResult(
        edges_considered=edges_considered,
        would_decay_count=len(to_decay),
        would_suppress_count=len(to_suppress),
    )


def new_canonicalized_as_id() -> str:
    """A fresh id, for callers that want to tag a `CANONICALIZED_AS` write with one."""
    return str(uuid.uuid4())
