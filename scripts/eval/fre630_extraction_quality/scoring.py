"""FRE-630 — pure case scorer: gold case × extractor output → a scored result.

``score_case`` is the single pure entry point the harness calls per extraction. It
parses the extractor's returned dict (``entities`` / ``relationships`` / ``stances`` /
``claims``) into the metric-core shapes, resolves entities against gold via the tiered
matcher, computes every metric, and records the per-case *diffs* (what was missed,
spurious, mis-typed, mis-classed, hallucinated) that make a run legible without a raw
dump. No I/O, no LLM.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from scripts.eval.fre630_extraction_quality import metrics
from scripts.eval.fre630_extraction_quality.gold import GoldCase, GoldEntity
from scripts.eval.fre630_extraction_quality.matching import (
    DEFAULT_FUZZY_THRESHOLD,
    match_entities,
    matches_any,
)
from scripts.eval.fre630_extraction_quality.metrics import PRF, ExtractedRel

#: FRE-771 — which gold entity-type field ``score_case`` scores against. ``"v2"`` resolves
#: each gold entity's ``v2_type or entity_type`` (the live extractor speaks the ADR-0109
#: V2 10-type taxonomy since the FRE-771 prompt swap; the fallback keeps hand-built test
#: fixtures that never set ``v2_type`` scoring unchanged). ``"v1"`` resolves ``entity_type``
#: only — used solely by the FRE-771 powered A/B's monkeypatched "current" comparison arm.
#: No default: every caller states its choice explicitly (codex plan-review finding — a
#: silent default here would hide a scoring-semantics change).
EntityTypeField = Literal["v1", "v2"]


@dataclass(frozen=True)
class ParsedExtraction:
    """The extractor's output dict, normalized into metric-core shapes.

    Attributes:
        entity_names: Emitted entity surface names (order preserved).
        entity_types: name → emitted entity type.
        entity_classes: name → emitted knowledge class.
        descriptions: Emitted entity descriptions.
        relationships: Emitted typed-edge triples (surface endpoints).
        stance_targets: Targets of emitted structured stances.
        claim_facets: Facets of emitted structured claims.
        is_empty_fallback: True when the extractor returned no entities at all.
    """

    entity_names: tuple[str, ...]
    entity_types: Mapping[str, str]
    entity_classes: Mapping[str, str]
    descriptions: tuple[str, ...]
    relationships: tuple[ExtractedRel, ...]
    stance_targets: tuple[str, ...]
    claim_facets: tuple[str, ...]
    is_empty_fallback: bool


def _str(value: Any) -> str:
    """Coerce a scalar to a stripped string."""
    return str(value if value is not None else "").strip()


def parse_extraction(result: Mapping[str, Any]) -> ParsedExtraction:
    """Normalize a raw extractor output dict into :class:`ParsedExtraction`.

    Tolerant of missing keys and duplicate names (later dicts win for the type/class
    maps, matching last-write semantics; the name list keeps every emission so
    dedup/hallucination counts are honest).

    Args:
        result: The dict returned by ``extract_entities_and_relationships``.

    Returns:
        The parsed, metric-ready view.
    """
    raw_entities = result.get("entities") or []
    entity_names: list[str] = []
    entity_types: dict[str, str] = {}
    entity_classes: dict[str, str] = {}
    descriptions: list[str] = []
    for e in raw_entities:
        name = _str(e.get("name"))
        if not name:
            continue
        entity_names.append(name)
        entity_types[name] = _str(e.get("type"))
        entity_classes[name] = _str(e.get("class"))
        descriptions.append(_str(e.get("description")))

    relationships = tuple(
        ExtractedRel(
            source=_str(r.get("source")),
            rel_type=_str(r.get("type")).upper(),
            target=_str(r.get("target")),
        )
        for r in (result.get("relationships") or [])
        if _str(r.get("source")) and _str(r.get("target")) and _str(r.get("type"))
    )
    stance_targets = tuple(
        _str(s.get("target")) for s in (result.get("stances") or []) if _str(s.get("target"))
    )
    claim_facets = tuple(
        _str(c.get("facet")) for c in (result.get("claims") or []) if _str(c.get("facet"))
    )
    return ParsedExtraction(
        entity_names=tuple(entity_names),
        entity_types=entity_types,
        entity_classes=entity_classes,
        descriptions=tuple(descriptions),
        relationships=relationships,
        stance_targets=stance_targets,
        claim_facets=claim_facets,
        is_empty_fallback=len(entity_names) == 0,
    )


@dataclass(frozen=True)
class CaseScore:
    """The scored outcome of one gold case against one extraction.

    Attributes:
        case_id: The case id.
        tags: The case tags (per-tag aggregation keys off these).
        entity: Entity-level P/R/F1.
        entity_type_accuracy: Type accuracy over matched entities.
        knowledge_class_accuracy: Class accuracy over matched entities.
        relationship: Relationship-level P/R/F1.
        relationship_type_correctness: Right-type-given-right-endpoints.
        hallucination_rate: Trap-hit fraction over extracted entities.
        forbidden_edge_type_rate: Off-vocab/forbidden edge-type fraction.
        dedup_convergence: Variant-pair collapse fraction.
        description_integrity: Deterministic description proxy.
        stance_emission_recall: Structured-stance recall.
        claim_emission_recall: Structured-claim recall.
        is_empty_fallback: Extractor returned nothing on a positive-labeled case.
        match_tier_counts: exact/alias/fuzzy match tallies (audit).
        diffs: Human-legible per-case diffs (missed/spurious/wrong-type/…).
    """

    case_id: str
    tags: tuple[str, ...]
    entity: PRF
    entity_type_accuracy: float | None
    knowledge_class_accuracy: float | None
    relationship: PRF
    relationship_type_correctness: float | None
    hallucination_rate: float | None
    forbidden_edge_type_rate: float | None
    dedup_convergence: float | None
    description_integrity: float | None
    stance_emission_recall: float | None
    claim_emission_recall: float | None
    is_empty_fallback: bool
    match_tier_counts: Mapping[str, int]
    diffs: Mapping[str, list[str]] = field(default_factory=dict)


def _gold_entity_type(entity: GoldEntity, entity_type_field: EntityTypeField) -> str:
    """Resolve one gold entity's scored type string for the active field (FRE-771).

    Args:
        entity: A :class:`~scripts.eval.fre630_extraction_quality.gold.GoldEntity`.
        entity_type_field: ``"v2"`` prefers ``v2_type``, falling back to ``entity_type``
            when unset (hand-built fixtures without a ``v2_type``); ``"v1"`` always
            resolves ``entity_type``.

    Returns:
        The gold type string to compare the extractor's output against.
    """
    if entity_type_field == "v1":
        return entity.entity_type
    return entity.v2_type or entity.entity_type


def score_case(
    case: GoldCase,
    result: Mapping[str, Any],
    *,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
    entity_type_field: EntityTypeField,
) -> CaseScore:
    """Score one gold case against one raw extractor output dict.

    Args:
        case: The gold case.
        result: The dict returned by ``extract_entities_and_relationships``.
        fuzzy_threshold: Tier-3 matcher threshold.
        entity_type_field: Which gold entity-type field to score against — ``"v2"``
            (the live, post-FRE-771 extractor vocabulary) or ``"v1"`` (the retired
            7-type vocabulary, only used by the FRE-771 powered A/B's comparison arm).
            Required, not defaulted (see :data:`EntityTypeField`).

    Returns:
        The fully-populated :class:`CaseScore`.
    """
    parsed = parse_extraction(result)
    match = match_entities(
        case.expect_entities, parsed.entity_names, fuzzy_threshold=fuzzy_threshold
    )

    gold_types = {e.name: _gold_entity_type(e, entity_type_field) for e in case.expect_entities}
    gold_classes = {e.name: e.knowledge_class for e in case.expect_entities}

    tier_counts = {"exact": 0, "alias": 0, "fuzzy": 0}
    for m in match.matches:
        tier_counts[m.tier] += 1

    wrong_type = [
        f"{m.extracted_name}→{m.gold_name}: {parsed.entity_types.get(m.extracted_name, '')!r} ≠ {gold_types[m.gold_name]!r}"
        for m in match.matches
        if parsed.entity_types.get(m.extracted_name, "") != gold_types[m.gold_name]
    ]
    wrong_class = [
        f"{m.extracted_name}→{m.gold_name}: {parsed.entity_classes.get(m.extracted_name, '')!r} ≠ {gold_classes[m.gold_name]!r}"
        for m in match.matches
        if parsed.entity_classes.get(m.extracted_name, "") != gold_classes[m.gold_name]
    ]
    hallucinated = [n for n in parsed.entity_names if matches_any(n, case.forbid_entities)]
    forbidden_edges = [
        f"{r.source} -{r.rel_type}-> {r.target}"
        for r in parsed.relationships
        if (metrics.forbidden_edge_type_rate([r], case.forbid_rel_types) or 0.0) > 0.0
    ]

    diffs: dict[str, list[str]] = {
        "missed_entities": list(match.unmatched_gold),
        "spurious_entities": list(match.unmatched_extracted),
        "wrong_type": wrong_type,
        "wrong_class": wrong_class,
        "hallucinated": hallucinated,
        "forbidden_edges": forbidden_edges,
    }

    return CaseScore(
        case_id=case.case_id,
        tags=case.tags,
        entity=metrics.entity_prf(match),
        entity_type_accuracy=metrics.entity_type_accuracy(match, gold_types, parsed.entity_types),
        knowledge_class_accuracy=metrics.knowledge_class_accuracy(
            match, gold_classes, parsed.entity_classes
        ),
        relationship=metrics.relationship_prf(
            case.expect_relationships, parsed.relationships, match
        ),
        relationship_type_correctness=metrics.relationship_type_correctness(
            case.expect_relationships, parsed.relationships, match
        ),
        hallucination_rate=metrics.hallucination_rate(parsed.entity_names, case.forbid_entities),
        forbidden_edge_type_rate=metrics.forbidden_edge_type_rate(
            parsed.relationships, case.forbid_rel_types
        ),
        dedup_convergence=metrics.dedup_convergence(parsed.entity_names, case.dedup_variants),
        description_integrity=metrics.description_integrity(parsed.descriptions),
        stance_emission_recall=metrics.stance_emission_recall(
            case.expect_stances, parsed.stance_targets
        ),
        claim_emission_recall=metrics.claim_emission_recall(
            case.expect_claims, parsed.claim_facets
        ),
        is_empty_fallback=metrics.extraction_empty(
            len(parsed.entity_names), case.has_positive_label
        ),
        match_tier_counts=tier_counts,
        diffs={k: v for k, v in diffs.items() if v},
    )
