"""FRE-488 — structured run report for the memory-recall harness (ADR-0087 §D3).

Holds the per-case (:class:`CaseResult`) and aggregate (:class:`RunReport`)
structures the harness fills, plus pure aggregation and JSON/markdown rendering.
The run meta stamps ``write_mode`` and ``embedding_backend`` so a degraded
keyword-only run (offline replay without an embedding model) is never misread as
the real vector pipeline (backend-aware truth-source, FRE-433/FRE-488).

Raw run dumps stay out of git — these render to the gitignored
``telemetry/evaluation/fre435-memory-recall/`` directory.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from scripts.eval.fre435_memory_recall.attribution import Hypothesis
from scripts.eval.fre435_memory_recall.metrics import (
    WriteOutcome,
    extraction_fire_rate,
    landing_rate,
    mean_optional,
)


@dataclass(frozen=True)
class CaseResult:
    """Scored result for one probe case.

    Attributes:
        case_id: The probe case id.
        tags: Probe tags (e.g. ``pedagogical``).
        relevant_count: Size of the expected-recall set.
        retrieved_ids: Ordered namespaced ids returned by the retrieval call.
        denied: Whether the system denied having prior context.
        recall_by_k: recall@k per swept ``k`` (``None`` when no relevant).
        precision_by_k: precision@k per swept ``k`` (always defined).
        reciprocal_rank: First-relevant reciprocal rank (``None`` when no relevant).
        ndcg_at_prod_k: nDCG at the production ``k`` (``None`` when no relevant).
        false_negative: ADR headline failure (``None`` when no relevant).
        retrieval_miss: recall@prod_k == 0 with non-empty retrieval (``None`` when
            no relevant).
        write_outcome: The write-path outcome for the case.
        failed: Whether the case failed overall.
        hypothesis: The attributed hypothesis.
    """

    case_id: str
    tags: tuple[str, ...]
    relevant_count: int
    retrieved_ids: tuple[str, ...]
    denied: bool
    recall_by_k: dict[int, float | None]
    precision_by_k: dict[int, float]
    reciprocal_rank: float | None
    ndcg_at_prod_k: float | None
    false_negative: bool | None
    retrieval_miss: bool | None
    write_outcome: WriteOutcome
    failed: bool
    hypothesis: Hypothesis


@dataclass(frozen=True)
class RunReport:
    """A full harness run.

    Attributes:
        run_id: Run identifier (tag in output).
        timestamp: ISO-8601 run timestamp (stamped by the harness).
        write_mode: ``replay`` (offline seed) or ``extract`` (real LLM).
        embedding_backend: ``real`` or ``zero-vector`` (keyword-only) — codex Q4.
        prod_k: The production cut-off the headline metrics key on.
        k_sweep: The ``k`` values swept.
        probe_set: Path/label of the probe set used.
        cases: Per-case results.
    """

    run_id: str
    timestamp: str
    write_mode: str
    embedding_backend: str
    prod_k: int
    k_sweep: tuple[int, ...]
    probe_set: str
    cases: tuple[CaseResult, ...]


@dataclass(frozen=True)
class RunAggregate:
    """Aggregate metrics over a :class:`RunReport`.

    Attributes:
        false_negative_rate: Headline ADR metric (``None`` if no scorable case).
        retrieval_miss_rate: recall@prod_k == 0 rate over scorable cases.
        recall_by_k: Mean recall per ``k`` (``None`` entries excluded).
        precision_by_k: Mean precision per ``k``.
        mrr: Mean reciprocal rank over scorable cases.
        mean_ndcg: Mean nDCG@prod_k over scorable cases.
        extraction_fire_rate: Write-path extraction fire rate.
        landing_rate: Write-path landing rate.
        hypothesis_counts: Count of each hypothesis over *failed* cases.
    """

    false_negative_rate: float | None
    retrieval_miss_rate: float | None
    recall_by_k: dict[int, float | None]
    precision_by_k: dict[int, float | None]
    mrr: float | None
    mean_ndcg: float | None
    extraction_fire_rate: float | None
    landing_rate: float | None
    hypothesis_counts: dict[str, int] = field(default_factory=dict)


def _rate(flags: list[bool | None]) -> float | None:
    """Fraction of ``True`` over the non-``None`` flags (``None`` if none present)."""
    return mean_optional([1.0 if f else 0.0 for f in flags if f is not None])


def aggregate(report: RunReport) -> RunAggregate:
    """Compute aggregate metrics over a run.

    Args:
        report: The completed run.

    Returns:
        The :class:`RunAggregate`.
    """
    cases = report.cases
    ks = sorted({k for c in cases for k in c.recall_by_k})
    recall_by_k = {k: mean_optional([c.recall_by_k.get(k) for c in cases]) for k in ks}
    precision_by_k = {k: mean_optional([c.precision_by_k.get(k) for c in cases]) for k in ks}
    failed_hyps = Counter(c.hypothesis.value for c in cases if c.failed)
    return RunAggregate(
        false_negative_rate=_rate([c.false_negative for c in cases]),
        retrieval_miss_rate=_rate([c.retrieval_miss for c in cases]),
        recall_by_k=recall_by_k,
        precision_by_k=precision_by_k,
        mrr=mean_optional([c.reciprocal_rank for c in cases]),
        mean_ndcg=mean_optional([c.ndcg_at_prod_k for c in cases]),
        extraction_fire_rate=extraction_fire_rate([c.write_outcome for c in cases]),
        landing_rate=landing_rate([c.write_outcome for c in cases]),
        hypothesis_counts=dict(failed_hyps),
    )


def _case_to_dict(case: CaseResult) -> dict[str, Any]:
    """Serialise a case to a JSON-friendly dict (enum -> value)."""
    payload = asdict(case)
    payload["hypothesis"] = case.hypothesis.value
    return payload


def render_json(report: RunReport) -> str:
    """Render the run as a structured JSON document.

    Args:
        report: The completed run.

    Returns:
        Indented JSON with ``meta``, ``aggregate``, and ``cases`` sections.
    """
    agg = aggregate(report)
    payload = {
        "meta": {
            "run_id": report.run_id,
            "timestamp": report.timestamp,
            "write_mode": report.write_mode,
            "embedding_backend": report.embedding_backend,
            "prod_k": report.prod_k,
            "k_sweep": list(report.k_sweep),
            "probe_set": report.probe_set,
        },
        "aggregate": asdict(agg),
        "cases": [_case_to_dict(c) for c in report.cases],
    }
    return json.dumps(payload, indent=2)


def _fmt(value: float | None) -> str:
    """Format an optional metric for the markdown table."""
    return "—" if value is None else f"{value:.2f}"


def render_markdown(report: RunReport) -> str:
    """Render an owner-readable markdown summary.

    Args:
        report: The completed run.

    Returns:
        Markdown: meta stamp, headline rollup, per-case table, hypothesis breakdown.
    """
    agg = aggregate(report)
    lines: list[str] = [
        f"# FRE-435 memory-recall run — {report.run_id}",
        "",
        f"- **probe_set**: `{report.probe_set}`",
        f"- **write_mode**: `{report.write_mode}`",
        f"- **embedding_backend**: `{report.embedding_backend}`"
        + ("  ⚠️ keyword-only (no vector search)" if report.embedding_backend != "real" else ""),
        f"- **prod_k**: {report.prod_k} · **k_sweep**: {list(report.k_sweep)}",
        f"- **timestamp**: {report.timestamp}",
        "",
        "## Headline",
        "",
        f"- **false-negative rate** (ADR §D1): **{_fmt(agg.false_negative_rate)}**",
        f"- retrieval-miss rate: {_fmt(agg.retrieval_miss_rate)}",
        f"- MRR: {_fmt(agg.mrr)} · mean nDCG@{report.prod_k}: {_fmt(agg.mean_ndcg)}",
        "- recall@k: " + ", ".join(f"@{k}={_fmt(v)}" for k, v in sorted(agg.recall_by_k.items())),
        f"- write-completeness: extraction-fire={_fmt(agg.extraction_fire_rate)}, "
        f"landing={_fmt(agg.landing_rate)}",
        "",
        "## Per-case",
        "",
        f"| case | tags | rel | recall@{report.prod_k} | RR | nDCG | FN | miss | landed | hypothesis |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in report.cases:
        prod_recall = c.recall_by_k.get(report.prod_k)
        lines.append(
            f"| {c.case_id} | {','.join(c.tags) or '-'} | {c.relevant_count} | "
            f"{_fmt(prod_recall)} | {_fmt(c.reciprocal_rank)} | {_fmt(c.ndcg_at_prod_k)} | "
            f"{'Y' if c.false_negative else ('-' if c.false_negative is None else 'n')} | "
            f"{'Y' if c.retrieval_miss else ('-' if c.retrieval_miss is None else 'n')} | "
            f"{c.write_outcome.entities_landed}/{c.write_outcome.entities_expected} | "
            f"{c.hypothesis.value} |"
        )
    lines += [
        "",
        "## Hypothesis breakdown (failed cases — ADR §D4)",
        "",
    ]
    if agg.hypothesis_counts:
        for hyp, count in sorted(agg.hypothesis_counts.items()):
            lines.append(f"- **{hyp}**: {count}")
    else:
        lines.append("- (no failed cases)")
    return "\n".join(lines)
