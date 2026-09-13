"""Markdown tables for the FRE-1498 document, generated from the JSONL rows.

python3 tables.py <run-id> <regime> [<regime> ...]
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

OUT = Path(__file__).parent / "out"


def load(run_id: str, regime: str) -> list[dict]:
    p = OUT / f"{run_id}.{regime}.jsonl"
    return (
        [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
        if p.exists()
        else []
    )


def worker_cells(row: dict) -> tuple[int, int, str]:
    caps = row.get("sub_agent_captures") or []
    landed = sum(1 for c in caps if c.get("success"))
    stops = collections.Counter((c.get("stop_reason") or "?") for c in caps)
    return len(caps), landed, ", ".join(f"{k}×{v}" for k, v in stops.items())


def table(run_id: str, regime: str) -> str:
    rows = load(run_id, regime)
    lines = [
        "| fixture | classifier | strategy / reason | planner | workers (landed) | worker stops | tools | web | ctx max | secs | reply chars | error | trace |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if "signals" not in r:
            lines.append(
                f"| {r['label']} | | {r.get('skipped') or r.get('http_error') or ''} | | | | | | | | | | |"
            )
            continue
        s, rt = r["signals"], r.get("route_trace") or {}
        pr = s["planner_raw"][0] if s["planner_raw"] else {}
        planner = f"{pr.get('raw_strategy') or '—'}/{pr.get('raw_task_count') if pr.get('raw_task_count') is not None else '—'}"
        if s.get("fallback_planner_used"):
            planner += " (fallback)"
        n, landed, stops = worker_cells(r)
        tools = collections.Counter(s["tool_names"])
        tools_s = ", ".join(f"{k}×{v}" for k, v in tools.most_common()) or "—"
        secs = (
            r.get("chat_wall_s")
            or (
                round((r.get("primary_capture") or {}).get("duration_ms") or 0) / 1000
                if r.get("primary_capture")
                else None
            )
            or round(s["model_call_span_s"])
        )
        reply = rt.get("final_reply_chars") if rt else r.get("reply_chars")
        err = rt.get("error_type") or "" if rt else ""
        rec = " ᴿ" if r.get("recovered") else ""
        lines.append(
            f"| {r['label']}{rec} | {r['deterministic_task_type']}/{r['deterministic_complexity']} | {rt.get('decomposition_strategy')} / {rt.get('decomposition_reason')} | {planner} | {n} ({landed}) | {stops or '—'} | {tools_s} | {s['web_search_count']} | {s['input_token_max']:,} | {int(secs) if secs else '—'} | {reply} | {err} | `{r['trace_id'][:8]}` |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    run_id, regimes = sys.argv[1], sys.argv[2:]
    for reg in regimes:
        print(f"\n### Regime {reg} — run {run_id}\n")
        print(table(run_id, reg))
