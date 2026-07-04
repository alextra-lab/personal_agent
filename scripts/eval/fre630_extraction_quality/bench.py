r"""FRE-766 — model × reasoning extraction benchmark driver (parallel by model).

Drives the *real* extractor across the owner-specified 5-cell matrix (+ a mini@medium
baseline) on the 36-case FRE-630 gold set, via the ``ExtractionModelOverride`` DI seam.
Cells run **concurrently, one worker per model** (``asyncio.gather``), each worker
running its cases **serially** — so per-call latency is production-faithful (no
self-concurrency) while the models overlap for ~5× wall-clock, and there is no
global-config mutation (concurrency-safe).

This is a DATA-gathering benchmark (measure-don't-assert): it records per-cell quality
(the FRE-630 metrics + case-level claim recall), std bands, cost (usage×cell-rates),
latency, and reasoning-token counts. It does NOT pick a winner or change prod config —
the production choice is deferred (batch + DSPy + cost analysis).

Usage::

    make test-infra-up   # isolated cost substrate (Postgres :5433)
    uv run python -m scripts.eval.fre630_extraction_quality.bench \
        --run-id fre766-$(date +%Y%m%d) --samples 3            # all cells + baseline

Output lands in the gitignored ``telemetry/evaluation/fre630-extraction-quality/``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

# Importing the harness runs its module-level test-substrate env setdefault (FRE-375)
# and gives us the shared cost-gate + provenance helpers.
from scripts.eval.fre630_extraction_quality import harness
from scripts.eval.fre630_extraction_quality.cells import (
    BASELINE_CELL,
    CELLS,
    ExtractionModelCell,
    baseline_compatible,
    classify_smoke,
    cost_usd,
)
from scripts.eval.fre630_extraction_quality.gold import GOLD_SCHEMA_VERSION, GoldCase, load_gold_set
from scripts.eval.fre630_extraction_quality.matching import DEFAULT_FUZZY_THRESHOLD, MATCHER_VERSION
from scripts.eval.fre630_extraction_quality.report import (
    CaseRun,
    RunMeta,
    RunReport,
    aggregate,
    claim_case_recall,
    render_json,
    render_markdown,
)
from scripts.eval.fre630_extraction_quality.scoring import score_case

from personal_agent.second_brain import entity_extraction

log = structlog.get_logger(__name__)

DEFAULT_GOLD_SET = harness.DEFAULT_GOLD_SET
DEFAULT_OUT = harness.DEFAULT_OUT


def _register_cell_pricing(cell: ExtractionModelCell) -> None:
    """Register the cell's per-token pricing into ``litellm.model_cost`` (codex P0.2).

    App startup registers config prices; the standalone harness does not, so the
    ADR-0065 cost-gate reservation would read zero for a model absent from the table.
    Registered under both the bare and provider-prefixed keys so whichever the
    estimator uses resolves. (The *reported* benchmark cost is still computed directly
    from usage×rates in :func:`_run_cell_case` — this only keeps the gate honest.)

    Args:
        cell: The cell whose pricing to register.
    """
    import litellm

    entry = {
        "input_cost_per_token": cell.input_rate,
        "output_cost_per_token": cell.output_rate,
    }
    for key in (cell.override.model_id, f"{cell.override.provider}/{cell.override.model_id}"):
        existing = dict(litellm.model_cost.get(key, {}))
        existing.update(entry)
        litellm.model_cost[key] = existing


def _cell_meta(
    run_id: str, gold_set: str, samples: int, fuzzy_threshold: float, cell: ExtractionModelCell
) -> RunMeta:
    """Build the run stamp FROM the cell object, not from global config (codex P0.1).

    Args:
        run_id: Output run id.
        gold_set: Gold-set path/label.
        samples: Samples per case.
        fuzzy_threshold: Tier-3 matcher threshold.
        cell: The active cell.

    Returns:
        A :class:`RunMeta` whose ``extractor_model`` is asserted to equal the cell's model.
    """
    meta = RunMeta(
        run_id=run_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        gold_set=gold_set,
        extractor_model=cell.override.model_id,
        entity_extraction_role="entity_extraction",
        provider=cell.override.provider,
        model_config_path="<override>",
        git_commit=harness._git_commit(),
        prompt_hash=harness._prompt_hash(),
        matcher_version=MATCHER_VERSION,
        gold_schema_version=GOLD_SCHEMA_VERSION,
        samples=samples,
        fuzzy_threshold=fuzzy_threshold,
        reasoning_effort=cell.override.reasoning_effort,
        cell_name=cell.name,
    )
    assert meta.extractor_model == cell.override.model_id, "RunMeta mis-stamped the cell model"
    return meta


async def _run_cell_case(
    case: GoldCase, cell: ExtractionModelCell, samples: int, fuzzy_threshold: float
) -> tuple[CaseRun, list[dict[str, Any]]]:
    """Run one case ``samples`` times through the cell's model; score + capture resources.

    Args:
        case: The gold case.
        cell: The active cell (its override drives the extractor).
        samples: Samples for this case.
        fuzzy_threshold: Matcher threshold.

    Returns:
        The scored :class:`CaseRun` plus one resource dict per sample
        (latency_ms, cost_usd, reasoning_tokens, error_class).
    """
    scores = []
    resources: list[dict[str, Any]] = []
    for _ in range(samples):
        # The by-model gather runs 5 cost-gate reservers concurrently against the same
        # entity_extraction budget row → the test Postgres can raise a transient
        # DeadlockDetectedError (the extractor swallows it into error_class). Retry the
        # sample a few times so an infra deadlock never masquerades as a quality miss.
        stats: list[dict[str, Any]] = []
        result: dict[str, Any] = {}
        latency_ms = 0
        for attempt in range(4):
            stats = []
            started = time.perf_counter()
            result = await entity_extraction.extract_entities_and_relationships(
                case.source_user,
                case.source_assistant,
                model_override=cell.override,
                call_stats_sink=stats,
            )
            latency_ms = int((time.perf_counter() - started) * 1000)  # final attempt's latency
            err = (stats[-1].get("error_class") if stats else None) or ""
            if "Deadlock" not in err:
                break
            await asyncio.sleep(0.25 * (attempt + 1))
        scores.append(
            score_case(case, result, fuzzy_threshold=fuzzy_threshold, entity_type_field="v2")
        )
        stat = stats[-1] if stats else {}
        usage = stat.get("usage") or {}
        resources.append(
            {
                "latency_ms": latency_ms,
                "cost_usd": cost_usd(
                    cell,
                    int(usage.get("prompt_tokens") or 0),
                    int(usage.get("completion_tokens") or 0),
                ),
                "reasoning_tokens": stat.get("reasoning_tokens"),
                "error_class": stat.get("error_class"),
            }
        )
    return CaseRun(case_id=case.case_id, tags=case.tags, samples=tuple(scores)), resources


def _summarise_resources(resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the per-sample resource dicts into a cell resource summary."""
    latencies = sorted(r["latency_ms"] for r in resources)
    costs = [r["cost_usd"] for r in resources]
    reasoning = [r["reasoning_tokens"] for r in resources if r["reasoning_tokens"] is not None]
    n = len(latencies)
    return {
        "calls": n,
        "cost_usd_total": round(sum(costs), 6),
        "cost_usd_mean": round(sum(costs) / n, 8) if n else None,
        "latency_ms_p50": latencies[n // 2] if n else None,
        "latency_ms_mean": int(sum(latencies) / n) if n else None,
        "reasoning_tokens_mean": (round(sum(reasoning) / len(reasoning), 1) if reasoning else None),
    }


async def _run_cell(
    cell: ExtractionModelCell,
    cases: list[GoldCase],
    samples: int,
    fuzzy_threshold: float,
    out_dir: str,
    smoke_only: bool = False,
    gold_set: str = DEFAULT_GOLD_SET,
) -> dict[str, Any]:
    """Smoke-gate then fully run one cell; write its report; return a cell summary.

    Args:
        cell: The cell to run.
        cases: The gold cases (full set).
        samples: Samples per case.
        fuzzy_threshold: Matcher threshold.
        out_dir: Output directory.
        smoke_only: When True, run only the smoke classification (1 case) and return —
            the cheap pre-flight that verifies the cell resolves + honours the contract
            before any full-spend run.
        gold_set: The gold-set path stamped into this cell's ``RunMeta`` (codex P1.1).

    Returns:
        A cell-summary dict (name, model, reasoning, smoke_class, quality aggregate,
        claim-case recall, resource summary). If the smoke gate fails, the full run is
        skipped and the summary records the failure class (codex P1.4).
    """
    _register_cell_pricing(cell)

    # Smoke: run the first case once and classify the raw result before committing the
    # full run — a provider rejection / schema violation is a finding, not a wasted
    # 108-call quality-zero (codex P1.4).
    smoke_stats: list[dict[str, Any]] = []
    smoke_result = await entity_extraction.extract_entities_and_relationships(
        cases[0].source_user,
        cases[0].source_assistant,
        model_override=cell.override,
        call_stats_sink=smoke_stats,
    )
    smoke_class = classify_smoke(smoke_result, smoke_stats)
    log.info("fre766_cell_smoke", cell=cell.name, smoke_class=smoke_class)
    if smoke_only or smoke_class != "ok":
        return {
            "cell": cell.name,
            "model": cell.override.model_id,
            "reasoning_effort": cell.override.reasoning_effort,
            "status": "smoke_ok" if (smoke_only and smoke_class == "ok") else "smoke_failed",
            "smoke_class": smoke_class,
        }

    # Full run — serial within the cell (production-faithful latency).
    runs: list[CaseRun] = []
    all_resources: list[dict[str, Any]] = []
    for case in cases:
        run, res = await _run_cell_case(case, cell, samples, fuzzy_threshold)
        runs.append(run)
        all_resources.extend(res)

    report = RunReport(
        meta=_cell_meta(f"{cell.name}", gold_set, samples, fuzzy_threshold, cell),
        cases=tuple(runs),
    )
    _write_cell_report(report, out_dir, cell.name)

    agg = aggregate([s for c in report.cases for s in c.samples])
    ccr = claim_case_recall(report)
    return {
        "cell": cell.name,
        "model": cell.override.model_id,
        "reasoning_effort": cell.override.reasoning_effort,
        "status": "ok",
        "smoke_class": smoke_class,
        "prompt_hash": report.meta.prompt_hash,
        "entity_type_accuracy": _ms(agg.get("entity_type_accuracy")),
        "claim_case_level_recall": {
            "passing": ccr.passing,
            "total": ccr.total,
            "fraction": ccr.fraction,
        },
        "knowledge_class_accuracy": _ms(agg.get("knowledge_class_accuracy")),
        "hallucination_rate": _ms(agg.get("hallucination_rate")),
        "dedup_convergence": _ms(agg.get("dedup_convergence")),
        "stance_emission_recall": _ms(agg.get("stance_emission_recall")),
        "relationship_type_correctness": _ms(agg.get("relationship_type_correctness")),
        "empty_fallback_rate": _ms(agg.get("empty_fallback_rate")),
        "resources": _summarise_resources(all_resources),
    }


def _ms(ms: Any) -> dict[str, Any] | None:
    """Serialise a MeanStd (or None) to a mean/std dict."""
    if ms is None:
        return None
    return {"mean": ms.mean, "std": ms.std, "n": ms.n}


def _write_cell_report(report: RunReport, out_dir: str, cell_name: str) -> None:
    """Write a cell's per-run JSON + markdown (gitignored)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{report.meta.run_id}.json").write_text(render_json(report), encoding="utf-8")
    (out / f"{report.meta.run_id}.md").write_text(render_markdown(report), encoding="utf-8")


def _current_baseline_meta(samples: int, fuzzy_threshold: float, gold_set: str) -> dict[str, Any]:
    """The metadata this environment would stamp for the mini@none baseline row."""
    return {
        "gold_schema_version": GOLD_SCHEMA_VERSION,
        "matcher_version": MATCHER_VERSION,
        "prompt_hash": harness._prompt_hash(),
        "samples": samples,
        "fuzzy_threshold": fuzzy_threshold,
        "gold_set": gold_set,
    }


def _load_baseline_row(
    baseline_json: str, samples: int, fuzzy_threshold: float, gold_set: str
) -> dict[str, Any] | None:
    """Reuse a prior baseline run as the mini@none row iff compatible (codex P1.3).

    Args:
        baseline_json: Path to the prior run's JSON (e.g. the FRE-759 flag-OFF run).
        samples: This run's sample count.
        fuzzy_threshold: This run's matcher threshold.
        gold_set: This run's gold-set path (compat also gates on dataset identity).

    Returns:
        A baseline cell-summary dict if the stored run is compatible, else ``None``
        (the caller then re-runs mini@none in-environment).
    """
    path = Path(baseline_json)
    if not path.exists():
        return None
    stored = json.loads(path.read_text(encoding="utf-8"))
    if not baseline_compatible(
        stored.get("meta", {}), _current_baseline_meta(samples, fuzzy_threshold, gold_set)
    ):
        log.warning("fre766_baseline_incompatible", baseline=baseline_json)
        return None
    agg = stored.get("aggregate", {})
    ccr = stored.get("claim_case_level_recall", {})
    return {
        "cell": "mini-none",
        "model": "gpt-5.4-mini",
        "reasoning_effort": None,
        "status": "reused",
        "source": baseline_json,
        "entity_type_accuracy": agg.get("entity_type_accuracy"),
        "claim_case_level_recall": ccr,
        "prompt_hash": stored.get("meta", {}).get("prompt_hash"),
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    """Run all cells concurrently (by model) + resolve the baseline row."""
    from personal_agent.cost_gate import set_default_gate

    cases = load_gold_set(args.gold_set)
    log.info(
        "fre766_started",
        run_id=args.run_id,
        cells=len(CELLS),
        cases=len(cases),
        samples=args.samples,
    )

    gate = await harness._with_cost_gate()
    try:
        # Parallelize BY MODEL: one worker per cell, serial within each.
        cell_summaries = await asyncio.gather(
            *[
                _run_cell(
                    c,
                    cases,
                    args.samples,
                    args.fuzzy_threshold,
                    args.out,
                    args.smoke_only,
                    args.gold_set,
                )
                for c in CELLS
            ]
        )
        baseline: dict[str, Any] | None
        if args.smoke_only:
            baseline = None  # baseline is skipped in the pre-flight smoke pass
        else:
            # Baseline: gated reuse, else re-run mini@none in-environment.
            baseline = _load_baseline_row(
                args.baseline_json, args.samples, args.fuzzy_threshold, args.gold_set
            )
            if baseline is None:
                log.info("fre766_baseline_rerun")
                baseline = await _run_cell(
                    BASELINE_CELL,
                    cases,
                    args.samples,
                    args.fuzzy_threshold,
                    args.out,
                    gold_set=args.gold_set,
                )
    finally:
        await gate.disconnect()  # type: ignore[attr-defined]
        set_default_gate(None)

    return {
        "run_id": args.run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "gold_set": args.gold_set,
        "samples": args.samples,
        "baseline": baseline,
        "cells": list(cell_summaries),
    }


def _parse_args() -> argparse.Namespace:
    """Parse the CLI arguments."""
    parser = argparse.ArgumentParser(description="FRE-766 model×reasoning extraction benchmark")
    parser.add_argument("--run-id", required=True, help="Run identifier")
    parser.add_argument("--gold-set", default=DEFAULT_GOLD_SET, help="Gold-set YAML")
    parser.add_argument("--samples", type=int, default=3, help="Samples per case")
    parser.add_argument(
        "--smoke-only",
        action="store_true",
        help="Pre-flight only: 1 case per cell, classify, skip the full run + baseline.",
    )
    parser.add_argument(
        "--fuzzy-threshold", type=float, default=DEFAULT_FUZZY_THRESHOLD, help="Matcher threshold"
    )
    parser.add_argument(
        "--baseline-json",
        default="telemetry/evaluation/fre630-extraction-quality/fre759-baseline-off-20260703.json",
        help="Prior mini@medium run to reuse if compatible; else mini@medium is re-run.",
    )
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output directory (gitignored)")
    return parser.parse_args()


def main() -> None:
    """CLI entry point."""
    args = _parse_args()
    summary = asyncio.run(_run(args))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary_path = out / f"{args.run_id}-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log.info("fre766_complete", run_id=args.run_id, summary=str(summary_path))


if __name__ == "__main__":
    main()
