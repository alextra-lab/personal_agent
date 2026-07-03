"""FRE-630 — pure extraction-quality metric core (ADR-0087 §D1, write-side analog).

Pure functions only — no substrate, no LLM, no I/O. They consume a
:class:`~scripts.eval.fre630_extraction_quality.matching.MatchResult` (extracted
names already resolved to gold entities) plus the gold and extracted structured
data, and return the write-side quality metrics:

* entity precision / recall / F1, entity-type accuracy, knowledge-class accuracy;
* relationship precision / recall (scored over *resolved* endpoints, codex P0.2) and
  relationship-type correctness (right endpoints — was the edge type right?);
* hallucination rate + forbidden-edge-type rate;
* extraction-empty-fallback detection (codex P2);
* dedup/normalization convergence and a description-integrity *proxy*;
* stance / claim emission recall.

Conventions mirror the FRE-435 core: metrics return ``None`` when their denominator
is vacuous so they are *excluded* from aggregates rather than averaged as a
misleading ``1.0``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from scripts.eval.fre630_extraction_quality.gold import (
    ALLOWED_REL_TYPES,
    GoldCase,
    GoldClaim,
    GoldRelationship,
    GoldStance,
)
from scripts.eval.fre630_extraction_quality.matching import MatchResult, matches_any, normalize_name


@dataclass(frozen=True)
class PRF:
    """A precision / recall / F1 triple with the raw confusion counts.

    Attributes:
        precision: ``tp / (tp + fp)`` (``None`` when nothing was produced).
        recall: ``tp / (tp + fn)`` (``None`` when nothing was expected).
        f1: Harmonic mean (``None`` when precision or recall is ``None``/0-degenerate).
        tp: True positives.
        fp: False positives.
        fn: False negatives.
    """

    precision: float | None
    recall: float | None
    f1: float | None
    tp: int
    fp: int
    fn: int


def _prf(tp: int, fp: int, fn: int) -> PRF:
    """Build a :class:`PRF` from confusion counts (vacuous denominators → ``None``)."""
    precision = tp / (tp + fp) if (tp + fp) > 0 else None
    recall = tp / (tp + fn) if (tp + fn) > 0 else None
    if precision is None or recall is None or (precision + recall) == 0:
        f1: float | None = None
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return PRF(precision=precision, recall=recall, f1=f1, tp=tp, fp=fp, fn=fn)


def entity_prf(match: MatchResult) -> PRF:
    """Entity precision/recall/F1 from a resolved match.

    Args:
        match: The resolved gold↔extracted match.

    Returns:
        The entity-level :class:`PRF` (tp = matched, fp = spurious, fn = missed).
    """
    return _prf(
        tp=len(match.matches), fp=len(match.unmatched_extracted), fn=len(match.unmatched_gold)
    )


def _accuracy_over_matches(
    match: MatchResult,
    gold_attr: Mapping[str, str],
    extracted_attr: Mapping[str, str],
) -> float | None:
    """Fraction of matched entities whose extracted attribute equals the gold one.

    Args:
        match: The resolved match.
        gold_attr: gold canonical name → gold attribute (type or class).
        extracted_attr: extracted surface name → extracted attribute.

    Returns:
        Accuracy over matched pairs, or ``None`` when nothing matched.
    """
    if not match.matches:
        return None
    correct = 0
    for m in match.matches:
        gold_value = gold_attr.get(m.gold_name, "")
        extracted_value = extracted_attr.get(m.extracted_name, "")
        if gold_value and gold_value == extracted_value:
            correct += 1
    return correct / len(match.matches)


def entity_type_accuracy(
    match: MatchResult, gold_types: Mapping[str, str], extracted_types: Mapping[str, str]
) -> float | None:
    """Entity-type accuracy over matched entities (one of the 7 types)."""
    return _accuracy_over_matches(match, gold_types, extracted_types)


def knowledge_class_accuracy(
    match: MatchResult, gold_classes: Mapping[str, str], extracted_classes: Mapping[str, str]
) -> float | None:
    """Knowledge-class accuracy over matched entities (World/Personal/System)."""
    return _accuracy_over_matches(match, gold_classes, extracted_classes)


@dataclass(frozen=True)
class ExtractedRel:
    """A normalized extracted relationship triple (surface endpoints).

    Attributes:
        source: Extracted source surface name.
        rel_type: Extracted edge type (upper-cased).
        target: Extracted target surface name.
    """

    source: str
    rel_type: str
    target: str


def _resolve_rel(rel: ExtractedRel, ext_to_gold: Mapping[str, str]) -> tuple[str, str] | None:
    """Resolve an extracted rel's endpoints to gold canonical names.

    Args:
        rel: The extracted relationship.
        ext_to_gold: extracted surface name → resolved gold canonical name.

    Returns:
        ``(gold_source, gold_target)`` when *both* endpoints resolved, else ``None``.
    """
    gold_source = ext_to_gold.get(rel.source)
    gold_target = ext_to_gold.get(rel.target)
    if gold_source is None or gold_target is None:
        return None
    return gold_source, gold_target


def relationship_prf(
    gold_rels: Sequence[GoldRelationship],
    extracted_rels: Sequence[ExtractedRel],
    match: MatchResult,
) -> PRF:
    """Relationship precision/recall/F1 over *resolved* typed-edge triples (codex P0.2).

    An extracted edge is a true positive iff both endpoints resolve (via the entity
    match) to the gold entities named in a gold triple *and* the edge type matches.
    Edges whose endpoints do not resolve count as false positives (spurious edges).

    Args:
        gold_rels: The gold typed-edge triples (over gold canonical names).
        extracted_rels: The extractor's emitted triples (surface endpoints).
        match: The resolved entity match.

    Returns:
        The relationship-level :class:`PRF`.
    """
    gold_set = {(r.source, r.rel_type.upper(), r.target) for r in gold_rels}
    ext_to_gold = match.extracted_to_gold()
    matched_gold: set[tuple[str, str, str]] = set()
    tp = 0
    fp = 0
    for rel in extracted_rels:
        resolved = _resolve_rel(rel, ext_to_gold)
        if resolved is None:
            fp += 1
            continue
        triple = (resolved[0], rel.rel_type.upper(), resolved[1])
        if triple in gold_set:
            tp += 1
            matched_gold.add(triple)
        else:
            fp += 1
    fn = len(gold_set - matched_gold)
    return _prf(tp=tp, fp=fp, fn=fn)


def relationship_type_correctness(
    gold_rels: Sequence[GoldRelationship],
    extracted_rels: Sequence[ExtractedRel],
    match: MatchResult,
) -> float | None:
    """Given the *right endpoints*, did the extractor pick the right edge type?

    Isolates the residence-vs-visit style failure from plain missing edges: it ranges
    only over extracted edges whose resolved ``(source, target)`` matches a gold pair
    (ignoring type), and asks what fraction carry the gold type. ``None`` when no
    extracted edge lands on a gold endpoint pair.

    Args:
        gold_rels: Gold triples.
        extracted_rels: Extracted triples (surface endpoints).
        match: The resolved entity match.

    Returns:
        Type-correctness over endpoint-aligned edges, or ``None``.
    """
    gold_type_by_pair: dict[tuple[str, str], set[str]] = {}
    for r in gold_rels:
        gold_type_by_pair.setdefault((r.source, r.target), set()).add(r.rel_type.upper())
    ext_to_gold = match.extracted_to_gold()
    aligned = 0
    correct = 0
    for rel in extracted_rels:
        resolved = _resolve_rel(rel, ext_to_gold)
        if resolved is None or resolved not in gold_type_by_pair:
            continue
        aligned += 1
        if rel.rel_type.upper() in gold_type_by_pair[resolved]:
            correct += 1
    if aligned == 0:
        return None
    return correct / aligned


def hallucination_rate(
    extracted_names: Sequence[str], forbid_entities: Sequence[str]
) -> float | None:
    """Fraction of extracted entities that hit a hallucination trap.

    Args:
        extracted_names: All extracted entity surface names.
        forbid_entities: Names that must not be extracted (role labels, tool names,
            a misspelled relationship type leaking in as an entity).

    Returns:
        Trap-hits / total-extracted, or ``None`` when nothing was extracted.
    """
    if not extracted_names:
        return None
    if not forbid_entities:
        return 0.0
    hits = sum(1 for name in extracted_names if matches_any(name, forbid_entities))
    return hits / len(extracted_names)


def forbidden_edge_type_rate(
    extracted_rels: Sequence[ExtractedRel], forbid_rel_types: Sequence[str]
) -> float | None:
    """Fraction of extracted edges whose type is off-vocabulary or case-forbidden.

    An edge type counts as forbidden when it is outside the controlled vocabulary
    (:data:`ALLOWED_REL_TYPES`) *or* explicitly listed in the case's
    ``forbid_rel_types`` (e.g. ``LIVES_IN`` asserted for a visit).

    Args:
        extracted_rels: The extractor's emitted triples.
        forbid_rel_types: Case-specific forbidden edge types.

    Returns:
        Forbidden-edge count / total edges, or ``None`` when no edges were emitted.
    """
    if not extracted_rels:
        return None
    forbid_upper = {t.upper() for t in forbid_rel_types}
    hits = sum(
        1
        for rel in extracted_rels
        if rel.rel_type.upper() not in ALLOWED_REL_TYPES or rel.rel_type.upper() in forbid_upper
    )
    return hits / len(extracted_rels)


def extraction_empty(extracted_entity_count: int, gold_has_positive: bool) -> bool:
    """Whether the extractor returned nothing while gold expected positives (codex P2).

    Catches the ``_default_extraction_result`` fallback (timeout / parse-fail / empty
    response) so it is counted separately and never masquerades as a precision/recall
    miss.

    Args:
        extracted_entity_count: Number of entities the extractor returned.
        gold_has_positive: Whether the case carries any positive expectation.

    Returns:
        ``True`` on an empty extraction against a positive-labeled case.
    """
    return extracted_entity_count == 0 and gold_has_positive


def _surface_key(name: str) -> str:
    """Whitespace-collapsed but *case-preserving* key (distinguishes case variants)."""
    return " ".join((name or "").split())


def dedup_convergence(
    extracted_names: Sequence[str], variant_pairs: Sequence[tuple[str, str]]
) -> float | None:
    """Fraction of variant pairs the extractor collapsed to a single entity.

    For each ``(a, b)`` case/spelling-variant pair, the extractor *converged* when it
    emitted at most one distinct surface form across the two variants. Case-folded
    normalization alone cannot see the failure (both variants fold to one key), so
    convergence counts *distinct case-preserving surface forms* whose normalized key
    equals either variant's key: >1 means the extractor emitted the variants as
    separate entities. Measures the extractor's own normalization only (not
    embedding/graph-write dedup — that is Phase 2).

    Args:
        extracted_names: All extracted entity surface names.
        variant_pairs: Case/spelling-variant pairs that must collapse.

    Returns:
        Collapsed-pairs / total-pairs, or ``None`` when the case has no pairs.
    """
    if not variant_pairs:
        return None
    collapsed = 0
    for a, b in variant_pairs:
        keys = {normalize_name(a), normalize_name(b)}
        surfaces = {_surface_key(n) for n in extracted_names if normalize_name(n) in keys}
        if len(surfaces) <= 1:
            collapsed += 1
    return collapsed / len(variant_pairs)


_SENTENCE_SPLIT_RE = re.compile(r"[.!?](?:\s|$)")
_STANCE_FLATTEN_RE = re.compile(
    r"\b(?:the user|owner)\s+(?:likes?|loves?|prefers?|enjoys?|is interested in|wants?)\b",
    re.IGNORECASE,
)


def _description_integrity_one(description: str) -> bool:
    """Whether one description passes the deterministic integrity proxy.

    Proxy (labeled, not a headline): non-empty, a single sentence, and free of a
    stance-flattening clause ("a concept the user likes strongly") — the FRE-636
    signature the extractor is meant to route to a structured stance instead.

    Args:
        description: The entity description text.

    Returns:
        ``True`` when the proxy passes.
    """
    text = (description or "").strip()
    if not text:
        return False
    if _STANCE_FLATTEN_RE.search(text):
        return False
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    return len(sentences) <= 1


def description_integrity(descriptions: Sequence[str]) -> float | None:
    """Deterministic description-integrity proxy over extracted descriptions.

    Args:
        descriptions: Extracted entity descriptions.

    Returns:
        Fraction passing the proxy, or ``None`` when there are no descriptions.
    """
    if not descriptions:
        return None
    return sum(1 for d in descriptions if _description_integrity_one(d)) / len(descriptions)


def stance_emission_recall(
    gold_stances: Sequence[GoldStance], extracted_stance_targets: Sequence[str]
) -> float | None:
    """Recall of expected structured stances by target entity.

    A stance is emitted when the extractor produced a structured stance whose target
    matches a gold stance target (normalized) — i.e. it did not flatten it into a
    World description.

    Args:
        gold_stances: Expected stances.
        extracted_stance_targets: Targets of the extractor's emitted stances.

    Returns:
        Emitted / expected, or ``None`` when no stance was expected.
    """
    if not gold_stances:
        return None
    hit = sum(1 for s in gold_stances if matches_any(s.target, extracted_stance_targets))
    return hit / len(gold_stances)


def claim_emission_recall(
    gold_claims: Sequence[GoldClaim], extracted_claim_facets: Sequence[str]
) -> float | None:
    """Recall of expected structured claims by facet slot.

    Args:
        gold_claims: Expected claims.
        extracted_claim_facets: Facets of the extractor's emitted claims.

    Returns:
        Emitted / expected, or ``None`` when no claim was expected. Claims whose gold
        facet is empty (free-form) are excluded from the denominator — a facetless
        claim cannot be matched by slot.
    """
    keyed = [c for c in gold_claims if c.facet]
    if not keyed:
        return None
    extracted_norm = {normalize_name(f) for f in extracted_claim_facets}
    hit = sum(1 for c in keyed if normalize_name(c.facet) in extracted_norm)
    return hit / len(keyed)


def mean_optional(values: Sequence[float | None]) -> float | None:
    """Mean of the non-``None`` values, or ``None`` if there are none."""
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present) / len(present)


@dataclass(frozen=True)
class MeanStd:
    """Mean and (population) standard deviation over a sample of runs.

    Attributes:
        mean: Sample mean (``None`` when the sample is empty).
        std: Population standard deviation (``None`` when < 1 present value).
        n: Number of present (non-``None``) values.
    """

    mean: float | None
    std: float | None
    n: int


def mean_std(values: Sequence[float | None]) -> MeanStd:
    """Mean/std over present values, for the ``--samples N`` stability band (codex P1.4)."""
    present = [v for v in values if v is not None]
    if not present:
        return MeanStd(mean=None, std=None, n=0)
    mean = sum(present) / len(present)
    if len(present) < 2:
        return MeanStd(mean=mean, std=None, n=len(present))
    variance = sum((v - mean) ** 2 for v in present) / len(present)
    return MeanStd(mean=mean, std=variance**0.5, n=len(present))


def case_has_positive(case: GoldCase) -> bool:
    """Thin re-export of the case's positive-label flag (harness convenience)."""
    return case.has_positive_label
