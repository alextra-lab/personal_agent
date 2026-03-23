"""Report generator for evaluation results.

Produces JSON (machine-readable) and markdown (human-readable) reports
from PathResult data.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

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
    }
