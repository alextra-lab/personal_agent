"""FRE-630 — gold-set schema + YAML loader for the extraction-quality benchmark.

A gold set is a list of :class:`GoldCase`. Each case pairs a *source turn* (the
``user`` / ``assistant`` text the extractor runs on) with the *expected* extraction —
entities (canonical name + accepted aliases + type + knowledge class), typed-edge
relationships, structured stances/claims — plus three *negative* label families that
encode the ticket's named failure modes:

* ``forbid_entities`` — hallucination traps that MUST NOT be extracted (role labels,
  tool names, a misspelled relationship type leaking in as an entity).
* ``forbid_rel_types`` — off-vocabulary edge types that MUST NOT appear (e.g.
  ``LIVES_IN`` asserted for a mere *visit*).
* ``dedup_variants`` — case/spelling variant pairs that MUST collapse to one canonical.

The schema is versioned (:data:`GOLD_SCHEMA_VERSION`) and the version is stamped into
every run report so a scored run is never silently compared across schema revisions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

#: Bumped whenever the gold-case shape changes in a way that affects scoring.
#: 1.1 (FRE-770): added the optional `v2_type` (+ adjudication metadata) fields
#: — additive only, the scored V1 `type`/`ALLOWED_ENTITY_TYPES` are unchanged.
#: 1.2 (FRE-773): added the optional `v2_rel_type` (+ adjudication metadata) fields
#: on relationships — additive only, the scored V1 `rel_type`/`ALLOWED_REL_TYPES`
#: are unchanged.
GOLD_SCHEMA_VERSION = "1.2"

#: The extractor's controlled relationship vocabulary (entity_extraction.py). An
#: extracted edge type outside this set is off-vocabulary regardless of the case.
ALLOWED_REL_TYPES = frozenset(
    {"PART_OF", "USES", "RELATED_TO", "SIMILAR_TO", "CREATED_BY", "LOCATED_IN"}
)

#: ADR-0109 V2 relationship vocabulary (FRE-773). Same 6 keys as V1 — the V2 change
#: is the *definitions* (directional, GoLLIE inclusion/exclusion) and the gating of
#: RELATED_TO as a last-resort None-of-the-Above fallback, not the key set. NOT yet
#: the scored vocab — the live extractor/harness still speak V1 (`ALLOWED_REL_TYPES`)
#: until a future prompt swap. This set only validates the new `v2_rel_type` field.
ALLOWED_REL_TYPES_V2 = frozenset(
    {"PART_OF", "USES", "RELATED_TO", "SIMILAR_TO", "CREATED_BY", "LOCATED_IN"}
)

#: The V2 gated last-resort relationship — emitted only when a clear association
#: exists but no specific type applies (never when a specific type fits).
REL_TYPE_NOTA = "RELATED_TO"

#: FRE-773 — a machine-checkable marker meaning "the V2 vocab says NO edge should
#: exist between this pair" (the ADR-0109 emit-nothing-if-none-fits rule). It is
#: deliberately NOT a member of `ALLOWED_REL_TYPES_V2`, so a rater-converged
#: "no edge" outcome can never be silently read as a real relationship type. A
#: relationship carrying this value always co-carries `v2_needs_owner_signoff`
#: (the V1 edge is retained; pruning the gold is out of scope for FRE-773).
REL_V2_NO_EDGE = "NONE"

#: The 7 entity types and 3 knowledge classes the extractor is allowed to emit.
ALLOWED_ENTITY_TYPES = frozenset(
    {"Person", "Organization", "Location", "Technology", "Concept", "Event", "Topic"}
)
ALLOWED_ENTITY_CLASSES = frozenset({"World", "Personal", "System"})

#: ADR-0109 V2 entity-type vocabulary (FRE-770). NOT yet the scored vocab — the
#: live extractor/harness still speak V1 (`ALLOWED_ENTITY_TYPES`) until FRE-771
#: swaps the prompt. This set only validates the new `v2_type` field.
ALLOWED_ENTITY_TYPES_V2 = frozenset(
    {
        "Person",
        "Organization",
        "Location",
        "TechnicalArtifact",
        "MethodOrConcept",
        "DomainOrTopic",
        "Phenomenon",
        "Event",
    }
)


class GoldSetError(ValueError):
    """Raised when a gold set is malformed or degenerate.

    A *degenerate* case carries no positive label at all (no expected entities,
    relationships, stances, or claims): it can make precision/recall pass
    vacuously, so loading it is rejected.
    """


@dataclass(frozen=True)
class GoldEntity:
    """One expected entity.

    Attributes:
        name: Canonical entity name (the scoring label).
        entity_type: One of :data:`ALLOWED_ENTITY_TYPES` (V1, scored today).
        knowledge_class: One of :data:`ALLOWED_ENTITY_CLASSES`.
        aliases: Accepted alternative surface forms (abbreviations, reorderings,
            known synonyms) that the tiered matcher will treat as an exact hit.
        v2_type: ADR-0109 V2 label (FRE-770), one of :data:`ALLOWED_ENTITY_TYPES_V2`
            when set. Not yet scored — informational until FRE-771 promotes it.
        v2_adjudicated: Whether ``v2_type`` required builder adjudication (a
            rater majority or 3-way split), rather than unanimous agreement.
        v2_adjudication_rationale: One-line reasoning for an adjudicated
            ``v2_type``, empty when unanimous.
        v2_needs_owner_signoff: Set when a 3-way rater split was adjudicated by
            the builder but still needs owner confirmation post-hoc.
    """

    name: str
    entity_type: str
    knowledge_class: str
    aliases: tuple[str, ...] = ()
    v2_type: str = ""
    v2_adjudicated: bool = False
    v2_adjudication_rationale: str = ""
    v2_needs_owner_signoff: bool = False


@dataclass(frozen=True)
class GoldRelationship:
    """One expected typed-edge triple over *gold canonical* entity names.

    Attributes:
        source: Source gold entity canonical name.
        rel_type: One of :data:`ALLOWED_REL_TYPES` (V1, scored today).
        target: Target gold entity canonical name.
        v2_rel_type: ADR-0109 V2 label (FRE-773), one of
            :data:`ALLOWED_REL_TYPES_V2` — or :data:`REL_V2_NO_EDGE` when the V2
            vocab says no edge should exist between this pair — when set. Not yet
            scored; informational until a future prompt swap promotes it.
        v2_adjudicated: Whether ``v2_rel_type`` required builder adjudication (a
            rater majority, a 3-way split, or a converged ``NONE``), rather than
            unanimous agreement on a single type.
        v2_adjudication_rationale: One-line reasoning for an adjudicated
            ``v2_rel_type``, empty when unanimous.
        v2_needs_owner_signoff: Set when the adjudication still needs owner
            confirmation — a 3-way rater split, or a converged ``NONE`` (the V2
            vocab contradicting the V1 gold edge). Always ``True`` when
            ``v2_rel_type == REL_V2_NO_EDGE``.
    """

    source: str
    rel_type: str
    target: str
    v2_rel_type: str = ""
    v2_adjudicated: bool = False
    v2_adjudication_rationale: str = ""
    v2_needs_owner_signoff: bool = False


@dataclass(frozen=True)
class GoldStance:
    """One expected structured stance (owner → World concept).

    Attributes:
        target: The World-concept the stance is about (a gold entity name).
        affect: Optional affect gist ("" when unspecified).
    """

    target: str
    affect: str = ""


@dataclass(frozen=True)
class GoldClaim:
    """One expected structured personal claim.

    Attributes:
        facet: The normalized slot key the claim fills ("" when free-form).
        content_gist: A short paraphrase of the claim's content (label, not verbatim).
    """

    facet: str
    content_gist: str


@dataclass(frozen=True)
class GoldCase:
    """A single gold extraction case.

    Attributes:
        case_id: Stable identifier.
        tags: Failure-mode + domain tags (per-tag metrics key off these).
        source_user: The user turn text fed to the extractor.
        source_assistant: The assistant turn text fed to the extractor.
        expect_entities: Gold entities.
        expect_relationships: Gold typed-edge triples.
        expect_stances: Gold structured stances (may be empty).
        expect_claims: Gold structured claims (may be empty).
        forbid_entities: Hallucination traps — names that MUST NOT be extracted.
        forbid_rel_types: Edge types that MUST NOT appear for this case.
        dedup_variants: Variant pairs that MUST collapse to one canonical entity.
    """

    case_id: str
    tags: tuple[str, ...]
    source_user: str
    source_assistant: str
    expect_entities: tuple[GoldEntity, ...]
    expect_relationships: tuple[GoldRelationship, ...]
    expect_stances: tuple[GoldStance, ...] = ()
    expect_claims: tuple[GoldClaim, ...] = ()
    forbid_entities: tuple[str, ...] = ()
    forbid_rel_types: tuple[str, ...] = ()
    dedup_variants: tuple[tuple[str, str], ...] = ()

    @property
    def has_positive_label(self) -> bool:
        """Whether the case carries at least one positive expectation."""
        return bool(
            self.expect_entities
            or self.expect_relationships
            or self.expect_stances
            or self.expect_claims
        )


def _as_str(value: Any) -> str:
    """Coerce a scalar YAML value to a stripped string."""
    return str(value if value is not None else "").strip()


def _parse_entity(raw: dict[str, Any], case_id: str) -> GoldEntity:
    """Parse one gold entity, validating type/class vocabulary.

    Args:
        raw: The mapping from YAML.
        case_id: Owning case id (for error messages).

    Returns:
        The parsed :class:`GoldEntity`.

    Raises:
        GoldSetError: On a missing name, an off-vocabulary type/class, or an
            off-vocabulary ``v2_type`` (validated only when present, so a gold
            file mid-relabel — not every entity annotated yet — still loads).
    """
    name = _as_str(raw.get("name"))
    if not name:
        raise GoldSetError(f"{case_id}: entity with empty name")
    entity_type = _as_str(raw.get("type"))
    if entity_type not in ALLOWED_ENTITY_TYPES:
        raise GoldSetError(f"{case_id}: entity {name!r} has off-vocab type {entity_type!r}")
    knowledge_class = _as_str(raw.get("class"))
    if knowledge_class not in ALLOWED_ENTITY_CLASSES:
        raise GoldSetError(f"{case_id}: entity {name!r} has off-vocab class {knowledge_class!r}")
    aliases = tuple(_as_str(a) for a in raw.get("aliases", []) if _as_str(a))
    v2_type = _as_str(raw.get("v2_type"))
    if v2_type and v2_type not in ALLOWED_ENTITY_TYPES_V2:
        raise GoldSetError(f"{case_id}: entity {name!r} has off-vocab v2_type {v2_type!r}")
    return GoldEntity(
        name=name,
        entity_type=entity_type,
        knowledge_class=knowledge_class,
        aliases=aliases,
        v2_type=v2_type,
        v2_adjudicated=bool(raw.get("v2_adjudicated", False)),
        v2_adjudication_rationale=_as_str(raw.get("v2_adjudication_rationale")),
        v2_needs_owner_signoff=bool(raw.get("v2_needs_owner_signoff", False)),
    )


def _parse_relationship(raw: dict[str, Any], case_id: str) -> GoldRelationship:
    """Parse one gold relationship, validating the edge type is in vocabulary.

    Args:
        raw: The mapping from YAML.
        case_id: Owning case id (for error messages).

    Returns:
        The parsed :class:`GoldRelationship`.

    Raises:
        GoldSetError: On a missing source/target, an off-vocabulary V1 ``type``,
            or an off-vocabulary ``v2_rel_type`` (validated only when present, so a
            gold file mid-relabel still loads — a valid ``v2_rel_type`` is one of
            :data:`ALLOWED_REL_TYPES_V2` or the :data:`REL_V2_NO_EDGE` marker).
    """
    source = _as_str(raw.get("source"))
    target = _as_str(raw.get("target"))
    rel_type = _as_str(raw.get("type"))
    if not source or not target:
        raise GoldSetError(f"{case_id}: relationship missing source/target")
    if rel_type not in ALLOWED_REL_TYPES:
        raise GoldSetError(f"{case_id}: relationship has off-vocab type {rel_type!r}")
    v2_rel_type = _as_str(raw.get("v2_rel_type"))
    if v2_rel_type and v2_rel_type not in ALLOWED_REL_TYPES_V2 and v2_rel_type != REL_V2_NO_EDGE:
        raise GoldSetError(
            f"{case_id}: relationship {source!r}->{target!r} has off-vocab "
            f"v2_rel_type {v2_rel_type!r}"
        )
    return GoldRelationship(
        source=source,
        rel_type=rel_type,
        target=target,
        v2_rel_type=v2_rel_type,
        v2_adjudicated=bool(raw.get("v2_adjudicated", False)),
        v2_adjudication_rationale=_as_str(raw.get("v2_adjudication_rationale")),
        v2_needs_owner_signoff=bool(raw.get("v2_needs_owner_signoff", False)),
    )


def _parse_case(raw: dict[str, Any]) -> GoldCase:
    """Parse one gold case from its YAML mapping.

    Args:
        raw: The case mapping.

    Returns:
        The parsed :class:`GoldCase`.

    Raises:
        GoldSetError: On a missing id, missing source, or a degenerate case.
    """
    case_id = _as_str(raw.get("case_id"))
    if not case_id:
        raise GoldSetError("case with empty case_id")
    source = raw.get("source") or {}
    source_user = _as_str(source.get("user"))
    source_assistant = _as_str(source.get("assistant"))
    if not source_user and not source_assistant:
        raise GoldSetError(f"{case_id}: source has no user or assistant text")

    entities = tuple(_parse_entity(e, case_id) for e in raw.get("expect_entities", []))
    relationships = tuple(
        _parse_relationship(r, case_id) for r in raw.get("expect_relationships", [])
    )
    stances = tuple(
        GoldStance(target=_as_str(s.get("target")), affect=_as_str(s.get("affect")))
        for s in raw.get("expect_stances", [])
    )
    claims = tuple(
        GoldClaim(facet=_as_str(c.get("facet")), content_gist=_as_str(c.get("content_gist")))
        for c in raw.get("expect_claims", [])
    )
    forbid_entities = tuple(_as_str(f) for f in raw.get("forbid_entities", []) if _as_str(f))
    forbid_rel_types = tuple(_as_str(f) for f in raw.get("forbid_rel_types", []) if _as_str(f))
    dedup_variants = tuple(
        (_as_str(pair[0]), _as_str(pair[1]))
        for pair in raw.get("dedup_variants", [])
        if len(pair) == 2 and _as_str(pair[0]) and _as_str(pair[1])
    )

    case = GoldCase(
        case_id=case_id,
        tags=tuple(_as_str(t) for t in raw.get("tags", []) if _as_str(t)),
        source_user=source_user,
        source_assistant=source_assistant,
        expect_entities=entities,
        expect_relationships=relationships,
        expect_stances=stances,
        expect_claims=claims,
        forbid_entities=forbid_entities,
        forbid_rel_types=forbid_rel_types,
        dedup_variants=dedup_variants,
    )
    if not case.has_positive_label:
        raise GoldSetError(f"{case_id}: degenerate case (no positive expectation)")
    return case


def load_gold_set(path: str | Path) -> list[GoldCase]:
    """Load and validate a gold set from YAML.

    Args:
        path: Path to the gold-set YAML (a mapping with a ``cases`` list).

    Returns:
        The parsed cases, in file order.

    Raises:
        GoldSetError: On a malformed file, duplicate case ids, or a degenerate case.
    """
    text = Path(path).read_text(encoding="utf-8")
    doc = yaml.safe_load(text) or {}
    raw_cases = doc.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise GoldSetError(f"{path}: no cases found")
    cases = [_parse_case(rc) for rc in raw_cases]
    seen: set[str] = set()
    for case in cases:
        if case.case_id in seen:
            raise GoldSetError(f"duplicate case_id {case.case_id!r}")
        seen.add(case.case_id)
    return cases


def all_authored_strings(cases: Sequence[GoldCase]) -> list[str]:
    """Every human-authored string in a gold set (for the PII denylist scan).

    Args:
        cases: The loaded gold cases.

    Returns:
        A flat list of all authored text: source turns, entity names/aliases,
        relationship endpoints, stance/claim text, and the trap lists.
    """
    out: list[str] = []
    for case in cases:
        out.extend([case.case_id, case.source_user, case.source_assistant, *case.tags])
        for e in case.expect_entities:
            out.extend([e.name, *e.aliases, e.v2_adjudication_rationale])
        for r in case.expect_relationships:
            out.extend([r.source, r.target, r.v2_adjudication_rationale])
        for s in case.expect_stances:
            out.extend([s.target, s.affect])
        for c in case.expect_claims:
            out.extend([c.facet, c.content_gist])
        out.extend(case.forbid_entities)
        for pair in case.dedup_variants:
            out.extend(pair)
    return [s for s in out if s]
