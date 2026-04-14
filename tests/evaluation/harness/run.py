r"""Standalone CLI entry point for running evaluation paths.

IMPORTANT — inference server load
----------------------------------
This harness fires 100+ real LLM inference calls sequentially against the local
GPU server. Running it accidentally will monopolise the GPU for 60-90 minutes.

You MUST set PERSONAL_AGENT_EVAL=1 before running:

    PERSONAL_AGENT_EVAL=1 uv run python -m tests.evaluation.harness.run

Usage:
    # Run all paths
    PERSONAL_AGENT_EVAL=1 uv run python -m tests.evaluation.harness.run

    # Run specific paths
    PERSONAL_AGENT_EVAL=1 uv run python -m tests.evaluation.harness.run --paths CP-01 CP-02 CP-03

    # Run a category (display name)
    PERSONAL_AGENT_EVAL=1 uv run python -m tests.evaluation.harness.run --category "Intent Classification"

    # Run one or more categories by slug (Phase 3 VERIFY — see context intelligence plan)
    PERSONAL_AGENT_EVAL=1 uv run python -m tests.evaluation.harness.run --categories context_management
    PERSONAL_AGENT_EVAL=1 uv run python -m tests.evaluation.harness.run --categories decomposition expansion

    # Custom agent URL
    PERSONAL_AGENT_EVAL=1 uv run python -m tests.evaluation.harness.run --agent-url http://localhost:9000

    # Save reports
    PERSONAL_AGENT_EVAL=1 uv run python -m tests.evaluation.harness.run \\
        --output-dir telemetry/evaluation --run-id EVAL-09-cat-context

    # Tune inter-path cooldown (default 8s; use 0 only on fast/multi-GPU servers)
    PERSONAL_AGENT_EVAL=1 uv run python -m tests.evaluation.harness.run --inter-path-delay 12
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import cast

import structlog

from tests.evaluation.harness.dataset import (
    ALL_PATHS,
    PATHS_BY_CATEGORY,
    PATHS_BY_ID,
)
from tests.evaluation.harness.models import ConversationPath
from tests.evaluation.harness.report import (
    generate_json_report,
    generate_markdown_report,
)
from tests.evaluation.harness.runner import EvaluationRunner
from tests.evaluation.harness.telemetry import TelemetryChecker

log = structlog.get_logger(__name__)

# Safety gate: the eval harness fires 100+ real LLM inference calls against the
# local GPU server. Running it accidentally (e.g. an AI agent doing `pytest tests/`)
# will overload the inference server for 60-90 minutes. Require an explicit opt-in.
_EVAL_ENV_VAR = "PERSONAL_AGENT_EVAL"


def _check_eval_gate() -> None:
    """Abort unless PERSONAL_AGENT_EVAL=1 is set in the environment.

    This prevents accidental runs by AI agents or `pytest tests/` sweeps.
    Set the variable explicitly when you intend to run the eval harness:

        PERSONAL_AGENT_EVAL=1 uv run python -m tests.evaluation.harness.run
    """
    if os.environ.get(_EVAL_ENV_VAR) != "1":
        log.error(
            "eval_gate_blocked",
            detail=(
                f"Set {_EVAL_ENV_VAR}=1 to run the evaluation harness. "
                "This harness fires 100+ LLM inference calls and will overload "
                "a single-GPU inference server if run accidentally."
            ),
        )
        sys.exit(1)


# Slugs for --categories (stable CLI for plans and CI; keys are dataset category labels).
CATEGORY_SLUGS: dict[str, str] = {
    "context_management": "Context Management",
    "memory_quality": "Memory Quality",
    "decomposition": "Decomposition Strategies",
    "expansion": "Expansion & Sub-Agents",
    "intent_classification": "Intent Classification",
    "memory_system": "Memory System",
    "tools_self_inspection": "Tools & Self-Inspection",
    "edge_cases": "Edge Cases",
    "cross_session": "Cross-Session Recall",
}


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the evaluation runner.

    Returns:
        Parsed argument namespace with paths, category, agent_url,
        es_url, neo4j_uri, output_dir, and skip_setup attributes.
    """
    parser = argparse.ArgumentParser(
        description="Run evaluation conversation paths against the live agent",
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        help="Specific path IDs to run (e.g., CP-01 CP-02)",
    )
    parser.add_argument(
        "--category",
        help="Run all paths in a category (e.g., 'Intent Classification')",
    )
    parser.add_argument(
        "--categories",
        nargs="+",
        metavar="SLUG",
        help=(
            "Run paths for one or more categories by slug: "
            f"{', '.join(sorted(CATEGORY_SLUGS.keys()))}. "
            "Order is preserved; paths are de-duplicated by path id."
        ),
    )
    parser.add_argument(
        "--agent-url",
        default="http://localhost:9000",
        help="Agent service URL (default: http://localhost:9000)",
    )
    parser.add_argument(
        "--es-url",
        default="http://localhost:9200",
        help="Elasticsearch URL (default: http://localhost:9200)",
    )
    parser.add_argument(
        "--run-id",
        help="Run identifier in EVAL-{NN}-{slug} format (e.g., EVAL-09-slice3-baseline). "
        "Creates a subdirectory under --output-dir for this run's reports.",
    )
    parser.add_argument(
        "--output-dir",
        default="telemetry/evaluation",
        help="Base directory for output reports (default: telemetry/evaluation)",
    )
    parser.add_argument(
        "--skip-setup",
        action="store_true",
        help="Skip paths that require manual setup (e.g., CP-18)",
    )
    parser.add_argument(
        "--inter-path-delay",
        type=float,
        default=8.0,
        metavar="SECONDS",
        help=(
            "Cooldown between paths in seconds (default: 8.0). "
            "Lets the inference server flush KV cache between paths. "
            "Set to 0 to disable (not recommended on single-GPU servers)."
        ),
    )
    parser.add_argument(
        "--skip-responsiveness-probe",
        action="store_true",
        help=(
            "Skip the inference responsiveness probe before running. "
            "Use only when you are confident the server is not overloaded."
        ),
    )
    args = parser.parse_args()
    if args.category and args.categories:
        log.error("conflicting_filters", detail="Use only one of --category or --categories")
        sys.exit(1)
    if args.paths and (args.category or args.categories):
        log.error("conflicting_filters", detail="Do not combine --paths with --category/--categories")
        sys.exit(1)
    return args


def select_paths(args: argparse.Namespace) -> list[ConversationPath]:
    """Select paths based on CLI arguments.

    Args:
        args: Parsed CLI arguments from parse_args().

    Returns:
        List of ConversationPath instances to run, filtered by --paths,
        --category, and --skip-setup flags.
    """
    if args.paths:
        paths = []
        for pid in args.paths:
            if pid not in PATHS_BY_ID:
                log.error("unknown_path_id", path_id=pid)
                sys.exit(1)
            paths.append(PATHS_BY_ID[pid])
        return paths

    if args.categories:
        paths = []
        seen_ids: set[str] = set()
        for slug in args.categories:
            if slug not in CATEGORY_SLUGS:
                log.error(
                    "unknown_category_slug",
                    slug=slug,
                    available=sorted(CATEGORY_SLUGS.keys()),
                )
                sys.exit(1)
            display = CATEGORY_SLUGS[slug]
            for p in PATHS_BY_CATEGORY[display]:
                if p.path_id not in seen_ids:
                    seen_ids.add(p.path_id)
                    paths.append(p)
        return _apply_skip_setup(paths, args.skip_setup)

    if args.category:
        if args.category not in PATHS_BY_CATEGORY:
            log.error(
                "unknown_category",
                category=args.category,
                available=list(PATHS_BY_CATEGORY.keys()),
            )
            sys.exit(1)
        paths = list(PATHS_BY_CATEGORY[args.category])
    else:
        paths = list(ALL_PATHS)

    return _apply_skip_setup(paths, args.skip_setup)


def _apply_skip_setup(
    paths: list[ConversationPath], skip_setup: bool
) -> list[ConversationPath]:
    """Optionally drop paths that need manual setup."""
    if skip_setup:
        return [p for p in paths if p.setup_notes is None]
    return paths


async def main() -> None:
    """Main entry point."""
    _check_eval_gate()

    args = parse_args()
    paths = select_paths(args)

    if not paths:
        log.error("no_paths_selected")
        sys.exit(1)

    log.info(
        "evaluation_starting",
        path_count=len(paths),
        path_ids=[p.path_id for p in paths],
        inter_path_delay_s=args.inter_path_delay,
    )

    telemetry = TelemetryChecker(es_url=args.es_url)

    runner = EvaluationRunner(
        agent_url=args.agent_url,
        telemetry=telemetry,
        inter_path_delay_s=args.inter_path_delay,
    )

    # Structural health check
    healthy = await runner.check_agent_health()
    if not healthy:
        log.error("agent_not_healthy", url=args.agent_url)
        sys.exit(1)

    # Inference responsiveness probe — verifies the LLM server can accept work
    # before queuing dozens of paths. Catches overloaded servers early.
    if not args.skip_responsiveness_probe:
        responsive = await runner.check_inference_responsive()
        if not responsive:
            log.error(
                "inference_not_responsive",
                url=args.agent_url,
                hint="Pass --skip-responsiveness-probe to override (not recommended).",
            )
            sys.exit(1)

    # Run paths
    results = await runner.run_paths(paths)

    # Generate reports
    output_dir = Path(args.output_dir)
    if args.run_id:
        output_dir = output_dir / args.run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "evaluation_results.json"
    md_path = output_dir / "evaluation_results.md"

    report = generate_json_report(results, json_path)
    generate_markdown_report(results, md_path)

    # Summary
    summary = cast(dict[str, object], report["summary"])
    log.info(
        "evaluation_complete",
        paths_passed=summary["paths_passed"],
        paths_total=summary["total_paths"],
        assertions_passed=summary["assertions_passed"],
        assertions_total=summary["total_assertions"],
        pass_rate=f"{summary['assertion_pass_rate']:.1%}",
    )

    log.info(
        "evaluation_reports_saved",
        json_path=str(json_path),
        md_path=str(md_path),
    )

    # Exit code: 0 if all passed, 1 if any failed
    if summary.get("paths_failed", 0):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
