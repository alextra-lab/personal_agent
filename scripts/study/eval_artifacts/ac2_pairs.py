"""ADR-0114 AC-2 hard-negative pair artifact builder (FRE-841).

Builds the frozen V+ / V- pair set AC-2 will later be scored against
(pairwise precision/recall over V+ union V- — FRE-843's job, not this
module's). This module only constructs the pairs; it never calls
`resolve_concept_hub`/`writer.py` or otherwise scores anything.

V+ (case-/near-variant surface pairs that SHOULD resolve to one hub) is
mined directly from the frozen `Entity` corpus — real data, not a hand-built
list; a `toLower(trim(name))` grouping alone finds 542 real case-variant
groups in the live sandbox, including the ADR's own named bug
(`Arterial calcification`/`Arterial Calcification`). A second, looser
grouping (punctuation/hyphen-stripped) catches ADR AC-2's "near-variant"
language beyond plain case-folding, additively (a pair already found by the
case-fold pass is never re-emitted under the near-variant provenance).

V- (homonym/polyseme pairs that must NOT merge) cannot be mined from this
corpus: a live check of known homonym-prone surface forms (python, apple,
mercury, turkey, amazon, mars, ...) found every one maps to exactly ONE
sense in the real data today. `writer.py`'s module docstring documents this
exact gap ("two byte-identical strings referring to genuinely different
things... left to FRE-841/843's fuller hard-negative test") — V- is
therefore a seeded/injected adversarial set, the same posture as the ADR's
own named illustrative examples. Each seeded pair is resolved against the
live corpus so a side that happens to be corpus-attested carries its real
`entity_id`/`kind`; a side with no corpus match is marked synthetic, with a
`scoring_note` telling FRE-843 how to instantiate it.
"""

from __future__ import annotations

import argparse
import itertools
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from scripts.study.neo4j_types import Neo4jDriver

_ALL_ENTITIES_QUERY = (
    "MATCH (e:Entity) WHERE e.name IS NOT NULL "
    "RETURN e.name AS name, e.entity_type AS entity_type, "
    "e._export_source_element_id AS entity_id"
)

_COSMETIC_PUNCTUATION_RE = re.compile(r"[-_'().:|\s]+")


def _normalize_case(name: str) -> str:
    """Case-fold + trim — the strict "case variant" normalizer."""
    return name.strip().lower()


def _normalize_near_variant(name: str) -> str:
    """Case-fold + strip low-information *cosmetic* punctuation only.

    Looser than `_normalize_case` — groups names that differ by
    hyphenation/underscoring/parenthetical spacing/colon-vs-period as well
    as case (ADR AC-2's "near-variant" language). Deliberately does **not**
    strip `+`, `*`, `/`, `&` — a manual audit of the first live run over the
    real corpus found those characters are load-bearing in this corpus's
    naming conventions (`Security` vs `Security+` is a general topic vs a
    specific certification, not a formatting variant; `Agent` vs `agent-*`
    /`Logs` vs `logs-*` are a concept vs an index-glob pattern). Stripping
    them created false-positive V+ pairs — entries asserting two genuinely
    different referents "should" resolve to one hub, corrupting AC-2's own
    ground truth. This is intentionally conservative (favors V+ precision
    over recall, matching AC-2's own "lowercasing every label would tank
    precision" framing) — a few genuine near-variants (e.g. a bare path vs.
    the same path with a trailing slash) are lost rather than risk another
    false positive.
    """
    return _COSMETIC_PUNCTUATION_RE.sub("", name.lower())


@dataclass(frozen=True)
class EntityRecord:
    """One `Entity` row from the frozen corpus."""

    name: str
    entity_type: str
    entity_id: str


@dataclass(frozen=True)
class CaseVariantGroup:
    """Every distinct raw name in the corpus sharing one normalized form."""

    normalized_name: str
    members: tuple[EntityRecord, ...]
    provenance: str


@dataclass(frozen=True)
class PositivePair:
    """One V+ pair: two surface forms that should resolve to one hub."""

    name_a: str
    kind_a: str
    entity_id_a: str
    name_b: str
    kind_b: str
    entity_id_b: str
    provenance: str

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "name_a": self.name_a,
            "kind_a": self.kind_a,
            "entity_id_a": self.entity_id_a,
            "name_b": self.name_b,
            "kind_b": self.kind_b,
            "entity_id_b": self.entity_id_b,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class SeededPairSpec:
    """One hand-authored V- candidate before resolution against the corpus."""

    surface_a: str
    sense_a: str
    kind_hint_a: str
    surface_b: str
    sense_b: str
    kind_hint_b: str


@dataclass(frozen=True)
class NegativePair:
    """One V- pair: two surface forms that must NOT resolve to one hub."""

    surface_a: str
    kind_a: str
    sense_a: str
    entity_id_a: str | None
    surface_b: str
    kind_b: str
    sense_b: str
    entity_id_b: str | None
    provenance: str
    scoring_note: str

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "surface_a": self.surface_a,
            "kind_a": self.kind_a,
            "sense_a": self.sense_a,
            "entity_id_a": self.entity_id_a,
            "surface_b": self.surface_b,
            "kind_b": self.kind_b,
            "sense_b": self.sense_b,
            "entity_id_b": self.entity_id_b,
            "provenance": self.provenance,
            "scoring_note": self.scoring_note,
        }


# The ADR's 2 named illustrative pairs (Python/python, Apple/apple; Mercury
# named as planet/element/software) plus additional pairs spanning this
# corpus's real domains (software, travel, cybersecurity, cooking, music) —
# "the two named trigger pairs are spot-checks, not the bar" (ADR AC-2), so
# this set deliberately goes well beyond 2 for a meaningfully-sized pairwise
# precision/recall test. Kinds are a pragmatic best fit against the 10
# ADR-0109 types — several of these senses (an animal, a fish, a bird) have
# no clean natural-kind slot in a taxonomy built for a "liberal-arts
# collaborator" KG, not a biology KG; `DomainOrTopic` is used as the
# least-wrong stand-in for "a common noun treated as a subject", matching
# how this corpus already tags e.g. `Apple`/`Mercury` today.
SEEDED_HARD_NEGATIVE_PAIRS: tuple[SeededPairSpec, ...] = (
    SeededPairSpec(
        surface_a="Python",
        sense_a="the programming language",
        kind_hint_a="TechnicalArtifact",
        surface_b="python",
        sense_b="the snake genus",
        kind_hint_b="DomainOrTopic",
    ),
    SeededPairSpec(
        surface_a="Apple",
        sense_a="the technology company",
        kind_hint_a="DomainOrTopic",
        surface_b="apple",
        sense_b="the fruit",
        kind_hint_b="DomainOrTopic",
    ),
    SeededPairSpec(
        surface_a="Mercury",
        sense_a="the planet",
        kind_hint_a="DomainOrTopic",
        surface_b="Mercury",
        sense_b="a mail-client/software product of the same name",
        kind_hint_b="TechnicalArtifact",
    ),
    SeededPairSpec(
        surface_a="Turkey",
        sense_a="the country",
        kind_hint_a="Location",
        surface_b="turkey",
        sense_b="the bird/food",
        kind_hint_b="DomainOrTopic",
    ),
    SeededPairSpec(
        surface_a="Amazon",
        sense_a="the technology/retail company",
        kind_hint_a="Organization",
        surface_b="Amazon",
        sense_b="the river/rainforest",
        kind_hint_b="Location",
    ),
    SeededPairSpec(
        surface_a="Mars",
        sense_a="the planet",
        kind_hint_a="Location",
        surface_b="Mars",
        sense_b="the confectionery company",
        kind_hint_b="Organization",
    ),
    SeededPairSpec(
        surface_a="Java",
        sense_a="the programming language",
        kind_hint_a="TechnicalArtifact",
        surface_b="Java",
        sense_b="the Indonesian island",
        kind_hint_b="Location",
    ),
    SeededPairSpec(
        surface_a="Saturn",
        sense_a="the planet",
        kind_hint_a="DomainOrTopic",
        surface_b="Saturn",
        sense_b="the defunct car brand",
        kind_hint_b="Organization",
    ),
    SeededPairSpec(
        surface_a="Venus",
        sense_a="the planet",
        kind_hint_a="DomainOrTopic",
        surface_b="Venus",
        sense_b="the Roman goddess",
        kind_hint_b="Person",
    ),
    SeededPairSpec(
        surface_a="Bass",
        sense_a="the musical register/instrument",
        kind_hint_a="MethodOrConcept",
        surface_b="bass",
        sense_b="the fish",
        kind_hint_b="DomainOrTopic",
    ),
    SeededPairSpec(
        surface_a="Crane",
        sense_a="the construction machine",
        kind_hint_a="TechnicalArtifact",
        surface_b="crane",
        sense_b="the bird",
        kind_hint_b="DomainOrTopic",
    ),
    SeededPairSpec(
        surface_a="Match",
        sense_a="an entity-resolution/string match (a tech-domain sense)",
        kind_hint_a="MethodOrConcept",
        surface_b="Match",
        sense_b="a fire-starting matchstick",
        kind_hint_b="TechnicalArtifact",
    ),
)


async def fetch_all_entities(driver: Neo4jDriver) -> list[EntityRecord]:
    """Fetch every named `Entity` in the frozen corpus.

    The only Neo4j-touching function in this module — grouping/pairing is
    pure Python over the returned list, so it stays unit-testable without a
    fake driver.
    """
    async with driver.session() as session:
        result = await session.run(_ALL_ENTITIES_QUERY)
        return [
            EntityRecord(
                name=str(r["name"]),
                entity_type=str(r["entity_type"]),
                entity_id=str(r["entity_id"]),
            )
            async for r in result
        ]


def group_by_normalizer(
    entities: list[EntityRecord], normalizer: Callable[[str], str], provenance: str
) -> list[CaseVariantGroup]:
    """Group entities by *normalizer*(name); keep only groups with >=2 distinct raw names.

    First-seen entity wins per distinct raw name (deterministic given the
    input order) — two `Entity` nodes sharing the exact same raw name
    collapse to one group member, since a group needs >=2 *distinct* names,
    not merely >=2 nodes.
    """
    buckets: dict[str, dict[str, EntityRecord]] = {}
    for entity in entities:
        normalized = normalizer(entity.name)
        bucket = buckets.setdefault(normalized, {})
        bucket.setdefault(entity.name, entity)

    return [
        CaseVariantGroup(
            normalized_name=normalized,
            members=tuple(sorted(by_name.values(), key=lambda e: e.name)),
            provenance=provenance,
        )
        for normalized, by_name in buckets.items()
        if len(by_name) > 1
    ]


def expand_to_pairs(groups: list[CaseVariantGroup]) -> list[PositivePair]:
    """Every group -> all pairwise combinations of its members."""
    return [
        PositivePair(
            name_a=a.name,
            kind_a=a.entity_type,
            entity_id_a=a.entity_id,
            name_b=b.name,
            kind_b=b.entity_type,
            entity_id_b=b.entity_id,
            provenance=group.provenance,
        )
        for group in groups
        for a, b in itertools.combinations(group.members, 2)
    ]


def build_positive_pairs(entities: list[EntityRecord]) -> list[PositivePair]:
    """The full V+ set: case-variant pairs, plus near-variant pairs the
    case-fold pass couldn't already find (deduped by entity-id pair, codex
    plan-review finding #3 — near-variant is additive, not duplicative).
    """
    case_pairs = expand_to_pairs(
        group_by_normalizer(entities, _normalize_case, "corpus_case_variant")
    )
    seen_id_pairs = {frozenset((p.entity_id_a, p.entity_id_b)) for p in case_pairs}

    near_pairs = expand_to_pairs(
        group_by_normalizer(entities, _normalize_near_variant, "corpus_near_variant")
    )
    additive_near_pairs = [
        p for p in near_pairs if frozenset((p.entity_id_a, p.entity_id_b)) not in seen_id_pairs
    ]

    return case_pairs + additive_near_pairs


def resolve_seeded_pair(spec: SeededPairSpec, entities: list[EntityRecord]) -> NegativePair:
    """Resolve one seeded V- spec against the live corpus.

    A side whose exact `surface` matches a real `Entity.name` is marked
    corpus-attested (real `entity_id`/`kind`); otherwise it is synthetic
    (`entity_id=None`, `kind` falls back to the hand-authored hint).

    Byte-identical same-case pairs (`surface_a == surface_b` — the hardest
    documented gap, e.g. `Mercury`/`Mercury` as planet vs. software) get a
    distinct `corpus_attested_same_surface_ambiguous` provenance rather than
    "both sides attested": a name-based lookup necessarily returns the
    *same* single real node for both sides when the surface string is
    identical, so `entity_id_a == entity_id_b` here reflects a real
    limitation, not two distinct corpus entities. FRE-843 cannot score this
    case by simply comparing `entity_id_a`/`entity_id_b` for equality (they
    will trivially be equal); the `scoring_note` says so explicitly.
    """
    by_name = {e.name: e for e in entities}
    match_a = by_name.get(spec.surface_a)
    match_b = by_name.get(spec.surface_b)
    same_surface = spec.surface_a == spec.surface_b

    if same_surface and match_a:
        provenance = "corpus_attested_same_surface_ambiguous"
    elif match_a and match_b:
        provenance = "corpus_attested_both_sides"
    elif match_a or match_b:
        provenance = "corpus_attested_one_side"
    else:
        provenance = "fully_synthetic"

    kind_a = match_a.entity_type if match_a else spec.kind_hint_a
    # A byte-identical same_surface pair means `by_name.get(spec.surface_b)` resolves to the
    # SAME corpus node as match_a (there is only one node for that surface string) — the corpus
    # only ever attests sense_a there, never sense_b. Using match_b.entity_type in that case
    # would silently overwrite the intended second-sense kind_hint_b with sense_a's real kind,
    # erasing the deliberate kind mismatch these adversarial pairs test (code-review finding,
    # FRE-841: this happened for real in the committed artifact's Mercury/Amazon/Mars pairs).
    kind_b = (
        spec.kind_hint_b if same_surface else (match_b.entity_type if match_b else spec.kind_hint_b)
    )

    if same_surface and match_a is not None:
        scoring_note = (
            f"'{spec.surface_a}' is a single real corpus entity "
            f"(entity_id={match_a.entity_id}) whose surface string is claimed to carry TWO "
            f"senses here — '{spec.sense_a}' vs '{spec.sense_b}'. A name-based lookup cannot "
            "tell these apart (that IS the gap this pair tests — writer.py's documented "
            "'byte-identical same-case homonyms' limitation). FRE-843 must NOT score this by "
            "comparing entity_id_a to entity_id_b (they are identical by construction); it "
            "needs a different fixture — e.g. two separate ingest episodes independently "
            "asserting each sense in a distinct conversational context, then checking whether "
            "the system ever conflates or successfully splits them."
        )
    else:
        scoring_note = (
            f"'{spec.surface_a}' ({spec.sense_a}) must NOT resolve to the same hub as "
            f"'{spec.surface_b}' ({spec.sense_b}). "
            + (
                f"'{spec.surface_a}' is a real corpus entity (entity_id={match_a.entity_id})."
                if match_a
                else f"'{spec.surface_a}' has no corpus entity — instantiate a synthetic Concept "
                f"with kind={kind_a}, canonical_name={spec.surface_a!r} for scoring."
            )
            + " "
            + (
                f"'{spec.surface_b}' is a real corpus entity (entity_id={match_b.entity_id})."
                if match_b
                else f"'{spec.surface_b}' has no corpus entity — instantiate a synthetic Concept "
                f"with kind={kind_b}, canonical_name={spec.surface_b!r} for scoring."
            )
        )

    return NegativePair(
        surface_a=spec.surface_a,
        kind_a=kind_a,
        sense_a=spec.sense_a,
        entity_id_a=match_a.entity_id if match_a else None,
        surface_b=spec.surface_b,
        kind_b=kind_b,
        sense_b=spec.sense_b,
        entity_id_b=match_b.entity_id if match_b else None,
        provenance=provenance,
        scoring_note=scoring_note,
    )


def build_negative_pairs(entities: list[EntityRecord]) -> list[NegativePair]:
    """Resolve every `SEEDED_HARD_NEGATIVE_PAIRS` spec against the corpus."""
    return [resolve_seeded_pair(spec, entities) for spec in SEEDED_HARD_NEGATIVE_PAIRS]


async def build_ac2_artifact(
    driver: Neo4jDriver, *, source_manifest_hash: str | None
) -> dict[str, Any]:
    """Build the (unstamped) AC-2 artifact payload.

    Args:
        driver: Connected async Neo4j driver pointed at the study sandbox.
        source_manifest_hash: The frozen corpus manifest's `content_hash`
            (`scripts/study/snapshots/snapshot_manifest.json`), so this
            artifact is traceable to the exact corpus it was mined from.

    Returns:
        `{positive_pairs, negative_pairs, source_manifest_hash}` — pass to
        `freeze.freeze_json_artifact` to stamp and commit.
    """
    entities = await fetch_all_entities(driver)
    positive_pairs = build_positive_pairs(entities)
    negative_pairs = build_negative_pairs(entities)

    return {
        "positive_pairs": [p.to_json_dict() for p in positive_pairs],
        "negative_pairs": [p.to_json_dict() for p in negative_pairs],
        "source_manifest_hash": source_manifest_hash,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Write the frozen artifact. Without this, prints counts only.",
    )
    parser.add_argument(
        "--out",
        default="scripts/study/eval_artifacts/frozen/ac2_hard_negative_pairs.json",
        help="Destination path for the frozen artifact.",
    )
    return parser.parse_args()


async def _amain() -> None:
    import json
    from datetime import datetime, timezone
    from pathlib import Path

    from neo4j import AsyncGraphDatabase

    from scripts.study.config import StudySettings
    from scripts.study.eval_artifacts.freeze import freeze_json_artifact

    args = _parse_args()
    settings = StudySettings()
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    try:
        manifest_path = Path("scripts/study/snapshots/snapshot_manifest.json")
        source_manifest_hash = None
        if manifest_path.exists():
            source_manifest_hash = json.loads(manifest_path.read_text())["content_hash"]

        payload = await build_ac2_artifact(driver, source_manifest_hash=source_manifest_hash)
    finally:
        await driver.close()

    if not args.execute:
        print(
            f"Dry run: {len(payload['positive_pairs'])} positive pairs, "
            f"{len(payload['negative_pairs'])} negative pairs. Pass --execute to write."
        )
        return

    result = freeze_json_artifact(payload, Path(args.out), generated_at=datetime.now(timezone.utc))
    print(json.dumps(result, indent=2, sort_keys=True))


def main() -> None:
    """CLI entrypoint."""
    import asyncio

    asyncio.run(_amain())


if __name__ == "__main__":
    main()
