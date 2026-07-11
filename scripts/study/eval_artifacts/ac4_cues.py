"""ADR-0114 AC-4 abstract-cue gold artifact builder (FRE-841).

Builds the frozen abstract-cue set + gold neighborhoods AC-4 will later be
scored against (Recall@20/nDCG@20 of the study vs. production-multipath
baseline — FRE-843's job, not this module's).

Candidate-pool generation is deliberately **two independent sources**, not
pure embedding-cosine kNN (codex plan-review finding #1): a pool built only
from embedding similarity to the cue text would systematically exclude
exactly the category-relevant-but-embedding-distant items arm C's
categorical entry exists to surface — pre-biasing the frozen gold set
toward what production's embedding-style recall already finds, and
pre-deciding the study's own falsifiable question before it is asked.

- Source A — `build_embedding_candidates`: brute-force cosine similarity
  between the cue's embedding and every `Entity.embedding` in the frozen
  corpus.
- Source B — `build_keyword_candidates`: a short, hand-authored keyword
  list per cue, substring-matched against every `Entity.name` — independent
  of embedding distance.

The two-pass blind annotation (one annotator + a second adjudicating
disagreements, per ADR AC-4) is **not** something this module performs: it
is Claude-Code `Agent`-tool dispatches run by the build session itself
against the candidate pools this module computes, never a call this script
makes. `build_ac4_artifact` therefore takes already-annotated
`CueAnnotationResult`s — the annotation's output, not a callback into it —
and assembles the final frozen JSON, including the full audit trail
(candidate pool, both annotators' raw labels, disagreements, adjudication
rationale) so the blind process stays inspectable (codex finding #5).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any

from personal_agent.memory.embeddings import cosine_similarity
from scripts.study.neo4j_types import Neo4jDriver

_ALL_ENTITIES_WITH_EMBEDDING_QUERY = (
    "MATCH (e:Entity) WHERE e.name IS NOT NULL "
    "RETURN e.name AS name, e.entity_type AS entity_type, "
    "e._export_source_element_id AS entity_id, e.embedding AS embedding"
)


@dataclass(frozen=True)
class AbstractCue:
    """One pre-registered abstract cue.

    `keywords` seeds the keyword-match candidate source (Source B) —
    independent of the cue's own embedding, so the candidate pool doesn't
    collapse to a single retrieval signal.
    """

    cue_text: str
    domain: str
    keywords: tuple[str, ...]


# >=30 cues (35 here) spanning 7 domains confirmed present in the live
# frozen snapshot (health, software/infra engineering, history &
# archaeology, cybersecurity, cooking, music, travel — well above the >=4
# bar). Abstract phrasing only — a broad topic label, never a precise-fact
# query (AC-6's honesty guard: this ticket must not smuggle precision-cue
# phrasing into the pre-registered abstract-cue set).
ABSTRACT_CUES: tuple[AbstractCue, ...] = (
    AbstractCue(
        "health issues",
        "health",
        (
            "health",
            "medical",
            "clinical",
            "disease",
            "infection",
            "diagnos",
            "physician",
            "patient",
        ),
    ),
    AbstractCue(
        "medical diagnoses and conditions",
        "health",
        ("diagnos", "condition", "disease", "syndrome", "disorder", "symptom", "medical"),
    ),
    AbstractCue(
        "healthcare providers and specialists",
        "health",
        ("doctor", "physician", "practitioner", "specialist", "clinic", "hospital", "nurse"),
    ),
    AbstractCue(
        "respiratory problems",
        "health",
        ("respiratory", "lung", "breath", "asthma", "pneumonia", "infection", "cough"),
    ),
    AbstractCue(
        "cardiovascular health concerns",
        "health",
        ("cardiovascular", "heart", "arterial", "calcification", "hypertension", "cardiac"),
    ),
    AbstractCue(
        "database performance problems",
        "software_infra_engineering",
        ("database", "postgres", "query", "index", "performance", "latency", "sql", "neo4j"),
    ),
    AbstractCue(
        "distributed systems architecture",
        "software_infra_engineering",
        ("distributed", "microservice", "architecture", "consistency", "replication", "cluster"),
    ),
    AbstractCue(
        "API design and web services",
        "software_infra_engineering",
        ("api", "endpoint", "rest", "fastapi", "gateway", "http", "request", "response"),
    ),
    AbstractCue(
        "software testing practices",
        "software_infra_engineering",
        ("test", "pytest", "tdd", "mock", "coverage", "assertion", "regression"),
    ),
    AbstractCue(
        "infrastructure monitoring and observability",
        "software_infra_engineering",
        (
            "monitoring",
            "observability",
            "metrics",
            "telemetry",
            "dashboard",
            "kibana",
            "elasticsearch",
            "grafana",
        ),
    ),
    AbstractCue(
        "ancient Mediterranean civilizations",
        "history_archaeology",
        (
            "greek",
            "roman",
            "minoan",
            "mycenaean",
            "aegean",
            "mediterranean",
            "ancient",
            "civilization",
        ),
    ),
    AbstractCue(
        "archaeological sites and artifacts",
        "history_archaeology",
        ("archaeolog", "excavation", "artifact", "ruins", "dig", "museum"),
    ),
    AbstractCue(
        "prehistoric human migration",
        "history_archaeology",
        (
            "migration",
            "prehistoric",
            "yamnaya",
            "anatolian",
            "farmer",
            "neolithic",
            "paleolithic",
            "ancestry",
        ),
    ),
    AbstractCue(
        "ancient religious practices and mythology",
        "history_archaeology",
        ("myth", "religion", "god", "goddess", "ritual", "temple", "deity", "sacred"),
    ),
    AbstractCue(
        "classical art and pottery",
        "history_archaeology",
        ("pottery", "fresco", "sculpture", "ceramic", "vase", "painting"),
    ),
    AbstractCue(
        "cryptography and encryption",
        "cybersecurity",
        ("crypto", "cipher", "encrypt", "hash", "tls", "ssl"),
    ),
    AbstractCue(
        "security certifications and training",
        "cybersecurity",
        ("ceh", "certif", "training", "exam", "comptia"),
    ),
    AbstractCue(
        "network security threats",
        "cybersecurity",
        ("threat", "attack", "vulnerabilit", "malware", "breach", "intrusion"),
    ),
    AbstractCue(
        "authentication and access control",
        "cybersecurity",
        ("auth", "token", "credential", "access", "permission", "role", "login"),
    ),
    AbstractCue(
        "penetration testing techniques",
        "cybersecurity",
        ("pentest", "penetration", "exploit", "recon", "vulnerabilit", "red team"),
    ),
    AbstractCue(
        "cooking techniques and recipes",
        "cooking",
        ("cook", "recipe", "bake", "roast", "saute", "boil", "grill"),
    ),
    AbstractCue(
        "seafood dishes",
        "cooking",
        ("seafood", "fish", "shrimp", "frutti di mare", "shellfish", "calamari"),
    ),
    AbstractCue(
        "vegetable preparation",
        "cooking",
        ("vegetable", "onion", "bean", "chop", "dice", "produce"),
    ),
    AbstractCue(
        "regional cuisines",
        "cooking",
        ("cuisine", "regional", "italian", "greek", "mediterranean", "dish"),
    ),
    AbstractCue(
        "food ingredients and flavor",
        "cooking",
        ("ingredient", "flavor", "spice", "seasoning", "taste"),
    ),
    AbstractCue(
        "musical composition and theory",
        "music",
        ("composition", "theory", "harmony", "counterpoint", "chord", "melody"),
    ),
    AbstractCue(
        "historical musical styles",
        "music",
        ("baroque", "polyphony", "renaissance", "classical", "medieval"),
    ),
    AbstractCue(
        "musical instruments",
        "music",
        ("instrument", "violin", "piano", "voice", "organ", "strings"),
    ),
    AbstractCue(
        "vocal and choral music",
        "music",
        ("vocal", "choral", "choir", "singing", "chant", "polyphony"),
    ),
    AbstractCue(
        "music performance practice",
        "music",
        ("performance", "concert", "recital", "ensemble", "orchestra"),
    ),
    AbstractCue(
        "travel destinations and itineraries",
        "travel",
        ("travel", "destination", "itinerary", "trip", "tour", "vacation"),
    ),
    AbstractCue(
        "historic landmarks and monuments",
        "travel",
        ("landmark", "monument", "basilica", "palace", "cathedral", "historic"),
    ),
    AbstractCue(
        "museums and cultural sites",
        "travel",
        ("museum", "gallery", "exhibit", "cultural", "heritage"),
    ),
    AbstractCue(
        "local transportation",
        "travel",
        ("transport", "train", "bus", "taxi", "metro", "transit"),
    ),
    AbstractCue(
        "accommodation and lodging",
        "travel",
        ("hotel", "lodging", "accommodation", "stay", "hostel"),
    ),
)


@dataclass(frozen=True)
class EntityWithOptionalEmbedding:
    """One `Entity` row from the frozen corpus; `embedding` is `None` when
    the corpus entity has no stored embedding.
    """

    name: str
    entity_type: str
    entity_id: str
    embedding: list[float] | None = None


@dataclass(frozen=True)
class CandidateEntity:
    """One candidate in a cue's pool, tagged with which source(s) found it."""

    entity_id: str
    name: str
    kind: str
    pool_source: str  # "embedding" | "keyword" | "both"

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "kind": self.kind,
            "pool_source": self.pool_source,
        }


@dataclass(frozen=True)
class CueAnnotationResult:
    """One cue's fully-annotated result — the input to `build_ac4_artifact`.

    Produced outside this module: the candidate pool comes from
    `build_candidate_pool_for_cue`, the two label dicts come from the two
    independent blind `Agent`-tool annotator dispatches, and `adjudications`
    from the build session's own resolution of any disagreement between
    them.
    """

    cue: AbstractCue
    candidate_pool: list[CandidateEntity]
    annotator_1_labels: dict[str, str]  # entity_id -> "gold" | "distractor"
    annotator_2_labels: dict[str, str]
    adjudications: dict[str, str]  # entity_id -> adjudication rationale (disagreements only)
    gold_neighborhood: tuple[str, ...]  # final entity_ids
    distractors: tuple[str, ...]  # final entity_ids


async def fetch_entities_for_candidate_pool(
    driver: Neo4jDriver,
) -> list[EntityWithOptionalEmbedding]:
    """Fetch every named `Entity` (embedding included where present).

    The only Neo4j-touching function in this module — candidate-pool
    generation/merging is pure Python over the returned list.
    """
    async with driver.session() as session:
        result = await session.run(_ALL_ENTITIES_WITH_EMBEDDING_QUERY)
        return [
            EntityWithOptionalEmbedding(
                name=str(r["name"]),
                entity_type=str(r["entity_type"]),
                entity_id=str(r["entity_id"]),
                embedding=list(r["embedding"]) if r["embedding"] is not None else None,
            )
            async for r in result
        ]


def build_embedding_candidates(
    cue_embedding: list[float], entities: list[EntityWithOptionalEmbedding], top_k: int = 25
) -> list[CandidateEntity]:
    """Source A: top-K entities by cosine similarity to *cue_embedding*."""
    scored = [
        (cosine_similarity(cue_embedding, e.embedding), e)
        for e in entities
        if e.embedding is not None
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        CandidateEntity(
            entity_id=e.entity_id, name=e.name, kind=e.entity_type, pool_source="embedding"
        )
        for _, e in scored[:top_k]
    ]


def build_keyword_candidates(
    cue: AbstractCue, entities: list[EntityWithOptionalEmbedding], max_candidates: int = 20
) -> list[CandidateEntity]:
    """Source B: entities whose name contains any of *cue*'s keywords."""
    keywords_lower = [k.lower() for k in cue.keywords]
    matches = [e for e in entities if any(keyword in e.name.lower() for keyword in keywords_lower)]
    matches.sort(key=lambda e: e.name)
    return [
        CandidateEntity(
            entity_id=e.entity_id, name=e.name, kind=e.entity_type, pool_source="keyword"
        )
        for e in matches[:max_candidates]
    ]


def merge_candidate_pools(
    embedding_candidates: list[CandidateEntity], keyword_candidates: list[CandidateEntity]
) -> list[CandidateEntity]:
    """Merge the two sources, deduped by `entity_id`; overlap is tagged "both"."""
    by_id: dict[str, CandidateEntity] = {}
    for candidate in embedding_candidates:
        by_id[candidate.entity_id] = candidate
    for candidate in keyword_candidates:
        existing = by_id.get(candidate.entity_id)
        if existing is None:
            by_id[candidate.entity_id] = candidate
        elif existing.pool_source != candidate.pool_source:
            by_id[candidate.entity_id] = CandidateEntity(
                entity_id=candidate.entity_id,
                name=candidate.name,
                kind=candidate.kind,
                pool_source="both",
            )
    return sorted(by_id.values(), key=lambda c: c.entity_id)


def build_candidate_pool_for_cue(
    cue: AbstractCue,
    cue_embedding: list[float],
    entities: list[EntityWithOptionalEmbedding],
    *,
    embedding_top_k: int = 25,
    keyword_max_candidates: int = 20,
) -> list[CandidateEntity]:
    """The full two-source candidate pool for one cue."""
    embedding_candidates = build_embedding_candidates(
        cue_embedding, entities, top_k=embedding_top_k
    )
    keyword_candidates = build_keyword_candidates(
        cue, entities, max_candidates=keyword_max_candidates
    )
    return merge_candidate_pools(embedding_candidates, keyword_candidates)


def compute_disagreements(labels_1: dict[str, str], labels_2: dict[str, str]) -> list[str]:
    """`entity_id`s where the two annotators disagree (sorted).

    Covers both a genuine label mismatch (both annotators judged the item,
    disagreeing on gold vs. distractor) and a coverage gap (one annotator
    omitted the item entirely while the other judged it) — the latter is
    also a real disagreement for the adjudicator to resolve, not something
    to silently drop (code-review finding, FRE-841: iterating only
    `labels_1`'s keys missed any `entity_id` present only in `labels_2`).
    """
    all_ids = labels_1.keys() | labels_2.keys()
    return sorted(
        entity_id for entity_id in all_ids if labels_1.get(entity_id) != labels_2.get(entity_id)
    )


_SCORING_NOTE = (
    "gold_neighborhood/distractors are Entity._export_source_element_id values (the frozen "
    "corpus's stable id). Scoring arm A (production multipath) against this gold set is a "
    "direct id comparison. Scoring arm C (the study's categorical-entry recall, which returns "
    "Concept nodes) requires first mapping each returned Concept back to its backing Entity "
    "id(s) via the Surface/ALIAS_OF chain established at ingest — comparing Concept.id directly "
    "against these entity_ids would silently fail to credit arm C for correct recalls."
)


def build_ac4_artifact(
    results: list[CueAnnotationResult], *, source_manifest_hash: str | None
) -> dict[str, Any]:
    """Build the (unstamped) AC-4 artifact payload from fully-annotated results.

    Args:
        results: One `CueAnnotationResult` per cue, already annotated by the
            two blind passes + adjudication (this module never performs
            that annotation itself).
        source_manifest_hash: The frozen corpus manifest's `content_hash`.

    Returns:
        `{cues, source_manifest_hash, annotation_method, scoring_note}` —
        pass to `freeze.freeze_json_artifact` to stamp and commit.
    """
    cues_json = []
    for result in results:
        disagreements = compute_disagreements(result.annotator_1_labels, result.annotator_2_labels)
        cues_json.append(
            {
                "cue_text": result.cue.cue_text,
                "domain": result.cue.domain,
                "keywords": list(result.cue.keywords),
                "candidate_pool": [c.to_json_dict() for c in result.candidate_pool],
                "annotator_1_labels": result.annotator_1_labels,
                "annotator_2_labels": result.annotator_2_labels,
                "disagreements": disagreements,
                "adjudications": result.adjudications,
                "gold_neighborhood": list(result.gold_neighborhood),
                "distractors": list(result.distractors),
            }
        )

    return {
        "cues": cues_json,
        "source_manifest_hash": source_manifest_hash,
        "annotation_method": (
            "Two independent Claude-Code Agent-tool dispatches (blind to each other and to any "
            "recall system's output — candidates are pre-computed by neutral embedding+keyword "
            "sources, never run through arm A or arm C), disagreements adjudicated by the build "
            "session with a recorded rationale."
        ),
        "scoring_note": _SCORING_NOTE,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Write the candidate-pool dump. Without this, prints counts only.",
    )
    parser.add_argument(
        "--out",
        default="scripts/study/eval_artifacts/frozen/ac4_candidate_pools.json",
        help="Destination path for the (intermediate, pre-annotation) candidate-pool dump.",
    )
    return parser.parse_args()


async def _dump_candidate_pools() -> dict[str, Any]:
    """Compute every cue's candidate pool against the live sandbox.

    This is the intermediate step: its output feeds the build session's own
    `Agent`-tool annotation dispatches, which then feed `build_ac4_artifact`
    directly (not through this function).
    """
    import asyncio

    from personal_agent.memory.embeddings import generate_embedding
    from scripts.study.config import StudySettings

    settings = StudySettings()

    from neo4j import AsyncGraphDatabase

    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    try:
        entities = await fetch_entities_for_candidate_pool(driver)
    finally:
        await driver.close()

    # The 35 cue embeddings are independent of each other (code-review finding,
    # FRE-841: awaiting them one at a time in a loop pays 35x the embedder
    # round-trip latency serially every time this is re-run).
    cue_embeddings = await asyncio.gather(
        *(generate_embedding(cue.cue_text, mode="query") for cue in ABSTRACT_CUES)
    )

    pools = {}
    for cue, cue_embedding in zip(ABSTRACT_CUES, cue_embeddings, strict=True):
        pool = build_candidate_pool_for_cue(cue, cue_embedding, entities)
        pools[cue.cue_text] = {
            "domain": cue.domain,
            "keywords": list(cue.keywords),
            "candidates": [c.to_json_dict() for c in pool],
        }
    return pools


async def _amain() -> None:
    import json
    from pathlib import Path

    args = _parse_args()
    pools = await _dump_candidate_pools()

    if not args.execute:
        total = sum(len(v["candidates"]) for v in pools.values())
        print(f"Dry run: {len(pools)} cues, {total} total candidates. Pass --execute to write.")
        return

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(pools, indent=2, sort_keys=True))
    print(f"Wrote {len(pools)} cue candidate pools to {out_path}")


def main() -> None:
    """CLI entrypoint."""
    import asyncio

    asyncio.run(_amain())


if __name__ == "__main__":
    main()
