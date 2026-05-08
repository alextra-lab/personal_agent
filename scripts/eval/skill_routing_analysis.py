"""Phase D — Skill Routing Eval: per-trace metric extraction from ES.

For each run directory produced by the recovery harness, reads the trace_id
from raw.json (or report.md fallback) and queries ES for Phase B/C specific
events. Writes a cell-level summary to the run directory.

Metrics (FRE-329 + FRE-331):
  Legacy:
    tool_iteration_limit_reached_rate
    read_skill_invoked_rate  (deprecated aggregate; split into 3 buckets below)
    guard_block_rate
    routing_call_rate
    es_first_call_correct_rate

  Router-only (FRE-331) — meaningful for model_decided mode:
    router_recall           mean(len(returned ∩ expected) / max(1,len(expected)))
    router_precision        mean(len(returned ∩ expected) / max(1,len(returned)))
    router_empty_rate       fraction where routing_skills_returned == []
    router_wrong_skill_rate fraction where a forbidden skill was returned

  Success-class breakdown (FRE-331):
    clean_success_rate      router loaded right skill, primary used it correctly
    recovered_success_rate  router missed; primary fetched via read_skill
    guard_saved_rate        B.5 guard intercepted bad call; trace completed
    failed_rate             iteration limit reached (or no answer)

  read_skill 3-bucket rates (FRE-331):
    read_skill_needed_and_invoked_rate
    read_skill_needed_but_not_invoked_rate
    read_skill_not_needed_but_invoked_rate

Usage:
    # Analyse a single cell's run
    uv run python scripts/eval/skill_routing_analysis.py \\
        --run-dir telemetry/evaluation/EVAL-skill-routing-2026-05/cloud-keyword-<RUN_ID>

    # Or import and call analyse_run() from the matrix runner
"""

from __future__ import annotations

import argparse
import json
import re
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


def _get_events(trace_id: str, event_name: str, size: int = 10) -> list[dict[str, Any]]:
    return _search({
        "size": size,
        "query": {
            "bool": {
                "must": [
                    {"term": {"trace_id": trace_id}},
                    {"term": {"event_type": event_name}},
                ]
            }
        },
        "sort": [{"@timestamp": "asc"}],
    })


# ---------------------------------------------------------------------------
# Ground-truth loading
# ---------------------------------------------------------------------------


def load_ground_truth(prompts_yaml: Path) -> dict[str, dict[str, Any]]:
    """Load per-prompt ground-truth labels from prompts.yaml.

    Args:
        prompts_yaml: Path to the prompts.yaml file.

    Returns:
        Dict mapping prompt_id to its ground-truth fields.
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        log.warning("pyyaml_not_installed", detail="ground-truth labels unavailable")
        return {}

    data = yaml.safe_load(prompts_yaml.read_text(encoding="utf-8"))
    gt: dict[str, dict[str, Any]] = {}
    for prompt in data.get("prompts", []):
        pid = prompt.get("id")
        if not pid:
            continue
        gt[pid] = {
            "expected_router_skills": prompt.get("expected_router_skills") or [],
            "forbidden_router_skills": prompt.get("forbidden_router_skills") or [],
            "expected_first_tool": prompt.get("expected_first_tool"),
            "expected_command_substring": prompt.get("expected_command_substring"),
        }
    return gt


# ---------------------------------------------------------------------------
# Trace-id fallback: read from report.md
# ---------------------------------------------------------------------------


def _read_trace_id_from_report(report_path: Path) -> str | None:
    """Extract trace_id from a report.md produced by the recovery harness.

    Args:
        report_path: Path to the ``report.md`` file in a prompt subdirectory.

    Returns:
        The trace_id string, or None if not found.
    """
    if not report_path.exists():
        return None
    text = report_path.read_text(encoding="utf-8")
    m = re.search(r"trace_id:\s+`([0-9a-f-]+)`", text)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Success-class classification
# ---------------------------------------------------------------------------


def _classify_success(trace: dict[str, Any], gt: dict[str, Any] | None) -> str:
    """Classify a trace into one of four outcome classes.

    Classification priority (first match wins):
      1. ``failed``            — iteration limit reached (structural signal only;
                                 LLM-judge correctness is out of scope until FRE-330)
      2. ``guard_saved``       — B.5 guard intercepted ≥1 bad command, no limit hit
      3. ``recovered_success`` — router missed expected skill(s); primary fetched
                                 them via read_skill; no limit hit
      4. ``clean_success``     — router pre-loaded expected skill(s) or no skills
                                 were required; no limit hit

    Args:
        trace: Per-trace metric dict produced by analyse_trace().
        gt: Ground-truth labels for this prompt, or None.

    Returns:
        One of ``"clean_success"``, ``"recovered_success"``,
        ``"guard_saved"``, or ``"failed"``.
    """
    if trace.get("tool_iteration_limit_reached"):
        return "failed"

    if trace.get("guard_blocks", 0) > 0:
        return "guard_saved"

    if gt:
        expected = set(gt.get("expected_router_skills") or [])
        returned = set(trace.get("routing_skills_returned") or [])
        read_names = set(trace.get("read_skill_names") or [])

        if expected:
            router_hit = expected.issubset(returned)
            if router_hit:
                return "clean_success"
            # Router missed; check if primary recovered via read_skill
            recovered = bool(expected & read_names)
            return "recovered_success" if recovered else "failed"

    # No expected skills (baseline) or no ground truth — assume clean unless failed
    return "clean_success"


# ---------------------------------------------------------------------------
# Per-trace analysis
# ---------------------------------------------------------------------------


def analyse_trace(
    trace_id: str,
    es_hits: list[dict[str, Any]] | None = None,
    ground_truth: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract Phase B/C metrics for a single trace.

    Reads from *es_hits* (events already fetched by the harness and stored in
    raw.json) when available, falling back to a direct ES query otherwise.

    Args:
        trace_id: The trace identifier.
        es_hits: Pre-fetched ES events from raw.json (preferred; avoids re-query).
        ground_truth: Per-prompt ground-truth labels from prompts.yaml.

    Returns:
        Dict with metric counts, router-only metrics, and success class.
    """
    result: dict[str, Any] = {"trace_id": trace_id}

    def _events(name: str) -> list[dict[str, Any]]:
        if es_hits is not None:
            return [h for h in es_hits if h.get("event_type") == name]
        return _search({
            "size": 50,
            "query": {"bool": {"must": [
                {"term": {"trace_id.keyword": trace_id}},
                {"term": {"event_type": name}},
            ]}},
            "sort": [{"@timestamp": "asc"}],
        })

    # --- Primary metric ---
    result["tool_iteration_limit_reached"] = len(_events("tool_iteration_limit_reached")) > 0

    # --- Phase B: skill_index_assembled ---
    index_events = _events("skill_index_assembled")
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
    read_events = _events("read_skill_invoked")
    result["read_skill_count"] = len(read_events)
    result["read_skill_names"] = [e.get("skill_name") for e in read_events]

    # --- Phase B.5: guard blocks ---
    guard_events = _events("tool_call_blocked_known_bad_pattern")
    result["guard_blocks"] = len(guard_events)
    result["guard_patterns"] = [e.get("pattern") for e in guard_events]

    # --- Phase C: routing call ---
    routing_events = _events("skill_routing_call_completed")
    if routing_events:
        r = routing_events[0]
        result["routing_call_fired"] = True
        result["routing_model_key"] = r.get("routing_model_key", "")
        result["routing_latency_ms"] = r.get("latency_ms", 0)
        result["routing_skills_returned"] = r.get("skills_returned", [])
    else:
        result["routing_call_fired"] = False
        result["routing_skills_returned"] = []

    # --- Incident-class: first bash command ---
    bash_events = _events("bash_started")
    if bash_events:
        cmd = bash_events[0].get("command", "")
        result["first_bash_command"] = cmd[:200]
        uses_target_index = "agent-logs-" in cmd
        uses_bad_pattern = "/logs-*" in cmd
        result["first_bash_uses_correct_index"] = uses_target_index and not uses_bad_pattern
    else:
        result["first_bash_command"] = ""
        result["first_bash_uses_correct_index"] = None

    # --- Router-only metrics (FRE-331) ---
    if ground_truth is not None:
        expected = set(ground_truth.get("expected_router_skills") or [])
        forbidden = set(ground_truth.get("forbidden_router_skills") or [])
        returned = set(result["routing_skills_returned"])

        if expected:
            intersection = returned & expected
            result["router_recall"] = len(intersection) / len(expected)
            result["router_precision"] = len(intersection) / max(1, len(returned))
        else:
            # No expected skills — recall undefined (None); precision: any return is wrong
            result["router_recall"] = None
            result["router_precision"] = 0.0 if returned else 1.0

        result["router_empty"] = len(returned) == 0
        result["router_has_forbidden"] = bool(returned & forbidden)

        # --- read_skill 3-bucket (FRE-331) ---
        read_names = set(result["read_skill_names"])
        result["read_skill_needed_and_invoked"] = bool(expected & read_names)
        result["read_skill_needed_but_not_invoked"] = bool(
            expected and not (expected & (returned | read_names))
        )
        result["read_skill_not_needed_but_invoked"] = bool(read_names - expected)
    else:
        result["router_recall"] = None
        result["router_precision"] = None
        result["router_empty"] = None
        result["router_has_forbidden"] = None
        result["read_skill_needed_and_invoked"] = None
        result["read_skill_needed_but_not_invoked"] = None
        result["read_skill_not_needed_but_invoked"] = None

    # --- Success class (FRE-331) ---
    result["success_class"] = _classify_success(result, ground_truth)

    return result


# ---------------------------------------------------------------------------
# Run-level analysis
# ---------------------------------------------------------------------------


def analyse_run(run_dir: Path, prompts_yaml: Path | None = None) -> dict[str, Any]:
    """Analyse all prompt results in a harness run directory.

    Reads trace_ids from raw.json when present; falls back to parsing
    report.md for runs collected without raw.json (e.g. 2026-05-07 runs).

    Args:
        run_dir: Path to the run directory (e.g.
            ``telemetry/evaluation/EVAL-skill-routing-2026-05/cloud-keyword-run1``).
        prompts_yaml: Path to prompts.yaml for ground-truth labels.
            Defaults to ``<run_dir>/../prompts.yaml`` when not provided.

    Returns:
        Cell-level summary dict including legacy and FRE-331 metrics.
    """
    # Resolve ground-truth labels
    if prompts_yaml is None:
        candidate = run_dir.parent / "prompts.yaml"
        prompts_yaml = candidate if candidate.exists() else None

    gt_map: dict[str, dict[str, Any]] = {}
    if prompts_yaml and prompts_yaml.exists():
        gt_map = load_ground_truth(prompts_yaml)
        if gt_map:
            log.info("ground_truth_loaded", prompts=list(gt_map.keys()))

    prompt_dirs = sorted(p for p in run_dir.iterdir() if p.is_dir())
    traces: list[dict[str, Any]] = []

    for p in prompt_dirs:
        raw_path = p / "raw.json"
        report_path = p / "report.md"
        trace_id: str | None = None
        es_hits: list[dict[str, Any]] = []

        if raw_path.exists():
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            first = raw[0] if isinstance(raw, list) and raw else raw
            trace_id = first.get("trace_id") if isinstance(first, dict) else None
            for turn in (raw if isinstance(raw, list) else [raw]):
                es_hits.extend(turn.get("es_hits", []))
        else:
            # Fallback: extract trace_id from report.md and query ES live
            trace_id = _read_trace_id_from_report(report_path)
            if trace_id:
                log.info("trace_id_from_report_md", prompt=p.name, trace_id=trace_id)

        if not trace_id:
            log.warning("no_trace_id", path=str(p))
            continue

        prompt_id = p.name
        gt = gt_map.get(prompt_id)
        trace_metrics = analyse_trace(trace_id, es_hits=es_hits or None, ground_truth=gt)
        trace_metrics["prompt_id"] = prompt_id
        traces.append(trace_metrics)

    n = len(traces)
    if n == 0:
        return {"prompts_analysed": 0, "error": "no traces found"}

    def _rate(pred: Any) -> float:
        """Fraction of traces where pred is truthy."""
        return sum(1 for t in traces if t.get(pred)) / n

    def _bool_rate(key: str) -> float:
        """Fraction of traces where bool field is True (ignores None)."""
        vals = [t[key] for t in traces if t.get(key) is not None]
        return sum(vals) / len(vals) if vals else 0.0

    def _mean(key: str) -> float | None:
        """Mean of numeric field, ignoring None."""
        vals = [t[key] for t in traces if t.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    # --- Legacy metrics ---
    summary: dict[str, Any] = {
        "prompts_analysed": n,
        "tool_iteration_limit_reached_rate": _rate("tool_iteration_limit_reached"),
        "read_skill_invoked_rate": sum(
            1 for t in traces if t.get("read_skill_count", 0) > 0
        ) / n,
        "guard_block_rate": sum(
            1 for t in traces if t.get("guard_blocks", 0) > 0
        ) / n,
        "routing_call_rate": _rate("routing_call_fired"),
        "es_first_call_correct_rate": sum(
            1 for t in traces
            if t.get("first_bash_uses_correct_index") is True
        ) / max(1, sum(1 for t in traces if t.get("first_bash_command"))),
        # --- Router-only metrics (FRE-331) ---
        "router_recall_mean": _mean("router_recall"),
        "router_precision_mean": _mean("router_precision"),
        "router_empty_rate": _bool_rate("router_empty"),
        "router_wrong_skill_rate": _bool_rate("router_has_forbidden"),
        # --- Success-class breakdown (FRE-331) ---
        "success_class": {
            cls: sum(1 for t in traces if t.get("success_class") == cls) / n
            for cls in ("clean_success", "recovered_success", "guard_saved", "failed")
        },
        # --- read_skill 3-bucket rates (FRE-331) ---
        "read_skill_needed_and_invoked_rate": _bool_rate("read_skill_needed_and_invoked"),
        "read_skill_needed_but_not_invoked_rate": _bool_rate("read_skill_needed_but_not_invoked"),
        "read_skill_not_needed_but_invoked_rate": _bool_rate("read_skill_not_needed_but_invoked"),
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
    parser.add_argument("--prompts-yaml", type=Path, default=None,
                        help="Path to prompts.yaml for ground-truth labels "
                             "(default: <run-dir>/../prompts.yaml)")
    args = parser.parse_args()

    run_dir = args.run_dir
    if not run_dir.exists():
        raise SystemExit(f"Run dir not found: {run_dir}")

    summary = analyse_run(run_dir, prompts_yaml=args.prompts_yaml)
    out = args.out or (run_dir / "skill_routing_summary.json")
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Written: {out}")
    print(f"  tool_iteration_limit_reached_rate : {summary.get('tool_iteration_limit_reached_rate', 'N/A'):.0%}")
    print(f"  es_first_call_correct_rate        : {summary.get('es_first_call_correct_rate', 'N/A'):.0%}")
    print(f"  read_skill_invoked_rate           : {summary.get('read_skill_invoked_rate', 'N/A'):.0%}")
    print(f"  guard_block_rate                  : {summary.get('guard_block_rate', 'N/A'):.0%}")
    print(f"  routing_call_rate                 : {summary.get('routing_call_rate', 'N/A'):.0%}")
    sc = summary.get("success_class") or {}
    print(f"  clean_success_rate                : {sc.get('clean_success', 0):.0%}")
    print(f"  recovered_success_rate            : {sc.get('recovered_success', 0):.0%}")
    print(f"  guard_saved_rate                  : {sc.get('guard_saved', 0):.0%}")
    print(f"  failed_rate                       : {sc.get('failed', 0):.0%}")
    if summary.get("router_recall_mean") is not None:
        print(f"  router_recall_mean                : {summary['router_recall_mean']:.2f}")
        print(f"  router_precision_mean             : {summary['router_precision_mean']:.2f}")
        print(f"  router_empty_rate                 : {summary.get('router_empty_rate', 0):.0%}")
        print(f"  router_wrong_skill_rate           : {summary.get('router_wrong_skill_rate', 0):.0%}")


if __name__ == "__main__":
    main()
