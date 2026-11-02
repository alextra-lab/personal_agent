"""FRE-1728: rank the defects that the pilot finds. Standard library only.

A defect is a failure class: ``(outcome, label, category)``. It counts only if it appears in every run, because
the second run is the noise arm: the same inputs on the same code. Its count is the lower of the two runs, and
its score is that count times the severity that the owner sets for its label. The gap between the runs is
reported, because a later fix must beat it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

FAILURE_OUTCOMES = frozenset({"write_failure", "retrieval_failure", "conversation_failure"})
TOP_N = 5
STOP_RULE = (
    "Stop rule: continue past the pilot only if the owner confirms that at least 3 of the top 5 "
    "defects are real and worth a ticket."
)
LIMITS = (
    "Limits: this tests memory and conversation, not tool use. It tests volume, not the passing of "
    "weeks. Simulated users shift success rates, so this finds defects and does not report a score. "
    "Humans agreed with MemAudit's labels 75% of the time, so the owner must read the top defects."
)


def rank_defects(
    facts_by_run: Mapping[str, Sequence[Mapping[str, object]]],
    severity: Mapping[str, float],
) -> list[dict[str, object]]:
    """Rank the failure classes that appear in every run.

    Args:
        facts_by_run: The scored facts of each run.
        severity: Severity per MemAudit label. A label that is not listed has severity 0.

    Returns:
        Defects, highest score first.
    """
    runs = list(facts_by_run)
    if len(runs) < 2:
        return []
    per_key: dict[tuple[str, str, str], dict[str, list[Mapping[str, object]]]] = {}
    for run, facts in facts_by_run.items():
        for fact in facts:
            if fact["outcome"] not in FAILURE_OUTCOMES or fact.get("invalid_reasons"):
                continue
            key = (str(fact["outcome"]), str(fact["label"]), str(fact["category"]))
            per_key.setdefault(key, {}).setdefault(run, []).append(fact)

    defects: list[dict[str, object]] = []
    for (outcome, label, category), by_run in per_key.items():
        if any(run not in by_run for run in runs):
            continue
        counts = {run: len(by_run[run]) for run in runs}
        count = min(counts.values())
        weight = float(severity.get(label, 0.0))
        defects.append(
            {
                "outcome": outcome,
                "label": label,
                "category": category,
                "count": count,
                "counts_by_run": counts,
                "gap": max(counts.values()) - count,
                "severity": weight,
                "score": count * weight,
                "evidence": [
                    {
                        "run": run,
                        "user_id": fact["user_id"],
                        "dimension": fact["dimension"],
                        "graph_score": fact["graph_score"],
                        "answer_score": fact["answer_score"],
                        "transcript": fact["transcript"],
                        "explanation": fact.get("explanation", ""),
                    }
                    for run in runs
                    for fact in by_run[run]
                ],
            }
        )
    defects.sort(
        key=lambda d: (-float(d["score"]), -int(d["count"]), str(d["outcome"]), str(d["category"]))  # type: ignore[call-overload]
    )
    return defects


def render_report(defects: Sequence[Mapping[str, object]], manifest: Mapping[str, object]) -> str:
    """Render the owner's report.

    Args:
        defects: The ranked defects.
        manifest: The run's manifest.

    Returns:
        Markdown text.
    """
    lines = [
        "# MemAudit pilot: ranked defects",
        "",
        f"Status: {manifest.get('status')}. Spent: {manifest.get('spent_usd')} USD.",
        f"Runs: {manifest.get('runs')}. Users: {manifest.get('users')}.",
        "",
        "| Rank | Outcome | Label | Category | Count | Gap | Severity | Score |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for rank, defect in enumerate(defects, 1):
        lines.append(
            f"| {rank} | {defect['outcome']} | {defect['label']} | {defect['category']} "
            f"| {defect['count']} | {defect['gap']} | {defect['severity']} | {defect['score']} |"
        )
    if not defects:
        lines.append("| - | No failure class appeared in every run. | | | | | | |")
    invalid = manifest.get("invalid_facts") or []
    if invalid:
        lines += [
            "",
            f"**{len(invalid)} facts are invalid and are not ranked**, so this list can undercount:",  # type: ignore[arg-type]
        ]
        lines += [
            f"- {i['run']} {i['user_id']} {i['dimension']}: {'; '.join(i['reasons'])}"  # type: ignore[index]
            for i in invalid  # type: ignore[attr-defined]
        ]
    if manifest.get("detail"):
        lines += ["", f"Detail: {manifest['detail']}"]
    lines += ["", STOP_RULE, "", LIMITS, ""]
    return "\n".join(lines)
