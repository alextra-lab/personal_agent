"""Phase D — Skill Routing Eval: per-trace metric extraction from ES.

For each run directory produced by the recovery harness, reads the trace_id
from raw.json and queries ES for Phase B/C specific events. Writes a
cell-level summary to the run directory.

Usage:
    # Analyse a single cell's run
    uv run python scripts/eval/skill_routing_analysis.py \\
        --run-dir telemetry/evaluation/EVAL-skill-routing-2026-05/cloud-keyword-<RUN_ID>

    # Or import and call analyse_run() from the matrix runner
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

ES_URL = "http://localhost:9200"
ES_INDEX = "agent-logs-*"


# ---------------------------------------------------------------------------
# ES queries
# ---------------------------------------------------------------------------


def _search(query: dict[str, Any]) -> list[dict[str, Any]]:
    resp = httpx.post(
        f"{ES_URL}/{ES_INDEX}/_search",
        json=query,
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return [hit["_source"] for hit in resp.json().get("hits", {}).get("hits", [])]


def _count_event(trace_id: str, event_name: str) -> int:
    hits = _search({
        "size": 0,
        "query": {
            "bool": {
                "must": [
                    {"term": {"trace_id": trace_id}},
                    {"term": {"event": event_name}},
                ]
            }
        },
        "aggs": {"count": {"value_count": {"field": "event"}}},
    })
    return len(hits)


def _get_events(trace_id: str, event_name: str, size: int = 10) -> list[dict[str, Any]]:
    return _search({
        "size": size,
        "query": {
            "bool": {
                "must": [
                    {"term": {"trace_id": trace_id}},
                    {"term": {"event": event_name}},
                ]
            }
        },
        "sort": [{"@timestamp": "asc"}],
    })


# ---------------------------------------------------------------------------
# Per-trace analysis
# ---------------------------------------------------------------------------


def analyse_trace(trace_id: str) -> dict[str, Any]:
    """Extract Phase B/C metrics for a single trace from ES.

    Args:
        trace_id: The trace identifier to query.

    Returns:
        Dict with metric counts and sampled event data.
    """
    result: dict[str, Any] = {"trace_id": trace_id}

    # --- Primary metric ---
    limit_events = _get_events(trace_id, "tool_iteration_limit_reached")
    result["tool_iteration_limit_reached"] = len(limit_events) > 0

    # --- Phase B: skill_index_assembled ---
    index_events = _get_events(trace_id, "skill_index_assembled", size=20)
    if index_events:
        first = index_events[0]
        result["skill_routing_mode"] = first.get("routing_mode", "unknown")
        result["skill_index_injected_chars"] = first.get("injected_chars", 0)
        result["skill_index_turns"] = len(index_events)
    else:
        result["skill_routing_mode"] = "none"
        result["skill_index_injected_chars"] = 0
        result["skill_index_turns"] = 0

    # --- Phase B: read_skill invocations ---
    read_events = _get_events(trace_id, "read_skill_invoked", size=20)
    result["read_skill_count"] = len(read_events)
    result["read_skill_names"] = [e.get("skill_name") for e in read_events]

    # --- Phase B.5: guard blocks ---
    guard_events = _get_events(trace_id, "tool_call_blocked_known_bad_pattern", size=10)
    result["guard_blocks"] = len(guard_events)
    result["guard_patterns"] = [e.get("pattern") for e in guard_events]

    # --- Phase C: routing call ---
    routing_events = _get_events(trace_id, "skill_routing_call_completed", size=5)
    if routing_events:
        r = routing_events[0]
        result["routing_call_fired"] = True
        result["routing_model_key"] = r.get("routing_model_key", "")
        result["routing_latency_ms"] = r.get("latency_ms", 0)
        result["routing_skills_returned"] = r.get("skills_returned", [])
    else:
        result["routing_call_fired"] = False

    # --- Incident-class: first bash command ---
    bash_events = _get_events(trace_id, "bash_started", size=1)
    if bash_events:
        cmd = bash_events[0].get("command", "")
        result["first_bash_command"] = cmd[:200]
        result["first_bash_uses_correct_index"] = (
            "agent-logs-" in cmd or "/logs-*" not in cmd
        )
    else:
        result["first_bash_command"] = ""
        result["first_bash_uses_correct_index"] = None

    return result


# ---------------------------------------------------------------------------
# Run-level analysis
# ---------------------------------------------------------------------------


def analyse_run(run_dir: Path) -> dict[str, Any]:
    """Analyse all prompt results in a harness run directory.

    Args:
        run_dir: Path to the run directory (e.g.
            ``telemetry/evaluation/EVAL-skill-routing-2026-05/cloud-keyword-run1``).

    Returns:
        Cell-level summary dict.
    """
    prompt_dirs = sorted(p for p in run_dir.iterdir() if p.is_dir())
    traces: list[dict[str, Any]] = []

    for p in prompt_dirs:
        raw_path = p / "raw.json"
        if not raw_path.exists():
            log.warning("raw_json_missing", path=str(raw_path))
            continue
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        trace_id = raw.get("trace_id")
        if not trace_id:
            log.warning("no_trace_id", path=str(raw_path))
            continue
        trace_metrics = analyse_trace(trace_id)
        trace_metrics["prompt_id"] = p.name
        traces.append(trace_metrics)

    n = len(traces)
    if n == 0:
        return {"prompts_analysed": 0, "error": "no traces found"}

    # Aggregate
    summary: dict[str, Any] = {
        "prompts_analysed": n,
        "tool_iteration_limit_reached_rate": sum(
            1 for t in traces if t.get("tool_iteration_limit_reached")
        ) / n,
        "read_skill_invoked_rate": sum(
            1 for t in traces if t.get("read_skill_count", 0) > 0
        ) / n,
        "guard_block_rate": sum(
            1 for t in traces if t.get("guard_blocks", 0) > 0
        ) / n,
        "routing_call_rate": sum(
            1 for t in traces if t.get("routing_call_fired")
        ) / n,
        "es_first_call_correct_rate": sum(
            1 for t in traces
            if t.get("first_bash_uses_correct_index") is True
        ) / max(1, sum(1 for t in traces if t.get("first_bash_command"))),
        "traces": traces,
    }
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyse a skill routing eval run")
    parser.add_argument("--run-dir", required=True, type=Path,
                        help="Path to the run directory")
    parser.add_argument("--out", type=Path, default=None,
                        help="Write JSON summary here (default: <run-dir>/skill_routing_summary.json)")
    args = parser.parse_args()

    run_dir = args.run_dir
    if not run_dir.exists():
        raise SystemExit(f"Run dir not found: {run_dir}")

    summary = analyse_run(run_dir)
    out = args.out or (run_dir / "skill_routing_summary.json")
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Written: {out}")
    print(f"  tool_iteration_limit_reached_rate : {summary.get('tool_iteration_limit_reached_rate', 'N/A'):.0%}")
    print(f"  es_first_call_correct_rate        : {summary.get('es_first_call_correct_rate', 'N/A'):.0%}")
    print(f"  read_skill_invoked_rate           : {summary.get('read_skill_invoked_rate', 'N/A'):.0%}")
    print(f"  guard_block_rate                  : {summary.get('guard_block_rate', 'N/A'):.0%}")
    print(f"  routing_call_rate                 : {summary.get('routing_call_rate', 'N/A'):.0%}")


if __name__ == "__main__":
    main()
