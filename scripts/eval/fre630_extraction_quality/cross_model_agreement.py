"""FRE-771 — cross-model type-agreement over the `type-boundary` gold set (ADR-0109 AC-1).

AC-1 asks whether two (or more) model *families* agree with **each other** on an entity's
type, not whether either agrees with a fixed gold label — that is what
``entity_type_accuracy`` (and the FRE-766/770/782 spot-checks) already measure. This
module resolves each model's raw extraction output against gold via the same tiered
matcher the rest of the harness uses, then reuses ``iaa.py``'s pairwise-agreement
machinery over the resolved per-model type labels — no new statistics, same discipline
as ``relabel_v2_types.py``'s blind-classification IAA, applied to *real* extractor output
instead of a bespoke classification prompt. Pure: no I/O, no LLM.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from scripts.eval.fre630_extraction_quality import iaa
from scripts.eval.fre630_extraction_quality.gold import GoldCase
from scripts.eval.fre630_extraction_quality.matching import DEFAULT_FUZZY_THRESHOLD, match_entities
from scripts.eval.fre630_extraction_quality.scoring import parse_extraction


def type_boundary_cases(cases: Sequence[GoldCase]) -> list[GoldCase]:
    """Filter to the gold cases tagged ``type-boundary`` — the "previously-ambiguous set".

    Args:
        cases: The full loaded gold set.

    Returns:
        Cases carrying the ``type-boundary`` tag, in file order.
    """
    return [c for c in cases if "type-boundary" in c.tags]


def _resolve_entity_types(
    case: GoldCase, result: Mapping[str, Any], *, fuzzy_threshold: float
) -> dict[str, str]:
    """Map each gold entity name in ``case`` to the type this extraction assigned it.

    Args:
        case: The gold case.
        result: One model's raw extractor output dict for this case.
        fuzzy_threshold: Tier-3 matcher threshold (matches the rest of the harness).

    Returns:
        ``{gold_entity_name: extracted_type}`` — omits gold entities the extraction
        did not resolve to (a miss, not a disagreement data point).
    """
    parsed = parse_extraction(result)
    match = match_entities(
        case.expect_entities, parsed.entity_names, fuzzy_threshold=fuzzy_threshold
    )
    gold_to_extracted = match.gold_to_extracted()
    return {
        gold_name: parsed.entity_types.get(extracted_name, "")
        for gold_name, extracted_name in gold_to_extracted.items()
    }


@dataclass(frozen=True)
class CrossModelAgreementReport:
    """Cross-model type-agreement over the `type-boundary` set for one taxonomy arm.

    Attributes:
        overall_agreement: Mean pairwise exact-match agreement over every fully-resolved
            item, or ``None`` when no item was resolved by every model.
        by_pair: Agreement fraction for each distinct model-name pair.
        n_items: Count of fully-resolved (case, entity) items the statistic covers.
        disagreements: ``"<case_id>::<entity_name>"`` keys where the models split.
    """

    overall_agreement: float | None
    by_pair: Mapping[tuple[str, str], float]
    n_items: int
    disagreements: tuple[str, ...]


def build_cross_model_agreement(
    cases: Sequence[GoldCase],
    results_by_model: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
) -> CrossModelAgreementReport:
    """Build the AC-1 cross-model type-agreement report over the `type-boundary` set.

    Args:
        cases: The full gold set (filtered internally to `type-boundary` cases).
        results_by_model: ``{model_name: {case_id: raw_extraction_result}}`` — one
            extraction result per case per model (the caller picks which sample, e.g.
            the first of N, or a majority vote across samples).
        fuzzy_threshold: Tier-3 matcher threshold.

    Returns:
        The assembled :class:`CrossModelAgreementReport`. An item (one gold entity in
        one boundary case) is only included when *every* model resolved it to some
        extracted type — a model that missed the entity entirely drops that item
        rather than counting as a disagreement (mirrors ``relabel_v2_types.py``'s
        "complete_items" filter for blind-rater responses).
    """
    model_names = sorted(results_by_model)
    boundary_cases = type_boundary_cases(cases)

    item_ids: list[str] = []
    rater_labels: list[list[str]] = []
    for case in boundary_cases:
        resolved_by_model = {
            model: _resolve_entity_types(
                case, results_by_model[model].get(case.case_id, {}), fuzzy_threshold=fuzzy_threshold
            )
            for model in model_names
        }
        for entity in case.expect_entities:
            labels = [resolved_by_model[model].get(entity.name, "") for model in model_names]
            if not all(labels):
                continue  # some model never resolved this entity — a miss, not a disagreement
            item_ids.append(f"{case.case_id}::{entity.name}")
            rater_labels.append(labels)

    if not rater_labels:
        return CrossModelAgreementReport(
            overall_agreement=None, by_pair={}, n_items=0, disagreements=()
        )

    overall_agreement = iaa.pairwise_agreement(rater_labels)
    by_pair = iaa.pairwise_agreement_by_pair(rater_labels, model_names)
    disagreements = tuple(
        item_id
        for item_id, labels in zip(item_ids, rater_labels, strict=True)
        if len(set(labels)) > 1
    )
    return CrossModelAgreementReport(
        overall_agreement=overall_agreement,
        by_pair=by_pair,
        n_items=len(rater_labels),
        disagreements=disagreements,
    )
