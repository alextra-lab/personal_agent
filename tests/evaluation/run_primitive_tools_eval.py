r"""FRE-262 PIVOT-3 evaluation runner — dual-path primitive-tools comparison.

Fires the 20 prompts from prompts.yaml against two live agent instances:

  Control   (port 9000): curated legacy tools, AGENT_PRIMITIVE_TOOLS_ENABLED=false
  Treatment (port 9001): primitives + skill docs, AGENT_PRIMITIVE_TOOLS_ENABLED=true

Writes results.json (raw) and report.md (side-by-side table for human grading).

IMPORTANT — inference server load
----------------------------------
This script fires 40 real LLM inference calls (20 prompts x 2 instances).
Running it accidentally will monopolise the GPU for ~30 minutes.

You MUST set PERSONAL_AGENT_EVAL=1 before running:

    PERSONAL_AGENT_EVAL=1 uv run python tests/evaluation/run_primitive_tools_eval.py

Usage:
    PERSONAL_AGENT_EVAL=1 uv run python -m tests.evaluation.run_primitive_tools_eval \
        --control-url http://localhost:9002 \
        --treatment-url http://localhost:9003 \
        --es-url http://localhost:9200 \
        --output-dir telemetry/evaluation/EVAL-primitive-tools/run-$(date +%Y-%m-%d)/

See telemetry/evaluation/EVAL-primitive-tools/README.md for full setup instructions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from tests.evaluation.harness.telemetry import TelemetryChecker

# ---------------------------------------------------------------------------
# Safety gate — same pattern as tests/evaluation/harness/run.py
# ---------------------------------------------------------------------------
_EVAL_ENV_VAR = "PERSONAL_AGENT_EVAL"


def _check_eval_gate() -> None:
    """Abort unless PERSONAL_AGENT_EVAL=1 is set in the environment.

    Prevents accidental runs by AI agents or `pytest tests/` sweeps.
    Set the variable explicitly when you intend to run the harness:

        PERSONAL_AGENT_EVAL=1 uv run python tests/evaluation/run_primitive_tools_eval.py

    Note: --help bypasses the gate so users can inspect flags without the env var.
    """
    if "--help" in sys.argv or "-h" in sys.argv:
        return
    if os.environ.get(_EVAL_ENV_VAR) != "1":
        sys.stderr.write(
            f"\nERROR: {_EVAL_ENV_VAR}=1 is required to run this evaluation harness.\n"
            "This script fires 40 real LLM inference calls (20 prompts x 2 instances)\n"
            "and will monopolise the GPU for ~30 minutes if run accidentally.\n\n"
            f"Set {_EVAL_ENV_VAR}=1 explicitly when you intend to run the harness:\n\n"
            f"    {_EVAL_ENV_VAR}=1 uv run python -m tests.evaluation.run_primitive_tools_eval\n\n"
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_DEFAULT_PROMPTS = "telemetry/evaluation/EVAL-primitive-tools/prompts.yaml"
_DEFAULT_CONTROL_URL = "http://localhost:9000"
_DEFAULT_TREATMENT_URL = "http://localhost:9001"
_DEFAULT_ES_URL = "http://localhost:9200"
_DEFAULT_SESSION_PREFIX = "eval-fre262"
_DEFAULT_DELAY = 2.0
_TIMEOUT_SECONDS = 120.0  # LLM calls can be slow; allow up to 2 min per prompt


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the PIVOT-3 evaluation runner.

    Returns:
        Parsed argument namespace with all harness configuration fields.
    """
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H%M%S")
    default_output = f"telemetry/evaluation/EVAL-primitive-tools/run-{ts}/"

    parser = argparse.ArgumentParser(
        description=(
            "FRE-262 PIVOT-3 dual-path harness: curated tools vs primitives + skill docs.\n"
            f"Requires {_EVAL_ENV_VAR}=1 to run."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--prompts",
        default=_DEFAULT_PROMPTS,
        metavar="PATH",
        help=f"Path to prompts.yaml (default: {_DEFAULT_PROMPTS})",
    )
    parser.add_argument(
        "--control-url",
        default=_DEFAULT_CONTROL_URL,
        metavar="URL",
        help=(
            f"Control agent URL — curated tools, PRIMITIVE_TOOLS_ENABLED=false"
            f" (default: {_DEFAULT_CONTROL_URL})"
        ),
    )
    parser.add_argument(
        "--treatment-url",
        default=_DEFAULT_TREATMENT_URL,
        metavar="URL",
        help=f"Treatment agent URL — primitives + skill docs (default: {_DEFAULT_TREATMENT_URL})",
    )
    parser.add_argument(
        "--output-dir",
        default=default_output,
        metavar="DIR",
        help=f"Output directory for results.json and report.md (default: {default_output})",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=_DEFAULT_DELAY,
        metavar="SECONDS",
        help=f"Seconds to sleep between prompt pairs (default: {_DEFAULT_DELAY})",
    )
    parser.add_argument(
        "--session-prefix",
        default=_DEFAULT_SESSION_PREFIX,
        metavar="STR",
        help=f"Prefix for session IDs (default: {_DEFAULT_SESSION_PREFIX!r})",
    )
    parser.add_argument(
        "--es-url",
        default=_DEFAULT_ES_URL,
        metavar="URL",
        help=(
            f"Elasticsearch URL for post-run telemetry fetch"
            f" (default: {_DEFAULT_ES_URL})"
        ),
    )
    parser.add_argument(
        "--skip",
        nargs="*",
        default=[],
        metavar="ID",
        help="Prompt IDs to skip (e.g. --skip es-04 es-01)",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Core HTTP helpers
# ---------------------------------------------------------------------------


def _post_chat(
    base_url: str,
    message: str,
    timeout: float = _TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    """POST a single message to /chat and return (status_code, response_text, trace_id).

    Args:
        base_url: Agent service base URL (e.g. "http://localhost:9000").
        message: The user message to send.
        timeout: HTTP timeout in seconds.

    Returns:
        Tuple of (HTTP status code, extracted response text or error string,
        trace_id from the response body or empty string on failure).
        On network/timeout errors the status code is 0.
    """
    url = f"{base_url.rstrip('/')}/chat"
    # /chat takes query params (FastAPI plain-type params = query, not body).
    # Don't pass session_id — let the server create a new session per prompt
    # and capture the returned UUID from the response for traceability.
    params: dict[str, str] = {"message": message}
    try:
        resp = httpx.post(url, params=params, timeout=timeout)
        status = resp.status_code
        if status == 200:
            try:
                body = resp.json()
            except (json.JSONDecodeError, ValueError):
                return status, f"ERROR: non-JSON response: {resp.text[:200]}", ""
            # Try common response field names in order of preference
            text: str = (
                body.get("response")
                or body.get("content")
                or body.get("message")
                or body.get("text")
                or json.dumps(body)
            )
            trace_id: str = str(body.get("trace_id", ""))
            return status, str(text), trace_id
        return status, f"ERROR: {status} {resp.reason_phrase}", ""
    except httpx.TimeoutException:
        return 0, "ERROR: request timed out", ""
    except httpx.RequestError as exc:
        return 0, f"ERROR: {exc}", ""


# ---------------------------------------------------------------------------
# Telemetry / ES metric helpers (FRE-274)
# ---------------------------------------------------------------------------

_EMPTY_METRICS: dict[str, Any] = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "tool_call_count": 0,
    "tool_names": [],
    "iteration_count": 0,
}


def _to_int(val: object) -> int:
    """Safely coerce an ES field value (typed object) to int."""
    if isinstance(val, int):
        return val
    if isinstance(val, (float, str)):
        try:
            return int(val)
        except (ValueError, TypeError):
            return 0
    return 0


async def _fetch_trace_metrics(es_url: str, trace_id: str) -> dict[str, Any]:
    """Aggregate per-trace LLM and tool metrics from Elasticsearch.

    Queries agent-logs-* for all events tagged with trace_id and sums
    the token counters, tool call count, and LLM iteration count.

    Args:
        es_url: Elasticsearch base URL.
        trace_id: Trace ID returned by /chat.

    Returns:
        Dict with prompt_tokens, completion_tokens, cache_read_tokens,
        cache_write_tokens, tool_call_count, tool_names, iteration_count.
        All zero/empty when trace_id is blank or ES is unreachable.
    """
    if not trace_id:
        return dict(_EMPTY_METRICS)
    try:
        checker = TelemetryChecker(es_url=es_url, max_retries=5, retry_delay_s=2.0)
        events = await checker.fetch_events(trace_id)
    except Exception:
        return dict(_EMPTY_METRICS)

    prompt_tokens = 0
    completion_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    tool_call_count = 0
    tool_names: list[str] = []
    iteration_count = 0

    for event in events:
        # event_type is the indexed field name (es_logger adds it); fall back to event/message
        ev_type = (
            event.get("event_type")
            or event.get("event")
            or event.get("message")
        )
        if ev_type == "litellm_request_complete":
            pt = _to_int(event.get("prompt_tokens") or 0)
            total = _to_int(event.get("tokens") or 0)
            prompt_tokens += pt
            completion_tokens += total - pt
            cache_read_tokens += _to_int(event.get("cache_read_tokens") or 0)
            cache_write_tokens += _to_int(event.get("cache_write_tokens") or 0)
        elif ev_type == "tool_call_started":
            tool_call_count += 1
            name = event.get("tool_name", "")
            if name:
                tool_names.append(str(name))
        elif ev_type == "model_call_started":
            iteration_count += 1

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "tool_call_count": tool_call_count,
        "tool_names": tool_names,
        "iteration_count": iteration_count,
    }


async def _fetch_both_metrics(
    es_url: str,
    ctrl_trace_id: str,
    trt_trace_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch control and treatment metrics concurrently.

    Args:
        es_url: Elasticsearch base URL.
        ctrl_trace_id: Trace ID for the control run.
        trt_trace_id: Trace ID for the treatment run.

    Returns:
        Tuple of (control_metrics, treatment_metrics).
    """
    ctrl, trt = await asyncio.gather(
        _fetch_trace_metrics(es_url, ctrl_trace_id),
        _fetch_trace_metrics(es_url, trt_trace_id),
    )
    return ctrl, trt


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def _truncate(text: str, max_chars: int = 200) -> str:
    """Truncate text to max_chars, appending a trailing ellipsis if cut.

    Args:
        text: Input string to truncate.
        max_chars: Maximum character length before truncation.

    Returns:
        Original string if short enough, otherwise truncated string with ellipsis.
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def _escape_md(text: str) -> str:
    """Escape pipe characters for Markdown table cells.

    Args:
        text: Raw text that may contain pipe characters.

    Returns:
        Text with pipe characters replaced by their HTML entity.
    """
    return text.replace("|", "&#124;")


def _write_results_json(results: list[dict[str, Any]], output_dir: Path) -> Path:
    """Write raw harness results to results.json.

    Args:
        results: List of per-prompt result dicts.
        output_dir: Directory to write into.

    Returns:
        Path to the written file.
    """
    path = output_dir / "results.json"
    path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    return path


def _cache_hit_pct(read: int, write: int) -> str:
    """Format cache hit percentage as a string.

    Args:
        read: Cache read (hit) token count.
        write: Cache write (miss) token count.

    Returns:
        Percentage string like "45%" or "—" when both are zero.
    """
    total = read + write
    if total == 0:
        return "—"
    return f"{round(100 * read / total)}%"


def _write_report_md(
    results: list[dict[str, Any]],
    output_dir: Path,
    control_url: str,
    treatment_url: str,
    generated_at: str,
) -> Path:
    """Write the side-by-side human-grading report to report.md.

    Args:
        results: List of per-prompt result dicts.
        output_dir: Directory to write into.
        control_url: Control agent URL (shown in report header).
        treatment_url: Treatment agent URL (shown in report header).
        generated_at: ISO timestamp string for the report header.

    Returns:
        Path to the written file.
    """
    lines: list[str] = [
        "# FRE-262 PIVOT-3 Evaluation Report",
        "",
        f"Generated: {generated_at}",
        f"Control URL: {control_url}",
        f"Treatment URL: {treatment_url}",
        "",
        "## Results",
        "",
        "| # | ID | Category | Prompt | Ctrl response (truncated) | Trt response (truncated)"
        " | Ctrl tok | Trt tok | Ctrl turns | Trt turns | Cache hit % | Quality |",
        "|---|----|-----------|--------------------|--------------------------|--------------------------|"
        "---------|---------|------------|-----------|-------------|---------|",
    ]

    total_ctrl_tok = 0
    total_trt_tok = 0
    total_ctrl_turns = 0
    total_trt_turns = 0
    total_ctrl_cache_read = 0
    total_ctrl_cache_write = 0
    total_trt_cache_read = 0
    total_trt_cache_write = 0
    total_ctrl_ms = 0
    total_trt_ms = 0
    graded_count = 0

    for i, r in enumerate(results, start=1):
        ctrl = r["control"]
        trt = r["treatment"]
        prompt_short = _escape_md(_truncate(r["prompt"], 60))
        ctrl_resp = _escape_md(_truncate(ctrl["response"], 200))
        trt_resp = _escape_md(_truncate(trt["response"], 200))
        quality = r.get("quality", "") or "<!-- ✅/⚠️/❌ -->"

        ctrl_tok = ctrl.get("prompt_tokens", 0) + ctrl.get("completion_tokens", 0)
        trt_tok = trt.get("prompt_tokens", 0) + trt.get("completion_tokens", 0)
        ctrl_turns = ctrl.get("iteration_count", 0)
        trt_turns = trt.get("iteration_count", 0)
        ctrl_cr = ctrl.get("cache_read_tokens", 0)
        ctrl_cw = ctrl.get("cache_write_tokens", 0)
        trt_cr = trt.get("cache_read_tokens", 0)
        trt_cw = trt.get("cache_write_tokens", 0)
        cache_col = f"ctrl {_cache_hit_pct(ctrl_cr, ctrl_cw)} / trt {_cache_hit_pct(trt_cr, trt_cw)}"

        total_ctrl_tok += ctrl_tok
        total_trt_tok += trt_tok
        total_ctrl_turns += ctrl_turns
        total_trt_turns += trt_turns
        total_ctrl_cache_read += ctrl_cr
        total_ctrl_cache_write += ctrl_cw
        total_trt_cache_read += trt_cr
        total_trt_cache_write += trt_cw
        total_ctrl_ms += ctrl.get("latency_ms", 0)
        total_trt_ms += trt.get("latency_ms", 0)
        graded_count += 1

        lines.append(
            f"| {i} | {r['id']} | {r['category']} | {prompt_short} "
            f"| {ctrl_resp} | {trt_resp} "
            f"| {ctrl_tok or '—'} | {trt_tok or '—'} "
            f"| {ctrl_turns or '—'} | {trt_turns or '—'} "
            f"| {cache_col} | {quality} |"
        )

    n = max(graded_count, 1)
    lines += [
        "",
        "## Gate Criteria",
        "",
        "- [ ] Primitive success rate >= curated success rate on >= 17/20 prompts",
        '- [ ] Zero "could not find primitive equivalent" failures (dead-end failures)',
        "",
        "## Token + Turns Summary",
        "",
        "| Metric | Control | Treatment |",
        "|--------|---------|-----------|",
        f"| Total tokens | {total_ctrl_tok:,} | {total_trt_tok:,} |",
        f"| Mean turns / prompt | {total_ctrl_turns / n:.1f} | {total_trt_turns / n:.1f} |",
        f"| Cache hit % | {_cache_hit_pct(total_ctrl_cache_read, total_ctrl_cache_write)}"
        f" | {_cache_hit_pct(total_trt_cache_read, total_trt_cache_write)} |",
        f"| Total wall clock | {total_ctrl_ms / 1000:.0f}s | {total_trt_ms / 1000:.0f}s |",
        "",
        "## Per-Category Gate",
        "",
        "If primitive < curated for a specific category, move that category's tools to PIVOT-4 keep list.",
        "",
        "## Grading Key",
        "",
        "Fill in the Quality column:  ✅ = correct/complete  ⚠️ = partial/extra turns  ❌ = wrong/missing",
        "",
        "Session IDs and full token breakdowns are in results.json — use them to look up traces in Kibana.",
    ]

    path = output_dir / "report.md"
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# Prompt loader
# ---------------------------------------------------------------------------


def load_prompts(prompts_path: Path) -> list[dict[str, str]]:
    """Load the harness prompts from a YAML file.

    Args:
        prompts_path: Path to prompts.yaml.

    Returns:
        List of prompt dicts, each with 'id', 'category', and 'prompt' keys.

    Raises:
        SystemExit: If the file cannot be read or parsed, or contains no prompts.
    """
    if not prompts_path.exists():
        sys.stderr.write(f"ERROR: prompts file not found: {prompts_path}\n")
        sys.exit(1)
    with prompts_path.open() as fh:
        try:
            data = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            sys.stderr.write(f"ERROR: failed to parse {prompts_path}: {exc}\n")
            sys.exit(1)
    prompts: list[dict[str, str]] = data.get("prompts", [])
    if not prompts:
        sys.stderr.write(f"ERROR: no prompts found in {prompts_path}\n")
        sys.exit(1)
    return prompts


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


def run_harness(args: argparse.Namespace) -> None:
    """Execute the full dual-path comparison and write output files.

    Iterates through all prompts, posts each to the control and treatment
    agent instances, records latency and responses, then writes results.json
    and report.md to the output directory.

    Args:
        args: Parsed CLI arguments from parse_args().
    """
    prompts_path = Path(args.prompts)
    output_dir = Path(args.output_dir)

    prompts = load_prompts(prompts_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    n = len(prompts)
    generated_at = datetime.now(tz=timezone.utc).isoformat()

    sys.stdout.write(
        f"FRE-262 PIVOT-3 harness — {n} prompts\n"
        f"  Control:   {args.control_url}\n"
        f"  Treatment: {args.treatment_url}\n"
        f"  ES:        {args.es_url}\n"
        f"  Output:    {output_dir}\n\n"
    )
    sys.stdout.flush()

    results: list[dict[str, Any]] = []
    skip_ids = set(args.skip or [])
    if skip_ids:
        sys.stdout.write(f"Skipping: {', '.join(sorted(skip_ids))}\n")

    for i, prompt in enumerate(prompts, start=1):
        pid = prompt["id"]
        category = prompt["category"]
        message = prompt["prompt"]

        if pid in skip_ids:
            sys.stdout.write(f"[{i:02d}/{n}] {pid}: SKIPPED\n")
            sys.stdout.flush()
            continue
        # uuid5 gives deterministic, valid UUIDs traceable by prompt ID
        ctrl_session = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{args.session_prefix}-ctrl-{pid}"))
        trt_session = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{args.session_prefix}-trt-{pid}"))

        # --- Control ---
        ctrl_start = time.monotonic()
        ctrl_status, ctrl_response, ctrl_trace_id = _post_chat(args.control_url, message)
        ctrl_ms = int((time.monotonic() - ctrl_start) * 1000)

        # --- Treatment ---
        trt_start = time.monotonic()
        trt_status, trt_response, trt_trace_id = _post_chat(args.treatment_url, message)
        trt_ms = int((time.monotonic() - trt_start) * 1000)

        # Progress line
        ctrl_mark = "ok" if ctrl_status == 200 else "ERR"
        trt_mark = "ok" if trt_status == 200 else "ERR"
        sys.stdout.write(
            f"[{i:02d}/{n}] {pid}: "
            f"ctrl={ctrl_mark} {ctrl_ms}ms  trt={trt_mark} {trt_ms}ms\n"
        )
        sys.stdout.flush()

        results.append(
            {
                "id": pid,
                "category": category,
                "prompt": message,
                "control": {
                    "trace_id": ctrl_trace_id,
                    "status": ctrl_status,
                    "response": ctrl_response,
                    "latency_ms": ctrl_ms,
                    "session_id": ctrl_session,
                    **dict(_EMPTY_METRICS),
                },
                "treatment": {
                    "trace_id": trt_trace_id,
                    "status": trt_status,
                    "response": trt_response,
                    "latency_ms": trt_ms,
                    "session_id": trt_session,
                    **dict(_EMPTY_METRICS),
                },
                "quality": "",
            }
        )

        # Incremental write — saves partial result (token fields all zero until post-run fetch)
        _write_results_json(results, output_dir)

        if i < n and args.delay > 0:
            time.sleep(args.delay)

    # Post-run ES metric fetch.
    # Container async ES writes lag behind the HTTP response by up to ~60s
    # under typical load. Batch-fetching after all LLM calls complete avoids
    # per-prompt timing races and lets the 30s wait amortise across the whole run.
    graded = [r for r in results if r["control"].get("trace_id")]
    if graded and args.es_url:
        sys.stdout.write(
            f"\nWaiting 15s for ES to index {len(graded)} trace(s)...\n"
        )
        sys.stdout.flush()
        time.sleep(15)

        sys.stdout.write("Fetching ES token metrics...\n")
        sys.stdout.flush()

        async def _fetch_all_metrics() -> None:
            for r in graded:
                ctrl_metrics, trt_metrics = await _fetch_both_metrics(
                    args.es_url,
                    r["control"].get("trace_id", ""),
                    r["treatment"].get("trace_id", ""),
                )
                r["control"].update(ctrl_metrics)
                r["treatment"].update(trt_metrics)
                pid = r["id"]
                ctrl_tok = ctrl_metrics["prompt_tokens"] + ctrl_metrics["completion_tokens"]
                trt_tok = trt_metrics["prompt_tokens"] + trt_metrics["completion_tokens"]
                sys.stdout.write(
                    f"  {pid}: ctrl={ctrl_tok} tok / {ctrl_metrics['iteration_count']} turns  "
                    f"trt={trt_tok} tok / {trt_metrics['iteration_count']} turns\n"
                )
                sys.stdout.flush()

        asyncio.run(_fetch_all_metrics())

    # Final write output files
    json_path = _write_results_json(results, output_dir)
    md_path = _write_report_md(
        results,
        output_dir,
        control_url=args.control_url,
        treatment_url=args.treatment_url,
        generated_at=generated_at,
    )

    sys.stdout.write(
        f"\nEval complete. {n} prompts.\n"
        f"  Results: {json_path}\n"
        f"  Grade:   {md_path}\n"
    )


def main() -> None:
    """Entry point — check safety gate, parse args, run harness."""
    _check_eval_gate()
    args = parse_args()
    run_harness(args)


if __name__ == "__main__":
    main()
