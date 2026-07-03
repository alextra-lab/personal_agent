"""FRE-630 — structured run report for the extraction-quality benchmark.

Holds the run metadata stamp (:class:`RunMeta`), the per-case sampled results
(:class:`CaseRun`), pure aggregation over samples (mean/std stability bands, codex
P1.4), a per-tag breakdown (codex P1.5), and JSON/markdown rendering. The metadata
stamps ``extractor_model`` / ``provider`` / ``prompt_hash`` / ``git_commit`` /
``matcher_version`` / ``gold_schema_version`` so a run is never silently compared
across a different model, prompt, or scoring revision (FRE-433 backend-aware truth).

Raw run dumps stay out of git — these render to the gitignored
``telemetry/evaluation/fre630-extraction-quality/`` directory.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass

from scripts.eval.fre630_extraction_quality.metrics import (
    ClaimCaseRecall,
    MeanStd,
    claim_case_level_recall,
    mean_std,
)
from scripts.eval.fre630_extraction_quality.scoring import CaseScore

#: The metric accessors aggregated in the report, in display order. Each maps a
#: :class:`CaseScore` to a scalar ``float | None`` (``None`` == excluded from aggregate).
METRIC_ACCESSORS: dict[str, Callable[[CaseScore], float | None]] = {
    "entity_precision": lambda s: s.entity.precision,
    "entity_recall": lambda s: s.entity.recall,
    "entity_f1": lambda s: s.entity.f1,
    "entity_type_accuracy": lambda s: s.entity_type_accuracy,
    "knowledge_class_accuracy": lambda s: s.knowledge_class_accuracy,
    "relationship_precision": lambda s: s.relationship.precision,
    "relationship_recall": lambda s: s.relationship.recall,
    "relationship_f1": lambda s: s.relationship.f1,
    "relationship_type_correctness": lambda s: s.relationship_type_correctness,
    "hallucination_rate": lambda s: s.hallucination_rate,
    "forbidden_edge_type_rate": lambda s: s.forbidden_edge_type_rate,
    "dedup_convergence": lambda s: s.dedup_convergence,
    "description_integrity": lambda s: s.description_integrity,
    "stance_emission_recall": lambda s: s.stance_emission_recall,
    "claim_emission_recall": lambda s: s.claim_emission_recall,
    "empty_fallback_rate": lambda s: 1.0 if s.is_empty_fallback else 0.0,
}


@dataclass(frozen=True)
class RunMeta:
    """Immutable provenance stamp for a benchmark run.

    Attributes:
        run_id: Run identifier.
        timestamp: ISO-8601 run timestamp (stamped by the harness, not the core).
        gold_set: Path/label of the gold set used.
        extractor_model: The resolved extractor model id (e.g. ``gpt-5.4-mini``).
        entity_extraction_role: The config role name that resolved it.
        provider: ``cloud`` provider name or ``local`` for the SLM path.
        model_config_path: The active model-config file the extractor loaded.
        git_commit: HEAD commit the run was produced at.
        prompt_hash: Short hash of the extraction system+user prompt templates.
        matcher_version: The tiered-matcher revision.
        gold_schema_version: The gold-case schema revision.
        samples: Extraction samples per case (``N`` for the stability band).
        fuzzy_threshold: The tier-3 matcher threshold used.
    """

    run_id: str
    timestamp: str
    gold_set: str
    extractor_model: str
    entity_extraction_role: str
    provider: str
    model_config_path: str
    git_commit: str
    prompt_hash: str
    matcher_version: str
    gold_schema_version: str
    samples: int
    fuzzy_threshold: float


@dataclass(frozen=True)
class CaseRun:
    """All sampled scores for one gold case.

    Attributes:
        case_id: The case id.
        tags: The case tags.
        samples: One :class:`CaseScore` per extraction sample.
    """

    case_id: str
    tags: tuple[str, ...]
    samples: tuple[CaseScore, ...]


@dataclass(frozen=True)
class RunReport:
    """A completed benchmark run.

    Attributes:
        meta: The provenance stamp.
        cases: Per-case sampled results.
    """

    meta: RunMeta
    cases: tuple[CaseRun, ...]


def _all_scores(report: RunReport) -> list[CaseScore]:
    """Flatten every sampled score across all cases."""
    return [s for c in report.cases for s in c.samples]


def aggregate(scores: Sequence[CaseScore]) -> dict[str, MeanStd]:
    """Mean/std per metric over a flat set of case scores (codex P1.4).

    Args:
        scores: The case scores to aggregate.

    Returns:
        Metric name → :class:`MeanStd` over its present values.
    """
    return {name: mean_std([acc(s) for s in scores]) for name, acc in METRIC_ACCESSORS.items()}


def aggregate_by_tag(report: RunReport) -> dict[str, dict[str, MeanStd]]:
    """Per-tag aggregate breakdown (codex P1.5).

    Args:
        report: The completed run.

    Returns:
        tag → (metric name → :class:`MeanStd`) over scores whose case carried the tag.
    """
    tags = sorted({t for c in report.cases for t in c.tags})
    out: dict[str, dict[str, MeanStd]] = {}
    for tag in tags:
        tag_scores = [s for c in report.cases if tag in c.tags for s in c.samples]
        out[tag] = aggregate(tag_scores)
    return out


def claim_case_recall(report: RunReport) -> ClaimCaseRecall:
    """Case-level claim-emission recall over the run's distinct claim cases (FRE-759).

    Adapts each :class:`CaseRun` to its per-sample ``claim_emission_recall`` values
    and defers to the pure :func:`claim_case_level_recall`, so AC-2 reads as
    "N of M distinct claim cases pass", never the sample-flattened aggregate.

    Args:
        report: The completed run.

    Returns:
        The passing/total counts and their fraction over distinct claim cases.
    """
    per_case = [[s.claim_emission_recall for s in c.samples] for c in report.cases]
    return claim_case_level_recall(per_case)


def _ms_to_dict(ms: MeanStd) -> dict[str, float | int | None]:
    """Serialise a :class:`MeanStd`."""
    return {"mean": ms.mean, "std": ms.std, "n": ms.n}


def render_json(report: RunReport) -> str:
    """Render the run as a structured JSON document.

    Args:
        report: The completed run.

    Returns:
        Indented JSON with ``meta``, ``aggregate``, ``by_tag``, and ``cases``.
    """
    overall = aggregate(_all_scores(report))
    by_tag = aggregate_by_tag(report)
    ccr = claim_case_recall(report)
    payload = {
        "meta": asdict(report.meta),
        "aggregate": {k: _ms_to_dict(v) for k, v in overall.items()},
        "claim_case_level_recall": {
            "passing": ccr.passing,
            "total": ccr.total,
            "fraction": ccr.fraction,
        },
        "by_tag": {tag: {k: _ms_to_dict(v) for k, v in m.items()} for tag, m in by_tag.items()},
        "cases": [
            {
                "case_id": c.case_id,
                "tags": list(c.tags),
                "samples": [_score_to_dict(s) for s in c.samples],
            }
            for c in report.cases
        ],
    }
    return json.dumps(payload, indent=2)


def _score_to_dict(score: CaseScore) -> dict[str, object]:
    """Serialise a :class:`CaseScore` (dataclass → JSON-friendly dict)."""
    payload = asdict(score)
    # PRF nested dataclasses already serialise via asdict; diffs are plain lists.
    return payload


def _fmt(ms: MeanStd) -> str:
    """Format a metric's mean(±std) for the markdown table."""
    if ms.mean is None:
        return "—"
    if ms.std is None or ms.n < 2:
        return f"{ms.mean:.2f}"
    return f"{ms.mean:.2f}±{ms.std:.2f}"


def render_markdown(report: RunReport) -> str:
    """Render an owner-readable markdown summary.

    Args:
        report: The completed run.

    Returns:
        Markdown: provenance stamp, headline aggregate, per-tag breakdown, per-case diffs.
    """
    m = report.meta
    overall = aggregate(_all_scores(report))
    lines: list[str] = [
        f"# FRE-630 extraction-quality run — {m.run_id}",
        "",
        "> **Pre-write** benchmark: scores the extractor's output dict, not the graph.",
        "> **Calibration/regression set — not statistically powered** (codex P1.3): read",
        "> per-tag and per-case, treat small aggregate deltas as noise.",
        "",
        f"- **gold_set**: `{m.gold_set}` · **cases**: {len(report.cases)} · **samples/case**: {m.samples}",
        f"- **extractor_model**: `{m.extractor_model}` · **role**: `{m.entity_extraction_role}` · **provider**: `{m.provider}`",
        f"- **model_config**: `{m.model_config_path}`",
        f"- **git_commit**: `{m.git_commit}` · **prompt_hash**: `{m.prompt_hash}`",
        f"- **matcher**: `{m.matcher_version}` · **gold_schema**: `{m.gold_schema_version}` · **fuzzy_threshold**: {m.fuzzy_threshold}",
        f"- **timestamp**: {m.timestamp}",
        "",
        "## Aggregate (mean±std over all sampled cases)",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    lines += [f"| {name} | {_fmt(overall[name])} |" for name in METRIC_ACCESSORS]
    ccr = claim_case_recall(report)
    ccr_str = f"{ccr.passing}/{ccr.total} distinct claim cases pass" + (
        f" ({ccr.fraction:.2f})" if ccr.fraction is not None else " (—)"
    )
    lines += [
        "",
        f"**claim_case_level_recall (FRE-759, AC-2 — distinct cases, not samples):** {ccr_str}",
        "",
        "## Per-tag (entity_f1 · rel_type_correctness · hallucination · empty_fallback)",
        "",
    ]
    lines += ["| tag | entity_f1 | rel_type_corr | halluc | empty_fb |", "|---|---|---|---|---|"]
    for tag, agg in aggregate_by_tag(report).items():
        lines.append(
            f"| {tag} | {_fmt(agg['entity_f1'])} | {_fmt(agg['relationship_type_correctness'])} "
            f"| {_fmt(agg['hallucination_rate'])} | {_fmt(agg['empty_fallback_rate'])} |"
        )
    lines += ["", "## Per-case diffs (first sample)", ""]
    for c in report.cases:
        first = c.samples[0] if c.samples else None
        if first is None or not first.diffs:
            lines.append(f"- **{c.case_id}** — clean")
            continue
        diff_str = "; ".join(f"{k}={v}" for k, v in first.diffs.items())
        lines.append(f"- **{c.case_id}** — {diff_str}")
    return "\n".join(lines)
