r"""FRE-771 — the powered A/B: V2 (10-type) vs V1 (retired 7-type) prompt (ADR-0109 step 4).

On the ADR-0109-relabeled gold set, two sequential phases over the same 2 model families
— ``mini-none`` (current prod cell) and ``sonnet5-adaptive`` (frontier, distinct family)
— the exact pairing ADR-0109's own
FRE-766 spot-check used (FRE-771 plan § D6: AC-1's bar is "between two model families",
not the full FRE-766 6-cell reasoning matrix). Phase 1 runs the LIVE (post-swap) V2
prompt, unpatched. Phase 2 monkeypatches
``personal_agent.second_brain.entity_extraction._EXTRACTION_PROMPT_TEMPLATE`` to a
frozen, verbatim pre-swap snapshot (the "current" prompt this ticket retires), runs the
same 2 cells, then restores the original template in a ``finally``. **The two phases
never run concurrently against the shared module global** — a monkeypatch is
process-wide state, so interleaving V1 and V2 calls would race (FRE-771 plan § D2).

Per phase: standard FRE-630 metrics (``entity_type_accuracy`` scored with the phase's
matching ``entity_type_field`` — D3) — including the "no regression" set (hallucination,
dedup, forbidden-edge, knowledge-class) — plus ADR-0109 AC-1's cross-model type-agreement
over the ``type-boundary`` gold subset (``cross_model_agreement.py``), computed from each
model's first extraction sample per case (the cross-model question is "do two models
agree with each other on this entity", which wants one canonical extraction per model per
case, not an average across resampled attempts).

Usage::

    make test-infra-up
    uv run python -m scripts.eval.fre630_extraction_quality.fre771_powered_ab \
        --run-id fre771-2026-07-04 --samples 3

Output lands in the gitignored ``telemetry/evaluation/fre630-extraction-quality/``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from scripts.eval.fre630_extraction_quality import harness
from scripts.eval.fre630_extraction_quality.bench import _register_cell_pricing
from scripts.eval.fre630_extraction_quality.cells import CELLS_BY_NAME, ExtractionModelCell
from scripts.eval.fre630_extraction_quality.cross_model_agreement import build_cross_model_agreement
from scripts.eval.fre630_extraction_quality.fre771_v1_prompt_snapshot import (
    v1_prompt_template_active,
)
from scripts.eval.fre630_extraction_quality.gold import GoldCase, load_gold_set
from scripts.eval.fre630_extraction_quality.matching import DEFAULT_FUZZY_THRESHOLD
from scripts.eval.fre630_extraction_quality.metrics import claim_case_level_recall
from scripts.eval.fre630_extraction_quality.report import CaseRun, aggregate
from scripts.eval.fre630_extraction_quality.scoring import EntityTypeField, score_case

from personal_agent.second_brain import entity_extraction

log = structlog.get_logger(__name__)

DEFAULT_GOLD_SET = harness.DEFAULT_GOLD_SET
DEFAULT_OUT = harness.DEFAULT_OUT

#: The 2 model families this A/B compares (FRE-771 plan § D6) — the exact pairing
#: ADR-0109's own FRE-766 spot-check used.
_AB_CELL_NAMES: tuple[str, ...] = ("mini-none", "sonnet5-adaptive")


def _ms(ms: Any) -> dict[str, Any] | None:
    """Serialize a ``MeanStd`` (or ``None``) to a mean/std/n dict."""
    if ms is None:
        return None
    return {"mean": ms.mean, "std": ms.std, "n": ms.n}


async def _run_cell_samples(
    cell: ExtractionModelCell,
    cases: list[GoldCase],
    samples: int,
    fuzzy_threshold: float,
    entity_type_field: EntityTypeField,
) -> tuple[list[CaseRun], dict[str, dict[str, Any]]]:
    """Run every case ``samples`` times through ``cell``; score + retain sample 0.

    Args:
        cell: The active model cell.
        cases: The gold cases to run (the full set — cross-model agreement filters to
            ``type-boundary`` internally).
        samples: Extraction samples per case.
        fuzzy_threshold: Tier-3 matcher threshold.
        entity_type_field: Which gold type field to score against (matches the active
            taxonomy phase — "v2" or "v1").

    Returns:
        The scored :class:`CaseRun` per case, and ``{case_id: first_sample_raw_result}``
        — the first sample's raw extraction dict, retained for the cross-model
        agreement calculation (which wants one canonical extraction per model per case,
        not an average across resampled attempts).
    """
    from personal_agent.cost_gate import BudgetDenied

    runs: list[CaseRun] = []
    first_sample_by_case: dict[str, dict[str, Any]] = {}
    for case in cases:
        scores = []
        for i in range(samples):
            try:
                result = await entity_extraction.extract_entities_and_relationships(
                    case.source_user, case.source_assistant, model_override=cell.override
                )
            except BudgetDenied as e:
                # A budget wall (this run's own spend, or concurrent shared usage on the
                # test substrate) is a denied SAMPLE, not a fatal crash — the caller
                # persists whatever completed so far rather than losing an entire
                # (expensive, already-paid-for) phase to one late denial.
                log.warning(
                    "fre771_sample_budget_denied",
                    cell=cell.name,
                    case_id=case.case_id,
                    error=str(e),
                )
                continue
            if i == 0:
                first_sample_by_case[case.case_id] = result
            scores.append(
                score_case(
                    case,
                    result,
                    fuzzy_threshold=fuzzy_threshold,
                    entity_type_field=entity_type_field,
                )
            )
        if scores:
            runs.append(CaseRun(case_id=case.case_id, tags=case.tags, samples=tuple(scores)))
    return runs, first_sample_by_case


async def _run_phase(
    cells: list[ExtractionModelCell],
    cases: list[GoldCase],
    samples: int,
    fuzzy_threshold: float,
    entity_type_field: EntityTypeField,
) -> dict[str, Any]:
    """Run one taxonomy arm (V1 or V2) across all ``cells`` concurrently.

    Args:
        cells: The model cells to run (concurrently, one worker per cell).
        cases: The full gold set.
        samples: Extraction samples per case.
        fuzzy_threshold: Tier-3 matcher threshold.
        entity_type_field: Which gold type field this arm scores against.

    Returns:
        ``{"per_cell": {cell_name: metric summary}, "cross_model_agreement": {...}}``.
    """
    for cell in cells:
        _register_cell_pricing(cell)
    results = await asyncio.gather(
        *[
            _run_cell_samples(cell, cases, samples, fuzzy_threshold, entity_type_field)
            for cell in cells
        ]
    )

    per_cell: dict[str, Any] = {}
    results_by_model: dict[str, dict[str, dict[str, Any]]] = {}
    for cell, (runs, first_samples) in zip(cells, results, strict=True):
        all_scores = [s for run in runs for s in run.samples]
        agg = aggregate(all_scores)
        per_case_claim_recalls = [[s.claim_emission_recall for s in run.samples] for run in runs]
        ccr = claim_case_level_recall(per_case_claim_recalls)
        per_cell[cell.name] = {
            "entity_type_accuracy": _ms(agg.get("entity_type_accuracy")),
            "knowledge_class_accuracy": _ms(agg.get("knowledge_class_accuracy")),
            "hallucination_rate": _ms(agg.get("hallucination_rate")),
            "dedup_convergence": _ms(agg.get("dedup_convergence")),
            "forbidden_edge_type_rate": _ms(agg.get("forbidden_edge_type_rate")),
            "claim_case_level_recall": {
                "passing": ccr.passing,
                "total": ccr.total,
                "fraction": ccr.fraction,
            },
        }
        results_by_model[cell.name] = first_samples

    boundary = build_cross_model_agreement(cases, results_by_model, fuzzy_threshold=fuzzy_threshold)
    return {
        "per_cell": per_cell,
        "cross_model_agreement": {
            "overall_agreement": boundary.overall_agreement,
            "by_pair": {f"{a}↔{b}": v for (a, b), v in boundary.by_pair.items()},
            "n_items": boundary.n_items,
            "disagreements": list(boundary.disagreements),
        },
    }


def _write_summary(summary: dict[str, Any], run_id: str, out_dir: str) -> Path:
    """Write (or overwrite) the run summary JSON; return its path."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{run_id}-summary.json"
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return path


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    """Load the gold set and run both taxonomy phases (V2 then V1) sequentially.

    Persists the summary to disk after EACH phase (not just at the end) — a budget
    wall or transient failure partway through the (unpatched, expensive) V1 phase must
    never discard the already-completed, already-paid-for V2 phase's results.
    """
    from personal_agent.cost_gate import set_default_gate

    cases = load_gold_set(args.gold_set)
    cells = [CELLS_BY_NAME[name] for name in _AB_CELL_NAMES]
    log.info(
        "fre771_powered_ab_started",
        run_id=args.run_id,
        cells=[c.name for c in cells],
        cases=len(cases),
        samples=args.samples,
    )

    summary: dict[str, Any] = {
        "run_id": args.run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "gold_set": args.gold_set,
        "samples": args.samples,
        "cells": [c.name for c in cells],
        "v2": None,
        "v1": None,
    }

    gate = await harness._with_cost_gate()
    try:
        summary["v2"] = await _run_phase(cells, cases, args.samples, args.fuzzy_threshold, "v2")
        _write_summary(summary, args.run_id, args.out)
        log.info("fre771_v2_phase_complete", run_id=args.run_id)
        with v1_prompt_template_active():
            summary["v1"] = await _run_phase(cells, cases, args.samples, args.fuzzy_threshold, "v1")
        _write_summary(summary, args.run_id, args.out)
    finally:
        await gate.disconnect()  # type: ignore[attr-defined]
        set_default_gate(None)

    return summary


def _parse_args() -> argparse.Namespace:
    """Parse the CLI arguments."""
    parser = argparse.ArgumentParser(description="FRE-771 powered A/B: V2 vs V1 taxonomy")
    parser.add_argument("--run-id", required=True, help="Run identifier")
    parser.add_argument("--gold-set", default=DEFAULT_GOLD_SET, help="Gold-set YAML")
    parser.add_argument("--samples", type=int, default=3, help="Samples per case")
    parser.add_argument(
        "--fuzzy-threshold", type=float, default=DEFAULT_FUZZY_THRESHOLD, help="Matcher threshold"
    )
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output directory (gitignored)")
    return parser.parse_args()


def main() -> None:
    """CLI entry point."""
    args = _parse_args()
    summary = asyncio.run(_run(args))
    summary_path = _write_summary(summary, args.run_id, args.out)
    log.info("fre771_powered_ab_complete", run_id=args.run_id, summary=str(summary_path))


if __name__ == "__main__":
    main()
