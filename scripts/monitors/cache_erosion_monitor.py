"""CLI entry point for the cache-erosion monitor (``make cache-erosion-status``).

Queries ``prompt_static_prefix_hash`` values from ``agent-logs-*`` for the
last ``--window-days`` calendar days, computes Jaccard similarity between
consecutive-day hash sets per callsite, and exits non-zero when any monitored
callsite is below the erosion threshold.

Usage::

    uv run python -m scripts.monitors.cache_erosion_monitor
    uv run python -m scripts.monitors.cache_erosion_monitor --window-days 7
    uv run python -m scripts.monitors.cache_erosion_monitor --callsites orchestrator.primary
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from personal_agent.config.settings import get_settings
from personal_agent.observability.cache_erosion.monitor import (
    EROSION_THRESHOLD,
    MONITORED_CALLSITES,
    compute_erosion_report,
    render_report,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cache-erosion-monitor",
        description="Cache-erosion gate for prompt prefix stability (FRE-406).",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=2,
        help="Consecutive-day comparison window (default: 2).",
    )
    parser.add_argument(
        "--callsites",
        nargs="+",
        default=list(MONITORED_CALLSITES),
        help="Callsites to monitor (default: orchestrator.primary gateway.chat).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=EROSION_THRESHOLD,
        help=f"Jaccard similarity floor (default: {EROSION_THRESHOLD}).",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    try:
        from elasticsearch import AsyncElasticsearch as ESClient
    except ModuleNotFoundError:
        sys.stderr.write("elasticsearch package is required\n")
        return 64

    es = ESClient([settings.elasticsearch_url], request_timeout=30)
    try:
        report = await compute_erosion_report(
            es,
            logs_prefix=settings.elasticsearch_index_prefix,
            callsites=tuple(args.callsites),
            window_days=args.window_days,
            threshold=args.threshold,
        )
    finally:
        await es.close()

    sys.stdout.write(render_report(report) + "\n")
    return 1 if report.any_eroded else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
