"""Render the FRE-1641 report as JSON and Markdown (AC-2)."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping, Sequence

from scripts.eval.fre1641_anchor_retention.score import (
    BAR,
    FetchCell,
    Retention,
    SearchCell,
    Selection,
)


def jsonable(value: object) -> object:
    """Convert the report document to plain JSON values.

    Args:
        value: Any part of the report document.

    Returns:
        The same content as dicts, lists and scalars. A retention carries its share.
    """
    if isinstance(value, Retention):
        return {"kept": value.kept, "supported": value.supported, "share": round(value.share, 4)}
    if isinstance(value, SearchCell):
        out = {f.name: jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
        return {**out, "passes": value.passes, "scored_classes": value.scored_classes()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    return value


def _pct(r: Retention) -> str:
    return f"{100 * r.share:.1f}%"


def _search_rows(cells: Sequence[SearchCell]) -> list[str]:
    rows = [
        "| Exa length | Results | Retention | Kept / supported | Median result chars | Classes scored | Passes |",
        "| --: | --: | --: | --: | --: | -- | -- |",
    ]
    for c in cells:
        classes = ", ".join(f"{k} {_pct(c.by_class[k])}" for k in c.scored_classes()) or "none"
        rows.append(
            f"| {c.length} | {c.count} | {_pct(c.pooled)} | {c.pooled.kept} / {c.pooled.supported} "
            f"| {c.median_chars:,.0f} | {classes} | {'yes' if c.passes else 'no'} |"
        )
    return rows


def _fetch_rows(cells: Sequence[FetchCell]) -> list[str]:
    rows = [
        "| Arm | Budget | Retention | Kept / supported | Median passage chars | Latency per fetch (s) | Cost per fetch (USD) | Text leaves host | Passes |",
        "| -- | --: | --: | --: | --: | --: | --: | --: | -- |",
    ]
    for c in cells:
        leaves = "no" if c.leaves_host == 0 else f"yes, {100 * c.leaves_host:.0f}% of fetches"
        rows.append(
            f"| {c.arm} | {c.budget:,} | {_pct(c.retention)} | {c.retention.kept} / "
            f"{c.retention.supported} | {c.median_chars:,.0f} | {c.latency_s:.3f} | "
            f"{c.cost_per_fetch:.6f} | {leaves} | {'yes' if c.retention.passes else 'no'} |"
        )
    return rows


def markdown(doc: Mapping[str, object]) -> str:
    """Render the report as Markdown.

    Args:
        doc: The report document from ``report.analyze``.

    Returns:
        The Markdown text.
    """
    fetch: Mapping[str, object] = doc["fetch"]  # type: ignore[assignment]
    search: Mapping[str, object] = doc["search"]  # type: ignore[assignment]
    code: Mapping[str, object] = doc["code"]  # type: ignore[assignment]
    prod: Mapping[str, object] = doc["prod_searxng"]  # type: ignore[assignment]
    cost: Mapping[str, object] = doc["cost"]  # type: ignore[assignment]
    s_sel: Selection = doc["search_selection"]  # type: ignore[assignment]
    f_sel: Selection = doc["fetch_selection"]  # type: ignore[assignment]
    lines = [
        "# FRE-1641 — anchor retention report (ADR-0158 T1)",
        "",
        f"- Scored code: commit `{code['commit']}`; uncommitted changes to the scored code: "
        f"{code['dirty'] or 'none'}",
        f"- Production `cloud-sim-searxng` StartedAt: before `{prod['before']}`, after "
        f"`{prod['after']}`",
        f"- Bar: {BAR:.0%} of supported findings kept (D5.7). Blocked: {doc['blocked'] or 'no'}",
        "",
        "## Selection",
        "",
        f"- **Search cell: {s_sel.cell or 'none'}.** {s_sel.rule}",
        f"- **Fetch cell: {f_sel.cell or 'none'}.** {f_sel.rule}",
        "",
        "## Search (D2.1, 8 cells)",
        "",
        f"- Findings eligible (helper has replayed searches): {search['eligible']}; supported at "
        f"the full form (2,000 × 10): {search['supported']}; drifted: {search['drifted']} "
        f"(drift rate {float(str(search['drift_rate'])):.1%}).",
        f"- Full form: {_pct(search['full_form'])} (must be 100%). Full-form median result "  # type: ignore[arg-type]
        f"chars: {float(str(search['full_form_median_chars'])):,.0f}.",
        f"- Seeded negative, 100-character snippets: {_pct(search['negative_100'])} (must be "  # type: ignore[arg-type]
        "below 95%).",
        f"- Replays: {search['replays']}; unusable per instance (a vendor engine down, or no data): {search['replay_errors']}.",
        "",
        *_search_rows(search["cells"]),  # type: ignore[arg-type]
        "",
        "## Fetch (D4, 12 cells)",
        "",
        f"- Findings with a fixture: {fetch['findings_with_fixture']}; fixture missing: "
        f"{fetch['fixture_missing']}; supported: {fetch['supported']}; drifted: {fetch['drifted']} "
        f"(drift rate {float(str(fetch['drift_rate'])):.1%}).",
        f"- Full form (whole recorded text): {_pct(fetch['full_form'])} (must be 100%).",  # type: ignore[arg-type]
        f"- Seeded negative, 200-character cut: {_pct(fetch['negative_200'])} (must be below "  # type: ignore[arg-type]
        "95%).",
        f"- Pages ranked: {fetch['pages_ranked']}; unranked: {fetch['pages_unranked']}; median "
        f"fixture chars: {float(str(fetch['median_fixture_chars'])):,.0f}.",
        f"- Fallback cut used (no focus term in the passages, D3.3): {fetch['fallback_cut_used']}.",
        "",
        *_fetch_rows(fetch["cells"]),  # type: ignore[arg-type]
        "",
        "## Cost",
        "",
        f"- Exa calls {cost['exa_calls']}, Brave calls {cost['brave_calls']}, rerank tokens "
        f"{cost['rerank_tokens']}: about ${cost['usd']}.",
        "",
        "## Stated limits",
        "",
        "- Anchors co-occurring in a window are a proxy for support (D5.3).",
        "- The focus is the helper's `spec_task` (D5.9), often longer than the 200-character "
        "`focus` limit.",
        "- Replays use the default `language` and the grid result count (D5.2).",
        "- The search median result size uses today's shaping. T2 changes the shaping (D2.2), so "
        "T2 must run `score` again on these stored responses at its own commit.",
    ]
    return "\n".join(lines) + "\n"


def dump_json(doc: Mapping[str, object]) -> str:
    """Serialize the report document."""
    return json.dumps(jsonable(doc), indent=2)
