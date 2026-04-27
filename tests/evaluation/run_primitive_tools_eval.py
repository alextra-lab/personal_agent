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
    PERSONAL_AGENT_EVAL=1 uv run python tests/evaluation/run_primitive_tools_eval.py \
        --control-url http://localhost:9000 \
        --treatment-url http://localhost:9001 \
        --output-dir telemetry/evaluation/EVAL-primitive-tools/run-$(date +%Y-%m-%d)/

See telemetry/evaluation/EVAL-primitive-tools/README.md for full setup instructions.
"""

from __future__ import annotations

import argparse
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
            f"    {_EVAL_ENV_VAR}=1 uv run python tests/evaluation/run_primitive_tools_eval.py\n\n"
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_DEFAULT_PROMPTS = "telemetry/evaluation/EVAL-primitive-tools/prompts.yaml"
_DEFAULT_CONTROL_URL = "http://localhost:9000"
_DEFAULT_TREATMENT_URL = "http://localhost:9001"
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
) -> tuple[int, str]:
    """POST a single message to /chat and return (status_code, response_text).

    Args:
        base_url: Agent service base URL (e.g. "http://localhost:9000").
        message: The user message to send.
        timeout: HTTP timeout in seconds.

    Returns:
        Tuple of (HTTP status code, extracted response text or error string).
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
                return status, f"ERROR: non-JSON response: {resp.text[:200]}"
            # Try common response field names in order of preference
            text: str = (
                body.get("response")
                or body.get("content")
                or body.get("message")
                or body.get("text")
                or json.dumps(body)
            )
            return status, str(text)
        return status, f"ERROR: {status} {resp.reason_phrase}"
    except httpx.TimeoutException:
        return 0, "ERROR: request timed out"
    except httpx.RequestError as exc:
        return 0, f"ERROR: {exc}"


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
        "| # | ID | Category | Prompt | Ctrl response (truncated) | Trt response (truncated) | Quality |",
        "|---|----|-----------|--------------------|--------------------------|--------------------------|---------|",
    ]

    for i, r in enumerate(results, start=1):
        prompt_short = _escape_md(_truncate(r["prompt"], 60))
        ctrl_resp = _escape_md(_truncate(r["control"]["response"], 200))
        trt_resp = _escape_md(_truncate(r["treatment"]["response"], 200))
        quality = r.get("quality", "") or "<!-- check/warn/fail -->"
        lines.append(
            f"| {i} | {r['id']} | {r['category']} | {prompt_short} "
            f"| {ctrl_resp} | {trt_resp} | {quality} |"
        )

    lines += [
        "",
        "## Gate Criteria",
        "",
        "- [ ] Primitive success rate >= curated success rate on >= 17/20 prompts",
        '- [ ] Zero "could not find primitive equivalent" failures (dead-end failures)',
        "",
        "## Per-Category Gate",
        "",
        "If primitive < curated for a specific category, move that category's tools to PIVOT-4 keep list.",
        "",
        "## Grading Key",
        "",
        "Fill in the Quality column:  ✅ = correct/complete  ⚠️ = partial/extra turns  ❌ = wrong/missing",
        "",
        "Session IDs are in results.json — use them to look up full traces in Elasticsearch / Kibana.",
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
        ctrl_status, ctrl_response = _post_chat(args.control_url, message)
        ctrl_ms = int((time.monotonic() - ctrl_start) * 1000)

        # --- Treatment ---
        trt_start = time.monotonic()
        trt_status, trt_response = _post_chat(args.treatment_url, message)
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
                    "status": ctrl_status,
                    "response": ctrl_response,
                    "latency_ms": ctrl_ms,
                    "session_id": ctrl_session,
                },
                "treatment": {
                    "status": trt_status,
                    "response": trt_response,
                    "latency_ms": trt_ms,
                    "session_id": trt_session,
                },
                "quality": "",
            }
        )

        # Incremental write after every prompt so kills don't lose data
        _write_results_json(results, output_dir)

        if i < n and args.delay > 0:
            time.sleep(args.delay)

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
