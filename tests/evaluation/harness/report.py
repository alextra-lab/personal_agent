"""Report generator for evaluation results.

Produces JSON (machine-readable) and markdown (human-readable) reports
from PathResult data.
"""

from __future__ import annotations

import json
import statistics as _stats
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from tests.evaluation.harness.models import PathResult


def generate_json_report(
    results: Sequence[PathResult],
    output_path: Path | None = None,
) -> dict[str, object]:
    """Generate a JSON report from evaluation results.

    Args:
        results: List of PathResult from runner.
        output_path: Optional path to write JSON file.

    Returns:
        Report dictionary.
    """
    total_assertions = sum(r.total_assertions for r in results)
    passed_assertions = sum(r.passed_assertions for r in results)
    paths_passed = sum(1 for r in results if r.all_assertions_passed)

    report: dict[str, object] = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "summary": {
            "total_paths": len(results),
            "paths_passed": paths_passed,
            "paths_failed": len(results) - paths_passed,
            "path_pass_rate": (paths_passed / len(results) if results else 0.0),
            "total_assertions": total_assertions,
            "assertions_passed": passed_assertions,
            "assertions_failed": total_assertions - passed_assertions,
            "assertion_pass_rate": (
                passed_assertions / total_assertions if total_assertions > 0 else 0.0
            ),
            "total_response_time_ms": sum(r.total_time_ms for r in results),
            "avg_turn_response_time_ms": (
                sum(r.total_time_ms for r in results) / sum(len(r.turns) for r in results)
                if any(r.turns for r in results)
                else 0.0
            ),
        },
        "by_category": _group_by_category(results),
        "prompt_version_summary": _build_prompt_version_summary(results),
        "paths": [_serialize_path(r) for r in results],
    }

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, default=str))

    return report


def generate_markdown_report(
    results: Sequence[PathResult],
    output_path: Path | None = None,
) -> str:
    """Generate a markdown report from evaluation results.

    Args:
        results: List of PathResult from runner.
        output_path: Optional path to write markdown file.

    Returns:
        Markdown string.
    """
    total_assertions = sum(r.total_assertions for r in results)
    passed_assertions = sum(r.passed_assertions for r in results)
    paths_passed = sum(1 for r in results if r.all_assertions_passed)

    lines: list[str] = []
    lines.append("# Evaluation Results Report")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now(tz=timezone.utc).isoformat()}")
    lines.append("")

    # Summary
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Paths Passed | {paths_passed}/{len(results)} |")
    lines.append(f"| Assertions Passed | {passed_assertions}/{total_assertions} |")
    rate = passed_assertions / total_assertions * 100 if total_assertions > 0 else 0.0
    lines.append(f"| Assertion Pass Rate | {rate:.1f}% |")
    avg_ms = (
        sum(r.total_time_ms for r in results) / sum(len(r.turns) for r in results)
        if any(r.turns for r in results)
        else 0.0
    )
    lines.append(f"| Avg Response Time | {avg_ms:.0f} ms |")
    lines.append("")

    # Prompt version summary (ADR-0078 P4 / FRE-408)
    pv_rows = _build_prompt_version_summary(results)
    lines.extend(_render_prompt_version_summary_md(pv_rows))

    # Results by category
    categories = _group_by_category(results)
    lines.append("## Results by Category")
    lines.append("")
    lines.append("| Category | Passed | Failed | Pass Rate |")
    lines.append("|----------|--------|--------|-----------|")
    for cat_name, cat_data in categories.items():
        p = cat_data["passed"]
        t = cat_data["total"]
        pct = p / t * 100 if t > 0 else 0.0
        lines.append(f"| {cat_name} | {p} | {t - p} | {pct:.0f}% |")
    lines.append("")

    # Per-path details
    lines.append("## Path Details")
    lines.append("")
    for r in results:
        status = "✅" if r.all_assertions_passed else "❌"
        lines.append(f"### {status} {r.path_id}: {r.path_name}")
        lines.append("")
        lines.append(f"**Category:** {r.category} | **Session:** `{r.session_id}`")
        lines.append(f"**Assertions:** {r.passed_assertions}/{r.total_assertions} passed")
        lines.append("")

        for turn in r.turns:
            lines.append(f"**Turn {turn.turn_index + 1}** ({turn.response_time_ms:.0f} ms)")
            msg = turn.user_message
            display = msg[:100] + "..." if len(msg) > 100 else msg
            lines.append(f"- **Sent:** {display}")
            lines.append(f"- **Trace:** `{turn.trace_id}`")

            for a in turn.assertion_results:
                icon = "✅" if a.passed else "❌"
                lines.append(f"  - {icon} {a.message}")
            lines.append("")

        # Post-path assertions (Neo4j)
        if r.post_path_assertion_results:
            lines.append("**Post-Path Assertions (Neo4j):**")
            for a in r.post_path_assertion_results:
                icon = "✅" if a.passed else "❌"
                lines.append(f"  - {icon} {a.message}")
            lines.append("")

        # Quality criteria (for human eval)
        if r.quality_criteria:
            lines.append("**Quality Criteria (Human Eval):**")
            for criterion in r.quality_criteria:
                lines.append(f"- [ ] {criterion}")
            lines.append("")

        lines.append("---")
        lines.append("")

    report_text = "\n".join(lines)

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report_text)

    return report_text


def _build_prompt_version_summary(
    results: Sequence[PathResult],
) -> list[dict[str, object]]:
    """Build per-static_prefix_hash rating statistics across all turns.

    Groups turns by ``static_prefix_hash``, computes mean/median/p25/p75 for
    any non-null ratings, and returns rows sorted descending by ``n_turns``.
    Ratings are ``None`` until P3 (FRE-406) ships; the table is still rendered
    so A/B prompt version buckets are visible in the eval report immediately.

    Args:
        results: PathResult list to aggregate.

    Returns:
        List of bucket dicts. Each dict has keys: ``static_prefix_hash``,
        ``callsite``, ``n_turns``, ``n_rated``, ``mean_rating``,
        ``median_rating``, ``p25``, ``p75``.
    """
    # hash -> (first_callsite_seen, list_of_ratings_or_None)
    buckets: dict[str, tuple[str, list[int | None]]] = {}

    for r in results:
        for t in r.turns:
            key = t.prompt_static_prefix_hash or "(unknown)"
            callsite = t.prompt_callsite or "(unknown)"
            if key not in buckets:
                buckets[key] = (callsite, [])
            buckets[key][1].append(t.rating)

    rows: list[dict[str, object]] = []
    for hash_key, (callsite, all_ratings) in buckets.items():
        rated = [r for r in all_ratings if r is not None]
        row: dict[str, object] = {
            "static_prefix_hash": hash_key,
            "callsite": callsite,
            "n_turns": len(all_ratings),
            "n_rated": len(rated),
            "mean_rating": None,
            "median_rating": None,
            "p25": None,
            "p75": None,
        }
        if rated:
            row["mean_rating"] = round(_stats.mean(rated), 1)
            row["median_rating"] = float(_stats.median(rated))
            if len(rated) >= 2:
                q = _stats.quantiles(rated, n=4)
                row["p25"] = float(q[0])
                row["p75"] = float(q[2])
            else:
                row["p25"] = float(rated[0])
                row["p75"] = float(rated[0])
        rows.append(row)

    rows.sort(key=lambda x: cast(int, x["n_turns"]), reverse=True)
    return rows


def _render_prompt_version_summary_md(rows: list[dict[str, object]]) -> list[str]:
    """Render the prompt version summary table as markdown lines.

    Args:
        rows: Output of _build_prompt_version_summary.

    Returns:
        Lines forming a markdown section.
    """
    lines: list[str] = ["## Prompt Version Summary", ""]
    if not rows:
        lines.append("_No prompt identity data recorded._")
        lines.append("")
        return lines

    lines.append(
        "| static_prefix_hash | callsite | n_turns | n_rated | mean | median | p25 | p75 |"
    )
    lines.append(
        "|---------------------|----------|---------|---------|------|--------|-----|-----|"
    )

    def _fmt(v: object) -> str:
        if v is None:
            return "—"
        if isinstance(v, float):
            return f"{v:.1f}"
        return str(v)

    for row in rows:
        lines.append(
            f"| `{row['static_prefix_hash']}` "
            f"| {row['callsite']} "
            f"| {row['n_turns']} "
            f"| {row['n_rated']} "
            f"| {_fmt(row['mean_rating'])} "
            f"| {_fmt(row['median_rating'])} "
            f"| {_fmt(row['p25'])} "
            f"| {_fmt(row['p75'])} |"
        )
    lines.append("")
    return lines


def _group_by_category(
    results: Sequence[PathResult],
) -> dict[str, dict[str, int]]:
    """Group results by category with pass/fail counts.

    Args:
        results: List of PathResult to group.

    Returns:
        Dict mapping category name to {"total": int, "passed": int}.
    """
    categories: dict[str, dict[str, int]] = {}
    for r in results:
        if r.category not in categories:
            categories[r.category] = {"total": 0, "passed": 0}
        categories[r.category]["total"] += 1
        if r.all_assertions_passed:
            categories[r.category]["passed"] += 1
    return categories


def _serialize_path(r: PathResult) -> dict[str, object]:
    """Serialize a PathResult to a JSON-compatible dict.

    Args:
        r: PathResult to serialize.

    Returns:
        JSON-serializable dict representation.
    """
    return {
        "path_id": r.path_id,
        "path_name": r.path_name,
        "category": r.category,
        "session_id": r.session_id,
        "all_passed": r.all_assertions_passed,
        "assertions_passed": r.passed_assertions,
        "assertions_total": r.total_assertions,
        "total_time_ms": r.total_time_ms,
        "turns": [
            {
                "turn_index": t.turn_index,
                "user_message": t.user_message,
                "response_text": t.response_text[:500],
                "trace_id": t.trace_id,
                "response_time_ms": t.response_time_ms,
                "prompt_callsite": t.prompt_callsite,
                "prompt_static_prefix_hash": t.prompt_static_prefix_hash,
                "prompt_dynamic_hash": t.prompt_dynamic_hash,
                "rating": t.rating,
                "assertions": [
                    {
                        "passed": a.passed,
                        "message": a.message,
                        "actual_value": a.actual_value,
                    }
                    for a in t.assertion_results
                ],
            }
            for t in r.turns
        ],
        "quality_criteria": list(r.quality_criteria),
        "post_path_assertions": [
            {
                "passed": a.passed,
                "message": a.message,
                "actual_value": a.actual_value,
            }
            for a in r.post_path_assertion_results
        ],
    }
